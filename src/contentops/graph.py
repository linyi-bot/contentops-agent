"""图的组装 —— 项目二（自媒体运营 AI 智能体）版。

    START → plan_topics → human_select ──(中断 #1：人选)──→ write_draft
          → human_review ──(中断 #2：人审)──┬─ approve → extract_visuals → generate_images → END
                                            └─ reject  → write_draft（带意见重写，最多 MAX_REVISION 版）

两个条件边是这份项目的「人在环里」得分点：
- `human_select` 停住等选题 —— 不是先跑完再让你改；
- `human_review` 停住等放行，打回就带着意见重写，改到满意为止（或额度用尽收工）。

`interrupt()` 必须配 checkpointer + `thread_id`，少任何一个都恢复不了。
"""
from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command

from .checkpoint import postgres_saver, sqlite_saver
from .nodes import (
    extract_visuals_node,
    generate_images_node,
    human_review_node,
    human_select_node,
    plan_topics_node,
    write_draft_node,
)
from .state import ArticleState, INITIAL_STATE

#: 正文最多写几版。每次打回 +1，用尽就收工（不硬发）。
MAX_REVISION = 3


def route_after_review(state: ArticleState) -> str:
    """审核之后去哪。"""
    if state.get("review_decision") == "approve":
        return "to_images"
    if int(state.get("revision") or 0) >= MAX_REVISION:
        return "end"
    return "to_draft"


def build_graph(checkpointer: Any = None) -> Any:
    g = StateGraph(ArticleState)
    g.add_node("plan_topics", plan_topics_node)
    g.add_node("human_select", human_select_node)
    g.add_node("write_draft", write_draft_node)
    g.add_node("human_review", human_review_node)
    g.add_node("extract_visuals", extract_visuals_node)
    g.add_node("generate_images", generate_images_node)

    g.add_edge(START, "plan_topics")
    g.add_edge("plan_topics", "human_select")
    g.add_edge("human_select", "write_draft")
    g.add_edge("write_draft", "human_review")
    g.add_conditional_edges(
        "human_review",
        route_after_review,
        {"to_images": "extract_visuals", "to_draft": "write_draft", "end": END},
    )
    g.add_edge("extract_visuals", "generate_images")
    g.add_edge("generate_images", END)
    return g.compile(checkpointer=checkpointer)


# --------------------------------------------------------------------------- #
# 会话工具
# --------------------------------------------------------------------------- #


def config_for(thread_id: str) -> dict[str, Any]:
    return {"configurable": {"thread_id": thread_id}}


def is_interrupted(state: Any) -> bool:
    return bool(state.get("__interrupt__")) if isinstance(state, dict) else bool(state)


def interrupt_payload(state: ArticleState) -> Any:
    """取出 `interrupt()` 推出来的包（选题包或审核包）。"""
    interrupts = state.get("__interrupt__") or ()
    return interrupts[0].value if interrupts else None


def new_thread_id() -> str:
    return f"session-{uuid.uuid4().hex[:8]}"


def new_session() -> tuple[Any, str]:
    """建一张带**内存** checkpointer 的图，返回 (app, thread_id)。"""
    app = build_graph(InMemorySaver())
    return app, new_thread_id()


@contextmanager
def session(
    thread_id: str | None = None, *, db: str | None = None, pg: str | None = None
) -> Iterator[tuple[Any, str]]:
    """开一次会话。后端三选一，优先级 `pg > db > 内存`。"""
    tid = thread_id or new_thread_id()
    if pg:
        with postgres_saver(pg) as saver:
            yield build_graph(saver), tid
    elif db:
        with sqlite_saver(db) as saver:
            yield build_graph(saver), tid
    else:
        yield build_graph(InMemorySaver()), tid


def initial_input(topic_direction: str, *, platform: str = "xiaohongshu") -> dict[str, Any]:
    return {
        **INITIAL_STATE,
        "topic_direction": topic_direction,
        "platform": platform,
        "status": "started",
    }


def start(app: Any, topic_direction: str, thread_id: str, *, platform: str = "xiaohongshu") -> ArticleState:
    return app.invoke(initial_input(topic_direction, platform=platform), config_for(thread_id))


def resume(app: Any, answer: Any, thread_id: str) -> ArticleState:
    return app.invoke(Command(resume=answer), config_for(thread_id))
