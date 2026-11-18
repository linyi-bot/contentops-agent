"""图的共享状态。

设计要点：
- `trace` 用 `operator.add` reducer 累加，任何节点都可以 append 一条，
  这是「可观测」的最小实现：不接 LangSmith 也能看清每一步发生了什么。
- 其它字段是覆盖语义，禁止节点之间隐式共享可变对象。
"""
from __future__ import annotations

import operator
from typing import Annotated, Any, TypedDict


class Hit(TypedDict):
    """一条检索结果。"""

    doc: str
    heading: str
    text: str
    score: float


class TraceEvent(TypedDict):
    """一条执行轨迹。"""

    node: str
    ms: int
    note: str


class ContentState(TypedDict, total=False):
    """整条流水线的状态。"""

    topic: str
    hits: list[Hit]
    draft: str
    claims: list[str]
    verdict: str
    approved: bool
    published: dict[str, Any]
    trace: Annotated[list[TraceEvent], operator.add]
