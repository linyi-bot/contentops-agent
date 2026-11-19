"""阶段 6：企业口径三件套 —— 节点成本、SSE、PostgreSQL 后端。

守三条底线：
1. 每个节点都留一条 `node_metrics`（耗时 + token），且和 trace 的 ms 逐条对得上；
2. `/stream` 真的边跑边推：`init → node* → interrupt → done`，且和 `POST /runs`
   写的是同一张 trace 表；
3. 没装 psycopg / 没起 PostgreSQL 时，报错必须是一句能照做的话。
"""
from __future__ import annotations

import json

import pytest

from contentops.trace_store import TraceStore


# --------------------------------------------------------------------------- #
# 节点成本
# --------------------------------------------------------------------------- #


def test_node_metrics_follow_trace(work, monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "mock")
    monkeypatch.setenv("IMAGE_PROVIDER", "mock")
    monkeypatch.setenv("IMAGE_OUT_DIR", str(work / "images"))
    monkeypatch.setenv("CONTENTOPS_NO_DOTENV", "1")
    from contentops.graph import new_session, resume, start

    app, tid = new_session()
    start(app, "AI 编程入门", tid)
    state = resume(app, {"selected_topic": "1"}, tid)
    state = resume(app, {"decision": "approve"}, tid)

    metrics = list(state.get("node_metrics") or [])
    events = list(state.get("trace") or [])

    assert [m["node"] for m in metrics] == [e["node"] for e in events]
    assert [m["ms"] for m in metrics] == [e["ms"] for e in events]
    assert all(isinstance(m["tokens"], int) and m["tokens"] >= 0 for m in metrics)
    draft = next(m for m in metrics if m["node"] == "write_draft")
    assert draft["model"], "写稿节点要记下用的哪个模型"


# --------------------------------------------------------------------------- #
# SSE
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


def _frames(text: str) -> list[dict]:
    return [json.loads(line[6:]) for line in text.splitlines() if line.startswith("data: ")]


def test_stream_pushes_nodes_then_interrupt(client, work):
    resp = client.get("/stream", params={"topic": "AI 编程入门"})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")

    frames = _frames(resp.text)
    types = [f["type"] for f in frames]

    assert types[0] == "init"
    assert types[-1] == "done"
    assert "interrupt" in types
    assert [f["data"]["node"] for f in frames if f["type"] == "node"] == ["plan_topics"]

    run_id = frames[0]["data"]["run_id"]
    assert frames[-1]["data"]["status"] == "paused"
    assert TraceStore(str(work / "traces.sqlite")).get_run(run_id)["status"] == "paused"


def test_stream_requires_topic(client):
    assert client.get("/stream", params={"topic": "  "}).status_code == 400


# --------------------------------------------------------------------------- #
# PostgreSQL 后端
# --------------------------------------------------------------------------- #


def test_pg_uri_expands_bare_database_name():
    from contentops.checkpoint import resolve_pg_uri

    assert resolve_pg_uri("contentops").endswith("/contentops")
    assert resolve_pg_uri("contentops").startswith("postgresql://")
    full = "postgresql://u:p@h:5433/db"
    assert resolve_pg_uri(full) == full


def test_pg_backend_failure_is_actionable():
    from contentops.checkpoint import postgres_saver

    with pytest.raises(RuntimeError) as exc:
        with postgres_saver("postgresql://postgres:x@127.0.0.1:1/none"):
            pass

    msg = str(exc.value)
    assert "psycopg" in msg or "连不上 PostgreSQL" in msg
