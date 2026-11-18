"""模型层：一个接口，两种实现。

为什么先写 mock：
1. 没有 API key 时项目也要能跑 —— 这是「clone 下来 3 条命令起得来」的前提；
2. 评测时 mock 是确定性基线，换真模型后指标变化才说明问题；
3. 单元测试不该花 token。

真实模型走 OpenAI 兼容协议（DeepSeek / 通义 / 智谱都兼容），
只用 httpx，不引入各家 SDK。
"""
from __future__ import annotations

import hashlib
import os
import time
from typing import Protocol

import httpx


class LLM(Protocol):
    name: str

    def complete(self, system: str, user: str, max_tokens: int = 800) -> str: ...


class MockLLM:
    """确定性假模型：不需要 key，输出可预测，用于跑通链路和写测试。"""

    name = "mock"

    def complete(self, system: str, user: str, max_tokens: int = 800) -> str:
        # 用输入哈希保证「同样的输入得到同样的输出」，测试才能断言
        digest = hashlib.sha256((system + user).encode("utf-8")).hexdigest()[:8]
        time.sleep(0.01)  # 模拟一点耗时，让 trace 里的耗时字段有值
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
