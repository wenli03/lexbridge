# =============================================================================
# LexBridge AI 服务镜像
# =============================================================================
# 多阶段构建。基础镜像 python:3.11-slim 与 4+1 文档 6.3 的容器清单一致，
# 也与 deepagents 支持的 Python 版本区间（3.11–3.14）吻合。
# =============================================================================

# ---------------------------------------------------------------------------
# 阶段 1：解析并安装依赖
# ---------------------------------------------------------------------------
FROM python:3.11-slim AS builder

# uv 官方推荐从镜像拷贝二进制，比 pip install uv 快且不污染依赖树
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

# 可选：Python 包索引源。默认走官方 PyPI，保证仓库在任何环境下可构建。
#
# uv 会读取 UV_DEFAULT_INDEX，同时兼容 PIP_INDEX_URL——
# 两个都设，覆盖不同版本的行为差异。
ARG PYPI_INDEX_URL=""
ENV UV_DEFAULT_INDEX=${PYPI_INDEX_URL} \
    PIP_INDEX_URL=${PYPI_INDEX_URL}

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# 先只拷贝依赖清单，让依赖层能被 Docker 缓存复用——
# 改业务代码时不会重装依赖，构建从几分钟降到几秒
COPY pyproject.toml uv.lock README.md ./

# --frozen：严格按 uv.lock 安装，锁文件与 pyproject 不一致时直接失败。
# 这正是把 uv.lock 入库的意义所在。
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev

# ---------------------------------------------------------------------------
# 阶段 2：运行
# ---------------------------------------------------------------------------
FROM python:3.11-slim

# curl 供 healthcheck 使用（compose 里用的是 python，这里装了便于人工排查）
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

# 非 root 运行。容器内被攻陷时，这一步决定了攻击者能不能直接改代码。
RUN useradd --create-home --shell /bin/bash --uid 10001 lexbridge

WORKDIR /app

COPY --from=builder --chown=lexbridge:lexbridge /app/.venv /app/.venv
COPY --chown=lexbridge:lexbridge app/ ./app/
COPY --chown=lexbridge:lexbridge fixtures/ ./fixtures/
COPY --chown=lexbridge:lexbridge pyproject.toml README.md ./

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    # 检查点反序列化的白名单模式。仓库公开，此项不可关闭（AC-7.3）。
    LANGGRAPH_STRICT_MSGPACK=true \
    # 默认回放模式：容器起来即可演示，不需要任何密钥
    MODEL_MODE=replay

# 上传文件落盘目录（由 compose 挂载卷）
#
# ⚠️ 必须在 USER lexbridge **之前**创建。
#    放在切用户之后会失败在 `mkdir: cannot create directory '/data/files':
#    Permission denied` —— 在根目录下建目录需要 root 权限。
#    这个顺序错误只在构建时才暴露，本地开发（直接跑进程）永远不会遇到。
RUN mkdir -p /data/files && chown -R lexbridge:lexbridge /data

USER lexbridge

VOLUME ["/data/files"]

EXPOSE 8000

# 不发布宿主端口，仅 backnet 内可达
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2"]
