"""评测器 —— 项目二（自媒体运营）流程版。

上一版的评测跟的是「检索 + 事实核查」，流程换掉后指标对不上，整条退役了。
这一版按新流程重新定义**四个可机判的指标**，每个指标都写清楚"机器判什么、不判什么"：

| 指标 | 机器判什么 | 机器判不了什么 |
|---|---|---|
| 选题合格率 | 长度、标题党词、5 条之间的同质化 | 选题是否真有流量 |
| 成稿合格率 | 字数、段落数、有没有小标题 | 文笔好不好、事实对不对 |
| 图点合格率 | 长度、与正文是否有字面重合 | 图点能不能画出好图 |
| 出图成功率 | 成功张数 / 图点张数 | 图好不好看 |

**这些都只测"流程产出合不合格"，不测"内容质量"** —— 后者要么人评，要么上 LLM 打分，
两种都贵且不稳定，所以这份报告里明确不报"准确率"。

用法：
    contentops eval --limit 3                 # 抽 3 条先看看
    contentops eval                           # 全量，出报告到 reports/
    contentops eval --image-provider mock     # 不花钱：图走占位 SVG
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from statistics import mean
from typing import Any

from .graph import interrupt_payload, is_interrupted, new_session, resume, start
from .llm import usage_snapshot

#: 数据集默认位置
DEFAULT_DATASET = Path("evals") / "topics" / "golden.jsonl"
DEFAULT_OUT = Path("reports")

#: 标题党词表（命中即判不合格）
CLICKBAIT = ("震惊", "必看", "绝了", "爆款", "速看", "99%", "!!", "！！", "史上最")
#: 正文里认这些小标题前缀
HEADING = re.compile(r"^\s*(?:[一二三四五六七八九十]、|##\s|\d{1,2}[.、])", re.M)
_CJK = re.compile(r"[\u4e00-\u9fff]")
_WORD = re.compile(r"[A-Za-z]{2,}|\d+(?:\.\d+)?%?")
_STOP = set("的了和是在有为与及或也就都要会被把从对不这那其之以而并个们着过地得上中时年月日一条每")


def _tokens(text: str) -> set[str]:
    """中文按字、英文按词 —— 只数中文会把 "LangChain/LangGraph" 这类当成没内容。"""
    return {c for c in _CJK.findall(text) if c not in _STOP} | {
        w.lower() for w in _WORD.findall(text)
    }


# --------------------------------------------------------------------------- #
# 数据结构
# --------------------------------------------------------------------------- #


@dataclass
class Sample:
    direction: str
    platform: str = "xiaohongshu"


@dataclass
class SampleResult:
    direction: str
    platform: str
    topics: list[str] = field(default_factory=list)
    topic_ok: list[bool] = field(default_factory=list)
    draft: str = ""
    draft_ok: bool = False
    draft_reasons: list[str] = field(default_factory=list)
    points: list[str] = field(default_factory=list)
    point_ok: list[bool] = field(default_factory=list)
    images: int = 0
    image_errors: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    ms: int = 0
    tokens: int = 0
    cost: float = 0.0
    error: str = ""

    @property
    def topic_rate(self) -> float:
        return sum(self.topic_ok) / len(self.topic_ok) if self.topic_ok else 0.0

    @property
    def point_rate(self) -> float:
        return sum(self.point_ok) / len(self.point_ok) if self.point_ok else 0.0

    @property
    def image_rate(self) -> float:
        return self.images / len(self.points) if self.points else 0.0

    @property
    def passed(self) -> bool:
        return (
            not self.error
            and self.topic_rate >= 0.8
            and self.draft_ok
            and self.point_rate >= 0.8
            and self.image_rate >= 1.0
        )


# --------------------------------------------------------------------------- #
# 打分规则（纯函数，可单测）
# --------------------------------------------------------------------------- #


def _len_cjk(text: str) -> int:
    """按中文字符计长度，避免把标点、空格算进"标题多长"。"""
    return len(_CJK.findall(text))


def score_topics(topics: list[str]) -> tuple[list[bool], list[str]]:
    """每条选题是否合格：数量、长度、标题党、互相不重复。"""
    notes: list[str] = []
    if not (3 <= len(topics) <= 6):
        notes.append(f"选题数量 {len(topics)} 不在 3-6")

    ok: list[bool] = []
    for t in topics:
        reasons = []
        n = _len_cjk(t)
        if not (8 <= n <= 24):
            reasons.append(f"长度 {n} 字不在 8-24")
        if any(word in t for word in CLICKBAIT):
            reasons.append("含标题党词")
        ok.append(not reasons)
        if reasons:
            notes.append(f"「{t}」：" + "、".join(reasons))

    # 同质化：两两「二字组」重合率 > 0.8 视为重复角度。
    # 用字集会把「Cursor 对比」和「Claude 对比」误判成一样（共享的字太多），
    # 换成二字组能区分开；阈值 0.8 也是在这批样本上试出来的。
    for i in range(len(topics)):
        for j in range(i + 1, len(topics)):
            a = _bigrams(topics[i])
            b = _bigrams(topics[j])
            if a and b and len(a & b) / len(a | b) > 0.8:
                ok[i] = ok[j] = False
                notes.append(f"「{topics[i]}」与「{topics[j]}」过于相似")
    return ok, notes


def _bigrams(text: str) -> set[str]:
    clean = "".join(_CJK.findall(text))
    return {clean[i : i + 2] for i in range(len(clean) - 1)}


def score_draft(draft: str) -> tuple[bool, list[str]]:
    """正文是否合格：字数、段落数、有小标题。"""
    reasons = []
    n = _len_cjk(draft)
    if not (250 <= n <= 1200):
        reasons.append(f"正文 {n} 字不在 250-1200")
    paragraphs = [p for p in re.split(r"\n\s*\n", draft.strip()) if p.strip()]
    if len(paragraphs) < 2:
        reasons.append(f"段落数 {len(paragraphs)} < 2")
    if not HEADING.search(draft):
        reasons.append("没有小标题")
    return (not reasons), reasons


def score_points(points: list[str], draft: str) -> tuple[list[bool], list[str]]:
    """每个图点是否合格：数量、长度、和正文有交集（防跑题）。"""
    notes: list[str] = []
    if not (3 <= len(points) <= 5):
        notes.append(f"图点数量 {len(points)} 不在 3-5")

    draft_tokens = _tokens(draft)
    ok: list[bool] = []
    for p in points:
        reasons = []
        n = _len_cjk(p)
        if not (6 <= n <= 24):
            reasons.append(f"长度 {n} 字不在 6-24")
        if draft_tokens and len(_tokens(p) & draft_tokens) < 3:
            reasons.append("与正文几乎没有交集（疑似跑题）")
        ok.append(not reasons)
        if reasons:
            notes.append(f"「{p}」：" + "、".join(reasons))
    return ok, notes


# --------------------------------------------------------------------------- #
# 跑一条样本
# --------------------------------------------------------------------------- #


def run_sample(sample: Sample, *, pick: int = 1) -> SampleResult:
    """自动选第 pick 个选题、自动放行，跑完整条链路并打分。"""
    r = SampleResult(direction=sample.direction, platform=sample.platform)
    t0 = time.perf_counter()
    u0 = usage_snapshot()
    try:
        app, tid = new_session()
        state = start(app, sample.direction, tid, platform=sample.platform)
        pkt = interrupt_payload(state) or {}
        options = pkt.get("options") or []
        state = resume(app, {"selected_topic": str(pick) if options else "1"}, tid)

        pkt = interrupt_payload(state) or {}
        if pkt.get("step") == "review":
            state = resume(app, {"decision": "approve", "feedback": ""}, tid)

        if is_interrupted(state):
            r.error = "流程没有跑完（仍停在中断）"
    except Exception as exc:  # noqa: BLE001
        r.error = f"{type(exc).__name__}: {exc}"
        return r
    finally:
        u1 = usage_snapshot()
        r.ms = int((time.perf_counter() - t0) * 1000)
        r.tokens = (u1.prompt_tokens - u0.prompt_tokens) + (
            u1.completion_tokens - u0.completion_tokens
        )
        r.cost = round(u1.cost_cny - u0.cost_cny, 6)

    r.topics = list(state.get("generated_topics") or [])
    r.topic_ok, topic_notes = score_topics(r.topics)
    r.draft = str(state.get("article_content") or "")
    r.draft_ok, r.draft_reasons = score_draft(r.draft)
    r.points = list(state.get("visual_points") or [])
    r.point_ok, point_notes = score_points(r.points, r.draft)
    r.images = len(state.get("image_urls") or [])
    r.image_errors = list(state.get("image_errors") or [])
    r.notes = topic_notes + point_notes
    return r


# --------------------------------------------------------------------------- #
# 汇总 + 报告
# --------------------------------------------------------------------------- #


def load_dataset(path: str | Path = DEFAULT_DATASET) -> list[Sample]:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"找不到数据集：{p}")
    samples: list[Sample] = []
    for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{p} 第 {i} 行不是合法 JSON：{exc}") from exc
        if not row.get("direction"):
            raise ValueError(f"{p} 第 {i} 行缺 direction")
        samples.append(Sample(direction=row["direction"], platform=row.get("platform", "xiaohongshu")))
    if not samples:
        raise ValueError(f"{p} 里没有样本")
    return samples


def summarize(results: list[SampleResult]) -> dict[str, Any]:
    ok = [r for r in results if not r.error]
    total_topics = sum(len(r.topic_ok) for r in ok)
    good_topics = sum(sum(r.topic_ok) for r in ok)
    total_points = sum(len(r.point_ok) for r in ok)
    good_points = sum(sum(r.point_ok) for r in ok)
    images = sum(r.images for r in ok)
    want_images = sum(len(r.points) for r in ok)
    ms = sorted(r.ms for r in ok)

    return {
        "样本": len(results),
        "出错": len(results) - len(ok),
        "通过": sum(1 for r in ok if r.passed),
        "选题合格率": round(good_topics / total_topics * 100, 1) if total_topics else 0.0,
        "成稿合格率": round(sum(1 for r in ok if r.draft_ok) / len(ok) * 100, 1) if ok else 0.0,
        "图点合格率": round(good_points / total_points * 100, 1) if total_points else 0.0,
        "出图成功率": round(images / want_images * 100, 1) if want_images else 0.0,
        "平均耗时ms": int(mean(ms)) if ms else 0,
        "P95耗时ms": ms[max(0, int(len(ms) * 0.95) - 1)] if ms else 0,
        "总token": sum(r.tokens for r in ok),
        "总成本": round(sum(r.cost for r in ok), 6),
    }


def to_markdown(results: list[SampleResult], summary: dict[str, Any], *, label: str = "") -> str:
    from .llm import describe
    from .image_service import describe_images

    now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    lines = [
        f"# 评测报告 · 自媒体运营流程 · {label or now}",
        "",
        f"> 模型：`{describe()['label']}`　出图：`{describe_images()['name']}`　"
        f"Python {__import__('platform').python_version()}",
        "",
        "## 一、汇总",
        "",
        "| 指标 | 本次 | 目标 | 说明 |",
        "| --- | ---: | --- | --- |",
        f"| 选题合格率 | **{summary['选题合格率']}%** | ≥ 80% | 长度 8-24、无标题党、彼此不同质 |",
        f"| 成稿合格率 | **{summary['成稿合格率']}%** | ≥ 90% | 250-1200 字、≥2 段、有小标题 |",
        f"| 图点合格率 | **{summary['图点合格率']}%** | ≥ 80% | 6-24 字、与正文有字面交集 |",
        f"| 出图成功率 | **{summary['出图成功率']}%** | 100% | 成功张数 / 图点张数 |",
        "",
        f"通过（四项全过）：**{summary['通过']} / {summary['样本']}**　"
        f"出错 {summary['出错']} 条　平均 {summary['平均耗时ms']} ms　P95 {summary['P95耗时ms']} ms",
        "",
        "## 二、成本",
        "",
        f"- 总 token **{summary['总token']}**，估算花费 **¥{summary['总成本']:.4f}**",
        f"- 平均每条 ¥{summary['总成本'] / max(1, summary['样本']):.4f}",
        "",
        "## 三、明细",
        "",
        "| # | 方向 | 选题 | 正文 | 图点 | 出图 | 耗时 | token | 结果 |",
        "| ---: | --- | ---: | --- | ---: | --- | ---: | ---: | --- |",
    ]
    for i, r in enumerate(results, 1):
        mark = "✅" if r.passed else ("❌" if r.error else "⚠️")
        lines.append(
            f"| {i} | {r.direction} | {r.topic_rate * 100:.0f}% | "
            f"{len(_CJK.findall(r.draft))} 字{'✔' if r.draft_ok else '✘'} | "
            f"{r.point_rate * 100:.0f}% | {r.images}/{len(r.points)} | "
            f"{r.ms} ms | {r.tokens} | {mark} |"
        )

    failures = [r for r in results if not r.passed]
    lines += ["", "## 四、失败明细", ""]
    if not failures:
        lines.append("（无）")
    else:
        for r in failures:
            lines.append(f"### {r.direction}")
            if r.error:
                lines.append(f"- 运行出错：`{r.error}`")
            if not r.draft_ok:
                lines.append(f"- 成稿：{'；'.join(r.draft_reasons)}")
            bad_topics = [t for t, ok in zip(r.topics, r.topic_ok) if not ok]
            if bad_topics:
                lines.append("- 选题不合格：" + "、".join(f"「{t}」" for t in bad_topics))
            bad_points = [p for p, ok in zip(r.points, r.point_ok) if not ok]
            if bad_points:
                lines.append("- 图点不合格：" + "、".join(f"「{p}」" for p in bad_points))
            if r.image_errors:
                lines.append("- 出图失败：" + "；".join(r.image_errors))
            lines.append("")

    lines += [
        "## 五、口径（读这张表前先看）",
        "",
        "1. **不测内容质量**。这四项都是可机判的形状约束，好文笔、事实正确与否机器判不了；",
        "2. **选题合格 ≠ 有流量**。「长度 + 不标题党 + 不同质」只是底线；",
        "3. **出图成功率依赖 provider**：mock 必然 100%，真图会暴露限流与安全策略问题；",
        "4. 单条样本一次通过，没有做多次采样取均值 —— 想看稳定性要跑 `--repeat 3`（还没实现）。",
        "",
    ]
    return "\n".join(lines)


def run_eval(
    dataset: str | Path = DEFAULT_DATASET,
    *,
    limit: int = 0,
    out: str | Path | None = DEFAULT_OUT,
    label: str = "",
    pick: int = 1,
    progress: bool = True,
) -> tuple[list[SampleResult], dict[str, Any], Path | None]:
    samples = load_dataset(dataset)
    if limit:
        samples = samples[:limit]
    results: list[SampleResult] = []
    for i, s in enumerate(samples, 1):
        if progress:
            print(f"  [{i}/{len(samples)}] {s.direction} ...", end="", flush=True)
        r = run_sample(s, pick=pick)
        results.append(r)
        if progress:
            print((" 通过" if r.passed else " 未通过") + f"（{r.ms} ms, {r.tokens} tok）")

    summary = summarize(results)
    path = None
    if out:
        directory = Path(out)
        directory.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M")
        name = f"eval-{stamp}-{label or 'topics'}.md"
        path = directory / name
        path.write_text(to_markdown(results, summary, label=label), encoding="utf-8")
    return results, summary, path
