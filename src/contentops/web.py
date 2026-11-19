"""极简页面 —— 项目二版：选题 → 审稿 → 出图，三步都在浏览器里完成。

刻意只做四件事（够用就行，页面是拿来证明链路和观测的，不是产品）：

1. 起一条运行：填方向 → 出 5 个选题；
2. 选题：点一个，图接着跑，停在人工审核；
3. 审稿：放行 / 打回（打回带意见，重写一版再停）；
4. 成品：正文 + 图点 + 配图 + trace + 节点成本。

数据来源分两处：轨迹读 `trace_store`（sqlite，重启还能翻），
草稿 / 选题包 / 图 URL 读 checkpointer 里的图状态（不重复存正文）。
"""
from __future__ import annotations

import html
import json
import os
import queue
import threading
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from . import auth as auth_mod
from .checkpoint import resolve_db
from .graph import (
    config_for,
    history as graph_history,
    initial_input,
    resume as graph_resume,
    rewind as graph_rewind,
    session,
    start,
)
from langgraph.types import Command
from .image_service import describe_images
from .llm import describe, usage_snapshot
from .nodes import token_sink
from .output import slug_for, write_markdown, write_publish
from .trace_store import TraceStore, usage_delta

try:
    from fastapi import Body, Depends, FastAPI, Form, Header, HTTPException
    from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
    from fastapi.staticfiles import StaticFiles
except ModuleNotFoundError as exc:  # 没装 fastapi 时给一句能照做的话
    raise SystemExit(
        "页面需要 fastapi + uvicorn：pip install -r requirements.txt"
    ) from exc

#: 页面用的 checkpointer（和命令行默认库分开，免得 `serve` 顺手改了 CLI 的会话）
WEB_DB = Path(".contentops") / "web.sqlite"
#: 成品 md 写到这里；图片在 outputs/images（由 /outputs 静态暴露）
OUT_DIR = Path("outputs")

CSS = """
:root{--bg:#0f1115;--card:#151922;--line:#262b35;--fg:#e8eaed;--dim:#8b93a1;
--ok:#3fb950;--warn:#d29922;--bad:#f85149;--accent:#4493f8}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
font:14px/1.6 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif}
main{max-width:980px;margin:0 auto;padding:28px 20px 64px}
a{color:var(--accent);text-decoration:none}a:hover{text-decoration:underline}
h1{font-size:20px;margin:0 0 4px}h2{font-size:15px;margin:28px 0 10px;color:var(--dim);
font-weight:600;letter-spacing:.04em;text-transform:uppercase}
.sub{color:var(--dim);font-size:13px;margin-bottom:20px;overflow-wrap:anywhere}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px 18px}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{text-align:left;padding:8px 10px;border-bottom:1px solid var(--line);vertical-align:top}
th{color:var(--dim);font-weight:600;font-size:12px}
tr:last-child td{border-bottom:none}
code,pre,.mono{font-family:ui-monospace,Consolas,"Cascadia Mono",monospace;overflow-wrap:anywhere}
pre{background:#0b0d12;border:1px solid var(--line);border-radius:8px;padding:14px;
overflow:auto;white-space:pre-wrap;word-break:break-word;font-size:13px;margin:0}
.badge{display:inline-block;padding:1px 8px;border-radius:999px;font-size:12px;
border:1px solid var(--line);color:var(--dim)}
.badge.ok{color:var(--ok);border-color:#1f4429;background:#122017}
.badge.warn{color:var(--warn);border-color:#4a3a12;background:#221a08}
.badge.bad{color:var(--bad);border-color:#5a1d1b;background:#241010}
form.inline{display:flex;gap:8px;margin:0}
input[type=text],textarea{background:#0b0d12;border:1px solid var(--line);color:var(--fg);
border-radius:8px;padding:9px 11px;font:inherit;width:100%}
textarea{min-height:64px;resize:vertical}
button{background:#1f6feb;color:#fff;border:0;border-radius:8px;padding:9px 16px;
font:inherit;font-weight:600;cursor:pointer}
button.ghost{background:transparent;border:1px solid var(--line);color:var(--fg);font-weight:500}
button.danger{background:#8f2a26}
.meta{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:10px 18px;font-size:13px}
.meta b{display:block;color:var(--dim);font-weight:500;font-size:12px;margin-bottom:2px}
.bar{height:6px;background:#222833;border-radius:3px;overflow:hidden;min-width:40px}
.bar>i{display:block;height:100%;background:var(--accent)}
.ok{color:var(--ok)}.bad{color:var(--bad)}.dim{color:var(--dim)}
.row{display:flex;gap:12px;align-items:center;flex-wrap:wrap}
.steps{display:flex;gap:6px;margin:0 0 18px;flex-wrap:wrap}
.step{padding:4px 12px;border-radius:999px;border:1px solid var(--line);font-size:12.5px;color:var(--dim)}
.step.on{color:#fff;background:#1f6feb;border-color:#1f6feb}
.step.done{color:var(--ok);border-color:#1f4429}
.options{display:flex;flex-direction:column;gap:10px}
.opt{display:flex;gap:10px;align-items:flex-start;padding:11px 13px;border:1px solid var(--line);
border-radius:9px;cursor:pointer}
.opt:hover{border-color:#3a4356}
.opt input{margin-top:3px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:14px}
.grid figure{margin:0;background:#0b0d12;border:1px solid var(--line);border-radius:10px;overflow:hidden}
.grid img{width:100%;display:block;aspect-ratio:3/4;object-fit:cover;background:#0b0d12}
.grid figcaption{padding:8px 10px;font-size:12px;color:var(--dim)}
"""

