"""ContentOps Agent —— 内容运营多智能体工作台。"""

__all__ = ["state", "llm", "retriever", "nodes", "graph", "eval", "trace_store", "checkpoint"]

#: 唯一版本源。`pyproject.toml` 的 version 与它必须一致（tests/test_packaging.py 守着），
#: 装完之后 `contentops --version` 读的是这一行。
__version__ = "0.4.0"
