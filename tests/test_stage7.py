"""阶段 7：把课程 PDF 里剩下的那几条工程纵深补上。

对应关系（PDF 清单 → 这里的用例）：

| PDF | 用例 |
|---|---|
| 结构化输出 | `test_structured_topics` / `test_schema_rejects_garbage` / `test_topic_falls_back` |
| 多模型路由 | `test_model_routing_by_role` |
| 出图超时重试 | `test_seedream_retries_then_succeeds` |
| 历史 / 时间旅行 | `test_history_and_rewind` / `test_api_history_and_rewind` |
| JWT 鉴权 | `test_jwt_roundtrip` / `test_api_requires_token` |
| 双平台发布 | `test_write_publish_two_platforms` |
| token 级流式 | `test_stream_emits_llm_tokens` |
"""
from __future__ import annotations

import json
import sys
import types

import pytest

from contentops.schemas import TopicList, VisualList


@pytest.fixture(autouse=True)
def _env(monkeypatch, work):
    monkeypatch.setenv("LLM_PROVIDER", "mock")
    monkeypatch.setenv("IMAGE_PROVIDER", "mock")
    monkeypatch.setenv("CONTENTOPS_NO_DOTENV", "1")
    monkeypatch.setenv("IMAGE_OUT_DIR", str(work / "out" / "images"))
    monkeypatch.setenv("CONTENTOPS_USERS_DB", str(work / "users.sqlite"))


# --------------------------------------------------------------------------- #
# 结构化输出
# --------------------------------------------------------------------------- #


def test_structured_topics():
    from contentops.llm import MockLLM
    from contentops.nodes import TOPIC_SYSTEM_JSON

    result = MockLLM().complete_json(TOPIC_SYSTEM_JSON, "方向：AI 编程入门", TopicList)
    assert isinstance(result, TopicList)
    assert 3 <= len(result.topics) <= 5


def test_schema_rejects_garbage():
    with pytest.raises(Exception):
        TopicList.model_validate({"topics": []})
    with pytest.raises(Exception):
        TopicList.model_validate({"nope": ["x"]})
    # 模型自己加的序号会被剥掉
    cleaned = TopicList.model_validate({"topics": ["1、第一条", "2. 第二条", "第三条"]})
    assert cleaned.topics[0] == "第一条"


def test_topic_falls_back_to_line_parsing(monkeypatch):
    """结构化失败时退回「按行解析」，流程不能断。"""
    from contentops import nodes
    from contentops.llm import MockLLM

    class Broken(MockLLM):
        def complete_json(self, *a, **kw):
            raise RuntimeError("模型不支持 json_object")

    monkeypatch.setattr(nodes, "build_llm", lambda role="default": Broken())
    out = nodes.plan_topics_node({"topic_direction": "AI 编程入门"})
    assert len(out["generated_topics"]) == 5
    assert "退回文本解析" in out["trace"][0]["note"]


# --------------------------------------------------------------------------- #
# 多模型路由
# --------------------------------------------------------------------------- #


def test_model_routing_by_role(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "deepseek")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    monkeypatch.setenv("LLM_MODEL", "base-model")
    monkeypatch.setenv("LLM_MODEL_CHEAP", "cheap-model")
    monkeypatch.setenv("LLM_MODEL_SMART", "smart-model")
    from contentops import llm

    assert llm.build_llm("cheap").name == "cheap-model"
    assert llm.build_llm("smart").name == "smart-model"
    assert llm.build_llm().name == "base-model"


# --------------------------------------------------------------------------- #
# 出图重试
# --------------------------------------------------------------------------- #


def test_seedream_retries_then_succeeds(monkeypatch):
    from contentops import image_service as svc

    calls = {"n": 0}
    sleeps: list[float] = []

    class Resp:
        def __init__(self, code, payload=None):
            self.status_code = code
            self._payload = payload or {}
            self.text = "boom"

        def json(self):
            return self._payload

    def fake_post(*a, **kw):
        calls["n"] += 1
        if calls["n"] < 3:
            return Resp(503)
        return Resp(200, {"data": [{"url": "https://example.com/a.png"}]})

    monkeypatch.setitem(sys.modules, "requests", types.SimpleNamespace(post=fake_post))
    monkeypatch.setattr(svc, "_sleep", lambda s: sleeps.append(s))

    r = svc.SeedreamImageService("sk-test").generate("图点")
    assert r.ok and r.url.endswith("a.png")
    assert calls["n"] == 3
    assert sleeps == [1.0, 2.0], "退避应该是 1s → 2s"


def test_seedream_gives_up_after_retries(monkeypatch):
    from contentops import image_service as svc

    class Resp:
        status_code = 500
        text = "boom"

        def json(self):
            return {}

    monkeypatch.setitem(sys.modules, "requests", types.SimpleNamespace(post=lambda *a, **kw: Resp()))
    monkeypatch.setattr(svc, "_sleep", lambda s: None)
    monkeypatch.setenv("IMAGE_RETRIES", "1")

    r = svc.SeedreamImageService("sk-test").generate("图点")
    assert not r.ok and "重试" in r.error


# --------------------------------------------------------------------------- #
# 历史 / 时间旅行
# --------------------------------------------------------------------------- #


def test_history_and_rewind(work, monkeypatch):
    from contentops.graph import history, new_session, resume, rewind, start

    app, tid = new_session()
    start(app, "AI 编程入门", tid)
    resume(app, {"selected_topic": "2"}, tid)

    rows = history(app, tid)
    assert len(rows) >= 3
    # 找一个「已经写出第 1 版稿子」的检查点
    target = next(r for r in rows if r["has_draft"] and r["revision"] == 1)

    state = rewind(app, tid, target["checkpoint_id"])
    assert state.get("selected_topic")
    assert state.get("article_content")


