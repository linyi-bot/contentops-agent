"""出图服务：一个接口，三个通道。

课程里用火山 Seedream，后来换成 nano banana（gemini-3-pro-image），共同点是：
**同步 HTTP、一张一张调、不给异步**。所以节点侧必须用线程池并发（见 nodes.generate_images）。

三个通道按 `IMAGE_PROVIDER` 选：

| provider | 需要 | 说明 |
|---|---|---|
| `mock`（默认） | 无 | 本地画一张 SVG 占位图，3:4、带图点文字；不联网、不花钱、可进 CI |
| `seedream` | `ARK_API_KEY` | 火山方舟 `images/generations` |
| `gemini` | `IMAGE_API_KEY` + `IMAGE_BASE_URL` | OpenAI 兼容中转的 gemini 图像模型，返回 base64 |

设计原则：**出图失败不能把整条流水线带崩**。每张图单独返回 ok/error，
节点把失败的图点收进 `image_errors`，页面照样能发稿（只是少几张图）。
"""
from __future__ import annotations

import base64
import hashlib
import os
import textwrap
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol, TypeVar

#: 默认出图落盘目录（相对当前工作目录）；web 通过 /outputs 静态暴露
DEFAULT_OUT_DIR = Path("outputs") / "images"

#: 出图重试：422/429/5xx 和网络抖动都值得再试一次（同步 API，不给你回调）。
RETRY_STATUS = {408, 425, 429, 500, 502, 503, 504}

_sleep = time.sleep  # 测试里替换掉，别真的等 1+2+4 秒

T = TypeVar("T")


def _retry(fn: Callable[[], T], *, attempts: int = 3, base: float = 1.0) -> T:
    """指数退避：1s → 2s → 4s。最后一次失败把异常抛出去，由调用方记成 error。"""
    last: Exception | None = None
    for i in range(max(1, attempts)):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001
            last = exc
            if i < attempts - 1:
                _sleep(base * (2**i))
    assert last is not None
    raise last


@dataclass
class ImageResult:
    """一张图的结果。`url` 是给页面用的，`path` 是给命令行看的。"""

    prompt: str
    ok: bool
    provider: str
    url: str = ""
    path: str = ""
    error: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "prompt": self.prompt,
            "ok": self.ok,
            "provider": self.provider,
            "url": self.url,
            "path": self.path,
            "error": self.error,
        }


class ImageService(Protocol):
    name: str

    def generate(self, prompt: str, *, index: int = 1) -> ImageResult: ...


# --------------------------------------------------------------------------- #
# mock：本地画一张 3:4 的 SVG 占位图
# --------------------------------------------------------------------------- #

_SVG = """<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="0 0 {w} {h}">
  <defs>
    <linearGradient id="g" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0%" stop-color="{c1}"/>
      <stop offset="100%" stop-color="{c2}"/>
    </linearGradient>
  </defs>
  <rect width="{w}" height="{h}" fill="url(#g)"/>
  <text x="48" y="96" font-family="Microsoft YaHei, PingFang SC, sans-serif" font-size="40"
        fill="#ffffff" opacity="0.85">图 {index} · mock</text>
  {lines}
  <text x="48" y="{h_minus}" font-family="Microsoft YaHei, PingFang SC, sans-serif"
        font-size="26" fill="#ffffff" opacity="0.55">ContentOps · 占位图（未接真模型）</text>
</svg>"""

_PALETTE = [
    ("#1f6feb", "#0b2a5b"),
    ("#8f2a26", "#3b100e"),
    ("#1f7a4d", "#0a2f1d"),
    ("#6b3fa0", "#25123b"),
    ("#b8860b", "#3d2c04"),
]