BADGE = {"paused": ("warn", "等你操作"), "completed": ("ok", "已完成"),
         "ended": ("", "未完成"), "error": ("bad", "出错"), "running": ("", "运行中")}

STEPS = [("topics", "出选题"), ("draft", "写稿"), ("review", "人工审"), ("images", "出图")]


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #


def _db_path() -> Path:
    return resolve_db(os.environ.get("CONTENTOPS_WEB_DB") or str(WEB_DB))


def _out_dir() -> Path:
    return Path(os.environ.get("CONTENTOPS_OUT_DIR") or OUT_DIR)


def _store() -> TraceStore:
    return TraceStore()


def _model() -> str:
    return str(describe()["label"])


def _page(title: str, body: str) -> str:
    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)} · ContentOps</title><style>{CSS}</style></head>
<body><main>{body}</main></body></html>"""


def _badge(status: str) -> str:
    cls, text = BADGE.get(status, ("", status))
    return f'<span class="badge {cls}">{html.escape(text)}</span>'


def _fmt_ms(ms: int) -> str:
    return f"{ms / 1000:.1f}s" if ms >= 1000 else f"{ms}ms"


def _trace_table(events: list[dict]) -> str:
    if not events:
        return '<p class="dim">还没有 trace —— 跑一条就有了。</p>'
    peak = max((e["ms"] for e in events), default=1) or 1
    rows = []
    for e in events:
        width = max(2, round(e["ms"] / peak * 100))
        rows.append(
            f'<tr><td class="mono dim">{e["seq"] + 1}</td>'
            f'<td class="mono" style="white-space:nowrap">{html.escape(e["node"])}</td>'
            f'<td class="mono" style="white-space:nowrap">{e["ms"]} ms</td>'
            f'<td style="width:120px"><div class="bar"><i style="width:{width}%"></i></div></td>'
            f'<td>{html.escape(e["note"])}</td>'
            f'<td class="mono dim" style="white-space:nowrap">{e["ts"][11:]}</td></tr>'
        )
    total = sum(e["ms"] for e in events)
    return (
        "<table><tr><th>#</th><th>节点</th><th>耗时</th><th></th><th>说明</th><th>时间</th></tr>"
        + "".join(rows)
        + f'<tr><td></td><td class="dim">合计</td><td class="mono">{_fmt_ms(total)}</td>'
        f"<td></td><td></td><td></td></tr></table>"
    )


def _metrics_table(metrics: list[dict]) -> str:
    if not metrics:
        return '<p class="dim">还没有节点指标。</p>'
    rows = []
    for m in metrics:
        tokens = m.get("tokens") or 0
        rows.append(
            f'<tr><td class="mono">{html.escape(str(m.get("node", "")))}</td>'
            f'<td class="mono" style="white-space:nowrap">{m.get("ms", 0)} ms</td>'
            f'<td class="mono" style="white-space:nowrap">{tokens or "—"}</td>'
            f'<td class="mono dim">{html.escape(str(m.get("model") or "—"))}</td></tr>'
        )
    total_ms = sum(int(m.get("ms") or 0) for m in metrics)
    total_tokens = sum(int(m.get("tokens") or 0) for m in metrics)
    return (
        "<table><tr><th>节点</th><th>耗时</th><th>token</th><th>模型</th></tr>"
        + "".join(rows)
        + f'<tr><td class="dim">合计</td><td class="mono">{_fmt_ms(total_ms)}</td>'
        f'<td class="mono">{total_tokens or "—"}</td><td></td></tr></table>'
    )


def _steps_done(state: dict, packet: dict | None) -> set[str]:
    """当前走到哪一步 —— 只影响阶段条的高亮。"""
    done: set[str] = set()
    if state.get("generated_topics"):
        done.add("topics")
    if state.get("article_content"):
        done.add("draft")
    if state.get("review_decision") and state.get("review_decision") != "":
        done.add("review")
    if state.get("image_urls"):
        done.add("images")
    return done


def _steps_bar(done: set[str], *, active: str = "") -> str:
    out = []
    for key, label in STEPS:
        cls = "step on" if key == active else ("step done" if key in done else "step")
        out.append(f'<span class="{cls}">{label}</span>')
    return '<div class="steps">' + "".join(out) + "</div>"


def _load(run: dict) -> tuple[dict, dict | None]:
    db = run.get("db") or str(_db_path())
    try:
        with session(run["run_id"], db=db) as (app, tid):
            snap = app.get_state(config_for(tid))
        packet = snap.interrupts[0].value if snap.interrupts else None
        return dict(snap.values or {}), packet
    except Exception as exc:  # noqa: BLE001 - 页面读不到状态时不该整页 500
        return {}, {"error": f"{type(exc).__name__}: {exc}"}


# --------------------------------------------------------------------------- #
# 路由
# --------------------------------------------------------------------------- #

app = FastAPI(title="ContentOps Agent", docs_url=None, redoc_url=None)


@app.get("/healthz")
def healthz() -> JSONResponse:
    return JSONResponse({"ok": True, "runs": len(_store().list_runs(limit=500))})


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    runs = _store().list_runs()
    rows = []
    for r in runs:
        rows.append(
            f'<tr><td class="mono dim" style="white-space:nowrap">{r["updated_at"][5:16]}</td>'
            f'<td><a href="/runs/{html.escape(r["run_id"])}">{html.escape(r["topic"])}</a></td>'
            f'<td>{_badge(r["status"])}</td>'
            f'<td class="mono">第 {r["revision"]} 版</td>'
            f'<td class="mono">{r["event_count"]}</td>'
            f'<td class="mono">{html.escape(r["mode"] or "—")}</td></tr>'
        )
    table = (
        "<table><tr><th>时间</th><th>选题方向</th><th>状态</th><th>版本</th>"
        "<th>事件</th><th>模型</th></tr>" + "".join(rows) + "</table>"
        if rows
        else '<p class="dim">还没有运行记录。上面填个方向，跑第一条。</p>'
    )
    body = f"""
