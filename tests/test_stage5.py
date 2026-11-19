"""阶段 5 的测试：trace 落库 + 页面上的三步人机接力。

两条底线：
1. 同一份 state 重复 sync 不能把事件写两遍（页面时间线会重影）；
2. 页面能「跑一条 → 选选题 → 打回一次 → 放行 → 出图」，且成品 md 落盘。
"""
from __future__ import annotations

import pytest

from contentops.trace_store import TraceStore


def _state(direction: str, *events: tuple[str, int, str], **extra) -> dict:
    return {
        "topic_direction": direction,
        "revision": extra.pop("revision", 0),
        "trace": [{"node": n, "ms": ms, "note": note} for n, ms, note in events],
        **extra,
    }


# --------------------------------------------------------------------------- #
# trace store
# --------------------------------------------------------------------------- #


def test_sync_is_idempotent(work):
    store = TraceStore(work / "t.sqlite")
    state = _state("选题", ("plan_topics", 3, "出了 5 个"))

    store.sync(state, run_id="r1")
    store.sync(state, run_id="r1")
    assert [e["node"] for e in store.events("r1")] == ["plan_topics"]

    state["trace"].append({"node": "write_draft", "ms": 5, "note": "写了一版"})
    store.sync(state, run_id="r1")
    assert [e["seq"] for e in store.events("r1")] == [0, 1]


def test_status_follows_state(work):
    store = TraceStore(work / "t.sqlite")

    store.sync(_state("a", ("plan_topics", 1, "x"), __interrupt__=("wait",)), run_id="paused")
    assert store.get_run("paused")["status"] == "paused"

    store.sync(_state("b", ("generate_images", 1, "x"), image_urls=["/outputs/a.svg"]), run_id="done")
    assert store.get_run("done")["status"] == "completed"

    store.sync(_state("c", ("plan_topics", 1, "x")), run_id="ended")
    assert store.get_run("ended")["status"] == "ended"

    store.sync(_state("d"), run_id="bad", error="RuntimeError: 炸了")
    assert store.get_run("bad")["status"] == "error"


def test_feedback_is_recorded(work):
    store = TraceStore(work / "t.sqlite")
    store.sync(_state("a", ("human_review", 1, "打回"),
                      review_decision="reject", review_feedback="太干了"),
               run_id="r1")
    row = store.get_run("r1")
    assert row["decision"] == "reject" and "太干" in row["review_note"]


# --------------------------------------------------------------------------- #
# 页面
# --------------------------------------------------------------------------- #


@pytest.fixture()
def client(work, monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "mock")
    monkeypatch.setenv("IMAGE_PROVIDER", "mock")
    monkeypatch.setenv("IMAGE_OUT_DIR", str(work / "out" / "images"))
    monkeypatch.setenv("CONTENTOPS_TRACE_DB", str(work / "traces.sqlite"))
    monkeypatch.setenv("CONTENTOPS_WEB_DB", str(work / "web.sqlite"))
    monkeypatch.setenv("CONTENTOPS_OUT_DIR", str(work / "out"))
    monkeypatch.setenv("CONTENTOPS_NO_DOTENV", "1")
    from fastapi.testclient import TestClient

    from contentops import web

    return TestClient(web.app)


def test_page_runs_the_whole_human_loop(client, work):
    assert "还没有运行记录" in client.get("/").text

    resp = client.post("/runs", data={"topic": "AI 编程入门"}, follow_redirects=False)
    assert resp.status_code == 303
    url = resp.headers["location"]

    page = client.get(url)
    assert "选一个选题" in page.text
    assert page.text.count('name="selected_topic"') == 5

    # 选题 → 停在审核
    picked = page.text.split('name="selected_topic" value="')[1].split('"')[0]
    import html as H

    picked = H.unescape(picked)
    client.post(f"{url}/select", data={"selected_topic": picked})
    page = client.get(url)
    assert "人工审核" in page.text and "第 1 版" in page.text

    # 打回一次 → 第 2 版，又停在审核
    client.post(f"{url}/review", data={"decision": "reject", "note": "太干，加例子"})
    page = client.get(url)
    assert "第 2 版" in page.text and "人工审核" in page.text

    # 放行 → 出图 → 成品落盘
    client.post(f"{url}/review", data={"decision": "approve", "note": ""})
    page = client.get(url)
    assert "配图" in page.text and page.text.count("<figure>") == 4
    assert "已完成" in page.text

    run_id = url.rsplit("/", 1)[-1]
    assert len(TraceStore(str(work / "traces.sqlite")).events(run_id)) >= 6
    assert list((work / "out").glob("*.md"))


def test_detail_404(client):
    assert client.get("/runs/run-not-exist").status_code == 404


def test_selection_accepts_custom_topic(client):
    resp = client.post("/runs", data={"topic": "AI 编程入门"}, follow_redirects=False)
    url = resp.headers["location"]
    client.post(f"{url}/select", data={"selected_topic": "我自己想的选题"})
    page = client.get(url)
    assert "我自己想的选题" in page.text
