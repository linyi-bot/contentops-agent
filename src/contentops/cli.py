"""命令行入口 —— 项目二（自媒体运营 AI 智能体）版。

一条链路两次人机接力：

    contentops run --topic "AI 编程入门" --pause
        → 出 5 个选题，停在「人选」（返回码 75）
    contentops resume --thread-id <tid> --select 2 --db ...
        → 写稿，停在「人审」（返回码 75）
    contentops resume --thread-id <tid> --decision approve --db ...
        → 提炼图点 → 并行出图 → 收工（返回码 0）

不想手动就加 `--auto approve`：自动选第 1 个选题、自动放行，一路跑到底。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from . import __version__
from .graph import (
    config_for,
    interrupt_payload,
    is_interrupted,
    resume,
    session,
    start,
)
from .llm import build_llm, describe, usage_snapshot
from .output import write_markdown
from .state import ArticleState
from .trace_store import TraceStore, usage_delta

LINE = "=" * 66
EXIT_PAUSED = 75  # 和 argparse / sysexits 的 EX_TEMPFAIL 对齐：不是失败，是「还在等」


# --------------------------------------------------------------------------- #
# 打印
# --------------------------------------------------------------------------- #


def _print_model() -> None:
    d = describe()
    note = "（没设 LLM_PROVIDER=deepseek）—— 不花 token，数字只用来验证链路" if d["provider"] == "mock" else ""
    print(f"模型：{d['label']}{note}")


def _print_topics(state: ArticleState) -> None:
    topics = list(state.get("generated_topics") or [])
    print(f"\n【选题】{len(topics)} 个")
    for i, t in enumerate(topics, start=1):
        print(f"  {i}. {t}")


def _print_draft(state: ArticleState) -> None:
    body = state.get("article_content") or ""
    print(f"\n【草稿 · 第 {state.get('revision')} 版】{len(body)} 字")
    print(body)


def _print_result(state: ArticleState, *, out: str = "") -> None:
    print(LINE)
    print(f"【完成】{state.get('selected_topic')}")
    print(f"  正文：{len(state.get('article_content') or '')} 字　"
          f"图点 {len(state.get('visual_points') or [])} 个　"
          f"出图 {len(state.get('image_urls') or [])} 张")
    for i, u in enumerate(state.get("image_urls") or [], start=1):
        print(f"  图 {i}: {u}")
    for e in state.get("image_errors") or []:
        print(f"  [!] {e}")
    published = state.get("published") or {}
    if published:
        print("  已生成两个平台版本：" + "、".join(
            f"{k}（{len(str(published[k].get('body') or ''))} 字）"
            for k in ("xiaohongshu", "wechat") if k in published
        ))
    if out:
        from .output import write_publish

        path = write_markdown(state, Path(out))
        files = write_publish(state, Path(out))
        print(f"\n已写出：{path}")
        for f in files:
            print(f"  {f}")
    print(LINE)


def _print_trace(state: ArticleState) -> None:
    events = state.get("trace") or []
    if not events:
        return
    print("\n【trace】")
    for e in events:
        print(f"  {e['node']:<15}{e['ms']:>5} ms  {e['note']}")


def _print_usage() -> None:
    u = usage_snapshot()
    if not u.calls:
        return
    print(f"\n【用量】调用 {u.calls} 次　输入 {u.prompt_tokens} / 输出 {u.completion_tokens} tokens"
          f"　估算 ¥{u.cost_cny:.4f}")


# --------------------------------------------------------------------------- #
# trace 落库（旁路，失败不影响主流程）
# --------------------------------------------------------------------------- #


def _sync_trace(state: ArticleState, *, run_id: str, db: str, usage=None) -> None:
    try:
        TraceStore().sync(state, run_id=run_id, db=db, mode=describe()["label"], usage=usage)
    except Exception as exc:  # noqa: BLE001
        print(f"\n[!] trace 落库失败（不影响本次运行）：{type(exc).__name__}: {exc}")


def _db_arg(value: str) -> str:
    return value or os.environ.get("CONTENTOPS_DB", "").strip()


def _pg_arg(value: str) -> str:
    return (value or os.environ.get("CONTENTOPS_PG_URI", "")).strip()


def _pause_hint(tid: str, db: str, pg: str, *, step: str) -> str:
    """停住时给人看的一句话：下一步该敲什么。"""
    where = f"--pg \"{pg}\"" if pg else (f'--db "{db}"' if db else "")
    state_note = "状态已写进 PostgreSQL" if pg else ("状态已落盘" if db else "状态只在内存里，这个进程一退就没了")
    if step == "select_topic":
        nxt = f"contentops resume --thread-id {tid} {where} --select 2"
        what = "选一个选题"
    else:
        nxt = (f"contentops resume --thread-id {tid} {where} --decision approve\n"
               f"    （打回重写：把 approve 换成 reject，再加 --note \"哪里不行\"）")
        what = "放行或打回"
    return (
        f"\n【已暂停】{state_note}，等你{what}\n"
        f"  会话：{tid}\n"
        f"  下一步：{nxt}"
    )


# --------------------------------------------------------------------------- #
# 交互：终端里能问就问，否则用默认值
# --------------------------------------------------------------------------- #


def _is_tty() -> bool:
    try:
        return sys.stdin.isatty()
    except Exception:  # noqa: BLE001
        return False


def _ask_select(packet: dict, auto: str | None) -> str:
    if auto:
        return "1"
    if _is_tty():
        raw = input("\n选第几个（回车=1）：").strip()
        return raw or "1"
    return "1"


def _ask_review(packet: dict, auto: str | None) -> dict:
    if auto:
        return {"decision": auto, "feedback": ""}
    if _is_tty():
        raw = input("\n放行(y) / 打回(n)？").strip().lower()
        decision = "approve" if raw in {"y", "yes", "1", ""} else "reject"
        note = ""
        if decision == "reject":
            note = input("打回意见：").strip()
        return {"decision": decision, "feedback": note}
    return {"decision": "approve", "feedback": ""}


# --------------------------------------------------------------------------- #
# 两个子命令
# --------------------------------------------------------------------------- #


def _cmd_run(args: argparse.Namespace) -> int:
    db = _db_arg(args.db)
    pg = _pg_arg(args.pg)
    u0 = usage_snapshot()
    with session(args.thread_id or None, db=db or None, pg=pg or None) as (app, tid):
        state = start(app, args.topic, tid, platform=args.platform)
        _sync_trace(state, run_id=tid, db=db, usage=usage_delta(u0, usage_snapshot()))

        print(LINE)
        print(f"方向：{state.get('topic_direction')}　会话：{tid}")
        _print_model()
        print(LINE)
        _print_topics(state)

        rounds = 0
        while is_interrupted(state) and rounds < 6:
            packet = interrupt_payload(state) or {}
            step = packet.get("step", "")

            # --pause：停在第一个中断点，交给下一条命令 —— 这是「进程 A」的用法
            if args.pause and rounds == 0:
                print(_pause_hint(tid, db, pg, step=step))
                _print_usage()
                return EXIT_PAUSED

            if step == "select_topic":
                choice = args.select or _ask_select(packet, args.auto)
                u0 = usage_snapshot()
                state = resume(app, {"selected_topic": choice}, tid)
            else:
                answer = {"decision": args.decision, "feedback": args.note} if args.decision \
                    else _ask_review(packet, args.auto)
                u0 = usage_snapshot()
                state = resume(app, answer, tid)

            _sync_trace(state, run_id=tid, db=db, usage=usage_delta(u0, usage_snapshot()))
            rounds += 1

            if is_interrupted(state):
                pkt = interrupt_payload(state) or {}
                if pkt.get("step") == "review":
                    _print_draft(state)
                    if args.pause:
                        print(_pause_hint(tid, db, pg, step="review"))
                        _print_usage()
                        return EXIT_PAUSED

        if is_interrupted(state):
            print("\n[!] 连打回 6 轮仍未收束，主动停下（检查 MAX_REVISION 或模型输出）")
            _print_trace(state)
            return 3

        _print_result(state, out=args.out)
        _print_trace(state)
        _print_usage()
        if args.json:
            print("\n【raw state】")
            print(json.dumps(state, ensure_ascii=False, indent=2, default=str))
        return 0


def _cmd_resume(args: argparse.Namespace) -> int:
    db = _db_arg(args.db)
    pg = _pg_arg(args.pg)
    with session(args.thread_id, db=db or None, pg=pg or None) as (app, tid):
        snap = app.get_state(config_for(tid))
        if not snap.values:
            print(f"[X] 找不到会话 {tid}：--db={db or '（没给）'}  --pg={pg or '（没给）'}")
            print("    --thread-id 和后端参数必须跟暂停那次一致。")
            return 2
        if not snap.interrupts:
            print(f"[!] 会话 {tid} 没有待处理的步骤（已经跑完，或从没暂停过）。")
            return 2

        packet = snap.interrupts[0].value or {}
        step = packet.get("step", "")

        if step == "select_topic":
            options = packet.get("options") or []
            print(f"\n【待选】{len(options)} 个选题")
            for i, t in enumerate(options, start=1):
                print(f"  {i}. {t}")
            choice = args.select or "1"
            print(f"\n【恢复】会话 {tid} ← 选中「{choice}」")
            u0 = usage_snapshot()
            state = resume(app, {"selected_topic": choice}, tid)
        else:
            print(f"\n【待审】第 {packet.get('revision')} 版，{len(packet.get('draft') or '')} 字")
            print((packet.get("draft") or "")[:400])
            decision = args.decision or "approve"
            print(f"\n【恢复】会话 {tid} ← {decision}（意见：{args.note or '无'}）")
            u0 = usage_snapshot()
            state = resume(app, {"decision": decision, "feedback": args.note}, tid)

        _sync_trace(state, run_id=tid, db=db, usage=usage_delta(u0, usage_snapshot()))

        if is_interrupted(state):
            nxt = interrupt_payload(state) or {}
            nxt_step = nxt.get("step", "")
            if nxt_step == "review":
                _print_draft(state)
            print(_pause_hint(tid, db, pg, step=nxt_step))
            _print_usage()
            return EXIT_PAUSED

        _print_result(state, out=args.out)
        _print_trace(state)
        _print_usage()
        if args.json:
            print("\n【raw state】")
            print(json.dumps(state, ensure_ascii=False, indent=2, default=str))
        return 0


# --------------------------------------------------------------------------- #
# 辅助子命令
# --------------------------------------------------------------------------- #


def _cmd_kb(args: argparse.Namespace) -> int:
    from .retriever import Retriever

    r = Retriever()
    hits = r.search(args.query, top_k=args.top_k) if args.query else []
    if not args.query:
        print(f"知识库目录：{r.kb_dir}　文档 {len(r.docs)} 篇")
        return 0
    print(f"「{args.query}」命中 {len(hits)} 条")
    for h in hits:
        print(f"  {h['score']:.3f}  《{h['doc']}》 {h['heading']}")
    return 0


def _cmd_llm_check(args: argparse.Namespace) -> int:
    print(LINE)
    d = describe()
    print(f"模型：{d['label']}（{d.get('model')}）")
    try:
        llm = build_llm()
        text = llm.complete("你是测试助手。", "只回两个字：收到", max_tokens=10)
        print(f"[OK] 模型回了「{text.strip()[:20]}」")
    except Exception as exc:  # noqa: BLE001
        print(f"[X] 调用失败：{type(exc).__name__}: {exc}")
        return 1
    u = usage_snapshot()
    print(f"【用量】{u.calls} 次调用　输入 {u.prompt_tokens} / 输出 {u.completion_tokens} tokens"
          f"　估算 ¥{u.cost_cny:.4f}")
    return 0


def _cmd_serve(args: argparse.Namespace) -> int:
    from . import web

    print(f"页面：http://{args.host}:{args.port}")
    return web.serve(host=args.host, port=args.port)


def _cmd_history(args: argparse.Namespace) -> int:
    """列出这条会话的所有检查点 —— 每个都能拿去 rewind。"""
    from .graph import history as graph_history

    db = _db_arg(args.db)
    pg = _pg_arg(args.pg)
    with session(args.thread_id, db=db or None, pg=pg or None) as (app, tid):
        rows = graph_history(app, tid)
    print(f"会话 {tid} 共 {len(rows)} 个检查点（新的在前）")
    for r in rows:
        mark = "⟳" if r["interrupt"] else " "
        print(f"  {mark} step={r['step']:>2}  第{r['revision']}版  "
              f"{'有稿' if r['has_draft'] else '无稿'}  图{r['images']}  "
              f"{r['checkpoint_id'][:8]}  next={r['next']}")
    return 0


def _cmd_rewind(args: argparse.Namespace) -> int:
    """回到某个检查点重跑（时间旅行）。"""
    from .graph import rewind as graph_rewind

    db = _db_arg(args.db)
    pg = _pg_arg(args.pg)
    with session(args.thread_id, db=db or None, pg=pg or None) as (app, tid):
        state = graph_rewind(app, tid, args.checkpoint, args.ns)
        if is_interrupted(state):
            pkt = interrupt_payload(state) or {}
            print(_pause_hint(tid, db, pg, step=pkt.get("step", "")))
            return EXIT_PAUSED
    _print_result(state, out=args.out)
    return 0


def _cmd_eval(args: argparse.Namespace) -> int:
    print("[!] 旧的黄金集评测跟的是「核查+发布」流程；换成项目二流程后正在重写。")
    print("    当前可用的验证方式：pytest（含真起两个进程的跨进程恢复）。")
    return 2


# --------------------------------------------------------------------------- #
# 参数
# --------------------------------------------------------------------------- #


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="contentops", description="ContentOps Agent（自媒体运营流水线）")
    p.add_argument("--version", action="version", version=f"contentops {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    pr = sub.add_parser("run", help="跑一条：出选题 → 停下等人选")
    pr.add_argument("--topic", required=True, help="选题方向，如「AI 编程入门」")
    pr.add_argument("--platform", default="xiaohongshu", choices=["xiaohongshu", "wechat"])
    pr.add_argument("--select", default="", help="直接选第几个选题（1 基）；不给则停住等人")
    pr.add_argument("--decision", default="", choices=["", "approve", "reject"], help="审稿结论")
    pr.add_argument("--note", default="", help="打回意见")
    pr.add_argument("--auto", default="", choices=["", "approve", "reject"], help="全自动：选第 1 个并自动审")
    pr.add_argument("--pause", action="store_true", help="停在第一个中断点（返回码 75）")
    pr.add_argument("--out", default="", help="跑完后把成品写成 markdown 到该目录")
    pr.add_argument("--json", action="store_true", help="打印完整 state")
    pr.add_argument("--thread-id", default="", help="会话 id（默认随机）")
    pr.add_argument("--db", default="", help="SQLite checkpointer 路径")
    pr.add_argument("--pg", default="", help="PostgreSQL 连接串或库名（如 contentops）")
    pr.set_defaults(func=_cmd_run)

    pres = sub.add_parser("resume", help="接着跑：选题 / 审稿")
    pres.add_argument("--thread-id", required=True)
    pres.add_argument("--select", default="", help="选第几个（1 基）")
    pres.add_argument("--decision", default="", choices=["", "approve", "reject"])
    pres.add_argument("--note", default="", help="打回意见")
    pres.add_argument("--out", default="")
    pres.add_argument("--json", action="store_true")
    pres.add_argument("--db", default="")
    pres.add_argument("--pg", default="")
    pres.set_defaults(func=_cmd_resume)

    pk = sub.add_parser("kb", help="看看知识库（沿用旧的检索器）")
    pk.add_argument("--query", default="")
    pk.add_argument("--top-k", type=int, default=3)
    pk.set_defaults(func=_cmd_kb)

    pl = sub.add_parser("llm-check", help="验证模型 key / 网络（只花几个 token）")
    pl.set_defaults(func=_cmd_llm_check)

    pe = sub.add_parser("eval", help="（旧流程的评测，正在重写）")
    pe.set_defaults(func=_cmd_eval)

    pv = sub.add_parser("serve", help="起页面：看运行 / 选选题 / 审稿 / 出图")
    pv.add_argument("--host", default="127.0.0.1")
    pv.add_argument("--port", type=int, default=8000)
    pv.set_defaults(func=_cmd_serve)

    ph = sub.add_parser("history", help="列出会话的所有检查点（时间旅行的入口）")
    ph.add_argument("--thread-id", required=True)
    ph.add_argument("--db", default="")
    ph.add_argument("--pg", default="")
    ph.set_defaults(func=_cmd_history)

    pw = sub.add_parser("rewind", help="回到某个检查点重跑（时间旅行）")
    pw.add_argument("--thread-id", required=True)
    pw.add_argument("--checkpoint", required=True)
    pw.add_argument("--ns", default="", help="检查点命名空间（history 里能看到，一般是空）")
    pw.add_argument("--out", default="")
    pw.add_argument("--db", default="")
    pw.add_argument("--pg", default="")
    pw.set_defaults(func=_cmd_rewind)

    args = p.parse_args(argv)
    try:
        return args.func(args)
    except RuntimeError as exc:
        print(f"[X] {exc}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
