"""极简页面：跑一条、看 trace、在浏览器里放行 / 打回。

刻意只做四件事（够用就行，不堆 UI —— 页面是用来证明链路和观测的，不是产品）：

1. 起一条运行：选题 → 检索 → 起草 → 核查，停在人工审核；
2. 列表：每条运行的状态 / 版本 / 事件数 / 成本 / 耗时；
3. 详情：trace 时间线 + 草稿 + 每条断言 + 发布记录；
4. 审核：放行 / 打回附言 —— 和命令行是**同一个** `interrupt()` 暂停点。

数据来源分两处，各司其职：
- 轨迹读 `trace_store`（sqlite 表），进程重启后还能翻历史；
- 草稿 / 断言 / 审核包读 checkpointer 里的图状态，不重复存一份正文。
"""
from __future__ import annotations

import html
import os
import uuid
from pathlib import Path
from typing import Any

from .checkpoint import resolve_db
from .graph import config_for, resume, session, start
from .llm import describe, usage_snapshot
from .output import slug_for, write_markdown
from .trace_store import TraceStore, usage_delta

try:
    from fastapi import FastAPI, Form, HTTPException
    from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
    from fastapi.staticfiles import StaticFiles
except ModuleNotFoundError as exc:  # 没装 fastapi 时给一句能照做的话
    raise SystemExit(
        "页面需要 fastapi + uvicorn：pip install -r requirements.txt\n"
        "（本机 pip 在沙箱里写不了临时文件时，用 python scripts/vendor_wheels.py fastapi==0.115.14 "
        "starlette==0.46.2 uvicorn==0.34.0 python-multipart）"
    ) from exc

#: 页面用的 checkpointer。和命令行默认库分开，免得 `serve` 顺手改了 CLI 的会话。
WEB_DB = Path(".contentops") / "web.sqlite"
#: 放行后 md 写到这里（容器里由 volume 映射出去）
OUT_DIR = Path("outputs")

CSS = """
:root{--bg:#0f1115;--card:#151922;--line:#262b35;--fg:#e8eaed;--dim:#8b93a1;
--ok:#3fb950;--warn:#d29922;--bad:#f85149;--accent:#4493f8}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
font:14px/1.6 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif}
main{max-width:960px;margin:0 auto;padding:28px 20px 64px}
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
overflow:auto;white-space:pre-wrap;word-break:break-word;font-size:12.5px;margin:0}
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
.meta{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:10px 18px;
font-size:13px}
.meta b{display:block;color:var(--dim);font-weight:500;font-size:12px;margin-bottom:2px}
.bar{height:6px;background:#222833;border-radius:3px;overflow:hidden;min-width:40px}
.bar>i{display:block;height:100%;background:var(--accent)}
.ok{color:var(--ok)}.bad{color:var(--bad)}.dim{color:var(--dim)}
.row{display:flex;gap:12px;align-items:center;flex-wrap:wrap}
output{font-size:13px;color:var(--dim)}
"""

BADGE = {"paused": ("warn", "等人工审核"), "published": ("ok", "已发布"),
         "ended": ("", "未发布"), "error": ("bad", "出错"), "running": ("", "运行中")}


def _db_path() -> Path:
    return resolve_db(os.environ.get("CONTENTOPS_WEB_DB") or str(WEB_DB))


def _out_dir() -> Path:
    return Path(os.environ.get("CONTENTOPS_OUT_DIR") or OUT_DIR)


def _store() -> TraceStore:
    return TraceStore()


def _model() -> str:
    d = describe()
    return str(d["label"])


def _page(title: str, body: str, *, sub: str = "") -> str:
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
            f'<td style="width:130px"><div class="bar"><i style="width:{width}%"></i></div></td>'
            f'<td>{html.escape(e["note"])}</td>'
            f'<td class="mono dim" style="white-space:nowrap">{e["ts"][11:]}</td></tr>'
        )
    total = sum(e["ms"] for e in events)
    return (
        "<table><tr><th>#</th><th>节点</th><th>耗时</th><th></th><th>说明</th><th>落库时间</th></tr>"
        + "".join(rows)
        + f'<tr><td></td><td class="dim">合计</td><td class="mono">{_fmt_ms(total)}</td>'
        f"<td></td><td></td><td></td></tr></table>"
    )


