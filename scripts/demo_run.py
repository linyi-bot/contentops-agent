"""双击运行用的演示入口。

为什么不让 .cmd 直接写中文：cmd.exe 按系统代码页逐行读批处理文件，
UTF-8 的中文在 chcp 65001 之后仍然可能被按 GBK 切开（实测报 '化"' 不是内部命令）。
所以批处理只留 ASCII，所有中文交给 Python 打印。
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LINE = "=" * 62


def main() -> int:
    topic = sys.argv[1] if len(sys.argv) > 1 else "用 AI 做周报自动化"

    print(LINE, flush=True)
    print("  ContentOps Agent 演示", flush=True)
    print("  检索 -> 草稿 -> 事实核查 -> 人工审核 -> 发布", flush=True)
    print(LINE, flush=True)
    print(f"选题：{topic}", flush=True)
    print("（--auto approve 自动放行；想亲手点 y/n 就去掉它，见 README）\n", flush=True)

    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join([str(ROOT / "src"), str(ROOT / ".venv-libs")])
    env["PYTHONIOENCODING"] = "utf-8"

    rc = subprocess.run(
        [
            sys.executable, "-m", "contentops.cli", "run",
            "--topic", topic,
            "--auto", "approve",
            "--out", "outputs",
        ],
        cwd=str(ROOT),
        env=env,
    ).returncode

    print()
    print(f"[退出码 {rc}]   0=已发布   75=停在人工审核   其他=出错")
    print(f"发布稿在 {ROOT / 'outputs'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