def test_api_history_and_rewind(work, monkeypatch):
    monkeypatch.setenv("CONTENTOPS_TRACE_DB", str(work / "t.sqlite"))
    monkeypatch.setenv("CONTENTOPS_WEB_DB", str(work / "w.sqlite"))
    monkeypatch.setenv("CONTENTOPS_OUT_DIR", str(work / "out"))
    from fastapi.testclient import TestClient

    from contentops import web

    client = TestClient(web.app)
    run_id = client.post("/api/runs", json={"topic": "AI 编程入门"}).json()["run_id"]
    detail = client.get(f"/api/runs/{run_id}").json()
    client.post(f"/api/runs/{run_id}/select",
                json={"selected_topic": detail["packet"]["options"][0]})

    hist = client.get(f"/api/runs/{run_id}/history").json()["history"]
    assert hist and all("checkpoint_id" in h for h in hist)

    target = next(h for h in hist if h["revision"] >= 1)
    r = client.post(f"/api/runs/{run_id}/rewind", json={"checkpoint_id": target["checkpoint_id"]})
    assert r.status_code == 200 and r.json()["ok"]


# --------------------------------------------------------------------------- #
# JWT 鉴权
# --------------------------------------------------------------------------- #


def test_jwt_roundtrip(monkeypatch):
    monkeypatch.setenv("CONTENTOPS_SECRET", "test-secret")
    from contentops import auth

    token = auth.create_token("alice")
    assert auth.decode_token(token)["sub"] == "alice"

    tampered = token[:-2] + ("AA" if token[-2:] != "AA" else "BB")
    with pytest.raises(ValueError):
        auth.decode_token(tampered)


def test_register_and_login():
    from contentops import auth

    ok, err = auth.register("bob", "secret123")
    assert ok, err
    assert auth.verify("bob", "secret123")
    assert not auth.verify("bob", "wrong")
    ok, err = auth.register("bob", "secret123")
    assert not ok and "已存在" in err


def test_api_requires_token(work, monkeypatch):
    monkeypatch.setenv("CONTENTOPS_TRACE_DB", str(work / "t.sqlite"))
    monkeypatch.setenv("CONTENTOPS_WEB_DB", str(work / "w.sqlite"))
    monkeypatch.setenv("CONTENTOPS_OUT_DIR", str(work / "out"))
    monkeypatch.setenv("CONTENTOPS_SECRET", "test-secret")
    monkeypatch.setenv("CONTENTOPS_AUTH", "on")
    from fastapi.testclient import TestClient

    from contentops import web

    client = TestClient(web.app)
    assert client.get("/api/runs").status_code == 401

    client.post("/api/auth/register", json={"username": "carol", "password": "secret123"})
    token = client.post("/api/auth/login", json={"username": "carol", "password": "secret123"}).json()["token"]
    ok = client.get("/api/runs", headers={"Authorization": f"Bearer {token}"})
    assert ok.status_code == 200


# --------------------------------------------------------------------------- #
# 双平台发布
# --------------------------------------------------------------------------- #


def test_write_publish_two_platforms(work):
    from contentops.output import write_publish

    state = {
        "selected_topic": "AI 编程入门",
        "article_content": "正文" * 200,
        "image_urls": ["/outputs/images/a.svg"],
        "published": {
            "xiaohongshu": {"title": "AI 编程入门", "body": "短版", "images": ["/outputs/images/a.svg"]},
            "wechat": {"title": "AI 编程入门", "body": "长版", "images": []},
            "published_at": "2026-11-19T21:00:00",
        },
    }
    files = write_publish(state, work / "out")
    names = sorted(p.name for p in files)
    assert names == ["AI-编程入门.md", "AI-编程入门.md"]
    assert {p.parent.name for p in files} == {"xiaohongshu", "wechat"}
    text = (work / "out" / "publish" / "wechat" / "AI-编程入门.md").read_text(encoding="utf-8")
    assert "长版" in text


# --------------------------------------------------------------------------- #
# token 级流式
# --------------------------------------------------------------------------- #


def test_stream_emits_llm_tokens(work, monkeypatch):
    monkeypatch.setenv("CONTENTOPS_TRACE_DB", str(work / "t.sqlite"))
    monkeypatch.setenv("CONTENTOPS_WEB_DB", str(work / "w.sqlite"))
    monkeypatch.setenv("CONTENTOPS_OUT_DIR", str(work / "out"))
    from fastapi.testclient import TestClient

    from contentops import web

    client = TestClient(web.app)
    run_id = client.post("/api/runs", json={"topic": "AI 编程入门"}).json()["run_id"]

    # 选题 → 写稿：这一段走 SSE，应当逐个吐 token
    resp = client.get(f"/stream/resume/{run_id}", params={"action": "select", "value": "1"})
    frames = [json.loads(l[6:]) for l in resp.text.splitlines() if l.startswith("data: ")]
    kinds = [f["type"] for f in frames]

    assert kinds[0] == "init" and kinds[-1] == "done"
    token_events = [f["data"] for f in frames if f["type"] == "llm_token"]
    assert token_events, "写稿必须推 llm_token"
    assert len("".join(token_events)) > 50
    assert "interrupt" in kinds  # 写完停在人工审核
