"""阶段 9：新流程的评测器。

守四件事：
1. 打分规则是纯函数，能单独测（选题长度/标题党/同质化、正文长度/段落/小标题、图点跑题）；
2. 数据集读错要立刻炸 —— 带着脏行跑出来的报告比没报告更糟；
3. 汇总口径固定：四项指标的分子分母写死在测试里；
4. 端到端跑一条（mock）能出报告，且报告里有「口径」那一节。
"""
from __future__ import annotations

import json

import pytest

from contentops.eval import (
    Sample,
    score_draft,
    score_points,
    score_topics,
    summarize,
    load_dataset,
    run_eval,
)


# --------------------------------------------------------------------------- #
# 打分规则
# --------------------------------------------------------------------------- #


def test_topics_accepts_good_list():
    ok, notes = score_topics([
        "新手怎么用 AI 写第一行代码",
        "转行做 AI 开发要补哪些课",
        "用 Cursor 改老项目的一个下午",
        "为什么你的提示词越写越长",
        "面试被问 RAG 时怎么答",
    ])
    assert all(ok), notes


def test_topics_rejects_clickbait_and_length():
    ok, notes = score_topics(["震惊！AI 编程原来这么简单", "短", "正常的十二个字左右的选题内容"])
    assert ok[0] is False and ok[1] is False
    assert any("标题党" in n for n in notes)


def test_topics_rejects_duplicates():
    ok, _ = score_topics([
        "Cursor 和 Claude Code 怎么选",
        "Cursor 和 Claude Code 实测对比",
        "零基础学 Python 的三个月路线",
    ])
    assert ok[0] is False and ok[1] is False
    assert ok[2] is True


def test_draft_needs_length_paragraphs_and_heading():
    good = "一、先说结论\n" + "字" * 300 + "\n\n二、怎么做\n" + "字" * 120
    ok, _ = score_draft(good)
    assert ok

    ok, reasons = score_draft("太短了")
    assert not ok and any("250" in r for r in reasons)

    ok, reasons = score_draft("字" * 400)  # 没有小标题
    assert not ok and any("小标题" in r for r in reasons)


def test_points_catch_off_topic_and_english_is_counted():
    draft = "LangChain 和 LangGraph 的区别在于职责不同，前者偏组件，后者偏编排。"
    ok, _ = score_points(["LangChain 负责组件拼装", "完全不相关的一张图"], draft)
    assert ok[0] is True          # 英文词也算交集
    assert ok[1] is False         # 跑题


# --------------------------------------------------------------------------- #
# 数据集
# --------------------------------------------------------------------------- #


def test_dataset_parsing(work):
    p = work / "d.jsonl"
    p.write_text('{"direction": "A"}\n\n{"direction": "B", "platform": "wechat"}\n', encoding="utf-8")
    samples = load_dataset(p)
    assert [s.direction for s in samples] == ["A", "B"]
    assert samples[1].platform == "wechat"


def test_dataset_errors_are_loud(work):
    with pytest.raises(FileNotFoundError):
        load_dataset(work / "nope.jsonl")

    bad = work / "bad.jsonl"
    bad.write_text("{not json}\n", encoding="utf-8")
    with pytest.raises(ValueError):
        load_dataset(bad)

    empty = work / "empty.jsonl"
    empty.write_text("\n", encoding="utf-8")
    with pytest.raises(ValueError):
        load_dataset(empty)


# --------------------------------------------------------------------------- #
# 汇总
# --------------------------------------------------------------------------- #


def test_summarize_math():
    from contentops.eval import SampleResult

    a = SampleResult(direction="a", platform="xiaohongshu", topics=["x", "y"], topic_ok=[True, False],
                     points=["p"], point_ok=[True], images=1, draft_ok=True)
    b = SampleResult(direction="b", platform="xiaohongshu", topics=["z"], topic_ok=[True],
                     points=["q"], point_ok=[False], images=0, draft_ok=False)

    s = summarize([a, b])
    assert s["样本"] == 2
    assert s["选题合格率"] == pytest.approx(66.7, abs=0.1)
    assert s["成稿合格率"] == 50.0
    assert s["图点合格率"] == 50.0
    assert s["出图成功率"] == 50.0
    assert s["通过"] == 0


# --------------------------------------------------------------------------- #
# 端到端（mock）
# --------------------------------------------------------------------------- #


def test_eval_end_to_end_with_mock(work, monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "mock")
    monkeypatch.setenv("IMAGE_PROVIDER", "mock")
    monkeypatch.setenv("IMAGE_OUT_DIR", str(work / "images"))
    monkeypatch.setenv("CONTENTOPS_NO_DOTENV", "1")

    ds = work / "two.jsonl"
    ds.write_text(
        "\n".join([
            json.dumps({"direction": "AI 编程入门"}, ensure_ascii=False),
            json.dumps({"direction": "转行做 AI 应用开发要多久"}, ensure_ascii=False),
        ]),
        encoding="utf-8",
    )

    results, summary, path = run_eval(ds, out=work / "reports", label="mock", progress=False)

    assert len(results) == 2
    assert summary["通过"] == 2
    assert path is not None and path.exists()
    text = path.read_text(encoding="utf-8")
    assert "## 一、汇总" in text and "## 五、口径" in text
    assert "出图成功率" in text
