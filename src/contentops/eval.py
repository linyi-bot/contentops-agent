"""阶段 3：评测器。

它只做一件事：把 `evals/golden/` 里的题跑一遍，算出一张能写进简历的表，
并且**必须**把每条失败样本的原因留下来 —— 只有总分的报告不算评测。

三条设计取舍：
1. 用「严格审核员」代替人：断言全部通过才放行，有未核实就打回。
   这是个**可复现的代理**，不是真人判断；所以报告里单独列出「代理」二字。
2. 「事实一致性」在 mock 下无法判语义，这里只判到「引用落地 + 引到期望小节」，
   属于可机判的下界指标。语义一致性等接真模型后靠人工抽检，不在这里假装。
3. 拒答题不检测「模型说了什么」，只检测「有没有发出去」——
   发出去就是答了不该答的题，没发出去才算拒答。
"""
from __future__ import annotations

import json
import math
import os
import platform
import statistics
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from . import graph as G
from .llm import describe, reset_usage, usage_snapshot

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DATASET = ROOT / "evals" / "golden"

#: 严格审核员：断言全通过才放行。改这个策略就等于换了一个人，前后报告不可直接比。
REVIEWER = "strict"

CASE_FIELDS = ("id", "kind", "topic", "must_cite", "should_refuse", "expect_claims")


@dataclass
class Case:
    id: str
    kind: str
    topic: str
    must_cite: list[str]
    should_refuse: bool
    expect_claims: list[str] = field(default_factory=list)


@dataclass
class CaseResult:
    case: Case
    published: bool
    revision: int
    unchecked: list[str]
    claims: list[dict[str, Any]]
    hits: list[dict[str, Any]]
    draft: str
    ms: int

    @property
    def cited_headings(self) -> list[str]:
        """草稿里真正引用到的小节（去重，保持出现顺序）。"""
        out: list[str] = []
        for claim in self.claims:
            for idx in claim.get("cites") or []:
                if 1 <= idx <= len(self.hits):
                    heading = self.hits[idx - 1]["heading"] or self.hits[idx - 1]["doc"]
                    if heading not in out:
                        out.append(heading)
        return out

    @property
    def claims_with_cite(self) -> list[dict[str, Any]]:
        return [c for c in self.claims if c.get("cites")]

    @property
    def correct_claims(self) -> list[dict[str, Any]]:
        """引用落地（supported）且引到了期望小节 —— 事实一致性的可机判下界。"""
        good = []
        for c in self.claims_with_cite:
            if not c.get("supported"):
                continue
            headings = {
                self.hits[i - 1]["heading"]
                for i in c["cites"]
                if 1 <= i <= len(self.hits)
            }
            if headings & set(self.case.must_cite):
                good.append(c)
        return good

    @property
    def ok(self) -> bool:
        if self.case.should_refuse:
            return not self.published
        return self.published and bool(self.correct_claims)

    def fail_reason(self) -> str:
        c = self.case
        if c.should_refuse:
            if not self.published:
                return ""
            return (
                f"应拒答却发布了：检索到 {len(self.hits)} 条不相关资料，"
                f"草稿里 {len(self.claims)} 条断言被判定通过，闸门放行。"
            )
        if not self.published:
            return f"应回答却没发布：{len(self.unchecked)} 条未核实断言被审核员打回，额度用尽后收工。"
        if not self.correct_claims:
            got = "、".join(self.cited_headings) or "（无引用）"
            want = "、".join(c.must_cite) or "（无）"
            return f"引用没落到期望小节：期望「{want}」，实际引用「{got}」，检索 Top{len(self.hits)} 未覆盖期望小节。"
        return ""