def _claims_table(claims: list[dict]) -> str:
    if not claims:
        return '<p class="dim">这一版没有可核查的断言。</p>'
    rows = []
    for c in claims:
        ok = bool(c.get("supported"))
        mark = '<span class="ok">✔</span>' if ok else '<span class="bad">✘</span>'
        cites = "、".join(f"[{i}]" for i in c.get("cites") or []) or "—"
        rows.append(
            f"<tr><td>{mark}</td><td>{html.escape(str(c.get('text', '')))}</td>"
            f'<td class="mono dim" style="white-space:nowrap">{html.escape(cites)}</td>'
            f"<td>{html.escape(str(c.get('reason', '')))}</td></tr>"
        )
    return (
        '<table><tr><th></th><th>断言</th><th style="white-space:nowrap">引用</th><th>判定理由</th></tr>'
        + "".join(rows)
        + "</table>"
    )


def _load(run: dict) -> tuple[dict, Any]:
    """从 checkpointer 里把这条运行的图状态读回来（草稿 / 断言 / 审核包）。"""
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
    """给容器健康检查用：不碰模型，只证明进程和两张表是活的。"""
    return JSONResponse({"ok": True, "runs": len(_store().list_runs(limit=500))})


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    runs = _store().list_runs()
    rows = []
    for r in runs:
        cost = f"¥{r['cost_cny']:.4f}" if r["cost_cny"] else '<span class="dim">—</span>'
        rows.append(
            f'<tr><td class="mono dim" style="white-space:nowrap">{r["updated_at"][5:16]}</td>'
            f'<td><a href="/runs/{html.escape(r["run_id"])}">{html.escape(r["topic"])}</a></td>'
            f'<td>{_badge(r["status"])}</td>'
            f'<td class="mono">第 {r["revision"]} 版</td>'
            f'<td class="mono">{r["event_count"]}</td>'
            f'<td class="mono">{html.escape(r["mode"] or "—")}</td>'
            f'<td class="mono">{cost}</td></tr>'
        )
    table = (
        "<table><tr><th>时间</th><th>选题</th><th>状态</th><th>版本</th>"
        "<th>事件</th><th>模型</th><th>成本</th></tr>" + "".join(rows) + "</table>"
        if rows
        else '<p class="dim">还没有运行记录。上面填个选题，跑第一条。</p>'
    )
    body = f"""
<h1>ContentOps Agent</h1>
<div class="sub">选题 → 检索 → 起草 → 事实核查 → 人工审核 → 发布　·
当前模型 <code>{html.escape(_model())}</code>　·　trace 落库在 <code>{html.escape(str(_db_path().parent))}</code></div>

<div class="card">
  <form class="inline" method="post" action="/runs">
    <input type="text" name="topic" placeholder="选题，例如：用 AI 做周报自动化" required>
    <button type="submit">跑一条</button>
  </form>
  <p class="sub" style="margin:10px 0 0">跑到「人工审核」就停下 —— 点进详情再放行或打回。</p>
</div>

<h2>全部运行</h2>
<div class="card">{table}</div>
"""
    return _page("全部运行", body, sub="runs")


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
    except Exception as exc:  # noqa: BLE001 - 出错也要落一行，页面才看得到原因
        store.sync({"topic": topic, "trace": []}, run_id=run_id, db=db,
                   mode=_model(), error=f"{type(exc).__name__}: {exc}")
        return RedirectResponse(f"/runs/{run_id}", status_code=303)

    store.sync(state, run_id=run_id, db=db, mode=_model(),
               usage=usage_delta(before, usage_snapshot()))
    return RedirectResponse(f"/runs/{run_id}", status_code=303)


@app.post("/runs/{run_id}/decision")
def decide(run_id: str, decision: str = Form("approve"), note: str = Form("")) -> RedirectResponse:
    store = _store()
    run = store.get_run(run_id)
    if run is None:
        raise HTTPException(404, "没有这条运行")
    if run["status"] != "paused":
        return RedirectResponse(f"/runs/{run_id}", status_code=303)

    db = run["db"] or str(_db_path())
    before = usage_snapshot()
    try:
        with session(run_id, db=db) as (app, tid):
            state = resume(app, {"decision": decision, "note": note.strip()}, tid)
    except Exception as exc:  # noqa: BLE001
        store.sync({"topic": run["topic"], "trace": []}, run_id=run_id, db=db,
                   mode=run["mode"], error=f"{type(exc).__name__}: {exc}")
        return RedirectResponse(f"/runs/{run_id}", status_code=303)

    store.sync(state, run_id=run_id, db=db, mode=_model(),
               usage=usage_delta(before, usage_snapshot()))
    if state.get("published"):
        write_markdown(state, _out_dir())
    return RedirectResponse(f"/runs/{run_id}", status_code=303)


