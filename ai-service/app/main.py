"""FastAPI 应用入口。

本服务只在内网可达（`backnet`），不发布宿主端口。唯一的调用方是 Java `api` 服务，
两者之间用内部令牌传递租户身份——租户上下文**绝不从请求体读取**，
这条纪律与 Java 侧一致，是 AC-5.1「跨租户 0 成功」的组成部分。
"""

from __future__ import annotations

import hashlib
import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.api import health, ingest
from app.core.config import ModelMode, get_settings
from app.core.db import init_db
from app.core.logging import setup_logging
from app.graph.checkpoint.postgres import build_checkpointer
from app.observability.langsmith import configure_langsmith

logger = logging.getLogger(__name__)

# 检查点器是进程级单例：AsyncPostgresSaver 内部持有连接池，
# 每次请求新建一个会让连接数失控。
_checkpointer: Any = None


def get_checkpointer() -> Any:
    if _checkpointer is None:
        raise RuntimeError("检查点器尚未初始化")
    return _checkpointer


def _warn_on_env_shadowing() -> None:
    """启动时检查：进程环境变量是否覆盖了 deploy/.env 里的值。

    为什么值得写这段：这个坑真实发生过，而且排查成本很高。

    pydantic-settings 与 Docker Compose 一样，**环境变量优先级高于 .env 文件**。
    于是当机器上存在一个陈旧的同名变量（比如早已失效的 SILICONFLOW_API_KEY），
    用户明明把正确的值写进了 .env，容器和服务却始终拿到那个旧的——
    症状是满屏 401 "Token is invalid"，而人会先去怀疑代码、网络、或模型账号。

    更隐蔽的是：清掉注册表里的用户级变量**不会影响已经在运行的进程**。
    父进程仍持有旧值并传给每个子进程，于是"明明清了还是不行"。

    这段检查把上述所有情况压缩成启动日志里的一行警告。
    容器里没有 deploy/.env，会自动跳过。
    """
    from pathlib import Path

    candidates = [
        Path("deploy/.env"),  # 从仓库根启动
        Path("../deploy/.env"),  # 从 ai-service/ 启动（uv run 的常见形态）
    ]
    env_path = next((p for p in candidates if p.exists()), None)
    if env_path is None:
        return

    shadowed: list[str] = []
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k, v = k.strip(), v.strip()
        ambient = os.environ.get(k)
        if v and ambient and ambient != v:
            shadowed.append(k)

    if shadowed:
        logger.warning(
            "以下变量被进程环境变量覆盖，deploy/.env 中的值不会生效：%s\n"
            "  排查提示：清掉用户级变量不会影响已在运行的进程，"
            "父进程仍持有旧值。重启终端/IDE 后才会生效。\n"
            "  临时绕过：在启动命令前显式赋值，例如 `SILICONFLOW_API_KEY=... docker compose up`。",
            ", ".join(shadowed),
        )


def _key_fingerprint(key: str) -> str:
    """密钥指纹：够用来核对是不是同一把，但不含任何密钥材料。

    存在的理由是一个具体的坑：宿主机上的环境变量优先级高于 `.env` 文件，
    因此容器里拿到的可能不是 .env 里的那一把。症状是满屏 401 "Token is invalid"，
    而排查的人会先怀疑代码或网络。启动时打一行指纹，
    `docker compose logs ai` 一眼就能确认拿到的是哪把钥匙。

    **刻意不用"首 6 位 + 末 4 位"的做法**。那种掩码看起来安全，实际仍在日志里
    输出了凭据材料——公开仓库的 CI 日志、`docker compose logs` 的输出都可能被
    转发或归档。改用 SHA-256 前缀：同样是"两把钥匙一比就知道是否相同"，
    但单向不可还原，且不含密钥的任何字符。
    """
    if not key:
        return "未设置"
    if len(key) < 16:
        return f"长度 {len(key)}（过短，疑似配置错误）"
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]
    return f"sha256:{digest} 长度={len(key)}"


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # LangSmith 必须在导入 langchain 相关模块之前配置，因此放在最前面
    settings = get_settings()
    setup_logging(settings.log_level)
    configure_langsmith(settings)

    logger.info(
        "启动 LexBridge AI 服务 | 模型模式=%s | chat=%s | embedding=%s(dim=%d)",
        settings.model_mode.value,
        settings.chat_model,
        settings.embedding_model,
        settings.embedding_dim,
    )
    logger.info("硅基流动密钥指纹：%s", _key_fingerprint(settings.siliconflow_api_key))
    _warn_on_env_shadowing()
    if settings.model_mode is ModelMode.REPLAY:
        logger.info(
            "当前为回放模式：不会调用任何外部模型，响应来自 %s 下的录制文件。",
            settings.replay_fixture_dir,
        )

    db = init_db(settings)
    await db.open()

    global _checkpointer
    _checkpointer = await build_checkpointer(
        db.checkpoint_pool,
        strict_msgpack=settings.langgraph_strict_msgpack,
    )
    app.state.db = db

    logger.info("LexBridge AI 服务已就绪")
    try:
        yield
    finally:
        await db.close()
        logger.info("LexBridge AI 服务已停止")


def create_app() -> FastAPI:
    app = FastAPI(
        title="LexBridge AI Service",
        version="0.1.0",
        description=(
            "LangChain / LangGraph / DeepAgent / LlamaIndex 运行时。"
            "仅内网可达，由 Java api 服务调用。"
        ),
        lifespan=lifespan,
        # 内部服务不需要公开文档
        docs_url=None,
        redoc_url=None,
        openapi_url="/openapi.json",
    )

    app.include_router(health.router)
    app.include_router(ingest.router)

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception) -> JSONResponse:
        """统一异常出口。

        记录完整堆栈但只返回类型与简短信息：内网服务也不应把堆栈回传给调用方，
        否则一旦 api 侧的日志被下游看到，就等于泄露了内部结构。
        """
        logger.exception("未处理异常 %s %s", request.method, request.url.path)
        return JSONResponse(
            status_code=500,
            content={
                "error": type(exc).__name__,
                "detail": str(exc)[:300],
                "path": request.url.path,
            },
        )

    return app


app = create_app()
