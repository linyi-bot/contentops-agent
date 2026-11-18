"""图的共享状态。

设计要点：
- `trace` 用 `operator.add` reducer 累加，任何节点都可以 append 一条，
  这是「可观测」的最小实现：不接 LangSmith 也能看清每一步发生了什么。
- 其它字段是覆盖语义，禁止节点之间隐式共享可变对象。
- `claims` / `unchecked` 是事实核查的产物：`claims` 记全部断言与判定理由，
  `unchecked` 只记没过的部分 —— 条件边只读 `unchecked`，人看的也是它。
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


class Claim(TypedDict):
    """从草稿里切出来的一条断言，以及它为什么算通过/不通过。

    注意：这里判的是「引用有没有落地」（编号存在、且引用的段落里能找到这句的
    关键词），不是「语义上是否真的支持」。后者要靠阶段 3 的黄金集和人工审核。
    """

    text: str
    cites: list[int]
    supported: bool
    reason: str


class TraceEvent(TypedDict):
    """一条执行轨迹。"""

    node: str
    ms: int
    note: str


class ReviewPacket(TypedDict):
    """推给人工审核的那一屏内容 —— 也就是 `interrupt()` 的 payload。

    审核的人只看这一份东西就要能做出放行 / 打回的决定，所以它必须自带
    选题、草稿、每条断言的核查结果、以及这是第几版。
    """

    topic: str
    draft: str
    claims: list[Claim]
    unchecked: list[str]
    revision: int


class ContentState(TypedDict, total=False):
    """整条流水线的状态。"""

    topic: str
    hits: list[Hit]
    draft: str
    claims: list[Claim]
    unchecked: list[str]
    revision: int
    review_decision: str
    review_note: str
    published: dict[str, Any]
    trace: Annotated[list[TraceEvent], operator.add]
