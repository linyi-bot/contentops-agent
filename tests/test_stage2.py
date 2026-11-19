"""阶段 2 的测试：选题解析、选择归一化、打回重写、额度上限。

这一层测的是「判断逻辑」和「流程控制」，不是「模型写得好不好」：
- 选题 / 图点的解析要能容忍模型加开场白、换编号符号；
- 人的选择可能是序号，也可能是标题，甚至是自定义；
- 打回真的回到写稿并 +1 版，额度用尽就收工，不硬发。
"""
from __future__ import annotations

import pytest

from contentops import graph as G
from contentops.graph import interrupt_payload, is_interrupted, new_session, resume, start
from contentops.nodes import _normalize_choice, _numbered_lines


@pytest.fixture(autouse=True)
def _mock(monkeypatch, work):
    monkeypatch.setenv("LLM_PROVIDER", "mock")
    monkeypatch.setenv("IMAGE_PROVIDER", "mock")
    monkeypatch.setenv("IMAGE_OUT_DIR", str(work / "images"))
    monkeypatch.setenv("CONTENTOPS_NO_DOTENV", "1")


# --------------------------------------------------------------------------- #
# 解析
# --------------------------------------------------------------------------- #


def test_numbered_lines_tolerates_chatter():
    text = """好的，以下是 5 个选题：

1. 第一个：先搞清概念
2、第二个用顿号
- 圆点也能认
* 星号也行
（这句不是选题）

5) 第五个
"""
    items = _numbered_lines(text, limit=10)
    assert items == ["第一个：先搞清概念", "第二个用顿号", "圆点也能认", "星号也行", "第五个"]


def test_normalize_choice_accepts_index_and_title():
    options = ["A 选题", "B 选题", "C 选题"]
    assert _normalize_choice("2", options) == "B 选题"
    assert _normalize_choice({"selected_topic": "C 选题"}, options) == "C 选题"
    assert _normalize_choice("B 选题", options) == "B 选题"
    assert _normalize_choice("D 我自己想的", options) == "D 我自己想的"
    assert _normalize_choice("99", options) == "99"  # 越界不猜，原样带走
    assert _normalize_choice("", options) == "A 选题"  # 空答案回落到第一个


# --------------------------------------------------------------------------- #
# 路由
# --------------------------------------------------------------------------- #


def test_route_after_review():
    assert G.route_after_review({"review_decision": "approve", "revision": 1}) == "to_images"
    assert G.route_after_review({"review_decision": "reject", "revision": 1}) == "to_draft"
    assert G.route_after_review(
        {"review_decision": "reject", "revision": G.MAX_REVISION}
    ) == "end"


# --------------------------------------------------------------------------- #
# 打回
# --------------------------------------------------------------------------- #


def test_reject_rewrites_and_bumps_revision():
    app, tid = new_session()
    start(app, "AI 编程入门", tid)
    state = resume(app, {"selected_topic": "1"}, tid)

    first = state["article_content"]
    state = resume(app, {"decision": "reject", "feedback": "太干了，加个例子"}, tid)

    assert state["revision"] == 2
    assert state["review_feedback"] == "太干了，加个例子"
    assert state["article_content"] != first
    assert is_interrupted(state)
    assert interrupt_payload(state)["revision"] == 2


def test_revision_cap_ends_without_images():
    """拒绝到上限就收工：不硬发，也不出图。"""
    app, tid = new_session()
    start(app, "AI 编程入门", tid)
    state = resume(app, {"selected_topic": "1"}, tid)

    for _ in range(G.MAX_REVISION):
        if not is_interrupted(state):
            break
        state = resume(app, {"decision": "reject", "feedback": "再来"}, tid)

    assert not is_interrupted(state)
    assert state["revision"] == G.MAX_REVISION
    assert not state.get("image_urls")
    assert "generate_images" not in [e["node"] for e in state["trace"]]


def test_approve_goes_straight_to_images():
    app, tid = new_session()
    start(app, "AI 编程入门", tid)
    state = resume(app, {"selected_topic": "3"}, tid)
    state = resume(app, {"decision": "approve"}, tid)
    assert state["revision"] == 1
    assert len(state["image_urls"]) == 4
