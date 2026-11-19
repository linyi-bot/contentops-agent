"""成品落盘（markdown）—— 项目二版。

一篇稿子 = 标题 + 正文 + 图点 + 配图。命令行 `--out` 和页面「出图完成」都调这里，
节点自己不写盘（单元测试跑链路时不该在仓库里撒文件）。
"""
from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

from .state import ArticleState

#: 文件名里允许中文，其余字符一律压成连字符
_SLUG_BAD = re.compile(r"[^\w\u4e00-\u9fff]+")


def slug_for(title: str) -> str:
    """选题 → 文件名。页面要用它拼成品链接，所以单独暴露出来。"""
    return _SLUG_BAD.sub("-", title).strip("-")[:40] or "untitled"


def write_markdown(state: ArticleState, out_dir: str | Path) -> Path:
    """把一条跑完的运行写成 markdown。

    图点在前、配图在后：小红书是先定「每张图讲什么」再出图的，
    顺序反过来读者会以为图是随手配的。
    """
    title = str(state.get("selected_topic") or state.get("topic_direction") or "untitled")
    body = str(state.get("article_content") or "")
    points = list(state.get("visual_points") or [])
    urls = list(state.get("image_urls") or [])
    errors = list(state.get("image_errors") or [])

    lines = [f"# {title}", "", body, ""]

    if points:
        lines += ["## 配图脚本（图点）", ""]
        lines += [f"{i}. {p}" for i, p in enumerate(points, start=1)]
        lines.append("")

    if urls:
        lines += ["## 配图", ""]
        for i, u in enumerate(urls, start=1):
            shown = u if u.startswith("http") else f"（本地）{u}"
            lines.append(f"![图 {i}]({shown})")
        lines.append("")

    if errors:
        lines += ["## 出图失败（没有藏）", ""]
        lines += [f"- {e}" for e in errors]
        lines.append("")

    lines += [
        "---",
        f"版本：第 {state.get('revision')} 版　图点 {len(points)} 个　"
        f"出图 {len(urls)}/{len(points)}　导出时间：{datetime.now().isoformat(timespec='seconds')}",
    ]

    directory = Path(out_dir)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{slug_for(title)}.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def write_publish(state: ArticleState, out_dir: str | Path) -> list[Path]:
    """把双平台版本各落一份：`publish/xiaohongshu/<slug>.md` 与 `publish/wechat/<slug>.md`。

    没有 `published` 就不写（比如打回次数用尽的那条），免得留下空壳文件。
    """
    published = state.get("published") or {}
    if not published:
        return []

    root = Path(out_dir) / "publish"
    written: list[Path] = []
    for platform in ("xiaohongshu", "wechat"):
        payload = published.get(platform) or {}
        title = str(payload.get("title") or "untitled")
        directory = root / platform
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{slug_for(title)}.md"
        lines = [f"# {title}", "", str(payload.get("body") or "")]
        images = payload.get("images") or []
        if images:
            lines += ["", "## 配图"]
            lines += [f"{i}. {img}" for i, img in enumerate(images, start=1)]
        lines += ["", "---", f"平台：{platform}　导出：{published.get('published_at', '')}"]
        path.write_text("\n".join(lines), encoding="utf-8")
        written.append(path)
    return written
