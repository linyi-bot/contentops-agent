"""节点实现 —— 项目二（自媒体运营 AI 智能体）版。

约定：
- 每个节点是纯函数 `(state) -> partial_state`，副作用只有一次外部调用（LLM / 出图）；
- 每个节点都往 `trace` 与 `node_metrics` 里各追加一条，耗时同源；
- 节点内部失败要「降级而不崩」：LLM 挂了就出一版说明性的稿子，
  出图挂了就把错误收进 `image_errors`，流程继续走 —— 页面要能看到失败，而不是 500。

六个节点：
    plan_topics      出 3-5 个选题
    human_select     中断 #1，等人挑一个
    write_draft      按选题写长文（打回时带意见重写）
    human_review     中断 #2，等人放行 / 打回
    extract_visuals  把正文提炼成 3-5 个图点
    generate_images  并行出图（同步 API 丢线程池）
"""
from __future__ import annotations

import re
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from .image_service import build_image_service
from .llm import LLM, build_llm, usage_snapshot
from .state import (
    ArticleState,
    NodeMetric,
    ReviewPacket,
    SelectPacket,
    TraceEvent,
)

# --------------------------------------------------------------------------- #
# 公共小工具
# --------------------------------------------------------------------------- #


def _finish(node: str, t0: float, note: str, *, tokens: int = 0, model: str = "") -> dict:
    """节点的统一收尾：一条时间线 + 一条成本记录，ms 只算一次。"""
    ms = int((time.perf_counter() - t0) * 1000)
    return {
        "trace": [TraceEvent(node=node, ms=ms, note=note)],
        "node_metrics": [NodeMetric(node=node, ms=ms, tokens=tokens, model=model)],
    }


_NUM_LINE = re.compile(r"^\s*(?:\d{1,2}\s*[.、)．]|[-*·])\s*(.+?)\s*$")


def _numbered_lines(text: str, limit: int = 6) -> list[str]:
    """把模型输出里的编号行抽出来。

    模型很爱加「好的，以下是选题：」这种开场白，也可能用 markdown 列表，
    所以不按行号解析，只认「编号/圆点 + 内容」，其余行丢掉。
    """
    out: list[str] = []
    for line in (text or "").splitlines():
        m = _NUM_LINE.match(line.strip())
        if not m:
            continue
        item = re.sub(r"^[*#\s]+", "", m.group(1)).strip(" 　。；;")
        if len(item) >= 2:
            out.append(item)
        if len(out) >= limit:
            break
    return out


def _clean(text: str) -> str:
    return re.sub(r"^\[mock:[0-9a-fA-F]{6,}\]\s*", "", (text or "").strip())


# --------------------------------------------------------------------------- #
# 提示词
# --------------------------------------------------------------------------- #

TOPIC_SYSTEM = """你是小红书内容策划，服务一家做 AI 编程培训的教育公司。
根据给定方向，出 5 个「技术干货型」选题。
硬规则：
1. 每行一个，格式必须是「1. 标题」，不要任何开场白和解释；
2. 标题 12-20 字，带具体收益或反差，不要标题党；
3. 5 个选题角度要不同（工具、方法、踩坑、对比、案例）。"""

DRAFT_SYSTEM = """你是内容运营，写一篇中文技术干货短文，发布在小红书。
硬规则：
1. 500 字以内，分 3 段，每段一个小标题（用「一、二、三、」开头）；
2. 口语化，讲人话，不堆术语；不虚构数据、不编造案例；
3. 结尾给一句可执行的建议；
4. 只输出正文，不要解释你写了什么。"""

VISUAL_SYSTEM = """你是视觉编辑，把一篇小红书文案拆成配图脚本。
硬规则：
1. 出 4 个图点，每行一个，格式必须是「1. 描述」；
2. 每个描述 10-16 字，是一张图要表达的内容（不是标题重复）；
3. 4 个图点覆盖：痛点、方法、步骤、结论；不要任何解释。"""


# --------------------------------------------------------------------------- #
# 1. 出选题
# --------------------------------------------------------------------------- #


