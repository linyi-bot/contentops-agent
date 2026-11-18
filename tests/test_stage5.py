"""阶段 5：trace 落库 + 极简页面。

两条底线，测试就守这两条：
1. 同一份 state 重复 sync 不能把事件写两遍（页面上的时间线会重影）；
2. 页面能「跑一条 → 停在审核 → 放行 → 出发布稿」，且 trace 一路累加。
"""
from __future__ import annotations

import pytest

from contentops.output import slug_for, write_markdown
from contentops.trace_store import TraceStore


def _state(topic: str, *events: tuple[str, int, str], **extra) -> dict:
    return {
        "topic": topic,
        "revision": extra.pop("revision", 0),
        "trace": [{"node": n, "ms": ms, "note": note} for n, ms, note in events],
        **extra,
    }


# --------------------------------------------------------------------------- #
# trace store
# --------------------------------------------------------------------------- #


def test_sync_is_idempotent(work):
    store = TraceStore(work / "t.sqlite")
    state = _state("选题", ("retrieve", 3, "检索到 3 条"))

    store.sync(state, run_id="r1")
    store.sync(state, run_id="r1")  # 同一份 state 再来一次
    assert [e["node"] for e in store.events("r1")] == ["retrieve"]

    state["trace"].append({"node": "draft", "ms": 5, "note": "写了一版"})
    store.sync(state, run_id="r1")  # 只补新增的那条
    assert [e["seq"] for e in store.events("r1")] == [0, 1]


def test_status_follows_state(work):
    store = TraceStore(work / "t.sqlite")

    store.sync(_state("a", ("retrieve", 1, "x"), __interrupt__=("wait",)), run_id="paused", mode="mock")
    assert store.get_run("paused")["status"] == "paused"

    store.sync(
        _state("b", ("retrieve", 1, "x"), published={"title": "b"}), run_id="pub", mode="mock"
    )
    assert store.get_run("pub")["status"] == "published"

    store.sync(_state("c", ("retrieve", 1, "x")), run_id="done", mode="mock")
    assert store.get_run("done")["status"] == "ended"

    store.sync(_state("d"), run_id="bad", error="RuntimeError: 炸了")
    row = store.get_run("bad")
    assert row["status"] == "error" and "炸了" in row["error"]


def test_mode_keeps_the_first_model(work):
    """跑到一半换了模型（恢复时回落 mock），这行仍该记第一次用的那个。"""
    store = TraceStore(work / "t.sqlite")
    store.sync(_state("a", ("retrieve", 1, "x")), run_id="m", mode="deepseek-flash")
    store.sync(_state("a", ("retrieve", 1, "x")), run_id="m", mode="mock")
    assert store.get_run("m")["mode"] == "deepseek-flash"


def test_usage_accumulates_and_list_is_newest_first(work):
    store = TraceStore(work / "t.sqlite")
    store.sync(_state("a", ("retrieve", 1, "x")), run_id="a", usage=(2, 100, 30, 0.0012))
    store.sync(_state("a", ("retrieve", 1, "x")), run_id="a", usage=(1, 10, 5, 0.0003))
    row = store.get_run("a")
    assert (row["calls"], row["tokens_in"], row["tokens_out"]) == (3, 110, 35)
    assert row["cost_cny"] == pytest.approx(0.0015)

    store.sync(_state("b"), run_id="b")
    assert [r["run_id"] for r in store.list_runs()] == ["b", "a"]


# --------------------------------------------------------------------------- #
# 发布稿落盘
# --------------------------------------------------------------------------- #


def test_write_markdown(work):
    state = {
        "published": {
            "title": "用 AI 做周报 / 自动化",
            "body": "正文",
            "sources": [{"index": 1, "doc": "周报.md", "heading": "一、为什么"}],
            "unchecked": ["某条没核实"],
            "gate": "passed",
            "revision": 1,
            "published_at": "2026-11-19T02:00:00",
        }
    }
    path = write_markdown(state, work)
    assert path.name == f"{slug_for('用 AI 做周报 / 自动化')}.md"
    text = path.read_text(encoding="utf-8")
    assert "## 来源" in text and "《周报.md》 · 一、为什么" in text
    assert "未核实断言" in text and "发布闸门：passed" in text


# --------------------------------------------------------------------------- #
# 页面
# --------------------------------------------------------------------------- #


@pytest.fixture()
def client(work, monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "mock")
    monkeypatch.setenv("CONTENTOPS_TRACE_DB", str(work / "traces.sqlite"))
    monkeypatch.setenv("CONTENTOPS_WEB_DB", str(work / "web.sqlite"))
    monkeypatch.setenv("CONTENTOPS_OUT_DIR", str(work / "out"))
    from fastapi.testclient import TestClient

    from contentops import web

    return TestClient(web.app)


def test_page_runs_the_whole_human_loop(client, work):
    assert client.get("/").status_code == 200
    assert "还没有运行记录" in client.get("/").text

    resp = client.post("/runs", data={"topic": "用 AI 做周报自动化"}, follow_redirects=False)
    assert resp.status_code == 303
    detail_url = resp.headers["location"]

    page = client.get(detail_url)
    assert page.status_code == 200
    assert "等人工审核" in page.text
    assert "retrieve" in page.text and "factcheck" in page.text
    assert "放行并发布" in page.text

    run_id = detail_url.rsplit("/", 1)[-1]
    assert len(TraceStore(str(work / "traces.sqlite")).events(run_id)) >= 3

    # 打回一次：会重写一版，然后又停在审核
    client.post(f"{detail_url}/decision", data={"decision": "reject", "note": "太干，加点例子"})
    page = client.get(detail_url)
    assert "等人工审核" in page.text and "第 2 版" in page.text

    # 放行：发布稿落盘，页面出现发布记录
    resp = client.post(
        f"{detail_url}/decision", data={"decision": "approve", "note": ""}, follow_redirects=False
    )
    assert resp.status_code == 303
    page = client.get(detail_url)
    assert "已发布" in page.text and "发布记录" in page.text

    saved = work / "out" / f"{slug_for('用 AI 做周报自动化')}.md"
    assert saved.exists()
    assert "发布闸门" in saved.read_text(encoding="utf-8")

    # 已经发布的运行再点一次，不该报错也不该再写一行
    assert client.post(f"{detail_url}/decision", data={"decision": "approve"}).status_code == 200

    runs = TraceStore(str(work / "traces.sqlite")).list_runs()
    assert len(runs) == 1 and runs[0]["status"] == "published"
    assert client.get("/healthz").json()["runs"] == 1


def test_detail_404(client):
    assert client.get("/runs/run-not-exist").status_code == 404