<h1>ContentOps Agent</h1>
<div class="sub">选题 → 人工选 → 写稿 → 人工审 → 提炼图点 → 并行出图　·
当前模型 <code>{html.escape(_model())}</code>　·　出图 <code>{html.escape(describe_images()['name'])}</code></div>

<div class="card">
  <form class="inline" method="post" action="/runs">
    <input type="text" name="topic" placeholder="选题方向，例如：AI 编程入门" required>
    <button type="submit">跑一条</button>
  </form>
  <p class="sub" style="margin:10px 0 0">跑到「人选选题」就停下 —— 点进详情选一个，再往下走。</p>
</div>

<h2>全部运行</h2>
<div class="card">{table}</div>
"""
    return _page("全部运行", body)


@app.post("/runs")
def create_run(topic: str = Form("")) -> RedirectResponse:
    topic = topic.strip()
    if not topic:
        return RedirectResponse("/", status_code=303)

    store = _store()
    db = str(_db_path())
    run_id = f"run-{uuid.uuid4().hex[:8]}"
    before = usage_snapshot()
    try:
        with session(run_id, db=db) as (app, tid):
            state = start(app, topic, tid)
        store.sync(state, run_id=run_id, db=db, mode=_model(),
                   usage=usage_delta(before, usage_snapshot()))
    except Exception as exc:  # noqa: BLE001
        store.sync({"topic_direction": topic, "trace": []}, run_id=run_id, db=db,
                   mode=_model(), error=f"{type(exc).__name__}: {exc}")
    return RedirectResponse(f"/runs/{run_id}", status_code=303)


@app.post("/runs/{run_id}/select")
def select_topic(run_id: str, selected_topic: str = Form("")) -> RedirectResponse:
    store = _store()
    run = store.get_run(run_id)
    if run is None:
        raise HTTPException(404, "没有这条运行")

    db = run["db"] or str(_db_path())
    before = usage_snapshot()
    try:
        with session(run_id, db=db) as (app, tid):
            state = graph_resume(app, {"selected_topic": selected_topic}, tid)
        store.sync(state, run_id=run_id, db=db, mode=_model(),
                   usage=usage_delta(before, usage_snapshot()))
    except Exception as exc:  # noqa: BLE001
        store.sync({"topic_direction": run["topic"], "trace": []}, run_id=run_id, db=db,
                   mode=run["mode"], error=f"{type(exc).__name__}: {exc}")
    return RedirectResponse(f"/runs/{run_id}", status_code=303)


@app.post("/runs/{run_id}/review")
def review(run_id: str, decision: str = Form("approve"), note: str = Form("")) -> RedirectResponse:
    store = _store()
    run = store.get_run(run_id)
    if run is None:
        raise HTTPException(404, "没有这条运行")

    db = run["db"] or str(_db_path())
    before = usage_snapshot()
    try:
        with session(run_id, db=db) as (app, tid):
            state = graph_resume(app, {"decision": decision, "feedback": note.strip()}, tid)
        store.sync(state, run_id=run_id, db=db, mode=_model(),
                   usage=usage_delta(before, usage_snapshot()))
        if state.get("image_urls"):
            write_markdown(state, _out_dir())
    except Exception as exc:  # noqa: BLE001
        store.sync({"topic_direction": run["topic"], "trace": []}, run_id=run_id, db=db,
                   mode=run["mode"], error=f"{type(exc).__name__}: {exc}")
    return RedirectResponse(f"/runs/{run_id}", status_code=303)


@app.get("/runs/{run_id}", response_class=HTMLResponse)
def run_detail(run_id: str) -> str:
    store = _store()
    run = store.get_run(run_id)
    if run is None:
        raise HTTPException(404, "没有这条运行")

    events = store.events(run_id)
    state, packet = _load(run)
    packet = packet if isinstance(packet, dict) else {}
    step = packet.get("step", "")

    topic = str(state.get("topic_direction") or run["topic"])
    total = sum(e["ms"] for e in events)
    meta = f"""
