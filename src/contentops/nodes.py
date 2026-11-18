"""节点实现。

约定：
- 每个节点是纯函数 `(state) -> partial_state`，不读写全局、不做 IO 副作用外的隐式事；
- 每个节点都往 `trace` 里追加一条，包含耗时和一句人话说明；
- 节点内部失败要「降级而不崩」，把错误写进 trace，让流水线继续走。
"""
from __future__ import annotations

import time

from .llm import LLM, build_llm
from .retriever import Retriever
from .state import ContentState, Hit, TraceEvent

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
    llm: LLM = build_llm()

    user = f"选题：{topic}\n\n资料：\n{_format_context(hits)}"
    try:
        draft = llm.complete(DRAFT_SYSTEM, user)
        note = f"模型 {llm.name} 生成 {len(draft)} 字"
    except Exception as exc:  # 网络/额度问题不应该让整条流水线死掉
        draft = f"（生成失败：{type(exc).__name__}: {exc}）\n资料共 {len(hits)} 条，已保留在 trace 中。"
        note = f"模型调用失败已降级：{type(exc).__name__}"

    return {"draft": draft, "trace": [_event("draft", t0, note)]}
