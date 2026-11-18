"""命令行入口。

    python -m contentops.cli run --topic "用 AI 做周报自动化"          # 会停在人工审核，等你敲 y/n
    python -m contentops.cli run --topic "..." --auto approve          # 不给 stdin 也能跑完
    python -m contentops.cli run --topic "..." --auto approve --out outputs

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
    interrupt_payload,
    is_interrupted,
    new_session,
    resume,
    start,
)
from .retriever import Retriever
from .state import ContentState

LINE = "=" * 66


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


def _cmd_run(args: argparse.Namespace) -> int:
    app, tid = new_session()
    state = start(app, args.topic, tid)

    print(LINE)
    print(f"选题：{state.get('topic')}　会话：{tid}")
    print(LINE)
    _print_hits(state)
    _print_draft(state)
    _print_factcheck(state)

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
    if args.json:
        print("\n【raw state】")
        print(json.dumps(state, ensure_ascii=False, indent=2, default=str))
    return 0


def _cmd_kb(args: argparse.Namespace) -> int:
    r = Retriever()
    print(f"知识库分块数：{r.size}")
    if args.query:
        for i, h in enumerate(r.search(args.query, top_k=args.top_k), 1):
            print(f"\n[{i}] {h['doc']} · {h['heading']}  score={h['score']:.3f}")
            print("   " + h["text"][:160].replace("\n", " "))
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
    pr.set_defaults(func=_cmd_run)

    pk = sub.add_parser("kb", help="查看/检索知识库")
    pk.add_argument("--query", default="", help="检索词")
    pk.add_argument("--top-k", type=int, default=3)
    pk.set_defaults(func=_cmd_kb)

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
