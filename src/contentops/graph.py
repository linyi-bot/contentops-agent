"""图的组装。

六节点流水线（阶段 2）：

    START → retrieve → draft → factcheck ─┬─(有未核实且还有重写额度)→ draft
                                          └─(通过 / 没资料可写 / 用尽额度)→ review
    review ─┬─(放行)→ publish → END
            ├─(打回且还有额度)→ draft
            └─(打回且额度用尽)→ END（宁可空手回去，也不硬发）

两个条件边是本项目的「编排」得分点：
- 回写环（factcheck → draft）证明的是**能用证据否定自己**；
- `review` 用 `interrupt()` 真的把进程停住，不是等一个 sleep —— 人第二天回来
  还能接着跑，因为 checkpointer 把状态存下来了。
"""
from __future__ import annotations

import uuid
from typing import Any

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command

from .nodes import draft_node, factcheck_node, publish_node, retrieve_node, review_node
from .state import ContentState

#: 一稿最多写几版。核查不过 / 人工打回都吃这个额度，用尽就交人或者收工。
MAX_REVISION = 3


def route_after_factcheck(state: ContentState) -> str:
    """核查之后去哪。"""
    if not state.get("unchecked"):
        return "review"
    if not state.get("hits"):
        # 手上没有资料，再写一版也补不出引用 —— 直接把问题推给人，别空转
        return "review"
    if state.get("revision", 0) >= MAX_REVISION:
        return "review"
    return "draft"


def route_after_review(state: ContentState) -> str:
    """人工答复之后去哪。"""
    if state.get("review_decision") == "approve":
        return "publish"
    if state.get("revision", 0) >= MAX_REVISION:
        return "end"
    return "draft"


def build_graph(checkpointer: Any = None) -> Any:
    g = StateGraph(ContentState)
    g.add_node("retrieve", retrieve_node)
    g.add_node("draft", draft_node)
    g.add_node("factcheck", factcheck_node)
    g.add_node("review", review_node)
    g.add_node("publish", publish_node)

    g.add_edge(START, "retrieve")
    g.add_edge("retrieve", "draft")
    g.add_edge("draft", "factcheck")
    g.add_conditional_edges(
        "factcheck", route_after_factcheck, {"draft": "draft", "review": "review"}
    )
    g.add_conditional_edges(
        "review", route_after_review, {"draft": "draft", "publish": "publish", "end": END}
    )
    g.add_edge("publish", END)
    return g.compile(checkpointer=checkpointer)


# --------------------------------------------------------------------------- #
# 会话工具：interrupt 必须配 checkpointer + thread_id，少了任何一个都恢复不了
# --------------------------------------------------------------------------- #


def config_for(thread_id: str) -> dict[str, Any]:
    return {"configurable": {"thread_id": thread_id}}


def is_interrupted(state: ContentState) -> bool:
    return bool(state.get("__interrupt__"))


def interrupt_payload(state: ContentState) -> Any:
    """取出 `interrupt()` 推出来的审核包。"""
    interrupts = state.get("__interrupt__") or ()
    return interrupts[0].value if interrupts else None


def new_session() -> tuple[Any, str]:
    """建一张带内存 checkpointer 的图，返回 (app, thread_id)。"""
    app = build_graph(InMemorySaver())
    return app, f"session-{uuid.uuid4().hex[:8]}"


def start(app: Any, topic: str, thread_id: str) -> ContentState:
    return app.invoke({"topic": topic, "revision": 0}, config_for(thread_id))


def resume(app: Any, answer: Any, thread_id: str) -> ContentState:
    return app.invoke(Command(resume=answer), config_for(thread_id))


def run(
    topic: str,
    *,
    decision: str | None = None,
    thread_id: str | None = None,
    max_rounds: int = 6,
) -> ContentState:
    """跑一条链路。

    `decision=None`（默认）→ 停在人工审核处，返回带 `__interrupt__` 的 state，
    这是「人在环里」的真实形态，测试和 CLI 都靠它。
    `decision="approve"/"reject"` → 自动代为答复，一路跑到 END（或额度用尽）。
    """
    app, default_tid = new_session()
    tid = thread_id or default_tid

    state = start(app, topic, tid)
    rounds = 0
    while decision is not None and is_interrupted(state) and rounds < max_rounds:
        state = resume(app, {"decision": decision, "note": "auto"}, tid)
        rounds += 1
    return state
