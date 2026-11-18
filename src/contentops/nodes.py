"""节点实现。

约定：
- 每个节点是纯函数 `(state) -> partial_state`，不读写全局、不做 IO 副作用外的隐式事；
- 每个节点都往 `trace` 里追加一条，包含耗时和一句人话说明；
- 节点内部失败要「降级而不崩」，把错误写进 trace，让流水线继续走。

阶段 2 新增三个节点：
- `factcheck`：把草稿切成断言，逐条查「引用有没有落地」，不过的写进 `unchecked`；
- `review`：`interrupt()` 暂停，把审核包推给人，等人回放行 / 打回；
- `publish`：组装发布记录（不写盘 —— 落盘是 CLI 的事，节点保持纯粹可测）。
"""
from __future__ import annotations

import re
import time
from datetime import datetime

from .llm import LLM, build_llm
from .retriever import Retriever
from .state import Claim, ContentState, Hit, ReviewPacket, TraceEvent

_RETRIEVER: Retriever | None = None


def get_retriever() -> Retriever:
    global _RETRIEVER
    if _RETRIEVER is None:
        _RETRIEVER = Retriever()
    return _RETRIEVER


def _event(node: str, t0: float, note: str) -> TraceEvent:
    return TraceEvent(node=node, ms=int((time.perf_counter() - t0) * 1000), note=note)


DRAFT_SYSTEM = """你是一名内容运营。根据给定资料写一段 200 字以内的中文初稿。
硬规则：
1. 只能使用资料中出现的事实，资料没写的不要编；
2. 每条事实后面用 [1] [2] 标注它来自第几条资料；
3. 输出纯文本，不要 markdown 标题。"""


def retrieve_node(state: ContentState) -> dict:
    t0 = time.perf_counter()
    topic = state.get("topic", "").strip()
    if not topic:
        return {"hits": [], "trace": [_event("retrieve", t0, "无选题，跳过检索")]}

    hits: list[Hit] = get_retriever().search(topic, top_k=3)
    note = (
        f"命中 {len(hits)} 条，最高分 {hits[0]['score']:.3f}"
        if hits
        else "知识库无命中（草稿阶段会显式说明这一点）"
    )
    return {"hits": hits, "trace": [_event("retrieve", t0, note)]}


def _format_context(hits: list[Hit]) -> str:
    if not hits:
        return "（知识库无相关资料）"
    return "\n\n".join(
        f"[{i + 1}] 出自《{h['doc']}》{('· ' + h['heading']) if h['heading'] else ''}\n{h['text']}"
        for i, h in enumerate(hits)
    )


def draft_node(state: ContentState) -> dict:
    t0 = time.perf_counter()
    topic = state.get("topic", "")
    hits = state.get("hits", [])
    revision = state.get("revision", 0) + 1
    llm: LLM = build_llm()

    user = f"选题：{topic}\n\n资料：\n{_format_context(hits)}"
    unchecked = state.get("unchecked") or []
    if revision > 1 and unchecked:
        user += "\n\n上一版被打回的问题（这次别再犯）：\n" + "\n".join(
            f"- {u}" for u in unchecked[:5]
        )

    try:
        draft = llm.complete(DRAFT_SYSTEM, user)
        note = f"第 {revision} 版，模型 {llm.name} 生成 {len(draft)} 字"
    except Exception as exc:  # 网络/额度问题不应该让整条流水线死掉
        draft = f"（生成失败：{type(exc).__name__}: {exc}）\n资料共 {len(hits)} 条，已保留在 trace 中。"
        note = f"第 {revision} 版，模型调用失败已降级：{type(exc).__name__}"

    return {"draft": draft, "revision": revision, "trace": [_event("draft", t0, note)]}


# --------------------------------------------------------------------------- #
# 事实核查
# --------------------------------------------------------------------------- #

_CITE = re.compile(r"\[(\d{1,2})\]")
_SENTENCE = re.compile(r"[^。！？!?\n]+[。！？!?]?")
_CJK = re.compile(r"[\u4e00-\u9fff]")
_WORD = re.compile(r"[a-zA-Z]{2,}|\d+(?:\.\d+)?%?")

# 模板话术 / 失败占位不是事实断言，不该拿去查引用
_META_MARKS = ("（生成失败", "占位草稿", "如果换成真模型")
# mock 模型会在句首打一个 [mock:xxxxxxxx]，那是模型身份而不是内容，先剥掉再判
_MOCK_TAG = re.compile(r"^\[mock:[0-9a-fA-F]{6,}\]\s*")

# 「的、了、是」这类高频虚词撞上不算证据
_STOP_CHARS = set("的了和是在有为与及或也就都要会被把从对不这那其之以而并个们着过地得上中时年月日一条每")


def _is_meta(sentence: str) -> bool:
    return any(mark in sentence for mark in _META_MARKS)


def _overlap(claim: str, source: str) -> tuple[int, set[str]]:
    """断言和引用段落共享了多少实字 / 实词 —— 用来抓「引错了段」。"""
    chars = {c for c in _CJK.findall(claim) if c not in _STOP_CHARS}
    shared_chars = len(chars & set(_CJK.findall(source)))
    shared_words = set(_WORD.findall(claim)) & set(_WORD.findall(source))
    return shared_chars, shared_words


def _split_sentences(text: str) -> list[str]:
    return [m.group(0).strip() for m in _SENTENCE.finditer(text) if m.group(0).strip()]


