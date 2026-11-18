"""模型层的测试：不联网、不烧 token，但每条都是真实会踩的坑。

刻意测的不是「模型答得好不好」（那是评测集的事），而是四件会悄悄坏掉的事：
1. 请求体里发的是什么 —— 模型名、思考模式开关，发错一个就是 400 或者白烧钱；
2. 重试策略 —— 429 该重试、401 不该重试（重试只是把同一个错误再犯一遍）；
3. 记账口径 —— 缓存命中和未命中单价差几十倍，混在一起算成本会错一个量级；
4. 空输出 —— 静默返回空串，下游会把空稿当草稿发出去。
"""
from __future__ import annotations

import os
import shutil
import uuid
from datetime import datetime
from pathlib import Path

import httpx
import pytest

from contentops import llm as L


class FakeResp:
    def __init__(self, payload=None, status: int = 200, text: str = "", headers=None):
        self._payload = payload or {}
        self.status_code = status
        self.text = text
        self.headers = headers or {}

    def json(self):
        return self._payload


def _ok(content: str = "据《内容运营基础》，周报自动化先从模板复用开始[1]。", usage=None):
    return FakeResp(
        {
            "choices": [{"message": {"content": content}}],
            "usage": usage
            or {
                "prompt_tokens": 100,
                "prompt_cache_hit_tokens": 60,
                "prompt_cache_miss_tokens": 40,
                "completion_tokens": 30,
            },
        }
    )


@pytest.fixture(autouse=True)
def _clean_usage():
    L.reset_usage()
    yield
    L.reset_usage()


@pytest.fixture()
def client():
    return L.OpenAICompatLLM(api_key="sk-test", model=L.DEFAULT_MODEL, retries=2)


def test_default_model_is_the_current_enum_member():
    """`deepseek-chat` 已经不在官方枚举里，默认值写成它会在真机上 400。"""
    assert L.DEFAULT_MODEL in {"deepseek-flash", "deepseek-v4-pro"}


def test_payload_disables_thinking_by_default(client):
    body = client.payload("sys", "user", 200)
    assert body["model"] == "deepseek-flash"
    assert body["thinking"] == {"type": "disabled"}
    assert body["temperature"] == 0.3
    assert "reasoning_effort" not in body


def test_payload_enables_thinking_when_asked():
    llm = L.OpenAICompatLLM(api_key="sk-test", thinking=True, reasoning_effort="low")
    body = llm.payload("sys", "user", 200)
    assert body["thinking"] == {"type": "enabled"}
    assert body["reasoning_effort"] == "low"
    # 思考模式下 temperature 不生效，传了只是噪音
    assert "temperature" not in body


def test_usage_and_cost_are_tracked(monkeypatch, client):
    monkeypatch.setenv("LLM_PRICE_IN_HIT", "1")
    monkeypatch.setenv("LLM_PRICE_IN_MISS", "2")
    monkeypatch.setenv("LLM_PRICE_OUT", "4")
    monkeypatch.setattr(L, "_post", lambda *a, **kw: _ok())

    client.complete("sys", "user")

    u = L.usage_snapshot()
    assert u.calls == 1
    assert (u.cache_hit_tokens, u.cache_miss_tokens, u.completion_tokens) == (60, 40, 30)
    assert u.prompt_tokens == 100, "prompt_tokens 应该等于命中 + 未命中"
    # (60*1 + 40*2 + 30*4) / 1e6 = 0.00026
    assert u.cost_cny == pytest.approx(0.00026)