def load_cases(dataset: Path | str = DEFAULT_DATASET) -> list[Case]:
    """读目录下所有 `*.jsonl`，每行一条样本。坏行直接报错，不静默跳过。"""
    root = Path(dataset)
    files = [root] if root.is_file() else sorted(root.glob("*.jsonl"))
    if not files:
        raise FileNotFoundError(f"{root} 下没有 *.jsonl 数据集")

    cases: list[Case] = []
    for f in files:
        for lineno, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            line = line.strip()
            if not line:
                continue
            try:
                raw = json.loads(line)
                missing = [k for k in CASE_FIELDS[:5] if k not in raw]
                if missing:
                    raise ValueError(f"缺字段 {missing}")
                cases.append(
                    Case(
                        id=str(raw["id"]),
                        kind=str(raw.get("kind") or "regular"),
                        topic=str(raw["topic"]),
                        must_cite=list(raw.get("must_cite") or []),
                        should_refuse=bool(raw.get("should_refuse")),
                        expect_claims=list(raw.get("expect_claims") or []),
                    )
                )
            except Exception as exc:  # 数据集错误要立刻炸，别带着脏数据出报告
                raise ValueError(f"{f.name}:{lineno} 不是合法样本：{exc}") from exc

    ids = [c.id for c in cases]
    dup = {i for i in ids if ids.count(i) > 1}
    if dup:
        raise ValueError(f"样本 id 重复：{sorted(dup)}")
    return cases


def run_case(case: Case) -> CaseResult:
    """跑一条，用一个「断言全过才放行」的审核员把流程推到底。"""
    t0 = time.perf_counter()
    app, tid = G.new_session()
    state = G.start(app, case.topic, tid)

    rounds = 0
    while G.is_interrupted(state) and rounds < 6:
        packet = G.interrupt_payload(state) or {}
        decision = "approve" if not packet.get("unchecked") else "reject"
        state = G.resume(app, {"decision": decision, "note": f"eval:{REVIEWER}"}, tid)
        rounds += 1

    ms = int((time.perf_counter() - t0) * 1000)
    return CaseResult(
        case=case,
        published=bool(state.get("published")),
        revision=int(state.get("revision", 0)),
        unchecked=list(state.get("unchecked") or []),
        claims=list(state.get("claims") or []),
        hits=list(state.get("hits") or []),
        draft=str(state.get("draft") or ""),
        ms=ms,
    )


def _p95(values: list[int]) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, math.ceil(0.95 * len(ordered)) - 1))
    return ordered[idx]


def _pct(num: int, den: int) -> float:
    return round(100.0 * num / den, 1) if den else 0.0


def evaluate(cases: list[Case]) -> dict[str, Any]:
    from .nodes import min_score as effective_min_score

    reset_usage()  # 报告里的成本是「这一轮」的，不是这个进程累计的
    results = [run_case(c) for c in cases]

    answerable = [r for r in results if not r.case.should_refuse]
    refusals = [r for r in results if r.case.should_refuse]

    claims_with_cite = sum(len(r.claims_with_cite) for r in answerable)
    supported = sum(1 for r in answerable for c in r.claims_with_cite if c.get("supported"))
    correct = sum(len(r.correct_claims) for r in answerable)
    total_claims = sum(len(r.claims) for r in answerable)

    retrieval_hit = sum(
        1
        for r in answerable
        if {h["heading"] for h in r.hits} & set(r.case.must_cite)
    )
    refused_ok = sum(1 for r in refusals if not r.published)
    over_refused = sum(1 for r in answerable if not r.published)
    latencies = [r.ms for r in results]

    metrics = {
        "引用命中率": _pct(supported, claims_with_cite),
        "事实一致性（代理）": _pct(correct, total_claims),
        "检索命中率": _pct(retrieval_hit, len(answerable)),
        "拒答准确率": _pct(refused_ok, len(refusals)),
        "过度拒答率": _pct(over_refused, len(answerable)),
    }
    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "reviewer": REVIEWER,
        "llm": describe()["label"],
        "usage": usage_snapshot().as_dict(),
        "min_score": effective_min_score(),
        "python": platform.python_version(),
        "counts": {
            "cases": len(results),
            "answerable": len(answerable),
            "refusals": len(refusals),
            "passed": sum(1 for r in results if r.ok),
            "claims": total_claims,
            "claims_with_cite": claims_with_cite,
        },
        "metrics": metrics,
        "latency_ms": {
            "avg": int(statistics.mean(latencies)) if latencies else 0,
            "p95": _p95(latencies),
            "max": max(latencies) if latencies else 0,
        },
        "draft_chars_avg": int(statistics.mean([len(r.draft) for r in results])) if results else 0,
        "results": results,
    }


def _fmt_row(cells: list[str]) -> str:
    return "| " + " | ".join(cells) + " |"


