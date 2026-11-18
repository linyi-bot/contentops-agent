"""模型层：一个接口，两种实现。

为什么先写 mock：
1. 没有 API key 时项目也要能跑 —— 这是「clone 下来 3 条命令起得来」的前提；
2. 评测时 mock 是确定性基线，换真模型后指标变化才说明问题；
3. 单元测试不该花 token。

mock 不是「随便回一句话」：它会真的读检索结果里 `[n] 出自《文档》` 的编号，
把引用的段落截一句、并标上 `[n]`。这样「事实核查 → 引用落地」这条链路在
没有 API key 的机器上也能被真正走一遍，而不是永远返回「无引用」把节点跑成
死循环。

真实模型走 OpenAI 兼容协议（DeepSeek / 通义 / 智谱都兼容），
只用 httpx，不引入各家 SDK。
"""
from __future__ import annotations

import hashlib
import os
import re
import time
from typing import Protocol

import httpx

# 检索上下文里的资料头：`[2] 出自《内容运营基础》· 事实核查`
# 用 MULTILINE 在整个 user 串里找，而不是按空行分块后只看第一行 ——
# 第一块的开头是「资料：」，按块解析会把第 1 条资料整条丢掉（mock 会因此
# 永远引用第 2、3 条，评测里表现为「引用命中率虚高、事实一致性虚低」）。
_DOC_HEADER = re.compile(r"^\[(\d+)\] 出自《([^》]*)》(?:·\s*(.*))?$", re.MULTILINE)
_LEAD_NOISE = re.compile(r"^[\s\-*>#|]+")


class LLM(Protocol):
    name: str

    def complete(self, system: str, user: str, max_tokens: int = 800) -> str: ...


def _first_sentence(text: str, limit: int = 46) -> str:
    """从一段 markdown 里摘一句干净的话，够 mock 用即可。"""
    text = _LEAD_NOISE.sub("", " ".join(text.split()))
    for sep in "。！？；":
        head, found, _ = text.partition(sep)
        if found and 0 < len(head) <= limit * 2:
            text = head
            break
    return text[:limit].rstrip("，,、：:")


def _parse_context(user: str) -> list[tuple[int, str, str, str]]:
    """把 `_format_context()` 产出的资料块解析回（编号, 文档, 小节, 正文）。

    按「资料头」在全串里定位，两两之间就是正文 —— 这样第一条资料不会被
    前面的「资料：」那一行挡住。
    """
    matches = list(_DOC_HEADER.finditer(user))
    docs: list[tuple[int, str, str, str]] = []
    for i, m in enumerate(matches):
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(user)
        body = user[start:end].strip()
        if not body:
            continue
        docs.append((int(m.group(1)), m.group(2), (m.group(3) or "").strip(), body))
    return docs


class MockLLM:
    """确定性假模型：不需要 key，输出可预测，用于跑通链路和写测试。"""

    name = "mock"

    def complete(self, system: str, user: str, max_tokens: int = 800) -> str:
        # 用输入哈希保证「同样的输入得到同样的输出」，测试才能断言
        digest = hashlib.sha256((system + user).encode("utf-8")).hexdigest()[:8]
        time.sleep(0.01)  # 模拟一点耗时，让 trace 里的耗时字段有值

        docs = _parse_context(user)
        if docs:
            parts = []
            for i, (idx, doc, heading, body) in enumerate(docs[:2]):
                where = f"《{doc}》" + (f"· {heading}" if heading else "")
                snippet = _first_sentence(body)
                prefix = f"[mock:{digest}] " if i == 0 else ""
                parts.append(f"{prefix}据{where}，{snippet}[{idx}]。")
            return "".join(parts)

        return (
            f"[mock:{digest}] 这是一段占位草稿，用于验证链路是否打通。\n"
            f"输入长度 system={len(system)} user={len(user)}，"
            f"如果换成真模型，这里会是真实内容。"
        )


class OpenAICompatLLM:
    """OpenAI 兼容的 chat/completions 客户端（DeepSeek、通义、智谱通用）。"""

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.deepseek.com",
        model: str = "deepseek-chat",
        timeout: float = 60.0,
    ) -> None:
        self.name = model
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._timeout = timeout

    def complete(self, system: str, user: str, max_tokens: int = 800) -> str:
        resp = httpx.post(
            f"{self._base_url}/chat/completions",
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": self._model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "max_tokens": max_tokens,
                "temperature": 0.3,
            },
            timeout=self._timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        return data["choices"][0]["message"]["content"]


def build_llm() -> LLM:
    """按环境变量选模型；没有 key 就回落 mock，绝不因为缺 key 而崩。"""
    provider = os.getenv("LLM_PROVIDER", "mock").strip().lower()
    api_key = os.getenv("DEEPSEEK_API_KEY", "").strip()

    if provider in {"deepseek", "openai", "compat"} and api_key:
        return OpenAICompatLLM(
            api_key=api_key,
            base_url=os.getenv("LLM_BASE_URL", "https://api.deepseek.com"),
            model=os.getenv("LLM_MODEL", "deepseek-chat"),
        )
    return MockLLM()
