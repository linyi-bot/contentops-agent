"""trace 落库：把「每一步发生了什么」从内存里搬到 sqlite。

为什么单独一张表，而不是只打印到终端：
- 终端里的 trace 一关窗口就没了，面试/复盘时要翻「上次那条为什么慢」只能靠猜；
- `interrupt()` 之后进程会退出，恢复时是新进程 —— 想看到完整时间线，
  它必须落在进程外面；
- 不接 LangSmith（要付费、要外网），一张 runs + 一张 events 就够用了。

两张表的职责：
- `runs`：一条运行一行（选题、状态、版本、成本、耗时），列表页就读它；
- `events`：一条 trace 一行（节点、耗时、一句话说明），详情页读它。

写入口只有一个 `sync()`：拿 state 去对账 —— 已经落过的条数对得上就不重复写，
所以「跑完一次 sync」「恢复后再 sync」都不会把同一批事件写两遍。
"""
from __future__ import annotations

import os
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from .state import ArticleState

#: 默认落盘位置（相对当前目录）。和 checkpointer 分开放：一个存图状态，一个存轨迹。
DEFAULT_TRACE_DB = Path(".contentops") / "traces.sqlite"

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id      TEXT PRIMARY KEY,
    thread_id   TEXT NOT NULL,
    topic       TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'running',
    mode        TEXT NOT NULL DEFAULT '',
    db          TEXT NOT NULL DEFAULT '',
    revision    INTEGER NOT NULL DEFAULT 0,
    decision    TEXT NOT NULL DEFAULT '',
    review_note TEXT NOT NULL DEFAULT '',
    event_count INTEGER NOT NULL DEFAULT 0,
    calls       INTEGER NOT NULL DEFAULT 0,
    tokens_in   INTEGER NOT NULL DEFAULT 0,
    tokens_out  INTEGER NOT NULL DEFAULT 0,
    cost_cny    REAL NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    error       TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS events (
    run_id TEXT NOT NULL,
    seq    INTEGER NOT NULL,
    node   TEXT NOT NULL,
    ms     INTEGER NOT NULL,
    note   TEXT NOT NULL,
    ts     TEXT NOT NULL,
    PRIMARY KEY (run_id, seq)
);
"""


def resolve_trace_db(explicit: str | os.PathLike[str] | None = None) -> Path:
    raw = explicit or os.environ.get("CONTENTOPS_TRACE_DB") or DEFAULT_TRACE_DB
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def status_of(state: Any, error: str = "") -> str:
    """把一次调用的结果翻译成一个状态词 —— 页面上那枚标签就是它。

    项目二的终局是「出完图」，所以判定顺序是：
    出错 > 停在中断 > 有图（完成）> 其它（没走到头，算 ended）。
    """
    if error:
        return "error"
    if state.get("__interrupt__"):
        return "paused"
    if state.get("image_urls"):
        return "completed"
    if state.get("status") == "done":
        return "completed"
    return "ended"


class TraceStore:
    """一个很薄的 sqlite 包装。没有 ORM，因为这里只需要两张表的增查。"""

    def __init__(self, path: str | os.PathLike[str] | None = None) -> None:
        self.path = resolve_trace_db(path)
        with self._conn() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    # -- 写 ---------------------------------------------------------------- #

    def sync(
        self,
        state: ArticleState,
        *,
        run_id: str | None = None,
        mode: str = "",
        db: str = "",
        error: str = "",
        usage: tuple[int, int, int, float] | None = None,
    ) -> str:
        """把 state 里的新事件补进库，顺手更新这行的状态。

        `usage` 是 (calls, tokens_in, tokens_out, cost) 的**增量**，由调用方算好；
        这里只做累加，避免「拿累计快照写一次、再写一次」把成本写重。
        """
        rid = run_id or str(state.get("__run_id") or "") or f"run-{uuid.uuid4().hex[:8]}"
        now = datetime.now().isoformat(timespec="seconds")
        events = list(state.get("trace") or [])

        with self._conn() as conn:
            row = conn.execute("SELECT event_count FROM runs WHERE run_id=?", (rid,)).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO runs (run_id, thread_id, topic, status, mode, db, created_at, updated_at)"
                    " VALUES (?,?,?,?,?,?,?,?)",
                    (
                        rid,
                        rid,
                        str(state.get("topic") or ""),
                        status_of(state, error),
                        mode,
                        db,
                        now,
                        now,
                    ),
                )
                done = 0
            else:
                done = int(row["event_count"])

            fresh = events[done:]
            for offset, ev in enumerate(fresh):
                conn.execute(
                    "INSERT OR IGNORE INTO events (run_id, seq, node, ms, note, ts) VALUES (?,?,?,?,?,?)",
                    (rid, done + offset, ev["node"], int(ev["ms"]), str(ev["note"]), now),
                )

            sets = [
                "topic=?",
                "status=?",
                "revision=?",
                "event_count=?",
                "updated_at=?",
            ]
            args: list[Any] = [
                str(state.get("topic") or ""),
                status_of(state, error),
                int(state.get("revision") or 0),
                len(events),
                now,
            ]
            if mode:
                # 只记「第一次跑它用的模型」：跑到一半换了模型（比如恢复时回落 mock），
                # 把这一行改成 mock 会让「这条花了 ¥0.0005」看起来像假的。
                sets.append("mode=CASE WHEN mode='' THEN ? ELSE mode END")
                args.append(mode)
            if db:
                sets.append("db=?")
                args.append(db)
            if state.get("review_decision"):
                sets.append("decision=?")
                args.append(str(state["review_decision"]))
            if state.get("review_feedback"):
                sets.append("review_note=?")
                args.append(str(state["review_feedback"]))
            if error:
                sets.append("error=?")
                args.append(error)
            if usage:
                calls, tin, tout, cost = usage
                sets += [
                    "calls=calls+?",
                    "tokens_in=tokens_in+?",
                    "tokens_out=tokens_out+?",
                    "cost_cny=cost_cny+?",
                ]
                args += [int(calls), int(tin), int(tout), float(cost)]

            conn.execute(f"UPDATE runs SET {', '.join(sets)} WHERE run_id=?", (*args, rid))
        return rid

    def touch(self, run_id: str, status: str, *, error: str = "") -> None:
        """只改状态（比如「正在跑」），不动事件。"""
        now = datetime.now().isoformat(timespec="seconds")
        with self._conn() as conn:
            if error:
                conn.execute(
                    "UPDATE runs SET status=?, error=?, updated_at=? WHERE run_id=?",
                    (status, error, now, run_id),
                )
            else:
                conn.execute(
                    "UPDATE runs SET status=?, updated_at=? WHERE run_id=?",
                    (status, now, run_id),
                )

    # -- 读 ---------------------------------------------------------------- #

    def list_runs(self, limit: int = 50) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM runs ORDER BY updated_at DESC, run_id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    def get_run(self, run_id: str) -> dict | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
        return dict(row) if row else None

    def events(self, run_id: str) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM events WHERE run_id=? ORDER BY seq", (run_id,)
            ).fetchall()
        return [dict(r) for r in rows]


def usage_delta(before: Any, after: Any) -> tuple[int, int, int, float]:
    """两帧 Usage 快照之差 —— 只记「这一次调用花掉的」，不是进程累计。"""
    return (
        after.calls - before.calls,
        after.prompt_tokens - before.prompt_tokens,
        after.completion_tokens - before.completion_tokens,
        round(after.cost_cny - before.cost_cny, 6),
    )
