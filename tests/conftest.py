"""测试环境的两条本地约定。

1. 关掉 .env 加载 —— 本机一旦填了 DEEPSEEK_API_KEY，`build_llm()` 就会走真模型，
   单元测试变成每跑一次烧一次 token，断言的还是网络回来的内容。CI 里没有 .env
   所以本来就没事，但「本机能过、本机一填 key 就挂」这种坑不能等演示当天才发现。

2. 不用 pytest 的 `tmp_path`，改用 `work` —— 见下面 fixture 的注释。
"""
import os
import shutil
from pathlib import Path

import pytest

os.environ["CONTENTOPS_NO_DOTENV"] = "1"


@pytest.fixture()
def work(request) -> Path:
    """一个干净的仓库内临时目录：`tests/_tmp/<用例名>`（已在 .gitignore 里）。

    为什么不用 `tmp_path`：Python 3.14 会把 `os.mkdir(mode=0o700)` 落到目录 DACL 上，
    而 `tempfile.mkdtemp()` 默认就是 0o700 —— 受限令牌进不去自己刚建的目录，
    报 `PermissionError: [WinError 5] ...pytest-of-<用户>`（README 第八节记了同一个坑）。
    `os.makedirs()`（默认权限）没这个问题，所以这里自己建，CI 上同样能用。
    """
    base = Path(__file__).parent / "_tmp" / request.node.name
    shutil.rmtree(base, ignore_errors=True)  # 上一次的残留（可能是锁死的，忽略失败）
    base.mkdir(parents=True, exist_ok=True)
    return base
