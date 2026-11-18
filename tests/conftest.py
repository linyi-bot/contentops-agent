"""测试期统一关掉 .env 加载。

原因：本机一旦填了 DEEPSEEK_API_KEY，`build_llm()` 就会走真模型 ——
单元测试变成每跑一次烧一次 token，而且断言的还是网络回来的内容。
CI 里没有 .env 所以本来就没事，但「本机能过、本机一填 key 就挂」这种坑
不能等到演示当天才发现。
"""
import os

os.environ["CONTENTOPS_NO_DOTENV"] = "1"
