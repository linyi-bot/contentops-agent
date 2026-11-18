# syntax=docker/dockerfile:1
# 单机部署镜像：一个 Python 解释器 + 本项目源码 + 知识库，没有前端依赖。
#
# 故意不做 multi-stage：依赖全是纯 wheel，镜像里没有编译工具链可删，
# 多一层只会让「build 之后为什么找不到文件」更难查。
# 真正的瘦身在 .dockerignore 里（把 .git / .probe / reports 挡在上下文外）。

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app/src

WORKDIR /app

# 依赖先拷：只改代码不会让这一层缓存失效
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ ./src/
COPY docs/kb/ ./docs/kb/
COPY evals/ ./evals/

# 非 root 跑。容器里唯一会被写的是这三个目录，其余保持只读。
RUN useradd --create-home --uid 10001 app \
    && mkdir -p /app/outputs /app/reports /app/.contentops \
    && chown -R app:app /app
USER app

# 健康检查故意不碰模型：没有 key 也要能判「镜像里的代码和知识库是活的」。
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python -m contentops.cli kb --query 周报 >/dev/null || exit 1

# 页面（`docker compose run --service-ports web` 或 compose 里的 web profile）
EXPOSE 8000

ENTRYPOINT ["python", "-m", "contentops.cli"]
CMD ["run", "--topic", "用 AI 做周报自动化", "--auto", "approve", "--out", "/app/outputs"]
