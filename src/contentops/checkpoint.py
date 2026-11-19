"""Checkpointer：让 `interrupt()` 的暂停真的跨进程。

两种后端，按需选：

- **SQLite**（默认，`--db`）：单文件、零依赖，本机演示和测试用它；
- **PostgreSQL**（`--pg`）：企业口径。四张表由 `PostgresSaver.setup()` 自动建，
  支持多进程/多实例共享同一个 `thread_id`，也支持 `aget_state_history` 做时间旅行。

`InMemorySaver` 只能活在一个 Python 进程里 —— 所谓「今天停、明天发」一关窗口就没了。
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

#: 默认 PostgreSQL 连接串。课程口径是本地 5432、库名 langgraph_db、密码 123456。
DEFAULT_PG_URI = "postgresql://postgres:123456@localhost:5432/contentops"


def resolve_db(explicit: str | os.PathLike[str] | None = None) -> Path:
    """把 `--db` / `CONTENTOPS_DB` / 默认值统一成一个绝对路径，并建好父目录。"""
    raw = explicit or os.environ.get("CONTENTOPS_DB") or DEFAULT_DB
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def resolve_pg_uri(explicit: str | None = None) -> str:
    """`--pg` / `CONTENTOPS_PG_URI` / 默认值。

    `--pg` 允许只给个名字：`--pg contentops` 会补成完整 DSN，
    省得每次敲一长串。
    """
    raw = (explicit or os.environ.get("CONTENTOPS_PG_URI") or DEFAULT_PG_URI).strip()
    if "://" not in raw:
        raw = f"postgresql://postgres:123456@localhost:5432/{raw}"
    return raw


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


@contextmanager
def postgres_saver(uri: str | None = None) -> Iterator[Any]:
    """打开一个 PostgreSQL checkpointer。

    两个必须这么写的点（课程项目踩出来的，照抄能省一天）：

    1. **建表要用 autocommit 连接**：`setup()` 里有 `CREATE INDEX CONCURRENTLY`，
       它不允许出现在事务块中，普通连接会直接报错；
    2. 建完表之后再开一条正式连接给 saver —— setup 用的那条连接用完就关，
       别把「初始化」和「运行期」混在一条连接上。
    """
    try:
        import psycopg
        from langgraph.checkpoint.postgres import PostgresSaver
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "PostgreSQL 后端需要 psycopg + langgraph-checkpoint-postgres："
            "pip install -r requirements.txt"
        ) from exc

    dsn = resolve_pg_uri(uri)
    try:
        with psycopg.connect(dsn, autocommit=True) as setup_conn:
            PostgresSaver(setup_conn).setup()  # 建 checkpoints / blobs / writes / migrations
    except Exception as exc:  # noqa: BLE001 - 连不上时给一句人话，别抛裸 traceback
        raise RuntimeError(
            f"连不上 PostgreSQL（{dsn.split('@')[-1]}）：{type(exc).__name__}: {exc}\n"
            "检查：服务是否启动、库是否建好、--pg 的连接串是否正确。"
        ) from exc

    conn = psycopg.connect(dsn, autocommit=True)
    try:
        yield PostgresSaver(conn)
    finally:
        conn.close()
