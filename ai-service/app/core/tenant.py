"""租户上下文：只从内部令牌来。

**这个模块是 `ai` 侧多租户隔离的起点。** 它只做一件事：把一个已签名的内部令牌
解成租户身份。请求体、查询参数、其它请求头里的任何 tenant 字段都不会流进来。

为什么值得单独一个模块而不是散在路由里：`api` 侧的对应纪律同样是集中的
（`TenantContextHolder`）。两侧各有一个唯一入口时，
「租户从哪来」这个问题才有确定答案；一旦允许在某个路由里"临时从 body 读一下"，
这个答案就没了，而 AC-5.1「跨租户 0 成功」正是建立在它之上。
"""

from __future__ import annotations

import logging
from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import jwt
from fastapi import Header, HTTPException, Request, status
from psycopg import AsyncConnection

from app.core.config import get_settings

logger = logging.getLogger(__name__)

#: HS256 要求密钥至少 256 位。低于此长度签名强度不足，且 PyJWT 行为随版本变化。
MIN_SECRET_BYTES = 32

INTERNAL_TOKEN_HEADER = "X-Internal-Token"  # noqa: S105 —— bandit 误报：这是请求头名称，不含凭据材料
TRACE_ID_HEADER = "X-Request-Id"

#: RLS 用的会话变量名。与 `kb` 的 RLS 策略（详细设计 §3.9.5）必须一致——
#: 两处拼写不一致的表现是"策略永远不生效"，而它不会报错。
TENANT_SETTING = "app.current_tenant"


@dataclass(frozen=True)
class TenantContext:
    """一次内部调用所代表的租户身份。"""

    tenant_id: UUID
    user_id: UUID | None
    run_id: UUID | None
    trace_id: str | None = None


def decode_internal_token(token: str) -> TenantContext:
    """校验并解析内部令牌。

    **任何失败都抛同一种异常、给同一句文案。** 区分「签名错误」「已过期」「载荷缺字段」
    对排查有帮助，但那属于日志的职责；对调用方给出差异化的响应只会让它去试探
    哪种差异可以被利用。

    Args:
        token: 令牌原文（不含 `Bearer ` 前缀）

    Raises:
        ValueError: 令牌不可用。调用方不应对原因做分支。
    """
    settings = get_settings()
    secret = settings.internal_token_secret
    if not secret:
        # 配置缺失是部署问题，不是调用方问题。抛 ValueError 会让它看起来像 401，
        # 于是排查会跑向"令牌为什么不对"而不是"密钥没配"
        raise RuntimeError(
            "INTERNAL_TOKEN_SECRET 未设置：ai 服务无法校验内部令牌。该变量必须与 api 服务保持一致。"
        )
    if len(secret.encode("utf-8")) < MIN_SECRET_BYTES:
        raise RuntimeError(
            f"INTERNAL_TOKEN_SECRET 过短（需至少 {MIN_SECRET_BYTES} 字节）。"
            "生成方式：openssl rand -base64 48"
        )

    try:
        payload: dict[str, Any] = jwt.decode(
            token,
            secret,
            algorithms=["HS256"],
            # 显式要求 exp：没有过期时间的内部令牌一旦泄露就永久有效，
            # 而它只应活一次调用的时间
            options={"require": ["exp", "tenantId"]},
        )
    except jwt.PyJWTError as exc:
        # 只记异常类型，不记令牌内容——令牌本身是凭据，写进日志等于泄露
        logger.warning("内部令牌校验失败：%s", type(exc).__name__)
        raise ValueError("内部令牌无效或已过期") from exc

    try:
        tenant_id = UUID(str(payload["tenantId"]))
        user_id = UUID(str(payload["userId"])) if payload.get("userId") else None
        run_id = UUID(str(payload["runId"])) if payload.get("runId") else None
    except (KeyError, ValueError, TypeError) as exc:
        logger.warning("内部令牌载荷无法解析：%s", type(exc).__name__)
        raise ValueError("内部令牌无效或已过期") from exc

    return TenantContext(tenant_id=tenant_id, user_id=user_id, run_id=run_id)


