r"""准备一个「在这台机器上真能装包」的 venv。

    python scripts\venv_bootstrap.py .venv        # 建 venv（已存在就复用）+ 补 sitecustomize + 装 pip
    python scripts\venv_bootstrap.py --check .venv

为什么需要这一步（本机实测，Python 3.14 + DSH 受限令牌）：

    1. Python 3.14 把 `os.mkdir(mode=0o700)` 落到目录 DACL 上，而 `ensurepip` /
       `setuptools` / pip 的临时目录全是 0o700 —— 沙箱令牌随后写不进自己刚建的目录。
       表现：`python -m venv .venv` 报 ensurepip exit 1；`pip install` 报
       `PermissionError: ...\*.whl.metadata`；editable 构建报
       `DistutilsFileError: could not create '...\.tmp-xxxx\contentops_agent.egg-info'`。

    2. 光在命令行里 patch `os.mkdir` 不够 —— pip 会把构建后端放进**子进程**跑
       （pyproject_hooks），补丁带不过去。所以补丁必须放在 venv 的 `sitecustomize.py`：
       这个文件由 `site` 模块在每个解释器启动时自动导入，子进程也逃不掉。

这个脚本做三件事：建 venv（可选）→ 往它的 site-packages 写一份 sitecustomize → 用补丁引导 pip。
它不改系统、不改全局 sitecustomize，只影响这一个 venv；删掉 venv 就全没了。
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

#: 写进 venv/Lib/site-packages/sitecustomize.py 的内容。
#: 每条都包在 try/except 里：宁可什么都不做，也不能让解释器启动失败。
SITECUSTOMIZE = '''"""本 venv 专用的启动补丁（由 scripts/venv_bootstrap.py 生成，可随时删除）。