@app.get("/runs/{run_id}", response_class=HTMLResponse)
def run_detail(run_id: str) -> str:
    store = _store()
    run = store.get_run(run_id)
    if run is None:
        raise HTTPException(404, "没有这条运行")
    events = store.events(run_id)
    state, packet = _load(run)
    topic = str(state.get("topic") or run["topic"])
    draft = str(state.get("draft") or "")
    claims = list(state.get("claims") or [])
    published = state.get("published") or {}

    total = sum(e["ms"] for e in events)
    cost = f"¥{run['cost_cny']:.4f}" if run["cost_cny"] else "—"
    meta = f"""
<div class="meta">
  <div><b>状态</b>{_badge(run["status"])}</div>
  <div><b>版本</b>第 {run["revision"]} 版</div>
  <div><b>模型</b><code>{html.escape(run["mode"] or "—")}</code></div>
  <div><b>调用 / 成本</b>{run["calls"]} 次 · {cost}</div>
  <div><b>trace 耗时</b>{_fmt_ms(total)}</div>
  <div><b>运行 id</b><code>{html.escape(run_id)}</code></div>
</div>"""

    parts = [
        f'<div class="row" style="margin-bottom:14px"><a href="/">← 全部运行</a></div>',
        f"<h1>{html.escape(topic)}</h1>",
        f'<div class="card">{meta}</div>',
    ]
    if run["error"]:
        parts.append(f'<h2>错误</h2><div class="card"><pre class="bad">{html.escape(run["error"])}</pre></div>')

    parts.append(f'<h2>trace · {len(events)} 条</h2><div class="card">{_trace_table(events)}</div>')

    if draft:
        parts.append(f'<h2>草稿 · 第 {state.get("revision", run["revision"])} 版</h2>'
                     f'<div class="card"><pre>{html.escape(draft)}</pre></div>')
        parts.append(f"<h2>事实核查 · {len(claims)} 条断言，"
                     f"{len(state.get('unchecked') or [])} 条未通过</h2>"
                     f'<div class="card">{_claims_table(claims)}</div>')

    if run["status"] == "paused" and isinstance(packet, dict):
        unchecked = packet.get("unchecked") or []
        warn = (f'<p class="bad">有 {len(unchecked)} 条未核实断言，放行会被记成 forced 闸门。</p>'
                if unchecked else '<p class="ok">断言全部通过。</p>')
        parts.append(
            '<h2>人工审核 · 图停在这里</h2><div class="card">'
            + warn
            + '<form method="post" action="/runs/' + html.escape(run_id) + '/decision">'
            '<textarea name="note" placeholder="打回附言（放行可留空）"></textarea>'
            '<div class="row" style="margin-top:10px">'
            '<button type="submit" name="decision" value="approve">放行并发布</button>'
            '<button type="submit" name="decision" value="reject" class="danger">打回重写</button>'
            "</div></form></div>"
        )

    if published:
        gate = html.escape(str(published.get("gate", "")))
        sources = "".join(
            f'<li>[{s["index"]}] 《{html.escape(s["doc"])}》'
            + (f' · {html.escape(s["heading"])}' if s["heading"] else "") + "</li>"
            for s in published.get("sources", [])
        )
        saved = _out_dir() / f"{slug_for(topic)}.md"
        link = (f'<p><a href="/outputs/{html.escape(saved.name)}">打开发布稿 markdown ↗</a></p>'
                if saved.exists() else "")
        parts.append(
            f'<h2>发布记录</h2><div class="card"><p>闸门：<code>{gate}</code>　'
            f'版本：第 {published.get("revision")} 版　时间：{html.escape(str(published.get("published_at", "")))}</p>'
            f"<ul>{sources}</ul>{link}</div>"
        )
    elif run["status"] == "ended":
        parts.append('<h2>结果</h2><div class="card"><p class="dim">'
                     "人工打回且重写额度用尽，未发布 —— 这一版不算交付。</p></div>")

    return _page(topic, "".join(parts), sub=run_id)


#: 发布稿直接静态暴露，省一个「读文件」的接口（目录不存在时返回 404，不影响启动）
app.mount("/outputs", StaticFiles(directory=str(_out_dir()), check_dir=False), name="outputs")


def serve(host: str = "127.0.0.1", port: int = 8000) -> int:
    """`contentops serve` 的实现。uvicorn 是可选依赖，缺了给一句人话。"""
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