def render_markdown(report: dict[str, Any]) -> str:
    results: list[CaseResult] = report["results"]
    counts = report["counts"]
    metrics = report["metrics"]

    targets = {
        "引用命中率": "≥ 90%",
        "事实一致性（代理）": "≥ 85%",
        "检索命中率": "（无目标，记录基线）",
        "拒答准确率": "≥ 80%",
        "过度拒答率": "≤ 10%",
    }
    lines = [
        f"# 评测报告 · {report['llm']} · {report['generated_at']}",
        "",
        f"> 生成命令：`python -m contentops.cli eval --dataset evals/golden --out reports`",
        f"> 审核员：`{report['reviewer']}`（断言全过才放行，是**代理**不是真人）"
        f"　相关度阈值：`{report['min_score']}`　Python {report['python']}",
        "",
        "## 一、汇总",
        "",
        _fmt_row(["指标", "本次", "目标", "样本"]),
        _fmt_row(["---", "---:", "---", "---"]),
    ]
    detail_lines = {
        "引用命中率": f"{counts['claims_with_cite']} 条带引用断言",
        "事实一致性（代理）": f"{counts['claims']} 条断言",
        "检索命中率": f"{counts['answerable']} 条应答样本",
        "拒答准确率": f"{counts['refusals']} 条应拒答",
        "过度拒答率": f"{counts['answerable']} 条应答样本",
    }
    for name, value in metrics.items():
        lines.append(_fmt_row([name, f"**{value}%**", targets.get(name, "—"), detail_lines[name]]))

    lines += [
        "",
        f"通过（发对了 / 拒对了）：**{counts['passed']} / {counts['cases']}**　"
        f"平均 {report['latency_ms']['avg']} ms　P95 {report['latency_ms']['p95']} ms　"
        f"草稿平均 {report['draft_chars_avg']} 字",
        "",
    ]

    usage = report.get("usage") or {}
    section = 2
    cn = {2: "二", 3: "三", 4: "四"}
    if usage.get("calls"):
        lines += [
            f"## 二、成本（{report['llm']}）",
            "",
            f"- 模型调用 **{usage['calls']}** 次，失败 {usage.get('errors', 0)} 次",
            f"- 输入 {usage['prompt_tokens']} tokens（其中缓存命中 {usage['cache_hit_tokens']}）"
            f"　输出 {usage['completion_tokens']} tokens",
            f"- 估算花费 **¥{usage['cost_cny']:.4f}**，平均每条样本 "
            f"¥{usage['cost_cny'] / max(counts['cases'], 1):.4f}",
            "",
        ]
        section = 3

    lines += [f"## {cn[section]}、失败明细", ""]

    failures = [r for r in results if not r.ok]
    if not failures:
        lines.append("（无失败样本）")
    for r in failures:
        c = r.case
        lines += [
            f"### {c.id} `{c.kind}` {c.topic}",
            "",
            f"- 期望：{'拒答（不发布）' if c.should_refuse else '发布且引用到「' + '、'.join(c.must_cite) + '」'}",
            f"- 实际：published={r.published}，第 {r.revision} 版，"
            f"检索 {len(r.hits)} 条，断言 {len(r.claims)} 条（未核实 {len(r.unchecked)} 条）",
            f"- 原因：{r.fail_reason()}",
            f"- 草稿：{r.draft[:120].replace(chr(10), ' ')}",
            "",
        ]

    lines += [
        f"## {cn[section + 1]}、口径（读这张表前先看这三条）",
        "",
        "1. **事实一致性是下界**：它只判「引用落地 + 引到期望小节」，语义是否真支持由人工抽检，"
        "机器判不了，这里不假装。",
        "2. **审核员是代理**：`strict` 只是「断言全过才放行」。换真人、换策略，数字就不可比。",
        "3. **阈值是在这 30 条上定的**：相关度阈值由「应答题最低分」与「拒答题最高分」之间取中，"
        "属于在测试集上调参；样本扩到 100 条后要重测，别把这个数当泛化结论。",
        "",
    ]
    return "\n".join(lines)


def write_report(
    report: dict[str, Any], out_dir: Path | str = ROOT / "reports", label: str = ""
) -> Path:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    suffix = label or ("gated" if report["min_score"] > 0 else "no-gate")
    path = out / f"eval-{stamp}-{suffix}.md"
    path.write_text(render_markdown(report), encoding="utf-8")
    return path
