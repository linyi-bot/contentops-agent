"""阶段 3 的测试：评测器本身也要被测。

重点不在「指标数字好不好看」，而在四件容易悄悄坏掉的事：
1. 数据集读错要立刻炸，不能带着脏行出报告；
2. 相关度闸门开/关必须真的改变结果（否则「A→B」是假的）；
3. mock 必须引用第 1 条资料 —— 曾经因为它按空行分块，把第一条整条丢掉，
   评测里表现为「引用命中率 100%、事实一致性 7.5%」这种自相矛盾的表；
4. 失败样本必须出现在报告的失败明细里，只报总数不算评测。
"""
from __future__ import annotations

import json
import shutil
import uuid
from pathlib import Path

import pytest

from contentops import eval as E
from contentops.llm import MockLLM, _parse_context
from contentops.retriever import tokenize

TOPIC = "团队周报的痛点集中在哪几处"


@pytest.fixture()
def workdir():
    """不用 pytest 自带的 workdir：这台机器的临时目录 ACL 会让沙箱里的 pytest 报拒绝访问。"""
    d = Path(__file__).resolve().parent / "_tmp" / uuid.uuid4().hex[:8]
    d.mkdir(parents=True)
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


def _write(d: Path, rows):
    p = d / "golden.jsonl"
    p.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows), encoding="utf-8")
    return p


def _row(**kw):
    base = {"id": "T1", "kind": "regular", "topic": TOPIC, "must_cite": ["周报自动化的典型场景"], "should_refuse": False}
    base.update(kw)
    return base


def test_dataset_bad_line_fails_loudly(workdir):
    p = workdir / "bad.jsonl"
    p.write_text('{"id": "T1", "topic": "缺字段的样本"}\n', encoding="utf-8")
    with pytest.raises(ValueError, match="不是合法样本"):
        E.load_cases(p)


def test_dataset_duplicate_id_fails(workdir):
    p = _write(workdir, [_row(), _row()])
    with pytest.raises(ValueError, match="id 重复"):
        E.load_cases(p)


def test_tokenize_keeps_bigrams():
    toks = tokenize("周报自动化")
    assert "周报" in toks and "自动" in toks, "二元组没进词表，闸门就会把短查询误判成不相关"
    assert "周" in toks, "单字仍要保留，避免漏召回"


def test_mock_parser_keeps_first_document():
    """回归测试：上下文开头有「资料：」一行时，第 1 条资料曾经被整条丢掉。"""
    user = (
        "选题：测试\n\n资料：\n"
        "[1] 出自《kb.md》· 第一条\n甲段正文。\n\n"
        "[2] 出自《kb.md》· 第二条\n乙段正文。"
    )
    docs = _parse_context(user)
    assert [d[0] for d in docs] == [1, 2], f"解析出来的资料编号不对：{docs}"
    assert docs[0][2] == "第一条"


def test_mock_cites_the_first_document():
    user = "选题：测试\n\n资料：\n[1] 出自《kb.md》· 第一条\n甲段正文[1]的意思。"
    draft = MockLLM().complete("sys", user)
    assert "[1]" in draft, f"mock 应该引用第 1 条资料，实际：{draft}"


def test_gate_blocks_offtopic_and_passes_ontopic(monkeypatch):
    from contentops.graph import run

    monkeypatch.setenv("RETRIEVAL_MIN_SCORE", "0.09")
    off = run("红烧肉怎么做才不腻")
    assert off["hits"] == [], "不相关选题必须被闸门挡住"
    assert any("低于相关度阈值" in ev["note"] for ev in off["trace"])

    on = run(TOPIC)
    assert on["hits"], "相关选题不该被闸门误伤"


def test_gate_off_is_a_real_baseline(monkeypatch):
    from contentops.graph import run

    monkeypatch.setenv("RETRIEVAL_MIN_SCORE", "0")
    off = run("红烧肉怎么做才不腻", decision="approve")
    assert off["hits"], "关掉闸门时应能检索到（不相关资料）"
    assert "published" in off, "关掉闸门时这条链路会把不相关稿子发出去 —— 这正是基线要暴露的问题"


def test_eval_scores_and_report_detail(monkeypatch, workdir):
    monkeypatch.setenv("RETRIEVAL_MIN_SCORE", "0")
    cases = E.load_cases(
        _write(
            workdir,
            [
                _row(id="A1"),
                _row(id="A2", topic="红烧肉怎么做才不腻", must_cite=["不存在的章节"], should_refuse=True),
            ],
        )
    )
    report = E.evaluate(cases)

    assert report["counts"]["cases"] == 2
    # 应拒答却被发了 → 必须算失败，并且失败明细里写清为什么
    assert report["counts"]["passed"] < 2

    md = E.render_markdown(report)
    assert "失败明细" in md
    assert "A2" in md, "失败样本没进明细，这张报告就不算评测"
    assert "应拒答却发布了" in md

