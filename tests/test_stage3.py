"""阶段 3 · 出图服务的测试。

出图是唯一「花了钱还可能失败」的环节，所以它必须：
1. 没有 key 时回落 mock（不能因为缺 key 就崩）；
2. mock 真的产出一个能放进页面的文件（SVG，3:4）；
3. 真通道在缺参数 / HTTP 失败时返回 `ok=False` 而不是抛异常 —— 一张图失败不该带崩整条流水线。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from contentops.image_service import (
    GeminiImageService,
    MockImageService,
    SeedreamImageService,
    build_image_service,
    describe_images,
)


def test_mock_writes_a_usable_svg(work):
    svc = MockImageService(work / "images")
    r = svc.generate("痛点：信息散落，收集最耗时", index=1)

    assert r.ok and r.provider == "mock"
    assert r.url.startswith("/outputs/images/") and r.url.endswith(".svg")
    path = Path(r.path)
    assert path.exists() and path.stat().st_size > 200
    text = path.read_text(encoding="utf-8")
    assert "<svg" in text and "图 1" in text
    # 3:4 是小红书的默认比例
    assert 'width="900"' in text and 'height="1200"' in text


def test_mock_escapes_xml(work):
    svc = MockImageService(work / "images")
    r = svc.generate("a < b & c > d", index=2)
    assert r.ok
    assert "&lt;" in Path(r.path).read_text(encoding="utf-8")


def test_no_key_falls_back_to_mock(monkeypatch):
    monkeypatch.delenv("IMAGE_PROVIDER", raising=False)
    monkeypatch.delenv("ARK_API_KEY", raising=False)
    monkeypatch.delenv("IMAGE_API_KEY", raising=False)
    assert build_image_service().name == "mock"
    assert describe_images()["name"] == "mock"


def test_seedream_without_key_is_mock(monkeypatch):
    monkeypatch.setenv("IMAGE_PROVIDER", "seedream")
    monkeypatch.delenv("ARK_API_KEY", raising=False)
    assert build_image_service().name == "mock"


def test_gemini_without_base_url_reports_error(monkeypatch):
    monkeypatch.delenv("IMAGE_BASE_URL", raising=False)
    svc = GeminiImageService("sk-test", base_url="")
    r = svc.generate("随便一张图")
    assert not r.ok
    assert "IMAGE_BASE_URL" in r.error


def test_seedream_http_failure_is_result_not_exception(monkeypatch):
    """HTTP 400 要变成 ImageResult(ok=False)，不是 raise。"""
    import types

    class FakeResp:
        status_code = 400
        text = "bad request"

    def fake_post(*a, **kw):
        return FakeResp()

    fake_requests = types.SimpleNamespace(post=fake_post)
    monkeypatch.setitem(__import__("sys").modules, "requests", fake_requests)

    svc = SeedreamImageService("sk-test")
    r = svc.generate("随便一张图")
    assert not r.ok and "HTTP 400" in r.error
