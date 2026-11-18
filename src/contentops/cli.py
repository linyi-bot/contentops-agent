"""命令行入口。

    python -m contentops.cli run --topic "用 AI 做周报自动化"

刻意不做 Web UI：阶段 1 只需要「一条命令能跑」，UI 是阶段 2 的事。
"""
from __future__ import annotations

import argparse
import json
import sys

from .graph import run
from .retriever import Retriever


def _cmd_run(args: argparse.Namespace) -> int:
    state = run(args.topic)
    print("=" * 60)
    print(f"选题：{state.get('topic')}")
    print("=" * 60)

    hits = state.get("hits", [])
    print(f"\n【检索】{len(hits)} 条")
    for i, h in enumerate(hits, 1):
        print(f"  [{i}] {h['doc']} · {h['heading']}  score={h['score']:.3f}")

    print(f"\n【草稿】\n{state.get('draft', '')}")

    print("\n【trace】")
    for ev in state.get("trace", []):
        print(f"  {ev['node']:<10} {ev['ms']:>5} ms  {ev['note']}")

    if args.json:
        print("\n【raw state】")
        print(json.dumps(state, ensure_ascii=False, indent=2))
    return 0


def _cmd_kb(args: argparse.Namespace) -> int:
    r = Retriever()
    print(f"知识库分块数：{r.size}")
    if args.query:
        for i, h in enumerate(r.search(args.query, top_k=args.top_k), 1):
            print(f"\n[{i}] {h['doc']} · {h['heading']}  score={h['score']:.3f}")
            print("   " + h["text"][:160].replace("\n", " "))
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="contentops", description="ContentOps Agent")
    sub = p.add_subparsers(dest="cmd", required=True)

    pr = sub.add_parser("run", help="跑一条完整流水线")
    pr.add_argument("--topic", required=True, help="选题")
    pr.add_argument("--json", action="store_true", help="打印完整 state")
    pr.set_defaults(func=_cmd_run)

    pk = sub.add_parser("kb", help="查看/检索知识库")
    pk.add_argument("--query", default="", help="检索词")
    pk.add_argument("--top-k", type=int, default=3)
    pk.set_defaults(func=_cmd_kb)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
