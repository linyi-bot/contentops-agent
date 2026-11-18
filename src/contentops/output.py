"""发布记录的落盘（markdown）。

放在单独模块里，是因为它有两个调用方：命令行 `run --out` 和 Web 页面的「放行」。
节点本身不写盘 —— `publish_node` 只组装字段，谁决定落盘谁负责写，
这样单元测试里跑一条链路不会顺手在仓库里撒文件。
"""
from __future__ import annotations

import re
from pathlib import Path

from .state import ContentState

#: 文件名里允许中文，其余字符一律压成连字符
_SLUG_BAD = re.compile(r"[^\w\u4e00-\u9fff]+")


def slug_for(title: str) -> str:
    """选题 → 文件名。页面要用它拼发布稿的链接，所以单独暴露出来。"""
    return _SLUG_BAD.sub("-", title).strip("-")[:40] or "untitled"


def write_markdown(state: ContentState, out_dir: str | Path) -> Path:
    published = state.get("published") or {}
    title = str(published.get("title") or "untitled")
    slug = slug_for(title)

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

    directory = Path(out_dir)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{slug}.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path