async def require_tenant(
    request: Request,
    x_internal_token: str | None = Header(default=None, alias=INTERNAL_TOKEN_HEADER),
    x_request_id: str | None = Header(default=None, alias=TRACE_ID_HEADER),
) -> TenantContext:
    """FastAPI 依赖：取得当前调用的租户上下文。

    缺少或无效的令牌一律 401，**不降级为匿名上下文**。降级的后果是
    "未认证的请求读到了某个租户的数据"——隔离机制失效时，
    正确的表现是查不到东西，而不是查到所有人的东西。
    """
    if not x_internal_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="缺少内部令牌",
        )
    try:
        context = decode_internal_token(x_internal_token)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc

    # traceId 由 api 侧生成并透传；两侧日志靠它对齐，因此放进上下文供各处理处使用
    context_with_trace = TenantContext(
        tenant_id=context.tenant_id,
        user_id=context.user_id,
        run_id=context.run_id,
        trace_id=x_request_id,
    )
    # 存到 request.state 供日志与审计使用；**不用它做鉴权判断**
    request.state.tenant = context_with_trace
    # 同时放进 contextvar：检索层不接受 tenant 参数，只从上下文取（§6.4）。
    # 这里 set 之后，同一请求内的检索调用无需再传任何身份信息。
    set_tenant_context(context_with_trace)
    return context_with_trace


async def bind_tenant(conn: AsyncConnection, tenant_id: UUID) -> None:
    """把租户绑到当前事务，供 RLS 使用。

    `is_local=True`（`set_config` 的第三个参数）让设置只在这个事务内有效。
    用会话级设置会在连接归还池后残留，于是下一个请求可能带着上一个租户的上下文——
    多租户系统里最难复现的一类故障。

    **本函数只用于"按租户读写"的路径。** 平台级写入（例如把公共法条库入库）
    必须在**没有**租户上下文的情况下执行：`kb` 的 RLS 策略刻意不允许
    在租户上下文下写 `tenant_scope='PLATFORM'` 的行（详细设计 §3.9.5，
    该行为已被实测验证）。这不是限制，而是设计意图——
    公共法条库只能由平台级路径写入。
    """
    async with conn.cursor() as cur:
        await cur.execute("SELECT set_config(%s, %s, true)", (TENANT_SETTING, str(tenant_id)))


#: 当前任务的租户上下文。用 contextvar 而不是函数参数，
#: 是为了让"忘记传租户"在语法上不可能发生（详细设计 §6.4）：
#: 检索函数根本不接受 tenant 参数，只能从这里取。
_CURRENT_TENANT: ContextVar[TenantContext | None] = ContextVar(
    "lexbridge_tenant_context", default=None
)


def set_tenant_context(context: TenantContext) -> Token:
    """把租户上下文放进当前任务。返回 token 供调用方 reset。"""
    return _CURRENT_TENANT.set(context)


def reset_tenant_context(token: Token) -> None:
    _CURRENT_TENANT.reset(token)


def require_current_tenant() -> TenantContext:
    """取当前租户上下文，取不到即抛。

    **缺失时抛异常，而不是退回默认租户或"查全部"。** 一个宽松的默认值
    会让"忘了设置上下文"表现为"查到了不该看到的数据"，
    而那是最难在事后归因的一类问题。

    传给 `require_tenant` 的 FastAPI 依赖负责在请求路径上设置它；
    脱离请求上下文的调用（脚本、后台任务）必须显式 set 一次——
    "在哪个租户下查"应当是一个被明确回答过的问题。
    """
    context = _CURRENT_TENANT.get()
    if context is None:
        raise RuntimeError(
            "当前任务没有租户上下文，操作被拒绝。"
            "HTTP 路径由 require_tenant 依赖设置；其它调用方需先 set_tenant_context()。"
        )
    return context