<div class="meta">
  <div><b>状态</b>{_badge(run["status"])}</div>
  <div><b>版本</b>第 {run["revision"]} 版</div>
  <div><b>模型</b><code>{html.escape(run["mode"] or "—")}</code></div>
  <div><b>trace 耗时</b>{_fmt_ms(total)}</div>
  <div><b>运行 id</b><code>{html.escape(run_id)}</code></div>
</div>"""

    done = _steps_done(state, packet)
    active = "topics" if step == "select_topic" else ("review" if step == "review" else "")

    parts = [
        '<div class="row" style="margin-bottom:14px"><a href="/">← 全部运行</a></div>',
        f"<h1>{html.escape(topic)}</h1>",
        _steps_bar(done, active=active),
        f'<div class="card">{meta}</div>',
    ]
    if run["error"]:
        parts.append(f'<h2>错误</h2><div class="card"><pre class="bad">{html.escape(run["error"])}</pre></div>')

    # --- 第一步：选题 ---
    if step == "select_topic":
        options = packet.get("options") or []
        opts = "".join(
            f'<label class="opt"><input type="radio" name="selected_topic" value="{html.escape(o)}"'
            f'{" checked" if i == 0 else ""}><span>{html.escape(o)}</span></label>'
            for i, o in enumerate(options)
        )
        parts.append(
            f'<h2>第 1 步 · 选一个选题（AI 出了 {len(options)} 个）</h2>'
            f'<div class="card"><form method="post" action="/runs/{html.escape(run_id)}/select">'
            f'<div class="options">{opts}</div>'
            '<div class="row" style="margin-top:12px"><button type="submit">定了，按这个写</button></div>'
            "</form></div>"
        )

    # --- 第二步：审稿 ---
    draft = str(state.get("article_content") or "")
    if step == "review":
        parts.append(
            f'<h2>第 2 步 · 人工审核（第 {packet.get("revision")} 版 · {len(packet.get("draft") or "")} 字）</h2>'
            f'<div class="card"><pre>{html.escape(str(packet.get("draft") or ""))}</pre>'
            f'<form method="post" action="/runs/{html.escape(run_id)}/review" style="margin-top:14px">'
            '<textarea name="note" placeholder="打回意见（放行可留空）"></textarea>'
            '<div class="row" style="margin-top:10px">'
            '<button type="submit" name="decision" value="approve">放行，去出图</button>'
            '<button type="submit" name="decision" value="reject" class="danger">打回重写</button>'
            "</div></form></div>"
        )
    elif draft and step != "select_topic":
        parts.append(f"<h2>正文 · 第 {state.get('revision')} 版</h2>"
                     f'<div class="card"><pre>{html.escape(draft)}</pre></div>')

    # --- 第三步：成品 ---
    urls = list(state.get("image_urls") or [])
    points = list(state.get("visual_points") or [])
    if urls:
        figs = []
        for i, (u, p) in enumerate(zip(urls, points or [""] * len(urls)), start=1):
            src = u if u.startswith("http") else u
            figs.append(
                f'<figure><img src="{html.escape(src)}" alt="图 {i}" loading="lazy">'
                f"<figcaption>{i}. {html.escape(str(p))}</figcaption></figure>"
            )
        saved = _out_dir() / f"{slug_for(str(state.get('selected_topic') or topic))}.md"
        link = (f'<p><a href="/outputs/{html.escape(saved.name)}">打开成品 markdown ↗</a></p>'
                if saved.exists() else "")
        parts.append(f'<h2>配图 · {len(urls)} 张</h2><div class="card"><div class="grid">'
                     + "".join(figs) + f"</div>{link}</div>")

    if state.get("image_errors"):
        parts.append('<h2>出图失败（没有藏）</h2><div class="card"><ul>'
                     + "".join(f"<li>{html.escape(e)}</li>" for e in state["image_errors"])
                     + "</ul></div>")

    if run["status"] == "ended" and not step:
        parts.append('<h2>结果</h2><div class="card"><p class="dim">'
                     "打回次数用尽，没有出图 —— 这一版不算交付。</p></div>")

    parts.append(f'<h2>trace · {len(events)} 条</h2><div class="card">{_trace_table(events)}</div>')
    metrics = list(state.get("node_metrics") or [])
    if metrics:
        parts.append(f'<h2>节点成本 · {len(metrics)} 个</h2>'
                     f'<div class="card">{_metrics_table(metrics)}</div>')

    return _page(str(state.get("selected_topic") or topic), "".join(parts))


#: 成品与图片：/outputs 下都静态暴露（目录不存在时返回 404，不影响启动）
app.mount("/outputs", StaticFiles(directory=str(_out_dir()), check_dir=False), name="outputs")

#: Vue3 前端（无构建步骤，Vue 已 vendor 进仓库）。目录不存在时跳过，不影响老页面。
WEBAPP_DIR = Path(__file__).resolve().parents[2] / "webapp"
if WEBAPP_DIR.exists():
    app.mount("/app", StaticFiles(directory=str(WEBAPP_DIR), html=True), name="webapp")


# --------------------------------------------------------------------------- #
# JSON API —— 给 Vue 前端用（老页面继续走服务端渲染，两套共用同一份存储）
# --------------------------------------------------------------------------- #


def current_user(authorization: str = Header(default="")) -> str:
    """`CONTENTOPS_AUTH=on` 时校验 Bearer token；关着就返回 local（本地演示不折腾）。"""
    if not auth_mod.auth_required():
        return "local"
    if not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "缺少 Authorization: Bearer <token>")
    try:
        return str(auth_mod.decode_token(authorization[7:]).get("sub", ""))
    except ValueError as exc:
        raise HTTPException(401, f"token 无效：{exc}") from exc


@app.post("/api/auth/register")
def api_register(payload: dict = Body(...)) -> JSONResponse:
    ok, err = auth_mod.register(str(payload.get("username", "")), str(payload.get("password", "")))
    if not ok:
        raise HTTPException(400, err)
    return JSONResponse({"token": auth_mod.create_token(str(payload["username"]).strip())})


@app.post("/api/auth/login")
def api_login(payload: dict = Body(...)) -> JSONResponse:
    username = str(payload.get("username", "")).strip()
    if not auth_mod.verify(username, str(payload.get("password", ""))):
        raise HTTPException(401, "用户名或密码不对")
    return JSONResponse({"token": auth_mod.create_token(username), "username": username})


@app.get("/api/me")
def api_me(user: str = Depends(current_user)) -> JSONResponse:
    return JSONResponse({"user": user, "auth_required": auth_mod.auth_required()})


@app.get("/api/runs")
def api_runs(user: str = Depends(current_user)) -> JSONResponse:
    return JSONResponse({"runs": _store().list_runs()})


@app.post("/api/runs")
def api_create(payload: dict = Body(...), user: str = Depends(current_user)) -> JSONResponse:
    topic = str(payload.get("topic", "")).strip()
    if not topic:
        raise HTTPException(400, "topic 不能为空")

    store = _store()
    db = str(_db_path())
    run_id = f"run-{uuid.uuid4().hex[:8]}"
    before = usage_snapshot()
    try:
        with session(run_id, db=db) as (app, tid):
            state = start(app, topic, tid)
        store.sync(state, run_id=run_id, db=db, mode=_model(),
                   usage=usage_delta(before, usage_snapshot()))
    except Exception as exc:  # noqa: BLE001
        store.sync({"topic_direction": topic, "trace": []}, run_id=run_id, db=db,
                   mode=_model(), error=f"{type(exc).__name__}: {exc}")
        raise HTTPException(500, f"{type(exc).__name__}: {exc}") from exc
    return JSONResponse({"run_id": run_id})


@app.get("/api/runs/{run_id}")
def api_run(run_id: str, user: str = Depends(current_user)) -> JSONResponse:
    store = _store()
    run = store.get_run(run_id)
    if run is None:
        raise HTTPException(404, "没有这条运行")
    state, packet = _load(run)
    packet = packet if isinstance(packet, dict) else None
    return JSONResponse({
        "run": run,
        "events": store.events(run_id),
        "state": state,
        "packet": packet,
        "step": (packet or {}).get("step", ""),
    })


def _resume_api(run_id: str, answer: dict, *, after_done: bool = False) -> JSONResponse:
    store = _store()
    run = store.get_run(run_id)
    if run is None:
        raise HTTPException(404, "没有这条运行")

    db = run["db"] or str(_db_path())
    before = usage_snapshot()
    try:
        with session(run_id, db=db) as (app, tid):
            state = graph_resume(app, answer, tid)
        store.sync(state, run_id=run_id, db=db, mode=_model(),
                   usage=usage_delta(before, usage_snapshot()))
        if after_done and state.get("image_urls"):
            write_markdown(state, _out_dir())
            write_publish(state, _out_dir())
    except Exception as exc:  # noqa: BLE001
        store.sync({"topic_direction": run["topic"], "trace": []}, run_id=run_id, db=db,
                   mode=run["mode"], error=f"{type(exc).__name__}: {exc}")
        raise HTTPException(500, f"{type(exc).__name__}: {exc}") from exc

    snap_step = ""
    if state.get("__interrupt__"):
        pkt = state["__interrupt__"][0].value or {}
        snap_step = pkt.get("step", "")
    return JSONResponse({"ok": True, "step": snap_step, "status": state.get("status", "")})


@app.post("/api/runs/{run_id}/select")
def api_select(run_id: str, payload: dict = Body(...), user: str = Depends(current_user)) -> JSONResponse:
    topic = str(payload.get("selected_topic", "")).strip()
    if not topic:
        raise HTTPException(400, "selected_topic 不能为空")
    return _resume_api(run_id, {"selected_topic": topic})


@app.post("/api/runs/{run_id}/review")
def api_review(run_id: str, payload: dict = Body(...), user: str = Depends(current_user)) -> JSONResponse:
    decision = str(payload.get("decision", "approve")).strip().lower()
    if decision not in {"approve", "reject"}:
        raise HTTPException(400, "decision 只能是 approve / reject")
    note = str(payload.get("note", "")).strip()
    return _resume_api(run_id, {"decision": decision, "feedback": note}, after_done=True)


@app.get("/api/runs/{run_id}/history")
def api_history(run_id: str, user: str = Depends(current_user)) -> JSONResponse:
    """时间旅行的入口：列出这条会话的每个检查点（新的在前）。"""
    run = _store().get_run(run_id)
    if run is None:
        raise HTTPException(404, "没有这条运行")
    db = run["db"] or str(_db_path())
    with session(run_id, db=db) as (app, tid):
        rows = graph_history(app, tid)
    return JSONResponse({"history": rows})


@app.post("/api/runs/{run_id}/rewind")
def api_rewind(run_id: str, payload: dict = Body(...), user: str = Depends(current_user)) -> JSONResponse:
    """回到指定检查点，从那里重新跑（打回重试 / 换选题重来都靠它）。"""
    checkpoint_id = str(payload.get("checkpoint_id", "")).strip()
    if not checkpoint_id:
        raise HTTPException(400, "checkpoint_id 不能为空")

    store = _store()
    run = store.get_run(run_id)
    if run is None:
        raise HTTPException(404, "没有这条运行")

    db = run["db"] or str(_db_path())
    before = usage_snapshot()
    try:
        with session(run_id, db=db) as (app, tid):
            state = graph_rewind(app, tid, checkpoint_id, str(payload.get("checkpoint_ns", "")))
        store.sync(state, run_id=run_id, db=db, mode=_model(),
                   usage=usage_delta(before, usage_snapshot()))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, f"{type(exc).__name__}: {exc}") from exc

    return JSONResponse({"ok": True, "status": store.get_run(run_id)["status"]})


# --------------------------------------------------------------------------- #
# SSE：边跑边推（只推节点级，token 级等 LLM 客户端接流式再说）
# --------------------------------------------------------------------------- #


def _sse(event_type: str, data: Any) -> str:
    payload = json.dumps({"type": event_type, "data": data}, ensure_ascii=False, default=str)
    return f"data: {payload}\n\n"


@app.get("/stream")
def stream_run(topic: str = "") -> StreamingResponse:
    """跑一条并推节点进度，停在「人选选题」。"""
    topic = topic.strip()
    if not topic:
        raise HTTPException(400, "topic 不能为空")

    store = _store()
    db = str(_db_path())
    run_id = f"run-{uuid.uuid4().hex[:8]}"

    def gen() -> Iterator[str]:
        """图在子线程里跑，token 通过队列回到这个生成器 —— 这样才是真流式。

        为什么要线程：`app.stream()` 是同步生成器，而节点的 `stream_complete()`
        也是同步迭代；直接在异步生成器里跑会卡住事件循环。
        """
        q: "queue.Queue[tuple[str, Any]]" = queue.Queue()
        yield _sse("init", {"run_id": run_id, "topic": topic, "model": _model()})

        def work() -> None:
            try:
                with session(run_id, db=db) as (app, tid):
                    config = config_for(tid)
                    with token_sink(lambda chunk: q.put(("llm_token", chunk))):
                        for chunk in app.stream(initial_input(topic), config, stream_mode="updates"):
                            for node, update in (chunk or {}).items():
                                if node == "__interrupt__":
                                    packet = getattr(update[0], "value", None) if update else None
                                    q.put(("interrupt", {"payload": packet}))
                                    continue
                                events = (update or {}).get("trace", []) if isinstance(update, dict) else []
                                q.put(("node", {"node": node, "trace": events}))
                    snap = app.get_state(config)
                    state = dict(snap.values or {})
                    if snap.interrupts:
                        state["__interrupt__"] = tuple(snap.interrupts)
                store.sync(state, run_id=run_id, db=db, mode=_model())
                row = store.get_run(run_id) or {}
                q.put(("done", {"run_id": run_id, "status": row.get("status", "unknown")}))
            except Exception as exc:  # noqa: BLE001
                store.sync({"topic_direction": topic, "trace": []}, run_id=run_id, db=db,
                           mode=_model(), error=f"{type(exc).__name__}: {exc}")
                q.put(("error", {"message": f"{type(exc).__name__}: {exc}"}))
            finally:
                q.put(("__end__", None))

        threading.Thread(target=work, daemon=True).start()

        while True:
            kind, data = q.get()
            if kind == "__end__":
                break
            yield _sse(kind, data)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )


@app.get("/stream/resume/{run_id}")
def stream_resume(run_id: str, action: str = "select", value: str = "", note: str = "") -> StreamingResponse:
    """流式推进下一步：选题或审稿。

    写稿那一步会把 LLM 的 token 逐个推给前端（`llm_token` 事件）——
    这是「token 级流式」，不是只推节点。
    """
    store = _store()
    run = store.get_run(run_id)
    if run is None:
        raise HTTPException(404, "没有这条运行")

    db = run["db"] or str(_db_path())
    if action == "select":
        answer: Any = {"selected_topic": value or "1"}
    else:
        answer = {"decision": value or "approve", "feedback": note}

    def gen() -> Iterator[str]:
        q: "queue.Queue[tuple[str, Any]]" = queue.Queue()
        yield _sse("init", {"run_id": run_id, "action": action})

        def work() -> None:
            try:
                with session(run_id, db=db) as (app, tid):
                    config = config_for(tid)
                    with token_sink(lambda chunk: q.put(("llm_token", chunk))):
                        for chunk in app.stream(Command(resume=answer), config, stream_mode="updates"):
                            for node, update in (chunk or {}).items():
                                if node == "__interrupt__":
                                    packet = getattr(update[0], "value", None) if update else None
                                    q.put(("interrupt", {"payload": packet}))
                                    continue
                                events = (update or {}).get("trace", []) if isinstance(update, dict) else []
                                q.put(("node", {"node": node, "trace": events}))
                    snap = app.get_state(config)
                    state = dict(snap.values or {})
                    if snap.interrupts:
                        state["__interrupt__"] = tuple(snap.interrupts)
                store.sync(state, run_id=run_id, db=db, mode=_model(),
                           usage=usage_delta(before_state, usage_snapshot()))
                if state.get("image_urls"):
                    write_markdown(state, _out_dir())
                    write_publish(state, _out_dir())
                row = store.get_run(run_id) or {}
                q.put(("done", {"status": row.get("status", "unknown")}))
            except Exception as exc:  # noqa: BLE001
                q.put(("error", {"message": f"{type(exc).__name__}: {exc}"}))
            finally:
                q.put(("__end__", None))

        before_state = usage_snapshot()
        threading.Thread(target=work, daemon=True).start()
        while True:
            kind, data = q.get()
            if kind == "__end__":
                break
            yield _sse(kind, data)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )


def serve(host: str = "127.0.0.1", port: int = 8000) -> int:
    try:
        import uvicorn
    except ModuleNotFoundError:
        print("[X] 需要 uvicorn：pip install -r requirements.txt")
        return 1
    uvicorn.run(app, host=host, port=port, log_level="info")
    return 0


def main(argv: list[str] | None = None) -> int:
    import argparse

    p = argparse.ArgumentParser(prog="contentops-web", description="ContentOps Agent 极简页面")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    a = p.parse_args(argv)
    return serve(a.host, a.port)


if __name__ == "__main__":
    raise SystemExit(main())