Windows + 受限令牌下，0o700 的目录建出来就连自己都写不进去（Python 3.14 会把 mode
映射成目录 DACL）。pip / setuptools / ensurepip 的临时目录默认就是 0o700，于是装包
必失败。这里把 0o700 放宽到 0o777 —— 只影响本 venv 启动的进程，不改系统。
"""
import os as _os
import tempfile as _tempfile

try:
    _orig_mkdir = _os.mkdir
    _orig_makedirs = _os.makedirs

    def _mkdir(path, mode=0o777, *args, **kwargs):
        if mode == 0o700:
            mode = 0o777
        return _orig_mkdir(path, mode, *args, **kwargs)

    def _makedirs(name, mode=0o777, *args, **kwargs):
        if mode == 0o700:
            mode = 0o777
        return _orig_makedirs(name, mode, *args, **kwargs)

    def _mkdtemp(suffix=None, prefix=None, dir=None):
        base = dir if dir is not None else _tempfile.gettempdir()
        names = _tempfile._get_candidate_names()
        for name in names:
            path = _os.path.join(base, (prefix or "tmp") + name + (suffix or ""))
            try:
                _orig_mkdir(path, 0o777)
                return path
            except FileExistsError:
                continue
        raise FileExistsError("no usable temporary directory name found")

    _os.mkdir = _mkdir
    _os.makedirs = _makedirs
    _tempfile.mkdtemp = _mkdtemp
except Exception:  # pragma: no cover - 补丁失败也不能拖垮解释器
    pass
'''


def check_patch(py: Path) -> tuple[bool, str]:
    """让目标解释器自己回答：0o700 的目录现在能不能写。"""
    code = (
        "import os, tempfile, sys;"
        "d = tempfile.mkdtemp(dir='.');"
        "open(os.path.join(d, 'x'), 'w').write('1');"
        "import shutil; shutil.rmtree(d);"
        "print('ok')"
    )
    proc = subprocess.run([str(py), "-c", code], capture_output=True, text=True, cwd=str(ROOT))
    return proc.returncode == 0 and "ok" in proc.stdout, (proc.stdout + proc.stderr).strip()


def install_patch(venv: Path) -> Path:
    """把 sitecustomize.py 写进 venv 的 site-packages（按当前平台挑目录）。"""
    if os.name == "nt":
        site_packages = venv / "Lib" / "site-packages"
    else:
        site_packages = venv / "lib" / f"python{sys.version_info.major}.{sys.version_info.minor}" / "site-packages"
    site_packages.mkdir(parents=True, exist_ok=True)
    target = site_packages / "sitecustomize.py"
    target.write_text(SITECUSTOMIZE, encoding="utf-8")
    return target


def bootstrap_pip(venv: Path) -> None:
    """用补丁后的解释器跑 ensurepip（不 patch 的话它死在 cleanup 的 chmod 上）。"""
    py = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    code = (
        "import sys;"
        f"sys.path.insert(0, {str(ROOT / 'scripts')!r});"
        "import venv_bootstrap as vb;"
        "vb.patch_local();"
        "import ensurepip;"
        "ensurepip.bootstrap(upgrade=True, default_pip=True);"
        "print('pip ok')"
    )
    proc = subprocess.run([str(py), "-c", code], cwd=str(ROOT), text=True, capture_output=True)
    if proc.returncode != 0 or "pip ok" not in proc.stdout:
        raise SystemExit(f"[X] ensurepip 失败：\n{proc.stdout}\n{proc.stderr}")


def patch_local() -> None:
    """在**当前**进程里打一遍同样的补丁（给没有 sitecustomize 的解释器用）。"""
    _orig_mkdir = os.mkdir
    _orig_makedirs = os.makedirs

    def mkdir(path, mode=0o777, *args, **kwargs):
        if mode == 0o700:
            mode = 0o777
        return _orig_mkdir(path, mode, *args, **kwargs)

    def makedirs(name, mode=0o777, *args, **kwargs):
        if mode == 0o700:
            mode = 0o777
        return _orig_makedirs(name, mode, *args, **kwargs)

    def mkdtemp(suffix=None, prefix=None, dir=None):
        base = dir if dir is not None else tempfile.gettempdir()
        for name in tempfile._get_candidate_names():
            path = os.path.join(base, (prefix or "tmp") + name + (suffix or ""))
            try:
                _orig_mkdir(path, 0o777)
                return path
            except FileExistsError:
                continue
        raise FileExistsError("no usable temporary directory name found")

    os.mkdir = mkdir
    os.makedirs = makedirs
    tempfile.mkdtemp = mkdtemp


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="准备一个本机能装包的 venv")
    ap.add_argument("target", nargs="?", default=".venv", help="venv 目录（默认 .venv）")
    ap.add_argument("--check", action="store_true", help="只体检，不建 venv 不写文件")
    args = ap.parse_args(argv)

    venv = Path(args.target)
    if not venv.is_absolute():
        venv = ROOT / venv
    py = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")

    if args.check:
        if not py.exists():
            print(f"[X] 没有解释器：{py}")
            return 1
        ok, detail = check_patch(py)
        print(("OK  " if ok else "FAIL") + f" 0o700 目录可写性：{detail}")
        return 0 if ok else 1

    if not py.exists():
        print(f"[1/3] 建 venv：{venv}")
        subprocess.run([sys.executable, "-m", "venv", str(venv)], check=False)
        if not py.exists():
            # ensurepip 可能会失败并留下半个 venv；--without-pip 至少把解释器建出来
            subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(venv)], check=True)
        if not py.exists():
            raise SystemExit("[X] venv 没建起来")
    else:
        print(f"[1/3] 复用已有 venv：{venv}")

    sitecustomize = install_patch(venv)
    print(f"[2/3] 写了启动补丁：{sitecustomize}")

    pip_ok = subprocess.run([str(py), "-m", "pip", "--version"], capture_output=True, text=True)
    if pip_ok.returncode != 0:
        print("[3/3] 引导 pip ...")
        bootstrap_pip(venv)
    else:
        print("[3/3] pip 已在，跳过引导")

    ok, detail = check_patch(py)
    print(("[OK] " if ok else "[X] ") + f"0o700 目录可写性：{detail}")
    exe = venv / ("Scripts/contentops.exe" if os.name == "nt" else "bin/contentops")
    print(f"\n下一步：\n  {py} -m pip install -e \".[test]\"\n  {exe} --version")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
