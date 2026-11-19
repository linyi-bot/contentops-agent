"""图的共享状态 —— 项目二（自媒体运营 AI 智能体）版。

一条流水线两次人机接力：

    选题方向 → AI 出 3-5 个选题 → **人选一个** → 写长文
           → **人审（通过 / 打回重写）** → 提炼 3-5 个图点 → 并行出图 → 结束

设计要点：
- `trace` / `node_metrics` 用 `operator.add` 累加：任何节点都能 append 一条，
  不接 LangSmith 也能看清每一步；两者都在 state 里，所以历史快照也能查成本。
- 其余字段是覆盖语义，节点之间不共享可变对象。
- `revision` 是「第几版正文」，打回一次 +1；上限在 graph 里（MAX_REVISION）。
"""
from __future__ import annotations

import operator
from typing import Annotated, Any, TypedDict


class TraceEvent(TypedDict):
    """一条执行轨迹（给人看的时间线）。"""

    node: str
    ms: int
    note: str


class Hit(TypedDict):
    """一条检索结果（`contentops kb` 还在用旧检索器，类型留着）。"""

    doc: str
    heading: str
    text: str
    score: float


class NodeMetric(TypedDict):
    """一个节点的成本归因（给成本表算的）。"""

    node: str
    ms: int
    tokens: int
    model: str


class SelectPacket(TypedDict):
    """第一次 `interrupt()` 推出去的包：让运营从候选里挑一个选题。"""

    step: str  # 固定 "select_topic"
    topic_direction: str
    options: list[str]


class ReviewPacket(TypedDict):
    """第二次 `interrupt()` 推出去的包：把整篇稿子交给审核人。"""

    step: str  # 固定 "review"
    topic: str
    draft: str
    revision: int
    options: list[str]  # ["approve", "reject"]


class ArticleState(TypedDict, total=False):
    """整条流水线的状态。"""

    # 输入
    topic_direction: str  # 用户给的大方向，如「LangChain 新特性」
    platform: str  # 发布平台：xiaohongshu / wechat（只影响提示词口吻）

    # 选题阶段
    generated_topics: list[str]  # AI 出的备选
    selected_topic: str  # 人选定的

    # 写作阶段
    article_content: str
    revision: int  # 第几版（从 1 开始）
    review_decision: str  # approve / reject
    review_feedback: str  # 打回意见，重写时带上

    # 出图阶段
    visual_points: list[str]  # 3-5 个图点（每张图一句话描述）
    image_urls: list[str]  # 出图结果（本地路径或远端 URL）
    image_errors: list[str]  # 失败的图点，不藏着

    # 发布阶段
    published: dict[str, Any]  # {"xiaohongshu": {...}, "wechat": {...}}

    # 流程状态（给页面显示）
    status: str

    # 观测
    trace: Annotated[list[TraceEvent], operator.add]
    node_metrics: Annotated[list[NodeMetric], operator.add]


#: 初始状态：图启动时铺一层，避免节点里到处 `state.get(..., [])`
INITIAL_STATE: dict[str, Any] = {
    "generated_topics": [],
    "selected_topic": "",
    "article_content": "",
    "revision": 0,
    "review_decision": "",
    "review_feedback": "",
    "visual_points": [],
    "image_urls": [],
    "image_errors": [],
    "status": "started",
}
