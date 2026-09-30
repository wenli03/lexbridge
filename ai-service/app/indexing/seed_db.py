"""把规范化后的种子语料写入 `kb`。

**写入必须在没有租户上下文的情况下进行。** `kb` 的 RLS 策略刻意不允许在租户上下文下
写 `tenant_scope='PLATFORM'` 的行（详细设计 §3.9.5，该行为已被实测验证：
租户上下文下写 PLATFORM 行会被拒绝）。平台公共法条库只能由平台级路径写入——
这不是绕过限制，这正是策略的设计意图。

**幂等性按「版本」而不是「法条」判定**：同一版本已存在即整组跳过。
按法条判定会得到一个更糟的中间态——重跑时为已存在的版本反复尝试插入同样的条，
每次都撞唯一约束，日志里全是冲突，而正确行为本应是"这一版已经在库里了"。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from psycopg import AsyncConnection
from psycopg.rows import DictRow

from app.indexing.seed import StatuteVersionGroup

logger = logging.getLogger(__name__)

PLATFORM_SCOPE = "PLATFORM"

#: 种子语料的条号与层级由采集脚本**从来源页面直接读出**（按 `<a name="secN">`
#: 这类锚点切分），没有经过模型抽取，因此不存在抽取置信度的问题。
#: 用 1.0 而不是留空：留空会让这些条在界面上显示为"置信度未知"，
#: 而实际上它们的确定性高于任何模型抽取结果。
SEED_CONFIDENCE = 1.0

#: 单批插入的条数。批太大在失败时要回滚更多，批太小则往返次数过多。
BATCH_SIZE = 500


@dataclass
class ImportSummary:
    """一次导入的结果。字段都是"事实"，不含推测。"""

    statutes_created: int = 0
    statutes_reused: int = 0
    versions_created: int = 0
    versions_skipped: int = 0
    articles_created: int = 0
    #: 未建向量的条数。向量需要调用 embedding 模型，本模块不做——
    #: 显式记下来，避免"导入了但检索不到"变成没有人知道的事
    articles_without_vector: int = 0

    def merged_with(self, other: ImportSummary) -> ImportSummary:
        return ImportSummary(
            statutes_created=self.statutes_created + other.statutes_created,
            statutes_reused=self.statutes_reused + other.statutes_reused,
            versions_created=self.versions_created + other.versions_created,
            versions_skipped=self.versions_skipped + other.versions_skipped,
            articles_created=self.articles_created + other.articles_created,
            articles_without_vector=self.articles_without_vector + other.articles_without_vector,
        )


async def import_groups(
    conn: AsyncConnection[DictRow],
    groups: list[StatuteVersionGroup],
    *,
    tenant_scope: str = PLATFORM_SCOPE,
) -> ImportSummary:
    """把按版本分组的语料写入 `kb`。

    **每个版本一个事务。** 一部法规写一半就失败时，回滚的是那一部，
    而不是整批——2,500 条语料分属几十个版本，一次网络抖动让整批回滚意味着重跑一遍全部，
    而按版本提交时最多重跑那一部。

    调用方负责不设置租户上下文（见模块 docstring）。
    """
    summary = ImportSummary()

    for group in groups:
        async with conn.transaction():
            statute_id, created = await _ensure_statute(conn, group, tenant_scope)
            if created:
                summary.statutes_created += 1
            else:
                summary.statutes_reused += 1

            existing_version = await _find_version(conn, statute_id, group, tenant_scope)
            if existing_version is not None:
                # 幂等：这一版已经在库里了，整组跳过
                summary.versions_skipped += 1
                logger.info("版本已存在，跳过 | %s / %s", group.key[1], group.key[2])
                continue

            version_id = await _create_version(conn, statute_id, group, tenant_scope)
            summary.versions_created += 1

            inserted = await _insert_articles(conn, version_id, group, tenant_scope)
            summary.articles_created += inserted
            summary.articles_without_vector += inserted

    return summary


async def _ensure_statute(
    conn: AsyncConnection[DictRow], group: StatuteVersionGroup, tenant_scope: str
) -> tuple[object, bool]:
    """返回 (statute_id, 是否新建)。

    先查后建，而不是依赖 `uq_statute_identity` 的 `ON CONFLICT`：
    那个约束包含 `statute_no`，而它可能为空——**NULL 在唯一索引里彼此不等**，
    于是冲突永远不会触发，重复导入会静默地建出多份同名法规。
    这与上传路径（`app/api/ingest.py`）采用同一策略，两处必须一致。
    """
    jurisdiction_code, title, _ = group.key
    async with conn.cursor() as cur:
        await cur.execute(
            """
            SELECT id FROM kb.statute
            WHERE jurisdiction_code = %s AND title_zh = %s
              AND (statute_no IS NOT DISTINCT FROM %s)
            LIMIT 1
            """,
            (jurisdiction_code, title, group.statute_no),
        )
        row = await cur.fetchone()
        if row:
            return row["id"], False

        await cur.execute(
            """
            INSERT INTO kb.statute
                (jurisdiction_code, title_zh, title_original, statute_no, category)
            VALUES (%s, %s, %s, %s, %s)
            RETURNING id
            """,
            (
                jurisdiction_code,
                title,
                group.statute_title_original,
                group.statute_no,
                "SEED",
            ),
        )
        created = await cur.fetchone()
    if created is None:  # pragma: no cover - RETURNING 必然有行
        raise RuntimeError(f"创建法规失败：{title}")
    return created["id"], True


async def _find_version(
    conn: AsyncConnection[DictRow],
    statute_id: object,
    group: StatuteVersionGroup,
    tenant_scope: str,
) -> object | None:
    async with conn.cursor() as cur:
        await cur.execute(
            """
            SELECT id FROM kb.statute_version
            WHERE statute_id = %s AND tenant_scope = %s AND version_label = %s
            LIMIT 1
            """,
            (statute_id, tenant_scope, group.key[2]),
        )
        row = await cur.fetchone()
    return row["id"] if row else None


async def _create_version(
    conn: AsyncConnection[DictRow],
    statute_id: object,
    group: StatuteVersionGroup,
    tenant_scope: str,
) -> object:
    async with conn.cursor() as cur:
        await cur.execute(
            """
            INSERT INTO kb.statute_version
                (statute_id, tenant_scope, version_label, effective_from, effective_to,
                 date_precision, source_url, source_fetched_at, content_hash,
                 publish_status, published_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'PUBLISHED', now())
            RETURNING id
            """,
            (
                statute_id,
                tenant_scope,
                group.key[2],
                group.effective_from,
                group.effective_to,
                group.date_precision,
                group.source_url,
                group.source_fetched_at,
                # 内容哈希在这里用「法域+名称+版本」的稳定摘要代替原件哈希：
                # 种子语料没有"原件"这个对象，而 content_hash 是非空列。
                # 用稳定摘要而不是随机值，是为了让重复导入时能看出是同一份。
                f"seed:{group.key[0]}:{group.key[1]}:{group.key[2]}",
            ),
        )
        created = await cur.fetchone()
    if created is None:  # pragma: no cover
        raise RuntimeError(f"创建法规版本失败：{group.key[1]}")
    return created["id"]


async def _insert_articles(
    conn: AsyncConnection[DictRow],
    version_id: object,
    group: StatuteVersionGroup,
    tenant_scope: str,
) -> int:
    """批量插入法条，返回插入条数。

    用 `executemany` 而不是逐条 execute：2,500 条如果逐条往返，光是握手开销
    就让导入从秒级变成分钟级；而分批提交同时限制了单次失败的影响面。
    """
    rows = [
        (
            version_id,
            tenant_scope,
            group.key[0],
            row.article_no,
            list(row.hierarchy_path),
            row.content,
            "PUBLISHED",
            row.effective_from,
            row.effective_to,
            SEED_CONFIDENCE,
        )
        for row in group.rows
    ]

    inserted = 0
    for start in range(0, len(rows), BATCH_SIZE):
        batch = rows[start : start + BATCH_SIZE]
        async with conn.cursor() as cur:
            await cur.executemany(
                """
                INSERT INTO kb.article
                    (statute_version_id, tenant_scope, jurisdiction_code, article_no,
                     hierarchy_path, content, publish_status, effective_from,
                     effective_to, confidence)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                batch,
            )
        inserted += len(batch)
    return inserted