def plan_topics_node(state: ArticleState) -> dict:
    t0 = time.perf_counter()
    direction = (state.get("topic_direction") or "").strip()
    llm: LLM = build_llm()

    before = usage_snapshot()
    try:
        raw = llm.complete(TOPIC_SYSTEM, f"方向：{direction}\n平台：{state.get('platform', 'xiaohongshu')}",
                           max_tokens=400)
        topics = _numbered_lines(raw, limit=5)
        spent = _delta(before)
        note = f"模型 {llm.name} 出了 {len(topics)} 个选题"
        if spent:
            note += f"（{spent} tokens）"
    except Exception as exc:  # noqa: BLE001 - 模型挂了也要让人能继续选
        topics = [f"{direction}：一个可以马上用的方法"]
        spent = 0
        note = f"选题生成失败已降级：{type(exc).__name__}"

    return {
        "generated_topics": topics,
        "status": "topics_generated",
        **_finish("plan_topics", t0, note, tokens=spent, model=llm.name),
    }


def _delta(before) -> int:
    after = usage_snapshot()
    return (after.prompt_tokens - before.prompt_tokens) + (
        after.completion_tokens - before.completion_tokens
    )


# --------------------------------------------------------------------------- #
# 2. 中断 #1：人选一个
# --------------------------------------------------------------------------- #


def human_select_node(state: ArticleState) -> dict:
    """在这里 `interrupt()`：图挂起，进程可以退出，人选完再回来。

    注意 LangGraph 1.0 的语义：恢复时**这个节点会整个重新跑一遍**，
    上面的代码（构造 packet）会再执行一次，`interrupt()` 这次拿到的是恢复值。
    所以这里除了「构造 packet」不要有任何副作用。
    """
    from langgraph.types import interrupt

    t0 = time.perf_counter()
    options = list(state.get("generated_topics") or [])
    packet = SelectPacket(
        step="select_topic",
        topic_direction=state.get("topic_direction", ""),
        options=options,
    )
    answer = interrupt(packet)  # ← 图在这里挂起

    picked = _normalize_choice(answer, options)

    return {
        "selected_topic": picked,
        "status": "topic_selected",
        **_finish("human_select", t0, f"选定：{picked}"),
    }


def _normalize_choice(answer: Any, options: list[str]) -> str:
    """人给的答案可能是 1 基序号、也可能是完整标题。都认。"""
    if isinstance(answer, dict):
        answer = answer.get("selected_topic") or answer.get("value") or answer.get("index")
    text = str(answer or "").strip()
    if not text:
        return options[0] if options else ""
    if text.isdigit():
        idx = int(text)
        if 1 <= idx <= len(options):
            return options[idx - 1]
    for opt in options:
        if text == opt or text in opt or opt in text:
            return opt
    return text  # 允许自定义：不在候选里也认，页面会显示「自定义选题」


# --------------------------------------------------------------------------- #
# 3. 写正文
# --------------------------------------------------------------------------- #


def write_draft_node(state: ArticleState) -> dict:
    t0 = time.perf_counter()
    topic = state.get("selected_topic") or state.get("topic_direction", "")
    feedback = (state.get("review_feedback") or "").strip()
    revision = int(state.get("revision") or 0) + 1
    llm: LLM = build_llm()

    user = f"选题：{topic}"
    if feedback:
        user += f"\n\n上一版被打回，修改意见（这次必须改）：\n{feedback}"

    before = usage_snapshot()
    try:
        draft = _clean(llm.complete(DRAFT_SYSTEM, user, max_tokens=900))
        if feedback:
            draft = draft  # 模型自己参考意见重写；这里不强加标记，避免污染正文
        spent = _delta(before)
        note = f"第 {revision} 版，{len(draft)} 字"
        if feedback:
            note += "（按意见重写）"
        if spent:
            note += f"，{spent} tokens"
    except Exception as exc:  # noqa: BLE001
        draft = f"（这一版生成失败：{type(exc).__name__}）\n选题：{topic}"
        spent = 0
        note = f"第 {revision} 版，模型调用失败已降级：{type(exc).__name__}"

    return {
        "article_content": draft,
        "revision": revision,
        "status": "draft_generated",
        **_finish("write_draft", t0, note, tokens=spent, model=llm.name),
    }


