"""知识库检索：按标题分块 + 关键词打分。

为什么不用 FAISS / sentence-transformers：
- 这台机器 Python 3.14，向量库的 wheel 大多还没跟上，装上就跑不起来；
- 阶段 1 的目标是「链路通」，不是「召回最准」；
- 纯 Python 的 BM25 式打分在几十篇文档的规模上够用，而且零依赖、零 GPU。

阶段 2 会把 `Retriever` 抽象成协议，再接向量检索做 A/B 对比 ——
那时「换检索策略后引用命中率从 X 到 Y」才是简历上能写的数据。
"""
from __future__ import annotations

import math
import re
from collections import Counter
from pathlib import Path

from .state import Hit

KB_DIR = Path(__file__).resolve().parents[2] / "docs" / "kb"

_TOKEN = re.compile(r"[\u4e00-\u9fff]|[a-zA-Z0-9_]+")


def tokenize(text: str) -> list[str]:
    """中文按字、英文按词切。够用且无需分词词典。"""
    return [t.lower() for t in _TOKEN.findall(text)]


def split_chunks(markdown: str) -> list[tuple[str, str]]:
    """按二级标题切块，保留标题作为元信息。

    比定长切分好在：引用时能告诉读者「这句出自哪一节」，
    草稿里加引用链接就有落脚点。没有标题的文档整篇当一个块。
    """
    chunks: list[tuple[str, str]] = []
    heading = ""
    buf: list[str] = []
    for line in markdown.splitlines():
        if line.startswith("## "):
            if buf:
                text = "\n".join(buf).strip()
                if text:
                    chunks.append((heading, text))
            heading = line[3:].strip()
            buf = []
        else:
            buf.append(line)
    if buf:
        text = "\n".join(buf).strip()
        if text:
            chunks.append((heading, text))
    return chunks or [("", markdown.strip())]


class Retriever:
    """内存里的知识库检索器，加载一次、查询多次。"""

    def __init__(self, kb_dir: Path | None = None) -> None:
        self.kb_dir = Path(kb_dir) if kb_dir else KB_DIR
        self._docs: list[Hit] = []
        self._df: Counter[str] = Counter()
        self._load()

    def _load(self) -> None:
        files = sorted(self.kb_dir.glob("*.md")) if self.kb_dir.exists() else []
        for f in files:
            for heading, text in split_chunks(f.read_text(encoding="utf-8")):
                toks = tokenize(heading + " " + text)
                self._docs.append(
                    Hit(doc=f.name, heading=heading, text=text, score=0.0)
                )
                for tok in set(toks):
                    self._df[tok] += 1

    @property
    def size(self) -> int:
        return len(self._docs)

    def search(self, query: str, top_k: int = 3) -> list[Hit]:
        """BM25 简化版：只保留 idf 加权 + 长度归一。"""
        n = max(len(self._docs), 1)
        q_toks = tokenize(query)
        scored: list[Hit] = []
        for doc in self._docs:
            toks = tokenize(doc["heading"] + " " + doc["text"])
            if not toks:
                continue
            tf = Counter(toks)
            score = 0.0
            for tok in q_toks:
                if tok not in tf:
                    continue
                idf = math.log((n + 1) / (self._df.get(tok, 0) + 0.5))
                score += (tf[tok] / len(toks)) * idf
            if score > 0:
                scored.append(Hit(**{**doc, "score": round(score, 6)}))
        scored.sort(key=lambda h: h["score"], reverse=True)
        return scored[:top_k]
