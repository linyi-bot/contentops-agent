"""阶段 3 · S3-03：SQLite checkpointer 的验收测试。

验收线（看板原话）：**进程 A 停在审核 → 进程 B resume 能接着发布**。
所以这里不用 in-process 的 `resume()` 冒充，而是真的起子进程：

    A: run --pause            → 返回 75，状态落进 sqlite
    B: resume --decision ...  → 读同一个库，接着跑

`InMemorySaver` 在这三条用例里都会挂 —— 这正是它们存在的意义。
"""
from __future__ import annotations

import os
import re
import shutil
import sqlite3
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TOPIC = "用 AI 做周报自动化"


@pytest.fixture()
def workdir() -> Path:
    """本机沙箱不给写 %TEMP%，pytest 的 tmp_path 会直接 PermissionError。

    所以临时目录放在仓库里的 tests/_tmp/（已 gitignore），用完删掉。
    """
    path = ROOT / "tests" / "_tmp" / uuid.uuid4().hex[:8]
    path.mkdir(parents=True)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def _env() -> dict[str, str]:
    """子进程也要能 import contentops —— 本机靠 .venv-libs，CI 靠 pip 装好的环境。"""
    env = dict(os.environ)
    paths = [str(ROOT / "src")]
    libs = ROOT / ".venv-libs"
    if libs.is_dir():
        paths.append(str(libs))
    env["PYTHONPATH"] = os.pathsep.join(paths)
    env["PYTHONIOENCODING"] = "utf-8"
    env.pop("CONTENTOPS_DB", None)  # 别让外部环境变量影响用例
    return env


def _cli(*args: str, timeout: int = 120) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "contentops.cli", *args],
        cwd=ROOT,
        env=_env(),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )


def _checkpoint_rows(db: Path) -> int:
    with sqlite3.connect(db) as conn:
        return int(conn.execute("select count(*) from checkpoints").fetchone()[0])


def test_pause_in_one_process_publish_in_another(workdir: Path):
    db = workdir / "state.sqlite"
    out = workdir / "out"

    a = _cli("run", "--topic", TOPIC, "--db", str(db), "--thread-id", "t-cross", "--pause")
    assert a.returncode == 75, f"进程 A 应停在审核（75），实际 {a.returncode}\n{a.stdout}\n{a.stderr}"
    assert "【人工审核】" in a.stdout
    assert "已暂停" in a.stdout
    assert db.is_file(), "暂停后没有生成 sqlite 状态库"
    assert _checkpoint_rows(db) >= 1, "sqlite 里没有 checkpoint 行"

    b = _cli(
        "resume",
        "--thread-id",
        "t-cross",
        "--db",
        str(db),
        "--decision",
        "approve",
        "--out",
        str(out),
    )
    assert b.returncode == 0, f"进程 B 恢复失败：\n{b.stdout}\n{b.stderr}"
    assert "【发布】" in b.stdout
    assert "闸门：passed" in b.stdout

    # 进程 B 的 trace 里必须能看到进程 A 跑过的节点 —— 证明状态是真从库里读回来的
    for node in ("retrieve", "draft", "factcheck"):
        assert node in b.stdout, f"trace 缺少 {node}，状态没恢复全"

    files = list(out.glob("*.md"))
    assert len(files) == 1, f"应当恰好写出一篇发布记录，实际 {files}"
    assert "## 来源" in files[0].read_text(encoding="utf-8")


def test_reject_in_second_process_rewrites_and_pauses_again(workdir: Path):
    db = workdir / "loop.sqlite"
    out = workdir / "out"

    assert _cli("run", "--topic", TOPIC, "--db", str(db), "--thread-id", "t-loop", "--pause").returncode == 75

    rejected = _cli(
        "resume",
        "--thread-id",
        "t-loop",
        "--db",
        str(db),
        "--decision",
        "reject",
        "--note",
        "太短",
    )
    assert rejected.returncode == 75, f"打回后应再次停在审核：\n{rejected.stdout}\n{rejected.stderr}"
    assert "第 2 版" in rejected.stdout, "打回后应当重写一版（revision+1）"
    assert "又停在审核" in rejected.stdout

    approved = _cli(
        "resume",
        "--thread-id",
        "t-loop",
        "--db",
        str(db),
        "--decision",
        "approve",
        "--out",
        str(out),
    )
    assert approved.returncode == 0, f"第三次恢复失败：\n{approved.stdout}\n{approved.stderr}"
    text = next(out.glob("*.md")).read_text(encoding="utf-8")
    revision = int(re.search(r"版本：第 (\d+) 版", text).group(1))
    assert revision >= 2, "最终发布的版本号应该带着打回后的重写痕迹"


def test_resume_unknown_thread_is_refused_cleanly(workdir: Path):
    db = workdir / "empty.sqlite"
    r = _cli("resume", "--thread-id", "nope", "--db", str(db), "--decision", "approve")
    assert r.returncode == 2
    assert "[X] 找不到会话" in r.stdout
    assert "Traceback" not in r.stderr