def factcheck_node(state: ContentState) -> dict:
    """逐条查「这条断言引用的编号，真的指向一段能支撑它的话吗」。

    只做两件可机判的事：
    1. 断言必须带 `[n]`，且 n 落在本次检索结果的范围内；
    2. 被引的那段文字里，得能找到这条断言至少 2 个实字或 1 个数字/单词。
    语义上到底支不支持，机器判不了，留给人工审核 —— 这也是为什么下一个节点是 interrupt。
    """
    t0 = time.perf_counter()
    draft = state.get("draft", "")
    hits = state.get("hits", [])
    n = len(hits)

    claims: list[Claim] = []
    unchecked: list[str] = []

    for sentence in _split_sentences(draft):
        sentence = _MOCK_TAG.sub("", sentence)
        if len(sentence) < 8 or _is_meta(sentence):
            continue

        cites = [int(c) for c in _CITE.findall(sentence)]
        if not cites:
            claims.append(Claim(text=sentence, cites=[], supported=False, reason="没有引用标记"))
            unchecked.append(sentence)
            continue

        bad = [c for c in cites if c < 1 or c > n]
        if bad:
            claims.append(
                Claim(
                    text=sentence,
                    cites=cites,
                    supported=False,
                    reason=f"引用编号 {bad} 不存在（本次只检索到 {n} 条资料）",
                )
            )
            unchecked.append(sentence)
            continue

        sources = " ".join(hits[c - 1]["text"] for c in cites)
        shared_chars, shared_words = _overlap(sentence, sources)
        if shared_chars < 2 and not shared_words:
            claims.append(
                Claim(
                    text=sentence,
                    cites=cites,
                    supported=False,
                    reason=f"引用 {'/'.join(f'[{c}]' for c in cites)} 的段落里找不到这条断言的关键词",
                )
            )
            unchecked.append(sentence)
            continue

        claims.append(
            Claim(
                text=sentence,
                cites=cites,
                supported=True,
                reason=f"引用编号有效，与原文共享 {shared_chars} 个实字"
                + (f"、{len(shared_words)} 个数字/单词" if shared_words else ""),
            )
        )

    if not claims:
        # 一句可核查的断言都没有，等于没有证据链 —— 不能算「通过」，
        # 必须留一条给人看，否则模型返回空稿也能一路绿灯发出去。
        reason = "本次检索 0 命中" if n == 0 else "草稿里没有任何可核查的断言"
        unchecked = [f"{reason}，这份稿子无法用资料支撑，必须人工确认后才能发布"]
        note = f"没有可核查的断言（{reason}）"
    else:
        note = f"{len(claims)} 条断言，{len(unchecked)} 条未通过"
    return {"claims": claims, "unchecked": unchecked, "trace": [_event("factcheck", t0, note)]}


# --------------------------------------------------------------------------- #
# 人工审核 + 发布
# --------------------------------------------------------------------------- #


def review_node(state: ContentState) -> dict:
    """在 `interrupt()` 处暂停，等人答复。

    注意：第一次执行到这里时 interrupt 直接抛出，节点下面的代码不跑，
    所以「review 那条 trace」要等恢复之后才落地 —— 这不是 bug，
    而是 LangGraph 的语义：节点要么整体提交，要么整体不提交。
    """
    from langgraph.types import interrupt  # 延迟导入：没有 checkpointer 的场景也能 import 本模块

    t0 = time.perf_counter()
    packet = ReviewPacket(
        topic=state.get("topic", ""),
        draft=state.get("draft", ""),
        claims=state.get("claims", []),
        unchecked=state.get("unchecked", []),
        revision=state.get("revision", 0),
    )
    answer = interrupt(packet)  # ← 图在这里挂起，进程可以退出，人第二天再回来

    if isinstance(answer, dict):
        raw = str(answer.get("decision", ""))
        note = str(answer.get("note", "")).strip()
    else:
        raw, note = str(answer), ""

    approved = raw.strip().lower() in {"approve", "approved", "y", "yes", "ok", "放行", "通过"}
    unchecked = state.get("unchecked") or []
    decision = "approve" if approved else "reject"

    if approved and unchecked:
        human_note = f"放行（带着 {len(unchecked)} 条未核实断言，审核人已知情）"
    elif approved:
        human_note = "放行，断言全部通过"
    else:
        human_note = f"打回，退回重写（附言：{note or '无'}）"

    return {
        "review_decision": decision,
        "review_note": note,
        "trace": [_event("review", t0, human_note)],
    }


def publish_node(state: ContentState) -> dict:
    """组装发布记录。不写盘：落盘由 CLI 负责，节点保持纯粹、可测。"""
    t0 = time.perf_counter()
    hits = state.get("hits", [])
    unchecked = state.get("unchecked") or []

    published = {
        "title": state.get("topic", ""),
        "body": state.get("draft", ""),
        "sources": [
            {"index": i + 1, "doc": h["doc"], "heading": h["heading"]}
            for i, h in enumerate(hits)
        ],
        "unchecked": list(unchecked),
        "revision": state.get("revision", 0),
        "review_note": state.get("review_note", ""),
        "gate": "passed" if not unchecked else f"forced（{len(unchecked)} 条未核实，人工放行）",
        "published_at": datetime.now().isoformat(timespec="seconds"),
    }
    note = f"发布记录已生成，引用 {len(hits)} 条资料，闸门 {published['gate']}"
    return {"published": published, "trace": [_event("publish", t0, note)]}
