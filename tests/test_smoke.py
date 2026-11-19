"""冒烟：整条流水线在 mock 模型下能跑通，且六个节点顺序正确。

守三件事：
1. 两次 `interrupt()` 都在该停的地方停；
2. 放行后真的出图（mock 出 SVG），trace 六条不多不少；
3. 成品能落成 markdown，且图点 / 配图都在。
"""
from __future__ import annotations

import pytest

from contentops.graph import (
    interrupt_payload,
    is_interrupted,
    new_session,
    resume,
    start,
)
from contentops.output import write_markdown


@pytest.fixture(autouse=True)
def _mock(monkeypatch, work):
    monkeypatch.setenv("LLM_PROVIDER", "mock")
    monkeypatch.setenv("IMAGE_PROVIDER", "mock")
    monkeypatch.setenv("IMAGE_OUT_DIR", str(work / "images"))
    monkeypatch.setenv("CONTENTOPS_NO_DOTENV", "1")


def test_full_flow_stops_twice_then_draws():
    app, tid = new_session()
    state = start(app, "AI 编程入门", tid)

    # 第一次停：选题
    assert is_interrupted(state)
    packet = interrupt_payload(state)
    assert packet["step"] == "select_topic"
    assert len(packet["options"]) == 5
    assert state["status"] == "topics_generated"

    # 人选了第 2 个
    state = resume(app, {"selected_topic": "2"}, tid)
    assert is_interrupted(state)
    assert interrupt_payload(state)["step"] == "review"
    assert state["revision"] == 1
    assert state["article_content"]
    assert "最小实验" in state["selected_topic"]

    # 放行 → 出图 → 结束
    state = resume(app, {"decision": "approve"}, tid)
    assert not is_interrupted(state)
    assert len(state["visual_points"]) == 4
    assert len(state["image_urls"]) == 4
    assert state["status"] == "done"

    assert [e["node"] for e in state["trace"]] == [
        "plan_topics",
        "human_select",
        "write_draft",
        "human_review",
        "extract_visuals",
        "generate_images",
    ]


def test_markdown_contains_points_and_images(work):
    app, tid = new_session()
    start(app, "AI 编程入门", tid)
    state = resume(app, {"selected_topic": "1"}, tid)
    state = resume(app, {"decision": "approve"}, tid)

    path = write_markdown(state, work / "out")
    text = path.read_text(encoding="utf-8")

    assert path.name.endswith(".md")
    assert "## 配图脚本（图点）" in text
    assert "## 配图" in text
    assert text.count("![图") == 4


def test_mock_llm_shapes():
    """mock 必须按提示词给出三种形状，否则流程测的就是解析器的容错。"""
    from contentops.llm import MockLLM
    from contentops.nodes import DRAFT_SYSTEM, TOPIC_SYSTEM, VISUAL_SYSTEM, _numbered_lines

    llm = MockLLM()
    topics = _numbered_lines(llm.complete(TOPIC_SYSTEM, "方向：AI 编程入门"), limit=5)
    points = _numbered_lines(llm.complete(VISUAL_SYSTEM, "文案：随便"), limit=5)

    assert len(topics) == 5 and all(len(t) > 4 for t in topics)
    assert len(points) == 4
    assert len(llm.complete(DRAFT_SYSTEM, "选题：x")) > 100


def test_no_key_falls_back_to_mock(monkeypatch):
    """没给 key 也不该崩：build_llm / build_image_service 都回落 mock。"""
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setenv("CONTENTOPS_NO_DOTENV", "1")
    from contentops.image_service import build_image_service
    from contentops.llm import build_llm

    assert build_llm().name == "mock"
    assert build_image_service().name == "mock"
