"""健康检查。

供 docker compose 的 healthcheck 与运维使用。

刻意做成**两档**：

  `/healthz`        —— 存活。只要进程还在跑就返回 200。
                       容器重启策略用这个，避免依赖抖动导致无谓重启。
  `/healthz/ready`  —— 就绪。真实检查数据库与向量扩展。
                       启动依赖顺序用这个：api 容器等它变成 healthy 才启动。

合并成一个接口会二选一踩坑：用存活做就绪判断会过早放行，
用就绪做存活判断会在数据库短暂不可用时触发重启风暴。
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Response, status

from app.core.config import get_settings
from app.core.db import get_db
from app.graph.checkpoint.postgres import THREAD_SEPARATOR

logger = logging.getLogger(__name__)
router = APIRouter(tags=["health"])

SERVICE_NAME = "lexbridge-ai"
SERVICE_VERSION = "0.1.0"


@router.get("/healthz")
async def liveness() -> dict[str, Any]:
    """存活探针：不触碰任何外部依赖。"""
    settings = get_settings()
    return {
        "status": "ok",
        "service": SERVICE_NAME,
        "version": SERVICE_VERSION,
        # 把运行模式暴露出来：演示时一眼能看出当前是真实调用还是回放，
        # 避免把回放结果误当成真实模型输出。
        "model_mode": settings.model_mode.value,
        "thread_id_format": f"{{tenant_id}}{THREAD_SEPARATOR}{{run_id}}",
    }


@router.get("/healthz/ready")
async def readiness(response: Response) -> dict[str, Any]:
    """就绪探针：验证数据库连通**且 pgvector 可用**。

    只测「连接是否通」是不够的——向量扩展没装时连接照样成功，
    但第一次写入向量才会失败，而那时服务已经对外宣称健康了。
    """
    try:
        info = await get_db().ping()
    except Exception as e:
        logger.warning("就绪检查失败：%s", e)
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": "not_ready", "reason": type(e).__name__, "detail": str(e)[:200]}

    if not info.get("pgvector"):
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": "not_ready", "reason": "pgvector 扩展缺失"}

    return {"status": "ready", "database": info}
