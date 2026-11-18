"""命令行入口。

    python -m contentops.cli run --topic "用 AI 做周报自动化"          # 会停在人工审核，等你敲 y/n
    python -m contentops.cli run --topic "..." --auto approve          # 不给 stdin 也能跑完
    python -m contentops.cli run --topic "..." --auto approve --out outputs

跨进程恢复（阶段 3 的 SQLite checkpointer）：

    :: 进程 A：停在审核就退出，返回码 75
    python -m contentops.cli run --topic "..." --db .contentops\\state.sqlite --thread-id demo --pause

    :: 进程 B：换一个终端/第二天，接着发布
    python -m contentops.cli resume --thread-id demo --db .contentops\\state.sqlite --decision approve --out outputs

刻意不做 Web UI：命令行把「检索 → 草稿 → 核查 → 人工审核 → 发布」全流程打印出来，
录屏讲项目时一屏就能讲清楚，比一个要装一堆前端依赖的页面更稳。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

from .graph import (
    config_for,
    interrupt_payload,
    is_interrupted,
    resume,
    session,
    start,
)
from .llm import build_llm, describe, usage_snapshot
from .retriever import Retriever
from .state import ContentState

LINE = "=" * 66

#: 「不是失败，是先停下等答复」。放在返回码里，脚本/测试才分得清
#: 75 = EX_TEMPFAIL（沿用 sysexits.h 的约定），比 0/1 更能表达「还可以接着跑」。
EXIT_PAUSED = 75


def _print_hits(state: ContentState) -> None:
    hits = state.get("hits", [])
    print(f"\n【检索】{len(hits)} 条")
    for i, h in enumerate(hits, 1):
        print(f"  [{i}] {h['doc']} · {h['heading']}  score={h['score']:.3f}")


def _print_draft(state: ContentState) -> None:
    print(f"\n【草稿 · 第 {state.get('revision', 0)} 版】\n{state.get('draft', '')}")


def _print_factcheck(state: ContentState) -> None:
    claims = state.get("claims", [])
    unchecked = state.get("unchecked", [])
    print(f"\n【事实核查】{len(claims)} 条断言，{len(unchecked)} 条未通过")
    for c in claims:
        mark = "OK " if c["supported"] else "!! "
        print(f"  {mark}{c['reason']}")
        print(f"     {c['text']}")


def _print_packet(packet: dict) -> None:
    print(f"\n{LINE}")
    print("【人工审核】图已暂停，等你放行")
    print(f"  选题：{packet['topic']}")
    print(f"  版本：第 {packet['revision']} 版")
    print(f"  草稿：{packet['draft']}")
    if packet["unchecked"]:
        print(f"  未核实断言（{len(packet['unchecked'])} 条）：")
        for u in packet["unchecked"]:
            print(f"    - {u}")
    else:
        print("  未核实断言：无")
    print(LINE)


def _ask(packet: dict, auto: str | None) -> dict:
    """拿到这一次审核的答复。"""
    if auto:
        return {"decision": auto, "note": "--auto 自动答复"}
    if not sys.stdin.isatty():
        return {"decision": "approve", "note": "非交互环境，默认放行（要拦请用 --auto reject 或跑测试）"}

    print("y 放行 / n 打回（可跟一句附言），回车=放行：")
    try:
        line = input("审核> ").strip()
    except (EOFError, KeyboardInterrupt):
        # 被管道/重定向进来时 isatty() 可能仍是 True，但一读就 EOF
        print("\n[!] 没有读到输入，按默认放行处理（要拦请用 --auto reject）")
        return {"decision": "approve", "note": "无可读输入，默认放行"}
    low = line.lower()
    if low in {"", "y", "yes", "ok", "放行", "通过"}:
        return {"decision": "approve", "note": ""}
    note = line[1:].strip(" ,，:：") if low.startswith("n") else line
    return {"decision": "reject", "note": note}


def _print_trace(state: ContentState) -> None:
    print("\n【trace】")
    for ev in state.get("trace", []):
        print(f"  {ev['node']:<10} {ev['ms']:>5} ms  {ev['note']}")


def _print_usage() -> None:
    """这一轮花了多少 token / 多少钱。mock 不调用模型，这行就不打。"""
    u = usage_snapshot()
    if not u.calls:
        return
    print(
        f"\n【成本】{u.calls} 次调用　输入 {u.prompt_tokens} tokens"
        f"（缓存命中 {u.cache_hit_tokens}）　输出 {u.completion_tokens} tokens"
        f"　估算 ¥{u.cost_cny:.4f}"
    )


def _print_model() -> None:
    d = describe()
    if d["real"]:
        thinking = "思考模式开" if d["thinking"] else "思考模式关"
        print(f"模型：{d['label']}（{thinking}，{d['base_url']}）")
    else:
        print(f"模型：mock（{d.get('note', '未配 key')}）—— 不花 token，数字只用来验证链路")


def _write_output(state: ContentState, out_dir: Path) -> Path:
    """把发布记录落成一篇 markdown —— 这一步有副作用，所以只放在 CLI 里。"""
    published = state.get("published") or {}
    title = str(published.get("title") or "untitled")
    slug = re.sub(r"[^\w\u4e00-\u9fff]+", "-", title).strip("-")[:40] or "untitled"

    lines = [f"# {title}", "", published.get("body", ""), "", "## 来源"]
    for s in published.get("sources", []):
        where = f"《{s['doc']}》" + (f" · {s['heading']}" if s["heading"] else "")
        lines.append(f"- [{s['index']}] {where}")
    if published.get("unchecked"):
        lines += ["", "## 未核实断言（发布时人工已知情）"]
        lines += [f"- {u}" for u in published["unchecked"]]
    lines += [
        "",
        "---",
        f"发布闸门：{published.get('gate')}　版本：第 {published.get('revision')} 版　"
        f"时间：{published.get('published_at')}",
    ]

    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{slug}.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def _db_arg(value: str) -> str:
    """`--db` 优先，其次 `CONTENTOPS_DB`；返回空串表示内存模式。"""
    return value or os.environ.get("CONTENTOPS_DB", "").strip()


def _pause_hint(tid: str, db: str) -> str:
    if not db:
        return (
            f"\n【已暂停】会话 {tid} 已停在人工审核（内存模式）\n"
            "  注意：没给 --db，状态只在内存里，这个进程一退就没了。\n"
            "  想跨进程接着发，加：--db .contentops/checkpoints.sqlite"
        )
    return "\n".join(
        [
            f"\n【已暂停】会话 {tid} 的状态已落盘，这个进程现在就可以退出",
            f"  状态库：{Path(db).resolve()}",
            "  换一个进程接着发：",
            f'    python -m contentops.cli resume --thread-id {tid} --db "{db}" --decision approve',
            '  （--decision reject --note "哪里不行" 则打回重写一版）',
        ]
    )


def _emit_result(state: ContentState, args: argparse.Namespace) -> int:
    """打印终局：发布 / 未发布。run 和 resume 共用。"""
    if state.get("published"):
        pub = state["published"]
        print(f"\n【发布】{pub['title']}　闸门：{pub['gate']}")
        for s in pub["sources"]:
            print(f"  来源 [{s['index']}] 《{s['doc']}》 · {s['heading']}")
        if args.out:
            path = _write_output(state, Path(args.out))
            print(f"\n已写出：{path}")
    else:
        print("\n【结束】人工打回且重写额度用尽，未发布 —— 这一版不算交付。")

    _print_trace(state)
    _print_usage()
    if args.json:
        print("\n【raw state】")
        print(json.dumps(state, ensure_ascii=False, indent=2, default=str))
    return 0


def _cmd_run(args: argparse.Namespace) -> int:
    db = _db_arg(args.db)
    with session(args.thread_id or None, db=db or None) as (app, tid):
        state = start(app, args.topic, tid)

        print(LINE)
        print(f"选题：{state.get('topic')}　会话：{tid}")
        _print_model()
        print(LINE)
        _print_hits(state)
        _print_draft(state)
        _print_factcheck(state)

        # --pause：跑到人工审核就停，不读 stdin —— 这是「进程 A」的用法
        if args.pause:
            if not is_interrupted(state):
                return _emit_result(state, args)
            _print_packet(interrupt_payload(state))
            print(_pause_hint(tid, db))
            _print_usage()
            return EXIT_PAUSED

        rounds = 0
        while is_interrupted(state) and rounds < 6:
            packet = interrupt_payload(state)
            _print_packet(packet)
            state = resume(app, _ask(packet, args.auto), tid)
            rounds += 1

        if is_interrupted(state):
            print("\n[!] 连打回 6 轮仍未收束，主动停下（检查路由或模型输出）")
            _print_trace(state)
            return 3

        return _emit_result(state, args)


def _cmd_resume(args: argparse.Namespace) -> int:
    """恢复一个停在人工审核的会话。可以跟暂停时不在同一个进程。"""
    db = _db_arg(args.db)
    with session(args.thread_id, db=db or None) as (app, tid):
        snap = app.get_state(config_for(tid))
        if not snap.values:
            print(f"[X] 找不到会话 {tid}：--db={db or '（没给，内存模式）'}")
            print("    --thread-id 和 --db 必须跟暂停那次一致；跨进程恢复不能省 --db。")
            return 2
        if not snap.interrupts:
            print(f"[!] 会话 {tid} 没有待审的草稿（已经跑完，或从没暂停过）。")
            return 2

        _print_packet(snap.interrupts[0].value)
        print(f"\n【恢复】会话 {tid} ← {args.decision}（附言：{args.note or '无'}）")

        state = resume(app, {"decision": args.decision, "note": args.note}, tid)

        if is_interrupted(state):
            print("\n【又停在审核】上一轮打回后已重写一版，还是等你答复。")
            _print_packet(interrupt_payload(state))
            print(_pause_hint(tid, db))
            _print_usage()
            return EXIT_PAUSED

        return _emit_result(state, args)


def _cmd_kb(args: argparse.Namespace) -> int:
    r = Retriever()
    print(f"知识库分块数：{r.size}")
    if args.query:
        for i, h in enumerate(r.search(args.query, top_k=args.top_k), 1):
            print(f"\n[{i}] {h['doc']} · {h['heading']}  score={h['score']:.3f}")
            print("   " + h["text"][:160].replace("\n", " "))
    return 0


def _cmd_llm_check(args: argparse.Namespace) -> int:
    """花几个 token 验证 key / 模型名 / 网络 —— 接真模型时第一件事就是跑它。"""
    d = describe()
    _print_model()
    print(f"  base_url：{d['base_url'] or '（mock，没有 base_url）'}")
    if not d["real"]:
        print("\n[!] 现在会走 mock：要接真模型，请在 .env 里设 LLM_PROVIDER=deepseek 并填 DEEPSEEK_API_KEY")
        return 1

    from .llm import LLMError

    try:
        llm = build_llm()
        result = llm.ping()  # type: ignore[attr-defined]
    except LLMError as exc:
        print(f"\n[X] 调用失败：{exc}")
        print("    401/403 → key 不对；400 → 模型名不在枚举里（现在只认 deepseek-flash / deepseek-v4-pro）")
        return 2

    print(f"\n[OK] 模型回了「{result['reply']}」，往返 {result['ms']} ms")
    _print_usage()
    return 0


def _cmd_eval(args: argparse.Namespace) -> int:
    from . import eval as E
    from .nodes import min_score as effective_min_score

    # 阈值在跑样本之前落地：nodes.min_score() 每次调用都会重读环境变量，
    # 所以「一条命令跑基线、一条命令跑加闸门」是可控的，不需要改代码。
    if args.min_score is not None:
        os.environ["RETRIEVAL_MIN_SCORE"] = str(args.min_score)

    cases = E.load_cases(Path(args.dataset))
    if args.limit:
        cases = cases[: args.limit]

    print(f"数据集 {args.dataset}：{len(cases)} 条（相关度阈值 {effective_min_score()}）")
    _print_model()
    report = E.evaluate(cases)

    for name, value in report["metrics"].items():
        print(f"  {name:<12} {value:>6}%")
    print(
        f"  通过 {report['counts']['passed']}/{report['counts']['cases']}"
        f"　平均 {report['latency_ms']['avg']} ms　P95 {report['latency_ms']['p95']} ms"
    )

    if args.out:
        path = E.write_report(report, Path(args.out), label=args.label)
        print(f"\n已写出：{path}")
    _print_usage()
    if args.json:
        print("\n【raw report】")
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))

    # 有失败样本就返回非零：CI 里能把「指标掉了」当红灯，而不是只打印一行看着好看的字
    return 0 if report["counts"]["passed"] == report["counts"]["cases"] else 2


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="contentops", description="ContentOps Agent")
    sub = p.add_subparsers(dest="cmd", required=True)

    pr = sub.add_parser("run", help="跑一条完整流水线（含人工审核）")
    pr.add_argument("--topic", required=True, help="选题")
    pr.add_argument(
        "--auto",
        choices=["approve", "reject"],
        default=None,
        help="自动答复人工审核（不给则交互式询问；非 tty 环境默认放行）",
    )
    pr.add_argument("--out", default="", help="把发布记录写成 markdown 到该目录")
    pr.add_argument("--json", action="store_true", help="打印完整 state")
    pr.add_argument("--thread-id", default="", help="会话 id（默认随机；跨进程恢复时两边要一致）")
    pr.add_argument(
        "--db",
        default="",
        help="checkpoint 落盘的 sqlite 路径；给了才能跨进程恢复（默认内存）",
    )
    pr.add_argument(
        "--pause",
        action="store_true",
        help="跑到人工审核就停，不读 stdin，返回码 75（给「进程 A 暂停 → 进程 B resume」用）",
    )
    pr.set_defaults(func=_cmd_run)

    pres = sub.add_parser("resume", help="恢复一个停在人工审核的会话（可跨进程）")
    pres.add_argument("--thread-id", required=True, help="暂停时的会话 id")
    pres.add_argument("--db", default="", help="暂停时用的 checkpoint sqlite 路径")
    pres.add_argument(
        "--decision",
        choices=["approve", "reject"],
        default="approve",
        help="放行 / 打回（打回后可再 resume 一次接着审）",
    )
    pres.add_argument("--note", default="", help="打回附言")
    pres.add_argument("--out", default="", help="放行后把发布记录写到该目录")
    pres.add_argument("--json", action="store_true", help="打印完整 state")
    pres.set_defaults(func=_cmd_resume)

    pk = sub.add_parser("kb", help="查看/检索知识库")
    pk.add_argument("--query", default="", help="检索词")
    pk.add_argument("--top-k", type=int, default=3)
    pk.set_defaults(func=_cmd_kb)

    pl = sub.add_parser("llm-check", help="验证 DeepSeek 的 key / 模型名 / 网络（只花几个 token）")
    pl.set_defaults(func=_cmd_llm_check)

    pe = sub.add_parser("eval", help="跑黄金集，出评测报告")
    pe.add_argument("--dataset", default=str(Path("evals") / "golden"), help="数据集目录或单个 jsonl")
    pe.add_argument("--out", default="", help="报告输出目录（留空只打印）")
    pe.add_argument(
        "--min-score",
        type=float,
        default=None,
        help="相关度阈值；0 = 关掉闸门跑基线对照（默认读 RETRIEVAL_MIN_SCORE，再不行用 0.13）",
    )
    pe.add_argument("--limit", type=int, default=0, help="只跑前 N 条（调试用）")
    pe.add_argument("--label", default="", help="报告文件名后缀，如 v0.3-gated（留空按阈值自动取）")
    pe.add_argument("--json", action="store_true", help="打印完整报告 JSON")
    pe.set_defaults(func=_cmd_eval)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