class MockImageService:
    """不联网的占位图。写 SVG 而不是 PNG：不需要 Pillow，体积小，页面能直接显示。"""

    name = "mock"

    def __init__(self, out_dir: str | Path | None = None) -> None:
        self.out_dir = Path(out_dir or os.getenv("IMAGE_OUT_DIR") or DEFAULT_OUT_DIR)

    def generate(self, prompt: str, *, index: int = 1) -> ImageResult:
        try:
            self.out_dir.mkdir(parents=True, exist_ok=True)
            digest = hashlib.sha1(prompt.encode("utf-8")).hexdigest()[:8]
            path = self.out_dir / f"img-{index:02d}-{digest}.svg"
            c1, c2 = _PALETTE[(index - 1) % len(_PALETTE)]
            lines = []
            for i, line in enumerate(textwrap.wrap(prompt, 16)[:6]):
                y = 200 + i * 62
                lines.append(
                    f'<text x="48" y="{y}" font-family="Microsoft YaHei, PingFang SC, sans-serif" '
                    f'font-size="44" fill="#ffffff">{_esc(line)}</text>'
                )
            path.write_text(
                _SVG.format(
                    w=900, h=1200, c1=c1, c2=c2, index=index,
                    lines="\n  ".join(lines), h_minus=1130,
                ),
                encoding="utf-8",
            )
            return ImageResult(
                prompt=prompt, ok=True, provider=self.name,
                url=f"/outputs/images/{path.name}", path=str(path),
            )
        except Exception as exc:  # noqa: BLE001
            return ImageResult(prompt=prompt, ok=False, provider=self.name,
                               error=f"{type(exc).__name__}: {exc}")


def _esc(text: str) -> str:
    return (
        text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        .replace('"', "&quot;")
    )


# --------------------------------------------------------------------------- #
# 火山方舟 Seedream
# --------------------------------------------------------------------------- #


class SeedreamImageService:
    """火山方舟的图片生成。同步 HTTP，不支持异步回调 —— 所以并发放在节点里做。"""

    name = "seedream"

    def __init__(self, api_key: str, *, model: str | None = None, size: str = "1024x1536") -> None:
        self.api_key = api_key
        self.model = model or os.getenv("IMAGE_MODEL", "doubao-seedream-5-0-flash-260915")
        self.size = size
        self.base_url = os.getenv("IMAGE_BASE_URL", "https://ark.cn-beijing.volces.com/api/v3")

    def generate(self, prompt: str, *, index: int = 1) -> ImageResult:
        try:
            import requests  # 延迟导入：mock 模式下不需要它
        except ModuleNotFoundError:
            return ImageResult(prompt=prompt, ok=False, provider=self.name,
                               error="缺 requests：pip install requests")

        attempts = int(os.getenv("IMAGE_RETRIES", "2")) + 1

        def call():
            resp = requests.post(
                f"{self.base_url.rstrip('/')}/images/generations",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json={
                    "model": self.model,
                    "prompt": prompt,
                    "size": self.size,
                    "response_format": "url",
                },
                timeout=120,
            )
            if resp.status_code in RETRY_STATUS:
                raise RuntimeError(f"可重试的 HTTP {resp.status_code}")
            return resp

        try:
            resp = _retry(call, attempts=attempts)
            if resp.status_code >= 400:
                return ImageResult(prompt=prompt, ok=False, provider=self.name,
                                   error=f"HTTP {resp.status_code}: {resp.text[:200]}")
            data = resp.json()
            url = (data.get("data") or [{}])[0].get("url", "")
            if not url:
                return ImageResult(prompt=prompt, ok=False, provider=self.name,
                                   error=f"响应里没有 url：{str(data)[:200]}")
            # 方舟返回的是带签名的临时 URL（默认 24h 过期），顺手存一份本地：
            # 页面就不用依赖「明天这条链接还活着」。
            local = self._download(url, prompt, index)
            return ImageResult(prompt=prompt, ok=True, provider=self.name,
                               url=local or url, path=local)
        except Exception as exc:  # noqa: BLE001
            return ImageResult(prompt=prompt, ok=False, provider=self.name,
                               error=f"重试 {attempts} 次仍失败：{type(exc).__name__}: {exc}")

    def _download(self, url: str, prompt: str, index: int) -> str:
        """把远程图落到 outputs/images，返回 web 路径；失败就返回空串（继续用远程 URL）。"""
        try:
            import requests

            out = Path(os.getenv("IMAGE_OUT_DIR") or DEFAULT_OUT_DIR)
            out.mkdir(parents=True, exist_ok=True)
            digest = hashlib.sha1(prompt.encode("utf-8")).hexdigest()[:8]
            path = out / f"img-{index:02d}-{digest}.jpeg"
            resp = requests.get(url, timeout=60)
            if resp.status_code != 200:
                return ""
            path.write_bytes(resp.content)
            return f"/outputs/images/{path.name}"
        except Exception:  # noqa: BLE001 - 存不下来不算失败，远程 URL 也能显示
            return ""


