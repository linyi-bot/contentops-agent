"""图的组装 —— 项目二（自媒体运营 AI 智能体）版。

主图负责「人机接力」，三张子图负责「纯 AI 的那三段活」：

    主图: START → topics_sub → human_select → writing_sub → human_review
                                                   ├ approve → visuals_sub → publish → END
                                                   └ reject  → writing_sub（带意见重写）

    子图: topics_sub   = plan_topics
          writing_sub  = write_draft
          visuals_sub  = extract_visuals → generate_images

为什么拆子图：三段活的输入输出边界很清楚，拆开之后可以单独编译、单独测、
将来也能被别的流程复用（比如「只出图」的服务）。**中断全部留在主图**——
子图里放 `interrupt()` 会让恢复位置变得难判断，课程里也提醒过这一点。
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
    publish_node,
    write_draft_node,
)
from .state import ArticleState, INITIAL_STATE

#: 正文最多写几版。每次打回 +1，用尽就收工（不硬发）。
MAX_REVISION = 3


# --------------------------------------------------------------------------- #
# 子图
# --------------------------------------------------------------------------- #


def build_topics_subgraph() -> Any:
    g = StateGraph(ArticleState)
    g.add_node("plan_topics", plan_topics_node)
    g.add_edge(START, "plan_topics")
    g.add_edge("plan_topics", END)
    return g.compile()


def build_writing_subgraph() -> Any:
    g = StateGraph(ArticleState)
    g.add_node("write_draft", write_draft_node)
    g.add_edge(START, "write_draft")
    g.add_edge("write_draft", END)
    return g.compile()


def build_visuals_subgraph() -> Any:
    g = StateGraph(ArticleState)
    g.add_node("extract_visuals", extract_visuals_node)
    g.add_node("generate_images", generate_images_node)
    g.add_edge(START, "extract_visuals")
    g.add_edge("extract_visuals", "generate_images")
    g.add_edge("generate_images", END)
    return g.compile()


# --------------------------------------------------------------------------- #
# 主图
# --------------------------------------------------------------------------- #


def route_after_review(state: ArticleState) -> str:
    """审核之后去哪。"""
    if state.get("review_decision") == "approve":
        return "to_images"
    if int(state.get("revision") or 0) >= MAX_REVISION:
        return "end"
    return "to_draft"


def _run_subgraph(sub: Any, state: ArticleState) -> dict:
    """跑一张子图，只把「它这次新产生的东西」交回主图。

    为什么不直接把编译好的子图当节点挂上去：那样每次 resume 主图都会把
    子图（连同上游）整体重放一遍，`operator.add` 的 trace 会被重复累加。
    包一层、只回传增量，行为就和普通节点一致了 —— 子图仍然是独立编译、
    可单独测、可复用的，只是不参与父图的 checkpoint 语义。
    """
    before_trace = len(state.get("trace") or [])
    before_metrics = len(state.get("node_metrics") or [])
    out = sub.invoke(state)

    delta: dict[str, Any] = {}
    for key, value in out.items():
        if key in {"trace", "node_metrics"}:
            continue
        if value != state.get(key):
            delta[key] = value
    delta["trace"] = list(out.get("trace") or [])[before_trace:]
    delta["node_metrics"] = list(out.get("node_metrics") or [])[before_metrics:]
    return delta


def build_graph(checkpointer: Any = None) -> Any:
    topics_sub = build_topics_subgraph()
    writing_sub = build_writing_subgraph()
    visuals_sub = build_visuals_subgraph()

    g = StateGraph(ArticleState)
    g.add_node("topics_sub", lambda state: _run_subgraph(topics_sub, state))
    g.add_node("human_select", human_select_node)
    g.add_node("writing_sub", lambda state: _run_subgraph(writing_sub, state))
    g.add_node("human_review", human_review_node)
    g.add_node("visuals_sub", lambda state: _run_subgraph(visuals_sub, state))
    g.add_node("publish", publish_node)

    g.add_edge(START, "topics_sub")
    g.add_edge("topics_sub", "human_select")
    g.add_edge("human_select", "writing_sub")
    g.add_edge("writing_sub", "human_review")
    g.add_conditional_edges(
        "human_review",
        route_after_review,
        {"to_images": "visuals_sub", "to_draft": "writing_sub", "end": END},
    )
    g.add_edge("visuals_sub", "publish")
    g.add_edge("publish", END)
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


# --------------------------------------------------------------------------- #
# 历史 / 时间旅行
# --------------------------------------------------------------------------- #


def history(app: Any, thread_id: str, limit: int = 50) -> list[dict[str, Any]]:
    """列出这条会话的所有检查点（新的在前）。

    每一条都能拿去 `rewind()` —— 这就是课程里说的「支持从任意历史节点回滚重跑」。
    """
    rows: list[dict[str, Any]] = []
    for snap in app.get_state_history(config_for(thread_id), limit=limit):
        values = dict(snap.values or {})
        rows.append(
            {
                "checkpoint_id": snap.config["configurable"]["checkpoint_id"],
                "checkpoint_ns": snap.config["configurable"].get("checkpoint_ns", ""),
                "step": int(snap.metadata.get("step", -1)),
                "next": list(snap.next or []),
                "created_at": str(snap.created_at or ""),
                "revision": int(values.get("revision") or 0),
                "has_draft": bool(values.get("article_content")),
                "images": len(values.get("image_urls") or []),
                "interrupt": bool(snap.interrupts),
            }
        )
    return rows


def rewind(app: Any, thread_id: str, checkpoint_id: str, checkpoint_ns: str = "") -> ArticleState:
    """回到某个检查点，并从这里重新跑到底（或到下一个中断）。

    做法：把 `checkpoint_id`（和它的命名空间）放进 config —— LangGraph 会
    **从那个检查点**继续，而不是从最新状态继续。这就是课程里说的「时间旅行」：
    同一个 thread 下，每一步都可以被当成断点重来。
    """
    cfg = {
        "configurable": {
            "thread_id": thread_id,
            "checkpoint_ns": checkpoint_ns,
            "checkpoint_id": checkpoint_id,
        }
    }
    app.update_state(cfg, {})  # 在该检查点上开一条新分支（不改变原历史）
    return app.invoke(None, cfg)
