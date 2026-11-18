"""冒烟测试：保证「clone 下来能跑」这件事不被后续改动破坏。

刻意不测模型输出内容 —— 那是评测集（阶段 3）的事。
这里只测四件不能坏的事：
1. 知识库能被加载并检索出结果；
2. 整张图能编译、能跑完、trace 里五个节点按顺序都在；
3. 没有 API key 时不会抛异常（mock 回落）；
4. 检索零命中时，流水线仍然能走到人工审核，并且不允许「裸发」。
"""
from __future__ import annotations

from contentops.graph import build_graph, run
from contentops.retriever import Retriever
from contentops.state import ContentState

PIPELINE = ["retrieve", "draft", "factcheck", "review", "publish"]


def test_kb_loads_and_searches():
    r = Retriever()
    assert r.size > 0, "知识库为空，docs/kb/*.md 没被加载"
    hits = r.search("事实核查 三类错误", top_k=3)
    assert hits, "检索无命中，打分逻辑或分词出了问题"
    assert hits[0]["score"] > 0


def test_graph_compiles_and_runs():
    app = build_graph()
    assert app is not None

    state: ContentState = run("用 AI 做周报自动化", decision="approve")
    assert state["topic"] == "用 AI 做周报自动化"
    assert state["draft"], "草稿为空"

    nodes = [ev["node"] for ev in state["trace"]]
    assert nodes == PIPELINE, f"trace 节点顺序不对：{nodes}"

    assert state["unchecked"] == [], "mock 会照着资料标引用，不该有未核实断言"
    assert state["published"]["gate"] == "passed"
    assert state["published"]["body"] == state["draft"]


def test_works_without_api_key(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setenv("LLM_PROVIDER", "deepseek")  # 故意选真模型但不给 key
    from contentops.llm import build_llm

    llm = build_llm()
    assert llm.name == "mock", "没 key 时必须回落 mock，否则新机器上跑不起来"
    assert "mock" in llm.complete("sys", "user")


def test_retrieval_miss_is_not_fatal():
    """检索不到东西时，流水线仍要走完，而且不能把「没有资料」当通过。

    注意：中文按字切分，随便一句中文都能撞上几个字，
    所以这里用纯拉丁乱码，才是真正的「零命中」。
    """
    state = run("zzqqxx vvbbnn mmcc", decision="approve")
    assert state["hits"] == [], "这串乱码不该命中任何块"
    assert state["draft"], "零命中也要出草稿（并说明没有资料）"
    assert any("无命中" in ev["note"] for ev in state["trace"])

    nodes = [ev["node"] for ev in state["trace"]]
    assert nodes == PIPELINE, f"零命中时 trace 断了：{nodes}"

    assert state["unchecked"], "没有任何可核实引用时，必须留一条给人确认"
    assert state["published"]["gate"].startswith("forced"), "零命中不该以 passed 闸门发布"