# --------------------------------------------------------------------------- #
# gemini-3-pro-image（走 OpenAI 兼容中转，返回 base64）
# --------------------------------------------------------------------------- #


class GeminiImageService:
    """nano banana 那类：`candidates[0].content.parts[].inlineData.data` 是 base64。"""

    name = "gemini"

    def __init__(self, api_key: str, *, model: str | None = None,
                 base_url: str | None = None, out_dir: str | Path | None = None) -> None:
        self.api_key = api_key
        self.model = model or os.getenv("IMAGE_MODEL", "gemini-3-pro-image-preview")
        self.base_url = (base_url or os.getenv("IMAGE_BASE_URL") or "").rstrip("/")
        self.out_dir = Path(out_dir or os.getenv("IMAGE_OUT_DIR") or DEFAULT_OUT_DIR)

    def generate(self, prompt: str, *, index: int = 1) -> ImageResult:
        try:
            import requests
        except ModuleNotFoundError:
            return ImageResult(prompt=prompt, ok=False, provider=self.name,
                               error="缺 requests：pip install requests")
        if not self.base_url:
            return ImageResult(prompt=prompt, ok=False, provider=self.name,
                               error="缺 IMAGE_BASE_URL（中转站地址）")

        attempts = int(os.getenv("IMAGE_RETRIES", "2")) + 1

        def call():
            resp = requests.post(
                f"{self.base_url}/v1beta/models/{self.model}:generateContent",
                headers={"Authorization": f"Bearer {self.api_key}",
                         "Content-Type": "application/json"},
                json={
                    "contents": [{"parts": [{"text": prompt}]}],
                    "generationConfig": {"responseModalities": ["IMAGE", "TEXT"]},
                },
                timeout=180,
            )
            if resp.status_code in RETRY_STATUS:
                raise RuntimeError(f"可重试的 HTTP {resp.status_code}")
            return resp

        try:
            resp = _retry(call, attempts=attempts)
            if resp.status_code >= 400:
                return ImageResult(prompt=prompt, ok=False, provider=self.name,
                                   error=f"HTTP {resp.status_code}: {resp.text[:200]}")
            data = resp.json()
            parts = ((data.get("candidates") or [{}])[0].get("content") or {}).get("parts") or []
            for part in parts:
                blob = (part.get("inlineData") or {}).get("data")
                if not blob:
                    continue
                self.out_dir.mkdir(parents=True, exist_ok=True)
                digest = hashlib.sha1(prompt.encode("utf-8")).hexdigest()[:8]
                path = self.out_dir / f"img-{index:02d}-{digest}.png"
                path.write_bytes(base64.b64decode(blob))
                return ImageResult(prompt=prompt, ok=True, provider=self.name,
                                   url=f"/outputs/images/{path.name}", path=str(path))
            return ImageResult(prompt=prompt, ok=False, provider=self.name,
                               error="响应里没有 inlineData（可能被安全策略拦了）")
        except Exception as exc:  # noqa: BLE001
            return ImageResult(prompt=prompt, ok=False, provider=self.name,
                               error=f"{type(exc).__name__}: {exc}")


# --------------------------------------------------------------------------- #
# OpenAI 兼容的中转（硅基流动 / 302.ai / 各种 base64 或 url 返回）
# --------------------------------------------------------------------------- #


