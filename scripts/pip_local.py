r"""给某个解释器跑 pip 时带上「0o700 临时目录」补丁。

    :: 用目标解释器跑（推荐，补丁一定跟在同一个进程里）
    .venv\Scripts\python.exe scripts\pip_local.py install -e .

    :: 已经用 venv_bootstrap.py 备好 venv 的话不需要它 —— 那份 sitecustomize.py 已经全局生效

为什么不用 `pip --python .venv\Scripts\python.exe`：pip 会为了 `--python` 重起子进程，
命令行里打的 `os.mkdir` 补丁带不过去（setuptools 的构建后端更是又套一层子进程），
表现就是装到一半报 `PermissionError: ...\*.whl.metadata` 或
`DistutilsFileError: could not create '...\.tmp-xxx\*.egg-info'`。

补丁本身只有一份，在 `scripts/venv_bootstrap.py` 的 `patch_local()` 里。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from venv_bootstrap import patch_local  # noqa: E402


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    patch_local()
    try:
        from pip._internal.cli.main import main as pip_main
    except ImportError:
        print(f"[X] {sys.executable} 里没有 pip。先跑 scripts\\venv_bootstrap.py（见 README 第九节）。")
        return 1
    return int(pip_main(argv) or 0)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
