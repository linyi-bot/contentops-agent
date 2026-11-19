"""一键演示：起服务 + 打开浏览器页面。

为什么要这个脚本：演示当天最怕两件事 ——
1. 忘了起服务，浏览器白屏；
2. 起了服务但数据是空的，页面上一片"还没有运行记录"。

所以它做三件事：能找到 `.demo/` 演示数据就用它、端口没起就在本进程里起、
起好之后自动打开 `/app/`。关掉这个窗口就等于停服务。

    python scripts/demo_serve.py            # 默认 8124
    python scripts/demo_serve.py --no-browser
"""
from __future__ import annotations

import argparse
import os
import socket
import sys
import threading
import time
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def port_open(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket() as s:
        s.settimeout(0.4)
        return s.connect_ex((host, port)) == 0


def use_demo_data() -> bool:
    """有 .demo/ 就用它（三条预置运行：选题中 / 审稿中 / 已完成），没有就空库。"""
    demo = ROOT / ".demo"
    if not demo.exists():
        return False
    os.environ.setdefault("CONTENTOPS_WEB_DB", str(demo / "web.sqlite"))
    os.environ.setdefault("CONTENTOPS_TRACE_DB", str(demo / "traces.sqlite"))
    os.environ.setdefault("CONTENTOPS_OUT_DIR", str(demo / "out"))
    os.environ.setdefault("IMAGE_OUT_DIR", str(demo / "out" / "images"))
    os.environ.setdefault("CONTENTOPS_USERS_DB", str(demo / "users.sqlite"))
    return True


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="ContentOps 演示入口")
    p.add_argument("--port", type=int, default=8124)
    p.add_argument("--no-browser", action="store_true")
    a = p.parse_args(argv)

    os.chdir(ROOT)
    with_demo = use_demo_data()
    url = f"http://127.0.0.1:{a.port}/app/"

    if port_open(a.port):
        print(f"[i] {a.port} 端口已经有服务了，直接打开页面")
    else:
        import uvicorn

        from contentops import web

        threading.Thread(
            target=lambda: uvicorn.run(web.app, host="127.0.0.1", port=a.port, log_level="warning"),
            daemon=True,
        ).start()
        for _ in range(60):
            if port_open(a.port):
                break
            time.sleep(0.2)
        else:
            print("[X] 服务没起来，检查依赖：pip install -e \".[test]\"")
            return 2

    print("=" * 60)
    print(f"  演示页面：{url}")
    print(f"  数据：{'三条预置运行（选题中 / 审稿中 / 已完成）' if with_demo else '空库（先跑一条）'}")
    print("  关闭这个窗口 = 停止服务")
    print("=" * 60)

    if not a.no_browser:
        webbrowser.open(url)

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n已停止")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
