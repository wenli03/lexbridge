"""种子语料落库的集成测试。

**默认跳过**，需要环境变量 `TEST_DATABASE_URL` 指向一个已应用 `V1`~`V3` 迁移的
PostgreSQL（含 pgvector 扩展）。

刻意不做成"有数据库就跑、没有就跳过"之外的花样：导入逻辑全部包在事务里，
用假连接测它，测到的是"我调用了哪几条 SQL"，而不是"数据真的写进去了没"。
而这里真正可能出错的地方恰恰在后者——列与约束的匹配、`text[]` 的传参、
RLS 的放行与否。

连接**必须使用非表属主角色**（`lexbridge_ai`）：表属主会绕过 RLS，
于是本文件里那条"租户上下文下不能写平台库"的断言会永远通过，等于没测。
"""

from __future__ import annotations

from uuid import uuid4

import psycopg
import pytest

from app.core.tenant import bind_tenant
from app.indexing.seed_db import PLATFORM_SCOPE, import_groups
from conftest import load_corpus_groups, requires_db

pytestmark = requires_db


class TestImport:
    async def test_import_writes_all_real_articles(self, db) -> None:
        groups, expected_rows = load_corpus_groups()
        summary = await import_groups(db, groups)

        assert summary.articles_created == expected_rows
        assert summary.versions_created == len(groups)
        assert summary.statutes_created == len(groups)

        async with db.cursor() as cur:
            await cur.execute(
                "SELECT count(*) AS n FROM kb.article WHERE tenant_scope = %s",
                (PLATFORM_SCOPE,),
            )
            row = await cur.fetchone()
        assert row is not None and row["n"] == expected_rows

    async def test_imported_articles_are_published(self, db) -> None:
        """只有 `PUBLISHED` 才会被检索到（检索 SQL 恒定带该过滤）。

        筛选项写错的表现是"导入了 2,500 条，但检索一条都命中不了"，
        而任务日志显示一切正常。
        """
        groups, _ = load_corpus_groups()
        await import_groups(db, groups)

        async with db.cursor() as cur:
            await cur.execute(
                """
                SELECT publish_status, count(*) AS n
                FROM kb.article GROUP BY publish_status
                """
            )
            counts = {row["publish_status"]: row["n"] for row in await cur.fetchall()}
        assert set(counts) == {"PUBLISHED"}

    async def test_second_import_is_idempotent(self, db) -> None:
        """重跑不产生第二份数据。

        这条断言守护的是"管理员点两次导入"这个真实场景：
        如果重跑会重复插入，知识库里会出现两份同名法规，
        而引用会随机落到其中一份上。
        """
        groups, expected_rows = load_corpus_groups()
        first = await import_groups(db, groups)
        second = await import_groups(db, groups)

        assert first.articles_created == expected_rows
        assert second.articles_created == 0
        assert second.versions_created == 0
        assert second.versions_skipped == len(groups)

        async with db.cursor() as cur:
            await cur.execute("SELECT count(*) AS n FROM kb.article")
            row = await cur.fetchone()
        assert row is not None and row["n"] == expected_rows

    async def test_traceability_survives_the_round_trip(self, db) -> None:
        """AC-1.6：落库后仍能解析出来源 URL（溯源字段在版本层）。"""
        groups, _ = load_corpus_groups()
        await import_groups(db, groups)

        async with db.cursor() as cur:
            await cur.execute(
                """
                SELECT count(*) AS n
                FROM kb.article a
                JOIN kb.statute_version v ON v.id = a.statute_version_id
                WHERE v.source_url IS NULL OR v.source_url = ''
                """
            )
            row = await cur.fetchone()
        assert row is not None and row["n"] == 0

    async def test_year_precision_is_preserved(self, db) -> None:
        """爱尔兰的 YEAR 精度必须原样落库，不能被"补全"成具体日期。"""
        groups, _ = load_corpus_groups()
        await import_groups(db, groups)

        async with db.cursor() as cur:
            await cur.execute(
                """
                SELECT s.jurisdiction_code, v.date_precision, count(*) AS n
                FROM kb.statute_version v
                JOIN kb.statute s ON s.id = v.statute_id
                GROUP BY s.jurisdiction_code, v.date_precision
                """
            )
            combos = {
                (row["jurisdiction_code"], row["date_precision"]) for row in await cur.fetchall()
            }
        assert ("IE", "YEAR") in combos


class TestRlsConstraint:
    """导入路径为什么不绑定租户上下文——用数据库行为来证明，而不是靠注释。"""

    async def test_tenant_context_cannot_write_platform_rows(self, db) -> None:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            async with db.transaction():
                await bind_tenant(db, uuid4())
                await db.execute(
                    """
                    INSERT INTO kb.statute (jurisdiction_code, title_zh)
                    VALUES ('IE', '不应写入成功')
                    """
                )
                await db.execute(
                    """
                    INSERT INTO kb.statute_version
                        (statute_id, tenant_scope, version_label, effective_from, content_hash)
                    SELECT id, 'PLATFORM', 'x', DATE '2020-01-01', 'h'
                    FROM kb.statute WHERE title_zh = '不应写入成功'
                    """
                )

    async def test_platform_rows_are_visible_to_a_tenant(self, db) -> None:
        """反面：写不进去，但读得到。

        这两条断言合起来才是完整语义——公共法条库对租户**只读**。
        """
        groups, expected_rows = load_corpus_groups()
        await import_groups(db, groups)

        tenant_id = uuid4()
        async with db.transaction():
            await bind_tenant(db, tenant_id)
            async with db.cursor() as cur:
                await cur.execute(
                    "SELECT count(*) AS n FROM kb.article WHERE tenant_scope = %s",
                    (PLATFORM_SCOPE,),
                )
                row = await cur.fetchone()
        assert row is not None and row["n"] == expected_rows