def test_retries_on_429_then_succeeds(monkeypatch, client):
    calls = {"n": 0}
    slept: list[float] = []

    def fake_post(*a, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            return FakeResp(status=429, text="rate limited", headers={"Retry-After": "0"})
        return _ok()

    monkeypatch.setattr(L, "_post", fake_post)
    monkeypatch.setattr(L, "_sleep", lambda s: slept.append(s))

    assert client.complete("sys", "user")
    assert calls["n"] == 2
    assert slept == [0.0], "429 且给了 Retry-After 时应按它等，而不是自己拍一个退避"


def test_401_is_not_retried(monkeypatch, client):
    calls = {"n": 0}

    def fake_post(*a, **kw):
        calls["n"] += 1
        return FakeResp(status=401, text="invalid api key")

    monkeypatch.setattr(L, "_post", fake_post)
    monkeypatch.setattr(L, "_sleep", lambda s: None)

    with pytest.raises(L.LLMError, match="认证失败"):
        client.complete("sys", "user")
    assert calls["n"] == 1, "401 重试没有意义，只会把同一个错误再犯一遍"


def test_transport_errors_exhaust_retries_then_raise(monkeypatch):
    calls = {"n": 0}

    def fake_post(*a, **kw):
        calls["n"] += 1
        raise httpx.ConnectTimeout("连接超时")

    monkeypatch.setattr(L, "_post", fake_post)
    monkeypatch.setattr(L, "_sleep", lambda s: None)

    llm = L.OpenAICompatLLM(api_key="sk-test", retries=2)
    with pytest.raises(L.LLMError, match="连不上模型服务"):
        llm.complete("sys", "user")
    assert calls["n"] == 3, "重试 2 次 = 一共打 3 次"
    assert L.usage_snapshot().errors == 1


def test_empty_content_raises(monkeypatch, client):
    monkeypatch.setattr(L, "_post", lambda *a, **kw: _ok(content="   "))

    with pytest.raises(L.LLMError, match="空内容"):
        client.complete("sys", "user")


def test_peak_hours_are_beijing_weekday_windows():
    def at(y, m, d, hh, mm=0):
        return datetime(y, m, d, hh, mm)

    # 2026-11-18 是周三
    assert L.is_peak(at(2026, 11, 18, 9, 0)) is True
    assert L.is_peak(at(2026, 11, 18, 11, 59)) is True
    assert L.is_peak(at(2026, 11, 18, 12, 0)) is False
    assert L.is_peak(at(2026, 11, 18, 14, 0)) is True
    assert L.is_peak(at(2026, 11, 18, 18, 0)) is False
    assert L.is_peak(at(2026, 11, 18, 2, 0)) is False
    # 2026-11-21 是周六，全天半价
    assert L.is_peak(at(2026, 11, 21, 10, 0)) is False


def test_price_switches_between_peak_and_offpeak(monkeypatch):
    monkeypatch.delenv("LLM_PRICE_IN_HIT", raising=False)
    monkeypatch.delenv("LLM_PRICE_IN_MISS", raising=False)
    monkeypatch.delenv("LLM_PRICE_OUT", raising=False)

    monkeypatch.setattr(L, "is_peak", lambda *a, **kw: False)
    off = L.price_for("deepseek-flash")
    monkeypatch.setattr(L, "is_peak", lambda *a, **kw: True)
    peak = L.price_for("deepseek-flash")

    assert off["miss"] == 1.0 and peak["miss"] == 2.0
    assert peak["out"] == 2 * off["out"], "高峰输出价应是空闲的两倍"


def test_unknown_model_falls_back_to_default_price_table(monkeypatch):
    monkeypatch.setattr(L, "is_peak", lambda *a, **kw: False)
    assert L.price_for("deepseek-chat") == L.price_for(L.DEFAULT_MODEL)


def test_parse_dotenv_handles_comments_quotes_and_export():
    text = """
# 注释行
LLM_PROVIDER=deepseek
export LLM_MODEL="deepseek-flash"
DEEPSEEK_API_KEY=sk-abc  # 行尾注释
这行没有等号
"""
    parsed = L.parse_dotenv(text)
    assert parsed == {
        "LLM_PROVIDER": "deepseek",
        "LLM_MODEL": "deepseek-flash",
        "DEEPSEEK_API_KEY": "sk-abc",
    }


@pytest.fixture()
def workdir():
    """不用 pytest 自带的 tmp_path：这台机器的临时目录 ACL 会让沙箱里的 pytest 报拒绝访问。"""
    d = Path(__file__).resolve().parent / "_tmp" / uuid.uuid4().hex[:8]
    d.mkdir(parents=True)
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_parse_dotenv_survives_bom_from_windows_editors():
    """记事本 / PowerShell 写 .env 会带 BOM，首行 key 不能变成 "\\ufeffLLM_PROVIDER"。"""
    parsed = L.parse_dotenv("\ufeffLLM_PROVIDER=deepseek\nDEEPSEEK_API_KEY=sk-x\n")
    assert parsed["LLM_PROVIDER"] == "deepseek"
    assert parsed["DEEPSEEK_API_KEY"] == "sk-x"


def test_load_dotenv_never_overrides_real_env(monkeypatch, workdir):
    """CI 用环境变量注入、docker 用 compose 注入，.env 只该补空，不该盖掉它们。"""
    monkeypatch.delenv("CONTENTOPS_NO_DOTENV", raising=False)
    monkeypatch.setenv("LLM_PROVIDER", "deepseek")
    monkeypatch.delenv("LLM_MODEL", raising=False)

    env_file = workdir / ".env"
    env_file.write_text("LLM_PROVIDER=mock\nLLM_MODEL=deepseek-v4-pro\n", encoding="utf-8")

    added = L.load_dotenv(env_file, force=True)
    assert os.environ["LLM_PROVIDER"] == "deepseek", "已存在的环境变量优先，不能被 .env 盖掉"
    assert os.environ["LLM_MODEL"] == "deepseek-v4-pro"
    assert added == 1

    assert L.load_dotenv(env_file, force=True) == 0, "第二次读同一个文件不该再补任何东西"
