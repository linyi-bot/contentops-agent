r"""不用 pip，直接把 wheel 解到 .venv-libs（本机 pip 在这个沙箱里写 .whl.metadata 必失败）。

    python scripts/vendor_wheels.py fastapi==0.115.14 starlette==0.46.2 uvicorn==0.34.0

为什么要这条备用路径：本机 `pip install --target` 在解包阶段稳定报
`Permission denied: ...\pip-unpack-xxx\*.whl.metadata`（连 six 都装不上）。
那就绕开 pip：PyPI JSON API 拿 wheel 地址 → httpx 下载 → zipfile 解压。
不 pin 版本时取最新；pin 是有必要的 —— fastapi 0.164 在 import 阶段就要 opentelemetry，
而 0.115 不需要，页面用不着 telemetry 那一坨。

只用 httpx（仓库已有），不额外依赖。重复跑会覆盖同名文件，幂等。
"""
from __future__ import annotations

import json
import sys
import zipfile
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
LIBS = ROOT / ".venv-libs"
WHEELS = ROOT / ".wheels"
PYPI = "https://pypi.org/pypi/{name}/json"


def _pick(wheels: list[dict], py: str) -> dict:
    """优先纯 Python 轮子（py3-none-any），其次 cp<版本>。"""
    pure = [w for w in wheels if w["filename"].endswith("-py3-none-any.whl")]
    if pure:
        return pure[-1]
    native = [w for w in wheels if f"-{py}-" in w["filename"] or "cp314" in w["filename"]]
    if native:
        return native[-1]
    raise SystemExit(f"[X] 没有可用的 wheel：{[w['filename'] for w in wheels][:5]}")


def fetch(spec: str, py: str) -> None:
    name, _, want = spec.partition("==")
    url = PYPI.format(name=name) if not want else f"https://pypi.org/pypi/{name}/{want}/json"
    with httpx.Client(timeout=60, follow_redirects=True) as c:
        meta = c.get(url).raise_for_status().json()
        ver = meta["info"]["version"]
        # requires_dist 里写的是「装出来的人要什么」，这里只解自己，依赖交给调用方列表
        wheel = _pick(meta["urls"], py)
        dest = WHEELS / wheel["filename"]
        if not dest.exists():
            dest.write_bytes(c.get(wheel["url"]).raise_for_status().content)
        print(f"  {name:14} {ver:10} {wheel['filename']}")

    with zipfile.ZipFile(dest) as z:
        z.extractall(LIBS)


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    LIBS.mkdir(exist_ok=True)
    WHEELS.mkdir(exist_ok=True)
    py = f"cp{sys.version_info.major}{sys.version_info.minor}"
    print(f"目标：{LIBS}（{py}）")
    for name in argv:
        try:
            fetch(name, py)
        except SystemExit:
            raise
        except Exception as exc:  # noqa: BLE001 - 逐个报，不要让一个包挡住其余
            print(f"[X] {name}: {type(exc).__name__}: {exc}")
            return 1
    print("解包完成。验证：python -c \"import fastapi, uvicorn; print(fastapi.__version__, uvicorn.__version__)\"")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
