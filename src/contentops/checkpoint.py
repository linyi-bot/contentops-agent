"""SQLite checkpointer：让 `interrupt()` 的暂停真的跨进程。

`InMemorySaver` 只能活在一个 Python 进程里 —— 所谓「今天停、明天发」一关窗口就没了。
这里把状态落到一个 sqlite 文件：进程 A 停在人工审核、进程 B 用同一个
`--thread-id` + `--db` 就能接着发布。

刻意做成「可选」：不给 `--db` 时仍然用内存 saver，保证 clone 下来不装额外包也能跑通。
缺包时给一句能照做的安装提示，而不是抛一屏 traceback。
"""
from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

#: 默认落盘位置（相对当前目录）。已在 .gitignore 里，别提交运行态数据库。
DEFAULT_DB = Path(".contentops") / "checkpoints.sqlite"


def resolve_db(explicit: str | os.PathLike[str] | None = None) -> Path:
    """把 `--db` / `CONTENTOPS_DB` / 默认值统一成一个绝对路径，并建好父目录。"""
    raw = explicit or os.environ.get("CONTENTOPS_DB") or DEFAULT_DB
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


@contextmanager
def sqlite_saver(db: str | os.PathLike[str]) -> Iterator[Any]:
    """打开一个落盘的 checkpointer；离开 with 就关连接（提交由 saver 自己负责）。"""
    try:
        from langgraph.checkpoint.sqlite import SqliteSaver
    except ModuleNotFoundError as exc:  # 缺包时别让人对着 traceback 猜
        raise RuntimeError(
            "跨进程恢复需要 langgraph-checkpoint-sqlite："
            "pip install -r requirements.txt（或用 --db 以外的默认内存模式）"
        ) from exc

    path = resolve_db(db)
    conn = sqlite3.connect(path, check_same_thread=False)
    try:
        saver = SqliteSaver(conn)
        saver.setup()  # 建表；重复调用是幂等的
        yield saver
    finally:
        conn.close()
