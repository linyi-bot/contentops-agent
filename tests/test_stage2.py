"""阶段 2 的测试：事实核查、人工中断、打回重写。

这一层测的是「判断逻辑」和「流程控制」，不是「模型写得好不好」：
- 核查节点：四种情况（无引用 / 编号越界 / 引错段 / 引对了）各自什么结论；
- 中断：图真的停住、恢复后真的接着跑、打回真的回到 draft；
- 额度：打回和核查都吃同一个 MAX_REVISION，用尽后宁可收工也不硬发。
"""
from __future__ import annotations

from contentops import graph as G
from contentops.nodes import factcheck_node
from contentops.state import Hit

KB_TEXT = "人工中断不是流程越长越安全。只在钱、对外发布、不可逆动作这三处停下来。"
TOPIC = "用 AI 做周报自动化"


def _hit(text: str = KB_TEXT) -> Hit:
    return Hit(doc="kb.md", heading="人工中断该放在哪里", text=text, score=1.0)


def test_factcheck_flags_claim_without_citation():
    out = factcheck_node({"draft": "AI 可以把周报初稿的时间从两小时压到十分钟。", "hits": [_hit()]})
    assert out["unchecked"], "没标引用的断言必须被拦下"
    assert out["claims"][0]["reason"] == "没有引用标记"


def test_factcheck_flags_out_of_range_citation():
    out = factcheck_node({"draft": "周报自动化的核心是模板复用[7]。", "hits": [_hit()]})
    assert out["unchecked"], "引用编号越界必须被拦下"
    assert "不存在" in out["claims"][0]["reason"]


def test_factcheck_flags_citation_pointing_at_the_wrong_source():
    out = factcheck_node(
        {
            "draft": "周报自动化的核心是向量数据库召回率调参[1]。",
            "hits": [_hit()],
        }
    )
    assert out["unchecked"], "引用的段落里找不到断言关键词，说明引错了段"
    assert "找不到" in out["claims"][0]["reason"]


def test_factcheck_passes_when_citation_matches():
    out = factcheck_node(
        {"draft": "人工中断不该遍布全流程，只在不可逆动作处停下[1]。", "hits": [_hit()]}
    )
    assert out["unchecked"] == []
    assert out["claims"][0]["supported"] is True


def test_interrupt_pauses_and_resume_publishes():
    app, tid = G.new_session()
    state = G.start(app, TOPIC, tid)

    assert G.is_interrupted(state), "图必须停在人工审核处，而不是直接发布"
    packet = G.interrupt_payload(state)
    assert packet["revision"] == 1
    assert packet["unchecked"] == []
    assert "publish" not in [ev["node"] for ev in state["trace"]]

    state = G.resume(app, {"decision": "approve", "note": "看过了，可以发"}, tid)
    assert not G.is_interrupted(state)
    assert state["published"]["body"] == state["draft"]
    assert state["review_note"] == "看过了，可以发"


def test_reject_sends_it_back_for_rewrite():
    app, tid = G.new_session()
    state = G.start(app, TOPIC, tid)

    state = G.resume(app, {"decision": "reject", "note": "太短，补数据"}, tid)
    assert G.is_interrupted(state), "打回后应该重写并再次停下来等审核"
    assert G.interrupt_payload(state)["revision"] == 2
    assert "draft" in [ev["node"] for ev in state["trace"]]


def test_reject_loop_caps_and_never_publishes():
    state = G.run(TOPIC, decision="reject")
    assert "published" not in state, "一直被拒时不该偷偷发布"
    assert state["revision"] == G.MAX_REVISION


def test_bad_model_loops_then_hands_to_human(monkeypatch):
    """模型永远不标引用时：重写 MAX_REVISION 版，然后带着问题交给人工，而不是死循环。"""

    class NoCiteLLM:
        name = "nocite"

        def complete(self, system: str, user: str, max_tokens: int = 800) -> str:
            return "周报自动化能省很多时间。"

    monkeypatch.setattr("contentops.nodes.build_llm", lambda: NoCiteLLM())

    app, tid = G.new_session()
    state = G.start(app, TOPIC, tid)

    packet = G.interrupt_payload(state)
    assert packet["revision"] == G.MAX_REVISION
    assert packet["unchecked"], "没引用的稿子必须带着「未核实断言」进人工审核"

    drafts = [ev for ev in state["trace"] if ev["node"] == "draft"]
    assert len(drafts) == G.MAX_REVISION, f"重写次数应受额度限制，实际 {len(drafts)}"
