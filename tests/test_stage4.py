"""阶段 4 的验收测试：**进程 A 停在选题 → 进程 B 选 → 进程 C 放行**。

所以这里不用 in-process 的 `resume()` 冒充，而是真的起子进程：

    A: run --pause            → 返回 75，状态落进 sqlite
    B: resume --select 2      → 写稿，停在审核（75）
    C: resume --decision approve → 出图收工（0）

`InMemorySaver` 在这几条用例里都会挂 —— 这正是它们存在的意义。
顺带守一条承诺：内存模式下 resume 必须给出能照做的提示（返回码 2，而不是 traceback）。
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _env(work: Path) -> dict[str, str]:
    env = dict(os.environ)
    env.update(
        {
            "LLM_PROVIDER": "mock",
            "IMAGE_PROVIDER": "mock",
            "CONTENTOPS_NO_DOTENV": "1",
            "IMAGE_OUT_DIR": str(work / "images"),
            "PYTHONIOENCODING": "utf-8",
        }
    )
    return env


def _run(work: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "contentops.cli", *args],
        cwd=str(ROOT),
        env=_env(work),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
    )


@pytest.mark.parametrize("backend", ["sqlite"])
def test_three_processes_finish_the_job(work, backend):
    db = work / "cp.sqlite"

    a = _run(work, "run", "--topic", "AI 编程入门", "--pause", "--db", str(db))
    assert a.returncode == 75, a.stderr
    assert "等你选一个选题" in a.stdout

    # 从输出里拿 thread_id
    tid = [line.split("：")[-1].strip() for line in a.stdout.splitlines() if line.startswith("  会话：")][0]

    b = _run(work, "resume", "--thread-id", tid, "--db", str(db), "--select", "2")
    assert b.returncode == 75, b.stderr
    assert "等你放行或打回" in b.stdout
    assert "第 1 版" in b.stdout

    out = work / "out"
    c = _run(work, "resume", "--thread-id", tid, "--db", str(db),
             "--decision", "approve", "--out", str(out))
    assert c.returncode == 0, c.stderr
    assert "出图 4 张" in c.stdout
    assert list(out.glob("*.md")), "成品没有落盘"


def test_memory_mode_cannot_resume(work):
    """没给 --db：进程一退状态就没了，resume 应该明确说找不到，而不是崩。"""
    a = _run(work, "run", "--topic", "AI 编程入门", "--pause")
    assert a.returncode == 75
    tid = [line.split("：")[-1].strip() for line in a.stdout.splitlines() if line.startswith("  会话：")][0]

    b = _run(work, "resume", "--thread-id", tid, "--select", "1")
    assert b.returncode == 2
    assert "找不到会话" in b.stdout


def test_auto_mode_runs_to_the_end(work):
    """--auto approve：自动选第 1 个 + 自动放行，一条命令跑完（CI / 演示用）。"""
    r = _run(work, "run", "--topic", "AI 编程入门", "--auto", "approve", "--db", str(work / "cp.sqlite"))
    assert r.returncode == 0, r.stderr
    assert "出图 4 张" in r.stdout
    assert "plan_topics" in r.stdout and "generate_images" in r.stdout
