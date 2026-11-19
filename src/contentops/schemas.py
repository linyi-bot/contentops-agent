"""结构化输出用的 Schema（Pydantic）。

为什么不再「按编号行解析」：模型只要多写一句开场白、或者把「1.」改成「1、」，
解析器就得打补丁；而**约束它输出 JSON** 一次到位，坏格式直接在校验层炸掉，
不会带着脏数据往下游走。

课程里的做法是 `with_structured_output(schema)`；这里用等价但更可控的
**JSON Mode + 手动校验**：因为前者拿不到 token 统计（课程 P50 也踩过这个坑），
而我们需要 `node_metrics` 里的 token 数。
"""
from __future__ import annotations

from pydantic import BaseModel, Field, field_validator


class TopicList(BaseModel):
    """AI 给出的备选选题。"""

    topics: list[str] = Field(min_length=3, max_length=6, description="3-5 个选题标题")

    @field_validator("topics")
    @classmethod
    def _clean(cls, value: list[str]) -> list[str]:
        out = []
        for item in value:
            text = str(item).strip().strip("「」\"' ")
            # 过滤掉「1. 」这种模型自己加的序号
            for sep in ("、", ".", "．", ")", "）"):
                head = text.split(sep, 1)
                if len(head) == 2 and head[0].isdigit():
                    text = head[1].strip()
                    break
            if len(text) >= 2:
                out.append(text)
        if not out:
            raise ValueError("topics 全空")
        return out[:5]


class VisualList(BaseModel):
    """配图脚本：每张图一句话。"""

    points: list[str] = Field(min_length=1, max_length=6, description="3-5 个图点")

    @field_validator("points")
    @classmethod
    def _clean(cls, value: list[str]) -> list[str]:
        out = [str(v).strip() for v in value if str(v).strip()]
        if not out:
            raise ValueError("points 全空")
        return out[:5]
