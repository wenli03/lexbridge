"""数据库连接。

**只用 psycopg 3，不引入 asyncpg。**

理由：`langgraph-checkpoint-postgres` 随包安装的就是 psycopg3。若向量查询另走
asyncpg，就会得到两套连接池、两套类型注册路径、两套事务语义——出问题时
排查成本远高于当初省下的那点便利。

**两个池，不是一个。** 检查点对连接有特殊要求（见下），业务查询没有；
混用一个池会把检查点的约束泄漏到所有查询上。
"""

from __future__ import annotations

import asyncio
import logging
import sys
from typing import Any

from pgvector.psycopg import register_vector_async
from psycopg import AsyncConnection
from psycopg.rows import DictRow, dict_row
from psycopg_pool import AsyncConnectionPool

from app.core.config import Settings

# ---------------------------------------------------------------------------
# Windows 事件循环兼容
# ---------------------------------------------------------------------------
# psycopg 3 的异步模式无法运行在 Windows 默认的 ProactorEventLoop 上，会抛：
#   "Psycopg cannot use the 'ProactorEventLoop' to run in async mode"
# 而连接池的表现是每个连接都建不起来，最终超时——
#   "PoolTimeout: couldn't get a connection after 30.00 sec"
# 后者的错误信息完全指向连接问题，很容易让人去查网络或口令。
#
# 生产运行时是 Linux 容器（python:3.11-slim），没有这个问题；
# 这段只为本地开发存在，因此用 sys.platform 守卫。
if sys.platform == "win32":  # pragma: no cover
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

logger = logging.getLogger(__name__)


class Database:
    """持有两个连接池，并负责它们的生命周期。"""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        # 池类型参数化到 `DictRow`：两个池都显式设了 `row_factory=dict_row`，
        # 因此"行是字典"是运行时事实，而不是愿望。写成裸 `AsyncConnectionPool`
        # 会让类型检查按默认的 tuple 行推断，于是每一处 `row["列名"]`
        # 都报 "tuple indices must be integers"——那正是这两个池当初要避免的错误。
        self._checkpoint_pool: AsyncConnectionPool[AsyncConnection[DictRow]] | None = None
        self._app_pool: AsyncConnectionPool[AsyncConnection[DictRow]] | None = None

    # ------------------------------------------------------------------ 启动
    async def open(self) -> None:
        cfg = self._settings

        # --- 检查点池 ---------------------------------------------------
        # 两个约束来自 langgraph-checkpoint-postgres 的官方说明，缺一个就会在
        # 运行时炸出 `TypeError: tuple indices must be integers or slices, not str`：
        #
        #   autocommit=True —— 检查点的写入依赖自动提交
        #   row_factory=dict_row —— 该库内部按列名取值
        #
        # max_size 与 worker 数相关：社区反馈 AsyncPostgresSaver 内部有锁，
        # 可能串行化并发请求（对应风险 R2）。池子开大一点，
        # 让并发的瓶颈出现在检查点实现本身而不是连接数上——这样压测才能测出真话。
        self._checkpoint_pool = AsyncConnectionPool(
            conninfo=_with_search_path(cfg.dsn, "runtime,public"),
            min_size=4,
            max_size=20,
            kwargs={"autocommit": True, "row_factory": dict_row},
            configure=_register_vector,
            open=False,
        )
        await self._checkpoint_pool.open()
        logger.info("检查点连接池已就绪 (min=4, max=20, autocommit=True, search_path=runtime)")

        # --- 业务池 -----------------------------------------------------
        # 业务查询要显式事务，因此不开 autocommit。
        # row_factory 同样用 dict_row：两个池的行类型不一致时，
        # 「同一个查询在哪个池上跑」会决定你能不能用列名取值，
        # 这类差异会在重构搬动查询时变成难找的 AttributeError。
        self._app_pool = AsyncConnectionPool(
            conninfo=_with_search_path(cfg.dsn, "app,kb,public"),
            min_size=2,
            max_size=10,
            kwargs={"autocommit": False, "row_factory": dict_row},
            configure=_register_vector,
            open=False,
        )
        await self._app_pool.open()
        logger.info("业务连接池已就绪 (min=2, max=10, search_path=app,kb)")

    async def close(self) -> None:
        for pool in (self._checkpoint_pool, self._app_pool):
            if pool is not None:
                await pool.close()
        logger.info("数据库连接池已关闭")

    # -------------------------------------------------------------- 访问器
    @property
    def checkpoint_pool(self) -> AsyncConnectionPool[AsyncConnection[DictRow]]:
        if self._checkpoint_pool is None:
            raise RuntimeError("数据库尚未初始化，请先 await db.open()")
        return self._checkpoint_pool

    @property
    def app_pool(self) -> AsyncConnectionPool[AsyncConnection[DictRow]]:
        if self._app_pool is None:
            raise RuntimeError("数据库尚未初始化，请先 await db.open()")
        return self._app_pool

    # ---------------------------------------------------------------- 探活
    async def ping(self) -> dict[str, Any]:
        """健康检查用。同时验证 pgvector 确实可用，而不只是连接通了。"""
        async with self.app_pool.connection() as conn, conn.cursor() as cur:
            await cur.execute("SELECT version() AS v")
            row = await cur.fetchone()
            await cur.execute("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
            ext = await cur.fetchone()
        return {
            "server": (row or {}).get("v", "unknown").split(",")[0],
            "pgvector": (ext or {}).get("extversion"),
        }


def _with_search_path(db_url: str, schemas: str) -> str:
    """把 search_path 写进连接串，而不是在连接后执行 SET。

    两者的区别在实际运行中有影响：`SET search_path` 若发生在一个尚未提交的
    事务里，事务回滚时会被一并撤销——连接池复用连接时就可能出现
    「有时表找不到、有时又正常」的间歇性故障，极难排查。

    通过连接串的 `options=-csearch_path=` 交给 libpq，在建立连接时生效，
    不受事务语义影响。
    """
    from urllib.parse import quote

    sep = "&" if "?" in db_url else "?"
    return f"{db_url}{sep}options={quote(f'-csearch_path={schemas}', safe='')}"


async def _register_vector(conn: Any) -> None:
    """连接池的 configure 回调：每个新连接都要注册 pgvector 类型。

    漏掉这一步的典型症状是插入向量时报
    `cannot adapt type 'list'`，而错误信息不会提示你少注册了类型。
    """
    await register_vector_async(conn)


# ---------------------------------------------------------------------------
# 进程级单例。由 FastAPI 的 lifespan 负责 open/close。
# ---------------------------------------------------------------------------
_db: Database | None = None


def init_db(settings: Settings) -> Database:
    global _db
    _db = Database(settings)
    return _db


def get_db() -> Database:
    if _db is None:
        raise RuntimeError("数据库尚未初始化")
    return _db