class OpenAICompatImageService:
    """`POST {base}/images/generations`，和 OpenAI 一个形状。

    国内大部分中转（硅基流动、302.ai、各类加速站）都是这个协议，
    所以这一个类能覆盖「除了火山和 gemini 之外」的绝大多数通道。
    """

    name = "openai"

    def __init__(self, api_key: str, base_url: str, *, model: str | None = None,
                 out_dir: str | Path | None = None) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model or os.getenv("IMAGE_MODEL", "Kwai-Kolors/Kolors")
        self.out_dir = Path(out_dir or os.getenv("IMAGE_OUT_DIR") or DEFAULT_OUT_DIR)

    def generate(self, prompt: str, *, index: int = 1) -> ImageResult:
        try:
            import requests
        except ModuleNotFoundError:
            return ImageResult(prompt=prompt, ok=False, provider=self.name,
                               error="缺 requests：pip install requests")

        attempts = int(os.getenv("IMAGE_RETRIES", "2")) + 1

        def call():
            resp = requests.post(
                f"{self.base_url}/images/generations",
                headers={"Authorization": f"Bearer {self.api_key}",
                         "Content-Type": "application/json"},
                json={"model": self.model, "prompt": prompt, "n": 1,
                      "size": os.getenv("IMAGE_SIZE", "1024x1536")},
                timeout=180,
            )
            if resp.status_code in RETRY_STATUS:
                raise RuntimeError(f"可重试的 HTTP {resp.status_code}")
            return resp

        try:
            resp = _retry(call, attempts=attempts)
            if resp.status_code >= 400:
                return ImageResult(prompt=prompt, ok=False, provider=self.name,
                                   error=f"HTTP {resp.status_code}: {resp.text[:200]}")
            item = (resp.json().get("data") or [{}])[0]
            if item.get("url"):
                return ImageResult(prompt=prompt, ok=True, provider=self.name, url=item["url"])
            if item.get("b64_json"):
                self.out_dir.mkdir(parents=True, exist_ok=True)
                digest = hashlib.sha1(prompt.encode("utf-8")).hexdigest()[:8]
                path = self.out_dir / f"img-{index:02d}-{digest}.png"
                path.write_bytes(base64.b64decode(item["b64_json"]))
                return ImageResult(prompt=prompt, ok=True, provider=self.name,
                                   url=f"/outputs/images/{path.name}", path=str(path))
            return ImageResult(prompt=prompt, ok=False, provider=self.name,
                               error=f"响应里既没有 url 也没有 b64_json：{str(resp.json())[:200]}")
        except Exception as exc:  # noqa: BLE001
            return ImageResult(prompt=prompt, ok=False, provider=self.name,
                               error=f"重试 {attempts} 次仍失败：{type(exc).__name__}: {exc}")


# --------------------------------------------------------------------------- #
# 工厂
# --------------------------------------------------------------------------- #


def build_image_service() -> ImageService:
    """按环境变量挑通道；**没有 key 就回落 mock**，和 llm.build_llm 一个脾气。"""
    from .llm import load_dotenv  # 延迟导入：避免 image_service ↔ llm 循环

    load_dotenv()  # 让「.env 里填 key」真的生效（和模型那边一致）
    provider = os.getenv("IMAGE_PROVIDER", "").strip().lower()
    if not provider:
        # 没显式指定：有火山 key 用火山，有中转 key 用 gemini，都没有就 mock
        if os.getenv("ARK_API_KEY"):
            provider = "seedream"
        elif os.getenv("IMAGE_API_KEY"):
            provider = "gemini"
        else:
            provider = "mock"

    if provider == "seedream":
        key = os.getenv("ARK_API_KEY", "").strip()
        if key:
            return SeedreamImageService(key)
        return MockImageService()
    if provider == "gemini":
        key = os.getenv("IMAGE_API_KEY", "").strip()
        if key:
            return GeminiImageService(key)
        return MockImageService()
    if provider in {"openai", "compat", "relay"}:
        key = os.getenv("IMAGE_API_KEY", "").strip()
        base = os.getenv("IMAGE_BASE_URL", "").strip()
        if key and base:
            return OpenAICompatImageService(key, base)
        return MockImageService()
    return MockImageService()


def describe_images() -> dict[str, Any]:
    """命令行/页面显示用：现在用哪个通道、为什么。"""
    svc = build_image_service()
    provider = os.getenv("IMAGE_PROVIDER", "").strip().lower() or "auto"
    return {"name": svc.name, "provider": provider}
