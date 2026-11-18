# ContentOps Agent

[![CI](https://github.com/linyi-bot/contentops-agent/actions/workflows/ci.yml/badge.svg)](https://github.com/linyi-bot/contentops-agent/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/python-3.12-blue)
![LangGraph](https://img.shields.io/badge/LangGraph-1.x-1f6feb)
![Tests](https://img.shields.io/badge/tests-12%20passed-brightgreen)

> 内容运营多智能体工作台 · LangGraph 编排 · 可评测、可观测、可复现

仓库：<https://github.com/linyi-bot/contentops-agent>
克隆：`git clone https://github.com/linyi-bot/contentops-agent.git`

一个「选题 → 检索 → 起草 → 事实核查 → 人工审核 → 发布」的六节点流水线。
它不是玩具 demo：每个节点都可单独测，每次运行留 trace，评测集能量化答对率与成本。

---

## 一、这个项目要证明什么

AI Agent 岗位的 JD 里反复出现这 7 个词，本项目一个不落地覆盖：

| JD 关键词 | 在本项目里对应什么 | 状态 |
|---|---|---|
| 编排（Orchestration） | LangGraph `StateGraph`，六节点 + 两条条件边（核查回写、人工打回） | ✅ 已完成 |
| 工具调用（Tool Use） | 检索工具已按 schema 接入；抓取 / 发布工具 | 检索 ✅，其余阶段 3 |
| RAG 检索增强 | 本地知识库 → 按二级标题分块 → BM25 式打分 → 拼上下文 | ✅ 已完成 |
| 评测（Eval） | 30 条黄金集，4 个指标出报告 | 阶段 3 |
| 可观测（Observability） | 自建 trace：每节点耗时 + 一句人话 | trace ✅，报告页阶段 3 |
| 人工中断（Human-in-the-loop） | `interrupt()` 真暂停 + checkpointer 可恢复 | ✅ 已完成 |
| 部署（Deploy） | Docker 单机 + 公网隧道 | 阶段 3 |

---

## 二、架构

```mermaid
flowchart LR
    A[选题 topic] --> B[检索 retrieve]
    B --> C[起草 draft]
    C --> D[事实核查 factcheck]
    D -->|有未核实断言| C
    D -->|通过| E{人工审核<br/>interrupt}
    E -->|驳回| C
    E -->|放行| F[发布 publish]
```

当前已实现的是完整六节点闭环（阶段 1 的三节点 → 阶段 2 补齐核查、审核、发布）：

```
选题 ──▶ 检索(RAG) ──▶ 起草 ──▶ 事实核查 ──┬─有未核实断言──▶ 起草（最多 3 版）
         │              │                │
         └─ docs/kb/ ───┘                └─通过──▶ 人工审核(interrupt) ──┬─放行──▶ 发布
                                                                └─打回──▶ 起草（吃同一个额度）
```

### 核查与闸门（阶段 2 的判断规则）

事实核查只做两件**可机器判**的事，不做「语义上支不支持」的假装判断：

1. 每条断言必须带 `[n]`，且 n 落在本次检索到的条数范围内；
2. 被引那一段里，得能找到这条断言至少 2 个实字（或 1 个数字/单词）—— 专抓「引错了段」。

不通过的三类原因会原样进审核包：`没有引用标记` / `引用编号 N 不存在` / `引用 [N] 的段落里找不到这条断言的关键词`。
语义是否真的支持，留给人工 —— 这就是为什么下一个节点是 `interrupt()` 而不是自动发布。

三道闸门：

| 闸门 | 规则 | 代码位置 |
|---|---|---|
| 有证据 | 核查不过就回写，但最多 `MAX_REVISION=3` 版 | `graph.route_after_factcheck` |
| 有出路 | 零命中或额度用尽 → 交人，不空转 | 同上 |
| 不裸发 | 没有可核查断言（空稿、零命中）也**不算通过**，必须人工确认；发布记录里标 `forced` | `nodes.factcheck_node` / `nodes.publish_node` |

---

## 三、快速开始

```bat
:: 0) 拿代码（仓库已公开）
git clone https://github.com/linyi-bot/contentops-agent.git
cd contentops-agent

:: 1) 依赖
python -m pip install -r requirements.txt

:: 2) 让它知道自己是 src 布局的包（Windows 下 set，mac/linux 用 export）
set PYTHONPATH=src

:: 3) 跑一次：会停在【人工审核】，敲 y 放行 / n 打回（可跟一句附言）
python -m contentops.cli run --topic "用 AI 做周报自动化"

:: 4) 免交互跑完，并把发布记录落成 markdown（CI / 录屏用这个）
python -m contentops.cli run --topic "用 AI 做周报自动化" --auto approve --out outputs

:: 5) 测试
python -m pytest -q
```

`scripts\dev.cmd` 只是把第 2 步和启动命令包了一下（Windows 专用，省得每次设环境变量）。

**人工审核长什么样**：图停在 `review` 节点，把审核包（选题、第几版、草稿、每条断言的核查结论）打印出来，
然后等一个答复。答复是 `Command(resume=...)` 喂回图的，不是命令行参数 ——
所以「今天先不发，明天接着发」是可行的（阶段 3 换成 SQLite checkpointer 就能跨进程）。

**用真实模型**：把 `.env.example` 复制成 `.env`，填 `DEEPSEEK_API_KEY`，
然后 `set LLM_PROVIDER=deepseek` 再运行。没填 key 时自动回落到 mock，
保证「clone 下来就能跑」——这是阶段 0 的验收线之一。

---

## 四、目录

```
contentops-agent/
├── README.md
├── requirements.txt
├── .env.example
├── .gitignore
├── .github/workflows/ci.yml   # push 即跑 pytest
├── docs/
│   ├── kb/                    # 知识库（检索的语料，纯 markdown）
│   ├── 评测口径.md             # 指标定义，阶段 3 填数
│   └── 简历回填模板.md
├── scripts/
│   └── dev.cmd                # 设好 PYTHONPATH 再跑，避免环境污染
├── src/contentops/
│   ├── state.py               # 图的状态定义（含断言 Claim 与审核包 ReviewPacket）
│   ├── llm.py                 # 模型层：mock / deepseek 统一接口
│   ├── retriever.py           # 知识库分块与检索
│   ├── nodes.py               # 五个节点：检索 / 起草 / 核查 / 审核 / 发布
│   ├── graph.py               # 图的组装 + 两条条件边 + 会话（checkpointer）工具
│   └── cli.py                 # 命令行入口（含人工审核交互）
└── tests/
    ├── test_smoke.py          # 链路能不能跑通
    └── test_stage2.py         # 核查规则 / 中断 / 打回额度
```

---

## 五、评测口径（阶段 3 交付）

| 指标 | 定义 | 目标 |
|---|---|---|
| 引用命中率 | 草稿里每条引用都能在知识库中找到出处 | ≥ 90% |
| 事实一致性 | 黄金集 30 条里核查节点判「通过」且人工判对的占比 | ≥ 85% |
| 平均 token 成本 | 单次运行 input+output token | 记录基线，逐版下降 |
| P95 延迟 | 端到端耗时 95 分位 | < 20s（mock 本地） |

> 现在这张表是空的，填数之前**不许写进简历**。等阶段 3 跑出报告，再用真实数字回填。

---

## 六、路线图

| 阶段 | 天数 | 产出 | 验收线 |
|---|---|---|---|
| 0 地基 | 1 | git / 依赖 / 仓库 / README / CI | ✅ 已推到 GitHub，Actions 跑 pytest |
| 1 最小闭环 | 5 | 三节点 + 检索 + mock 模型 | 命令行跑通、有 commit ✅ |
| 2 主项目 MVP | 7 | 六节点 + 事实核查 + 人工中断 + 打回重写 | ✅ 输入选题产出带引用稿子，12 条测试全绿 |
| 3 工程化 | 5 | 评测集 + SQLite checkpointer + Docker + 打包 | pytest 绿、评测出报告 |
| 4 讲述 | 3 | 录屏 + 讲稿 + 10 问 | 脱稿 3 分钟讲完 |
| 5 投递 | 持续 | 简历回填 + 投递表 | 每周 ≥10 家 |

---

## 七、在受限环境里跑（可选）

如果目标机器是「没有管理员权限 + 出网受限 + 系统 TLS 损坏」的那类环境，
本项目刻意避开了三个依赖：不依赖 GPU、不依赖向量库、不依赖外部可观测平台。
另外两个实测坑值得记下来：

| 现象 | 真实原因 | 解法 |
|---|---|---|
| `pip install` 报 `PermissionError: ...whl` | Python 3.14 会把 `os.mkdir(mode=0o700)` 应用到目录 DACL，而 `tempfile.mkdtemp()` 默认就用 0o700；受限令牌因此进不去自己刚建的目录 | 用一个包装脚本把 0o700 改成 0o777 再调 pip（见提交历史里的 `pipx.py`） |
| `curl` / PowerShell 的 HTTPS 全挂 | schannel 凭证链损坏 | 出网一律走 Python `requests`；git 配 `http.sslBackend=openssl` |