# --------------------------------------------------------------------------- #
# 4. 中断 #2：人审
# --------------------------------------------------------------------------- #


def human_review_node(state: ArticleState) -> dict:
    """第二次 `interrupt()`：推草稿给人，拿回 approve / reject + 意见。"""
    from langgraph.types import interrupt

    t0 = time.perf_counter()
    packet = ReviewPacket(
        step="review",
        topic=state.get("selected_topic", ""),
        draft=state.get("article_content", ""),
        revision=int(state.get("revision") or 0),
        options=["approve", "reject"],
    )
    answer = interrupt(packet)

    if isinstance(answer, dict):
        raw = str(answer.get("decision", "")).strip().lower()
        feedback = str(answer.get("feedback") or answer.get("note") or "").strip()
    else:
        raw, feedback = str(answer).strip().lower(), ""

    approved = raw in {"approve", "approved", "y", "yes", "ok", "通过", "放行"}
    decision = "approve" if approved else "reject"
    note = "放行" if approved else f"打回（意见：{feedback or '无'}）"

    return {
        "review_decision": decision,
        "review_feedback": feedback,
        "status": "reviewed",
        **_finish("human_review", t0, note),
    }


# --------------------------------------------------------------------------- #
# 5. 提炼图点
# --------------------------------------------------------------------------- #


def extract_visuals_node(state: ArticleState) -> dict:
    t0 = time.perf_counter()
    content = state.get("article_content", "")
    llm: LLM = build_llm()

    before = usage_snapshot()
    try:
        raw = llm.complete(VISUAL_SYSTEM, f"文案：\n{content}", max_tokens=300)
        points = _numbered_lines(raw, limit=5) or _fallback_points(content)
        spent = _delta(before)
        note = f"提炼出 {len(points)} 个图点"
        if spent:
            note += f"（{spent} tokens）"
    except Exception as exc:  # noqa: BLE001
        points = _fallback_points(content)
        spent = 0
        note = f"提炼失败已降级：{type(exc).__name__}"

    return {
        "visual_points": points,
        "status": "visuals_ready",
        **_finish("extract_visuals", t0, note, tokens=spent, model=llm.name),
    }


def _fallback_points(content: str) -> list[str]:
    """模型不听话时的兜底：从正文里抽前几句当图点，保证一定能出图。"""
    sentences = [s.strip() for s in re.split(r"[。！？\n]", content) if len(s.strip()) >= 6]
    return [s[:16] for s in sentences[:4]] or ["封面：选题价值一句话"]


# --------------------------------------------------------------------------- #
# 6. 并行出图
# --------------------------------------------------------------------------- #


def generate_images_node(state: ArticleState) -> dict:
    """同步出图 API 丢线程池并发 —— 课程里也是这么干的（run_in_executor）。

    一张失败不影响其它张：成功的进 `image_urls`，失败的进 `image_errors`。
    """
    t0 = time.perf_counter()
    points = list(state.get("visual_points") or [])
    service = build_image_service()

    if not points:
        return {
            "image_urls": [],
            "status": "images_skipped",
            **_finish("generate_images", t0, "没有图点，跳过出图"),
        }

    with ThreadPoolExecutor(max_workers=min(4, len(points))) as pool:
        results = list(pool.map(lambda p: service.generate(p[1], index=p[0]),
                                list(enumerate(points, start=1))))

    urls = [r.url for r in results if r.ok and r.url]
    errors = [f"[{r.prompt[:12]}] {r.error}" for r in results if not r.ok]

    note = f"{service.name} 出图 {len(urls)}/{len(points)} 张"
    if errors:
        note += f"，失败 {len(errors)} 张（见 image_errors）"
    return {
        "image_urls": urls,
        "image_errors": errors,
        "status": "done" if urls else "images_failed",
        **_finish("generate_images", t0, note),
    }
