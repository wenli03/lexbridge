"""LangGraph 检查点装配（PostgreSQL）。

检查点存在 PostgreSQL，**不在 Redis**。PRD 3.9 曾把检查点写进 Redis 的职责，
与 4+1 的 AD-3 矛盾；以 AD-3 为准，PRD 待修订——否则会出现两个真相源。

本模块从第一行代码起就把 `thread_id` 的租户约定固化下来。原因是它同时是一个
**安全边界**：检查点表的主键只有 `thread_id`，任何知道该值的人都能
`Command(resume=...)` 别人的会话。等到 P1 再补这条约定，代价是数据已经用错误的
命名写进去了。
"""

from __future__ import annotations

import logging
import os

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg import AsyncConnection
from psycopg.rows import DictRow
from psycopg_pool import AsyncConnectionPool

logger = logging.getLogger(__name__)

# 分隔符用冒号：tenant_id 与 run_id 都是 UUID/短标识，不会包含它。
THREAD_SEPARATOR = ":"


def thread_id_for(tenant_id: str, run_id: str) -> str:
    """构造检查点 thread_id。

    约定：`{tenant_id}:{run_id}`

    把租户 ID 编进 thread_id 而不是单开一列，是为了让「拿到 thread_id 就能猜出
    它属于谁」这件事消失——猜中的 thread_id 仍然会被 resume 路径上的前缀校验拒绝。
    """
    if not tenant_id or not run_id:
        raise ValueError("tenant_id 与 run_id 都不能为空")
    if THREAD_SEPARATOR in tenant_id or THREAD_SEPARATOR in run_id:
        raise ValueError(f"tenant_id 与 run_id 不得包含 {THREAD_SEPARATOR!r}，否则前缀校验可被绕过")
    return f"{tenant_id}{THREAD_SEPARATOR}{run_id}"


def tenant_of(thread_id: str) -> str:
    """从 thread_id 中取出租户 ID。格式非法即抛错，不做兜底猜测。"""
    parts = thread_id.split(THREAD_SEPARATOR, 1)
    if len(parts) != 2 or not parts[0] or not parts[1]:
        raise ValueError(f"thread_id 格式非法：{thread_id!r}。期望 '{{tenant_id}}:{{run_id}}'。")
    return parts[0]


def assert_thread_ownership(thread_id: str, tenant_id: str) -> None:
    """resume 路径上的强制校验：令牌里的租户必须与 thread_id 前缀一致。

    这是跨租户越权的第二道闸（第一道是 RLS）。不加这道校验的话，
    RLS 也保护不了检查点表——因为它是 ai 服务自己建的，走的是同一个数据库角色。
    """
    actual = tenant_of(thread_id)
    if actual != tenant_id:
        # 刻意不透露 thread_id 属于谁，避免把越权尝试变成信息泄露
        raise PermissionError("无权访问该会话")
    del actual  # 仅用于校验


def enable_strict_msgpack(desired: bool = True) -> None:
    """开启检查点反序列化的白名单模式。

    不开启时，数据库一旦被攻陷，攻击者可以写入恶意序列化载荷，
    在服务读取检查点时触发任意代码执行。本项目仓库公开（OQ-10），
    对应 AC-7.3，此项不可关闭。

    必须在**任何检查点读取之前**设置，因此放在装配阶段而不是别处。

    注意这里是「以配置为准写回环境变量」，而不是「看环境变量是否已设置」：
    配置项的默认值本来就是 True，但它不会自动出现在 os.environ 里，
    而 LangGraph 只认环境变量。早先按环境变量判断，导致每次启动都误报
    「未开启，已强制设为 true」——一个永远为真的警告等于没有警告。
    """
    current = os.environ.get("LANGGRAPH_STRICT_MSGPACK", "").lower() in ("true", "1", "yes")
    if current and desired:
        return

    if current and not desired:
        # 配置里显式关掉了——这是危险动作，必须留下痕迹
        os.environ["LANGGRAPH_STRICT_MSGPACK"] = "false"
        logger.error(
            "LANGGRAPH_STRICT_MSGPACK 被显式关闭。检查点反序列化因此成为"
            "代码执行入口，仅供本地诊断使用，绝不可用于任何对外环境。"
        )
        return

    os.environ["LANGGRAPH_STRICT_MSGPACK"] = "true"
    logger.info("已启用 LANGGRAPH_STRICT_MSGPACK（检查点反序列化白名单模式）")


async def build_checkpointer(
    pool: AsyncConnectionPool[AsyncConnection[DictRow]],
    *,
    run_setup: bool = True,
    strict_msgpack: bool = True,
) -> AsyncPostgresSaver:
    """装配检查点器。

    `setup()` 建表，只需执行一次，但重复执行是幂等的（内部用 IF NOT EXISTS）。
    在容器启动时跑一次最省心，代价是启动慢一点点。
    """
    enable_strict_msgpack(strict_msgpack)

    checkpointer = AsyncPostgresSaver(pool)
    if run_setup:
        await checkpointer.setup()
        logger.info("LangGraph 检查点表已就绪")
    return checkpointer
