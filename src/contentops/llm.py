"""模型层：一个接口，两种实现。

为什么保留 mock：
1. 没有 API key 时项目也要能跑 —— 这是「clone 下来 3 条命令起得来」的前提；
2. 评测时 mock 是确定性基线，换真模型后指标变化才说明问题；
3. 单元测试不该花 token。

mock 不是「随便回一句话」：它会真的读检索结果里 `[n] 出自《文档》` 的编号，
把引用的段落截一句、并标上 `[n]`。这样「事实核查 → 引用落地」这条链路在
没有 API key 的机器上也能被真正走一遍，而不是永远返回「无引用」把节点跑成
死循环。

真实模型走 OpenAI 兼容协议（DeepSeek），只用 httpx，不引入各家 SDK。

2026-11 接真模型时踩到/查到的三件事，都写进这里了：

1. **模型 ID 是枚举，不是随便填**。官方当前只认 `deepseek-flash` 和
   `deepseek-v4-pro`；以前常见的 `deepseek-chat` 已经不在枚举里，硬编会 400。
2. **思考模式默认是开的**。草稿这种活不需要思维链，开着只是多花输出 token，
   所以默认 `thinking=disabled`，要开就用 `LLM_THINKING=on`。
3. **价格分空闲/高峰两档**（北京时间工作日 9-12、14-18 是高峰，其余半价），
   所以成本不能写死一个数，要按调用时刻取价。

记账口径：`prompt_cache_hit_tokens` / `prompt_cache_miss_tokens` 分开记，
因为缓存命中的单价差 50 倍；只报一个「总 token」会把成本算高一个数量级。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Protocol

import httpx

# 检索上下文里的资料头：`[2] 出自《内容运营基础》· 事实核查`
# 用 MULTILINE 在整个 user 串里找，而不是按空行分块后只看第一行 ——
# 第一块的开头是「资料：」，按块解析会把第 1 条资料整条丢掉（mock 会因此
# 永远引用第 2、3 条，评测里表现为「引用命中率虚高、事实一致性虚低」）。
_DOC_HEADER = re.compile(r"^\[(\d+)\] 出自《([^》]*)》(?:·\s*(.*))?$", re.MULTILINE)
_LEAD_NOISE = re.compile(r"^[\s\-*>#|]+")

#: 官方枚举里的默认型号。改名前先看 pricing 页，别凭记忆写 deepseek-chat。
DEFAULT_MODEL = "deepseek-flash"
DEFAULT_BASE_URL = "https://api.deepseek.com"

#: 官方价（元 / 百万 tokens），键是模型名，值是 (空闲时段, 高峰时段)。
#: 来源：https://api-docs.deepseek.com/zh-cn/quick_start/pricing （2026-11 抄录）
#: 价格会变，所以允许用环境变量覆盖，见 `price_for()`。
PRICES: dict[str, dict[str, tuple[float, float]]] = {
    "deepseek-flash": {"hit": (0.02, 0.04), "miss": (1.0, 2.0), "out": (4.0, 8.0)},
    "deepseek-v4-pro": {"hit": (0.15, 0.30), "miss": (4.5, 9.0), "out": (13.5, 27.0)},
}

_CST = timezone(timedelta(hours=8))

#: 可重试的状态码：限流和服务端抽风，等一会儿再来大概率就好了。
#: 400/401/403/404/422 是「请求本身不对」，重试只是把同一个错误再犯一遍。
RETRYABLE_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}


class LLMError(RuntimeError):
    """模型调用失败。带上人话原因，别让调用方去猜 HTTP 码。"""


# --------------------------------------------------------------------------- #
# .env：没有它，README 里「填 .env 再跑」是句空话
# --------------------------------------------------------------------------- #

#: 项目根目录（src/contentops/llm.py 往上三层）
PROJECT_ROOT = Path(__file__).resolve().parents[2]

_dotenv_loaded = False


def parse_dotenv(text: str) -> dict[str, str]:
    """解析 `KEY=VALUE` 行。

    不引 python-dotenv：为了读十行配置多一个依赖不划算。
    语义跟 docker compose 对齐：`#` 开头是注释、允许 `export `、值可以带引号，
    行尾的「空格 + #」当注释切掉（key 里不会出现空格+#，这样切是安全的）。
    """
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip().lstrip("\ufeff")  # 防 BOM 混进第一行的 key
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if not key:
            continue
        if len(value) >= 2 and value[0] in {'"', "'"} and value[-1] == value[0]:
            value = value[1:-1]
        else:
            value = re.sub(r"\s+#.*$", "", value).strip()
        out[key] = value
    return out


def load_dotenv(path: Path | str | None = None, *, force: bool = False) -> int:
    """把 `.env` 读进 `os.environ`，**已存在的变量一律不覆盖**。

    为什么必须「不覆盖」：CI 里靠真环境变量注入、docker 里靠 compose 注入，
    如果 .env 能盖掉它们，那台机器上就永远调不到正确的配置。
    返回真正补进去的条数（0 表示没读或全都已存在）。
    """
    global _dotenv_loaded
    if _dotenv_loaded and not force:
        return 0
    _dotenv_loaded = True
    if os.getenv("CONTENTOPS_NO_DOTENV", "").strip():
        return 0  # 测试里关掉：本地填了 key 也不该让单测真的去调模型

    candidates: list[Path] = []
    if path is not None:
        candidates.append(Path(path))
    else:
        candidates += [PROJECT_ROOT / ".env", Path.cwd() / ".env"]

    added = 0
    seen: set[Path] = set()
    for candidate in candidates:
        candidate = candidate.resolve()
        if candidate in seen or not candidate.is_file():
            continue
        seen.add(candidate)
        # utf-8-sig：Windows 记事本 / PowerShell 的 Set-Content 默认会写 BOM，
        # 用 utf-8 读会把首行的 key 读成 "\ufeffLLM_PROVIDER"，然后静默失效。
        raw = candidate.read_text(encoding="utf-8-sig")
        for key, value in parse_dotenv(raw).items():
            if key not in os.environ:
                os.environ[key] = value
                added += 1
    return added


# --------------------------------------------------------------------------- #
# 记账：token、成本、延迟
# --------------------------------------------------------------------------- #


@dataclass
class Usage:
    calls: int = 0
    errors: int = 0
    prompt_tokens: int = 0
    cache_hit_tokens: int = 0
    cache_miss_tokens: int = 0
    completion_tokens: int = 0
    cost_cny: float = 0.0
    latency_ms: int = 0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def as_dict(self) -> dict[str, Any]:
        out = asdict(self)
        out["total_tokens"] = self.total_tokens
        return out


_USAGE = Usage()
_USAGE_LOCK = threading.Lock()


def usage_snapshot() -> Usage:
    """当前累计用量的一份拷贝 —— 调用方拿去算差量，别拿去改。"""
    with _USAGE_LOCK:
        return Usage(
            calls=_USAGE.calls,
            errors=_USAGE.errors,
            prompt_tokens=_USAGE.prompt_tokens,
            cache_hit_tokens=_USAGE.cache_hit_tokens,
            cache_miss_tokens=_USAGE.cache_miss_tokens,
            completion_tokens=_USAGE.completion_tokens,
            cost_cny=_USAGE.cost_cny,
            latency_ms=_USAGE.latency_ms,
        )


def reset_usage() -> None:
    global _USAGE
    with _USAGE_LOCK:
        _USAGE = Usage()


def _record_usage(
    *, hit: int, miss: int, completion: int, cost: float, latency_ms: int
) -> None:
    with _USAGE_LOCK:
        _USAGE.calls += 1
        _USAGE.cache_hit_tokens += hit
        _USAGE.cache_miss_tokens += miss
        _USAGE.prompt_tokens += hit + miss
        _USAGE.completion_tokens += completion
        _USAGE.cost_cny += cost
        _USAGE.latency_ms += latency_ms


def _record_error() -> None:
    with _USAGE_LOCK:
        _USAGE.errors += 1


def is_peak(now: datetime | None = None) -> bool:
    """是否高峰时段（北京时间工作日 9-12、14-18）。

    只判到「周一到周五」这一层：法定节假日没有本地日历可查，
    节假日会被当高峰算，属于**把成本算高**的保守误差。
    """
    now = now or datetime.now(_CST)
    if now.tzinfo is None:
        now = now.replace(tzinfo=_CST)
    else:
        now = now.astimezone(_CST)
    if now.weekday() >= 5:
        return False
    hour = now.hour + now.minute / 60
    return 9 <= hour < 12 or 14 <= hour < 18


def price_for(model: str) -> dict[str, float]:
    """取当前时刻的单价（元 / 百万 tokens）。

    环境变量可覆盖：`LLM_PRICE_IN_HIT` / `LLM_PRICE_IN_MISS` / `LLM_PRICE_OUT`。
    覆盖了就一律用覆盖值，不再分时段 —— 手工填价的人自己知道口径。
    """
    override = {
        "hit": os.getenv("LLM_PRICE_IN_HIT", "").strip(),
        "miss": os.getenv("LLM_PRICE_IN_MISS", "").strip(),
        "out": os.getenv("LLM_PRICE_OUT", "").strip(),
    }
    if all(override.values()):
        try:
            return {k: float(v) for k, v in override.items()}
        except ValueError:
            pass  # 填错了就当没填，别因为一个环境变量把整条流水线弄崩

    table = PRICES.get(model, PRICES[DEFAULT_MODEL])
    idx = 1 if is_peak() else 0
    return {k: v[idx] for k, v in table.items()}


def cost_of(model: str, hit: int, miss: int, completion: int) -> float:
    p = price_for(model)
    return (hit * p["hit"] + miss * p["miss"] + completion * p["out"]) / 1_000_000


# --------------------------------------------------------------------------- #
# 接口 + mock
# --------------------------------------------------------------------------- #


class LLM(Protocol):
    name: str

    def complete(self, system: str, user: str, max_tokens: int = 800) -> str: ...

    def complete_json(self, system: str, user: str, schema: Any, max_tokens: int = 800) -> Any: ...

    def stream_complete(self, system: str, user: str, max_tokens: int = 800): ...


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
    """确定性假模型：不需要 key，输出可预测，用于跑通链路和写测试。

    它会看 system 提示词在问什么，分别给出「选题清单 / 正文 / 图点」三种形状 ——
    项目二的三个节点都要解析结构，mock 如果只会说一句占位话，测试就变成
    在测解析器的容错，而不是测流程。
    """

    name = "mock"

    def complete(self, system: str, user: str, max_tokens: int = 800) -> str:
        # 用输入哈希保证「同样的输入得到同样的输出」，测试才能断言
        digest = hashlib.sha256((system + user).encode("utf-8")).hexdigest()[:8]
        time.sleep(0.01)  # 模拟一点耗时，让 trace 里的耗时字段有值

        direction = self._direction(user)

        if "选题" in system:
            return "\n".join(
                f"{i}. {t}"
                for i, t in enumerate(
                    [
                        f"{direction}：先搞清这 3 个概念",
                        f"用 {direction} 做一个最小实验",
                        f"{direction} 最容易踩的 3 个坑",
                        f"两种方案怎么选：{direction} 对比",
                        f"一个真实场景：{direction} 落地",
                    ],
                    start=1,
                )
            )

        if "图点" in system or "配图" in system:
            return "\n".join(
                [
                    "1. 痛点：信息散落，收集最耗时",
                    "2. 方法：先归并原始信息",
                    "3. 步骤：三步跑通一条流水线",
                    "4. 结论：人只做判断与措辞",
                ]
            )

        if "文案" in system or "正文" in system or "写" in system:
            revised = "修改意见" in user
            head = ""
            if revised:
                m = re.search(r"修改意见[^：:]*[：:]\s*(.+)", user)
                head = f"（按意见修改：{(m.group(1).strip() if m else '已调整')[:20]}）\n"
            return (
                head
                + f"[mock:{digest}] 一、先说结论\n"
                f"{direction} 值得做的原因只有一个：把重复劳动交给机器，"
                f"人只保留判断。多数团队的问题不是不会写，而是每条信息都要人工归并一遍。\n\n"
                f"二、怎么做\n"
                f"把原始材料先归并成「事实清单」，再让模型按清单出稿，最后人工只改措辞和判断。"
                f"这样模型没有机会编，因为它的输入里只有已经确认过的事实。\n\n"
                f"三、马上能做的\n"
                f"下一次写周报前，先把散落的信息贴进一个文件，再让模型做归并。"
                f"先跑通这一小步，比一次性上全套流程有用。\n\n"
                f"四、常见误区\n"
                f"最常见的误区是「一步到位」：上来就想让模型自己找素材、自己核实、自己发。"
                f"结果是每一步都半成品，出了问题也不知道是哪一环。"
                f"更稳的做法是一段一段换：先让机器归并，再让机器出初稿，最后才让它碰发布。"
                f"每换一段，都留一次人工确认，这样出了问题还能退回去。"
            )

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

    @staticmethod
    def _direction(user: str) -> str:
        m = re.search(r"(?:方向|选题)[：:]\s*(.+)", user or "")
        return (m.group(1).strip().splitlines()[0] if m else "这个方向")[:24]

    def complete_json(self, system: str, user: str, schema: Any, max_tokens: int = 800) -> Any:
        """结构化输出（mock 也照 schema 给，测试才能断言「解析这一层」也在工作）。"""
        direction = self._direction(user)
        if "图点" in system or "配图" in system:
            payload = {
                "points": [
                    "痛点：信息散落，收集最耗时",
                    "方法：先归并原始信息",
                    "步骤：三步跑通一条流水线",
                    "结论：人只做判断与措辞",
                ]
            }
        else:
            payload = {
                "topics": [
                    f"{direction}：先搞清这 3 个概念",
                    f"用 {direction} 做一个最小实验",
                    f"{direction} 最容易踩的 3 个坑",
                    f"两种方案怎么选：{direction} 对比",
                    f"一个真实场景：{direction} 落地",
                ]
            }
        return schema.model_validate(payload)

    def stream_complete(self, system: str, user: str, max_tokens: int = 800):
        """逐块吐字 —— 给 SSE 的 `llm_token` 事件用。"""
        text = self.complete(system, user, max_tokens)
        for i in range(0, len(text), 12):
            time.sleep(0.005)
            yield text[i : i + 12]


# --------------------------------------------------------------------------- #
# 真模型：OpenAI 兼容 /chat/completions
# --------------------------------------------------------------------------- #

#: 这两个指向模块级函数而不是直接调 httpx，是为了测试能替换掉它们 ——
#: 单元测试既不该联网，也不该为了测重试真的 sleep 几秒。
_post = httpx.post
_sleep = time.sleep


class OpenAICompatLLM:
    """OpenAI 兼容的 chat/completions 客户端（DeepSeek）。"""

    def __init__(
        self,
        api_key: str,
        base_url: str = DEFAULT_BASE_URL,
        model: str = DEFAULT_MODEL,
        timeout: float = 60.0,
        retries: int = 2,
        thinking: bool = False,
        reasoning_effort: str = "",
    ) -> None:
        if not api_key:
            raise LLMError("缺少 API key")
        self.name = model
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._timeout = timeout
        self._retries = max(0, retries)
        self._thinking = thinking
        self._effort = reasoning_effort

    # -- 组装 ------------------------------------------------------------- #

    def payload(self, system: str, user: str, max_tokens: int) -> dict[str, Any]:
        """请求体。单独拆出来，测试才能断言「发出去的到底是什么」。"""
        body: dict[str, Any] = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "max_tokens": max_tokens,
        }
        if self._thinking:
            # 思考模式下 temperature 不生效，传了也只是噪音，干脆不传
            body["thinking"] = {"type": "enabled"}
            if self._effort:
                body["reasoning_effort"] = self._effort
        else:
            body["thinking"] = {"type": "disabled"}
            body["temperature"] = 0.3
        return body

    # -- 调用 ------------------------------------------------------------- #

    def complete(self, system: str, user: str, max_tokens: int = 800) -> str:
        body = self.payload(system, user, max_tokens)
        t0 = time.perf_counter()
        data = self._request(body)
        latency = int((time.perf_counter() - t0) * 1000)

        try:
            choice = data["choices"][0]["message"]
            content = (choice.get("content") or "").strip()
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError(f"响应结构不认识：{str(data)[:200]}") from exc

        if not content:
            # 空内容常见于「输出被 max_tokens 截断」或安全策略拦截，
            # 静默返回空串会让下游把空稿当草稿发出去，必须炸。
            raise LLMError("模型返回空内容（可能被截断或拦截）")

        self._record(data.get("usage") or {}, latency)
        return content

    def complete_json(self, system: str, user: str, schema: Any, max_tokens: int = 800) -> Any:
        """JSON Mode + Pydantic 校验。

        为什么不用 `with_structured_output`：它返回的是解析后的对象，拿不到
        `usage`（课程 P50 踩过同一个坑）。这里自己拿原始响应，既能统计 token，
        又能拿到「模型到底回了什么」用于报错。
        """
        body = self.payload(system, user, max_tokens)
        body["response_format"] = {"type": "json_object"}
        t0 = time.perf_counter()
        data = self._request(body)
        latency = int((time.perf_counter() - t0) * 1000)

        try:
            content = (data["choices"][0]["message"].get("content") or "").strip()
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError(f"响应结构不认识：{str(data)[:200]}") from exc
        self._record(data.get("usage") or {}, latency)

        try:
            payload = json.loads(content)
        except ValueError:
            # 有的中转会把 JSON 包在 ```json 里，兜一层
            m = re.search(r"\{.*\}", content, re.S)
            if not m:
                raise LLMError(f"模型没回 JSON：{content[:200]}") from None
            payload = json.loads(m.group(0))
        return schema.model_validate(payload)

    def stream_complete(self, system: str, user: str, max_tokens: int = 800):
        """真流式：读 chat/completions 的 SSE，逐 delta 吐字。"""
        body = self.payload(system, user, max_tokens)
        body["stream"] = True
        url = f"{self._base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        with httpx.stream("POST", url, headers=headers, json=body, timeout=self._timeout) as resp:
            if resp.status_code != 200:
                raise LLMError(f"流式请求失败 HTTP {resp.status_code}")
            for line in resp.iter_lines():
                if not line or not line.startswith("data: "):
                    continue
                chunk = line[6:].strip()
                if chunk == "[DONE]":
                    break
                try:
                    obj = json.loads(chunk)
                except ValueError:
                    continue
                delta = ((obj.get("choices") or [{}])[0].get("delta") or {}).get("content")
                if delta:
                    yield delta

    def _record(self, usage: dict[str, Any], latency_ms: int) -> None:
        hit = int(usage.get("prompt_cache_hit_tokens") or 0)
        miss = int(usage.get("prompt_cache_miss_tokens") or 0)
        prompt = int(usage.get("prompt_tokens") or 0)
        if not hit and not miss:
            miss = prompt  # 老响应没有缓存字段，保守按未命中算
        completion = int(usage.get("completion_tokens") or 0)
        _record_usage(
            hit=hit,
            miss=miss,
            completion=completion,
            cost=cost_of(self._model, hit, miss, completion),
            latency_ms=latency_ms,
        )

    def _request(self, body: dict[str, Any]) -> dict[str, Any]:
        url = f"{self._base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        last_error = ""
        for attempt in range(self._retries + 1):
            try:
                resp = _post(url, headers=headers, json=body, timeout=self._timeout)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                if attempt < self._retries:
                    _sleep(self._backoff(attempt))
                    continue
                _record_error()
                raise LLMError(f"连不上模型服务（已重试 {attempt} 次）：{last_error}") from exc

            if resp.status_code == 200:
                try:
                    return resp.json()
                except ValueError as exc:
                    _record_error()
                    raise LLMError(f"返回的不是 JSON：{resp.text[:200]}") from exc

            snippet = (resp.text or "")[:300].replace("\n", " ")
            if resp.status_code in RETRYABLE_STATUS and attempt < self._retries:
                _sleep(self._retry_after(resp, attempt))
                last_error = f"HTTP {resp.status_code}: {snippet}"
                continue

            _record_error()
            if resp.status_code in {401, 403}:
                raise LLMError(f"认证失败（HTTP {resp.status_code}）：key 失效或没有该模型权限")
            if resp.status_code == 400:
                raise LLMError(f"请求被拒（HTTP 400）：{snippet}")
            raise LLMError(f"模型服务返回 HTTP {resp.status_code}：{snippet}")

        _record_error()
        raise LLMError(f"重试 {self._retries} 次仍失败：{last_error}")

    @staticmethod
    def _backoff(attempt: int) -> float:
        # 0.8s, 1.6s, 3.2s ... 不加 jitter：单进程 CLI，不需要防惊群
        return 0.8 * (2**attempt)

    @classmethod
    def _retry_after(cls, resp: Any, attempt: int) -> float:
        """429 时优先听服务端的 Retry-After，别自作聪明。"""
        raw = resp.headers.get("Retry-After") if hasattr(resp, "headers") else None
        if raw:
            try:
                return min(max(float(raw), 0.0), 30.0)
            except ValueError:
                pass
        return cls._backoff(attempt)

    def ping(self) -> dict[str, Any]:
        """花几个 token 验证 key/模型/网络是否通 —— 给 `cli llm-check` 用。"""
        t0 = time.perf_counter()
        reply = self.complete("只回复两个字：收到", "ping", max_tokens=16)
        return {
            "ok": True,
            "model": self._model,
            "reply": reply[:40],
            "ms": int((time.perf_counter() - t0) * 1000),
        }


# --------------------------------------------------------------------------- #
# 工厂
# --------------------------------------------------------------------------- #


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, "").strip() or default)
    except ValueError:
        return default


def _flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "on", "true", "yes", "enabled"}


@lru_cache(maxsize=8)
def _client(
    provider: str,
    api_key: str,
    base_url: str,
    model: str,
    timeout: float,
    retries: int,
    thinking: bool,
    effort: str,
) -> OpenAICompatLLM:
    """同一组参数复用同一个客户端。

    缓存键把 key 也算进去：换了 key 就是另一个客户端，不会拿着旧 key 一直打。
    """
    return OpenAICompatLLM(
        api_key=api_key,
        base_url=base_url,
        model=model,
        timeout=timeout,
        retries=retries,
        thinking=thinking,
        reasoning_effort=effort,
    )


def _model_for(role: str) -> str:
    """按角色挑模型：便宜活（选题、图点）走小模型，正式稿走旗舰。

    三个环境变量的优先级：
    `LLM_MODEL_CHEAP` / `LLM_MODEL_SMART` > `LLM_MODEL` > 默认值。
    """
    base = os.getenv("LLM_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL
    if role == "cheap":
        return os.getenv("LLM_MODEL_CHEAP", "").strip() or base
    if role == "smart":
        return os.getenv("LLM_MODEL_SMART", "").strip() or base
    return base


def build_llm(role: str = "default") -> LLM:
    """按环境变量选模型；没有 key 就回落 mock，绝不因为缺 key 而崩。

    `role`: `cheap`（选题/图点）/ `smart`（写稿）/ `default`。
    """
    load_dotenv()  # 让「复制 .env.example 成 .env 再跑」真的生效
    provider = os.getenv("LLM_PROVIDER", "mock").strip().lower()
    api_key = os.getenv("DEEPSEEK_API_KEY", "").strip()

    if provider in {"deepseek", "openai", "compat"} and api_key:
        return _client(
            provider,
            api_key,
            os.getenv("LLM_BASE_URL", DEFAULT_BASE_URL).strip() or DEFAULT_BASE_URL,
            _model_for(role),
            _env_float("LLM_TIMEOUT", 60.0),
            int(_env_float("LLM_RETRIES", 2)),
            _flag("LLM_THINKING", False),
            os.getenv("LLM_REASONING_EFFORT", "").strip(),
        )
    return MockLLM()


def describe() -> dict[str, Any]:
    """当前会用哪个模型 —— 打印在 CLI 头上，别让人猜这轮花没花钱。"""
    load_dotenv()
    provider = os.getenv("LLM_PROVIDER", "mock").strip().lower()
    has_key = bool(os.getenv("DEEPSEEK_API_KEY", "").strip())
    if provider in {"deepseek", "openai", "compat"} and has_key:
        model = os.getenv("LLM_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL
        return {
            "provider": "deepseek",
            "model": model,
            "label": model,
            "real": True,
            "thinking": _flag("LLM_THINKING", False),
            "base_url": os.getenv("LLM_BASE_URL", DEFAULT_BASE_URL).strip() or DEFAULT_BASE_URL,
        }
    reason = "没设 LLM_PROVIDER=deepseek" if not provider.startswith(("deepseek", "openai", "compat")) else "没给 DEEPSEEK_API_KEY"
    return {
        "provider": "mock",
        "model": "mock",
        "label": "mock",
        "real": False,
        "thinking": False,
        "base_url": "",
        "note": reason,
    }
