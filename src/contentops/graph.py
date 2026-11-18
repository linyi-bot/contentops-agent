"""图的组装。

阶段 1 只挂三个节点；阶段 2 会在 `draft` 和 `END` 之间插入
factcheck → (条件边回 draft) → human_review(interrupt) → publish。
"""
from __future__ import annotations

from typing import Any

from langgraph.graph import END, START, StateGraph

from .nodes import draft_node, retrieve_node
from .state import ContentState


def build_graph() -> Any:
    g = StateGraph(ContentState)
    g.add_node("retrieve", retrieve_node)
    g.add_node("draft", draft_node)

    g.add_edge(START, "retrieve")
    g.add_edge("retrieve", "draft")
    g.add_edge("draft", END)
    return g.compile()


def run(topic: str) -> ContentState:
    """跑一条完整链路。返回最终 state（含 trace）。"""
    app = build_graph()
    return app.invoke({"topic": topic})
