"""混合检索的测试。

**融合数学与租户强制读取是纯逻辑，在这里穷尽地测**；SQL 与 RLS 的交互需要真实
数据库，放在 `TestRetrievalAgainstDatabase`（由 `requires_db` 门控）。

这么分的原因：融合的边界情况（某条只被一路召回、两路名次相差很大、
分数撞车）用纯函数能测得很彻底，而走数据库测它们只会让用例又慢又难读。
"""

from __future__ import annotations

import pytest

from app.core.tenant import (
    TenantContext,
    require_current_tenant,
    reset_tenant_context,
    set_tenant_context,
)
from app.indexing.seed_db import import_groups
from app.retrieval.hybrid import (
    RRF_K,
    SearchScope,
    escape_like,
    fuse_rrf,
    retrieve,
)
from conftest import load_corpus_groups, requires_db


class TestEscapeLike:
    def test_percent_is_escaped(self) -> None:
        """`%` 不转义会把"税率为 25%"变成"税率为 25 后接任意内容"。"""
        assert escape_like("税率为 25%") == "税率为 25\\%"

    def test_underscore_is_escaped(self) -> None:
        assert escape_like("a_b") == "a\\_b"

    def test_backslash_is_escaped_first(self) -> None:
        """反斜杠必须先处理，否则它会把新加的转义符再转义一次。"""
        assert escape_like("a\\b") == "a\\\\b"
        assert escape_like("50\\%") == "50\\\\\\%"

    def test_plain_text_is_untouched(self) -> None:
        assert escape_like("常设机构") == "常设机构"


class TestFuseRrf:
    def test_single_route_keeps_its_order(self) -> None:
        fused = fuse_rrf({"vector": ["a", "b", "c"]})
        assert [item[0] for item in fused] == ["a", "b", "c"]

    def test_score_matches_the_formula(self) -> None:
        fused = fuse_rrf({"vector": ["a", "b"]})
        scores = {article_id: score for article_id, score, _ in fused}
        assert scores["a"] == pytest.approx(1.0 / (RRF_K + 1))
        assert scores["b"] == pytest.approx(1.0 / (RRF_K + 2))

    def test_agreement_across_routes_outranks_a_single_first_place(self) -> None:
        """两路都排第二，胜过只有一路排第一。

        这正是 RRF 的设计意图：**多路一致**比**单路极度自信**更可信。
        若这条断言反了，说明融合退化成了"取某一路的结果"。
        """
        fused = fuse_rrf(
            {
                "vector": ["x", "agree"],
                "lexical": ["y", "agree"],
            }
        )
        assert fused[0][0] == "agree"

    def test_route_ranks_are_one_based_and_recorded_per_route(self) -> None:
        fused = fuse_rrf({"vector": ["a", "b"], "substring": ["b"]})
        ranks = {article_id: r for article_id, _, r in fused}
        assert ranks["a"] == {"vector": 1}
        assert ranks["b"] == {"vector": 2, "substring": 1}

    def test_ties_are_broken_by_best_rank_then_id(self) -> None:
        """RRF 分数是离散的，撞分很常见，次序必须稳定且可解释。"""
        fused = fuse_rrf({"vector": ["b"], "lexical": ["a"]})
        # a 与 b 分数相同（都是 1/(60+1)），此时按 id 兜底
        assert [item[0] for item in fused] == ["a", "b"]

    def test_empty_route_is_ignored(self) -> None:
        fused = fuse_rrf({"vector": ["a"], "lexical": [], "substring": []})
        assert [item[0] for item in fused] == ["a"]

    def test_no_input_gives_no_output(self) -> None:
        assert fuse_rrf({}) == []

    def test_item_only_in_one_route_is_still_returned(self) -> None:
        """三路各自的独有召回都要能进结果——某路独有恰恰是它存在的理由。"""
        fused = fuse_rrf({"vector": ["v"], "lexical": ["l"], "substring": ["s"]})
        assert {item[0] for item in fused} == {"v", "l", "s"}


class TestTenantIsMandatory:
    """`详细设计 §6.4`：租户不是参数，缺失即抛。"""

    async def test_retrieve_without_context_is_rejected(self) -> None:
        """没有租户上下文时检索必须抛，**而不是"查全部"**。

        连接传 None 是刻意的：如果实现先取上下文再碰连接，
        这个测试就能在完全不连库的情况下证明那条纪律；
        若它改成先连库再取上下文，这个测试会以另一种错误失败——
        两种失败都能说明问题，而不是静默通过。
        """
        from app.core.tenant import _CURRENT_TENANT

        token = _CURRENT_TENANT.set(None)
        try:
            with pytest.raises(RuntimeError, match="租户上下文"):
                await retrieve(
                    conn=None,  # type: ignore[arg-type]
                    query="常设机构",
                    scope=SearchScope(jurisdictions=("NL",)),
                )
        finally:
            _CURRENT_TENANT.reset(token)

    def test_require_current_tenant_raises_when_unset(self) -> None:
        from app.core.tenant import _CURRENT_TENANT

        token = _CURRENT_TENANT.set(None)
        try:
            with pytest.raises(RuntimeError, match="租户上下文"):
                require_current_tenant()
        finally:
            _CURRENT_TENANT.reset(token)


# =============================================================================
# 真实数据库
# =============================================================================
async def embed_corpus(db) -> None:
    """导入并向量化真实语料，供检索测试使用。"""
    from app.indexing.embed import embed_pending_articles
    from test_embed import DIM, MODEL, RecordingEmbeddings

    groups, _ = load_corpus_groups()
    await import_groups(db, groups)
    await embed_pending_articles(
        db, embeddings=RecordingEmbeddings(), model=MODEL, expected_dim=DIM
    )


@requires_db
class TestRetrievalAgainstDatabase:
    async def test_returns_platform_articles_for_a_tenant(self, db) -> None:
        from uuid import uuid4

        from app.core.tenant import bind_tenant

        await embed_corpus(db)
        tenant_id = uuid4()

        async with db.transaction():
            await bind_tenant(db, tenant_id)
            token = set_tenant_context(
                TenantContext(tenant_id=tenant_id, user_id=None, run_id=None)
            )
            try:
                result = await retrieve(
                    db,
                    query="invordering van rijksbelastingen",
                    scope=SearchScope(jurisdictions=("NL",), top_k=5),
                    embedder=None,  # 只用关键词/子串路，避免依赖向量维度
                )
            finally:
                reset_tenant_context(token)

        assert result.articles, "关键词路应当能召回荷兰语条文"
        assert all(item.article.tenant_scope == "PLATFORM" for item in result.articles)
        # 至少一路有召回，且每条都带得回它来自哪一路
        assert result.route_sizes.get("lexical") or result.route_sizes.get("substring")
        assert all(item.route_ranks for item in result.articles)

    async def test_jurisdiction_filter_excludes_other_jurisdictions(self, db) -> None:
        """法域过滤必须生效：查 CN 不应返回 IE/NL 的条文。"""
        from uuid import uuid4

        from app.core.tenant import bind_tenant

        await embed_corpus(db)
        tenant_id = uuid4()

        async with db.transaction():
            await bind_tenant(db, tenant_id)
            token = set_tenant_context(
                TenantContext(tenant_id=tenant_id, user_id=None, run_id=None)
            )
            try:
                result = await retrieve(
                    db,
                    query="invordering",
                    scope=SearchScope(jurisdictions=("CN",), top_k=5),
                    embedder=None,
                )
            finally:
                reset_tenant_context(token)

        assert result.articles == []

    async def test_tenant_private_article_is_invisible_to_others(self, db) -> None:
        """AC-5.1：跨租户检索成功次数必须为 0。

        构造一条只属于租户 A 的条文，然后用租户 B 的上下文检索——
        它不应当出现在结果里。**这条用例是整套隔离机制的核心断言**。
        """
        from uuid import uuid4

        from app.core.tenant import bind_tenant

        tenant_a, tenant_b = uuid4(), uuid4()
        marker = "ZEEPAARD_ALLEEN_VOOR_A"

        # **插入租户专属行必须绑定该租户**：`kb.statute_version` / `kb.article` 的
        # RLS `WITH CHECK` 只允许写入"当前租户"或平台上下文的行。
        # 这一约束本身已经在 test_seed_db 里被反向验证过（租户上下文写不了平台行），
        # 这里顺着它来构造数据，而不是绕过它。
        async with db.transaction():
            await bind_tenant(db, tenant_a)
            async with db.cursor() as cur:
                await cur.execute(
                    """
                    INSERT INTO kb.statute (jurisdiction_code, title_zh, statute_no)
                    VALUES ('NL', '荷兰《租户A专属测试法》', 'PRIV-A') RETURNING id
                    """
                )
                statute_id = (await cur.fetchone())["id"]
                await cur.execute(
                    """
                    INSERT INTO kb.statute_version
                        (statute_id, tenant_scope, version_label, effective_from,
                         content_hash, publish_status)
                    VALUES (%s, %s, 'v1', DATE '2020-01-01', 'priv', 'PUBLISHED')
                    RETURNING id
                    """,
                    (statute_id, str(tenant_a)),
                )
                version_id = (await cur.fetchone())["id"]
                await cur.execute(
                    """
                    INSERT INTO kb.article
                        (statute_version_id, tenant_scope, jurisdiction_code, article_no,
                         hierarchy_path, content, publish_status, effective_from)
                    VALUES (%s, %s, 'NL', 'Artikel 1', %s, %s, 'PUBLISHED', DATE '2020-01-01')
                    """,
                    (
                        version_id,
                        str(tenant_a),
                        ["Priv"],
                        f"{marker} Deze bepaling is uitsluitend voor tenant A bestemd.",
                    ),
                )

        async with db.transaction():
            await bind_tenant(db, tenant_b)
            token = set_tenant_context(TenantContext(tenant_id=tenant_b, user_id=None, run_id=None))
            try:
                result = await retrieve(
                    db,
                    query=marker,
                    scope=SearchScope(jurisdictions=("NL",), top_k=20),
                    embedder=None,
                )
            finally:
                reset_tenant_context(token)

        leaked = [item for item in result.articles if marker in item.article.content]
        assert leaked == [], "租户 B 检索到了租户 A 的专属条文"

    async def test_as_of_excludes_articles_not_yet_in_force(self, db) -> None:
        """时点过滤：在生效日之前提问，不应命中该条（AC-1.4 的基础）。"""
        from uuid import uuid4

        from app.core.tenant import bind_tenant

        await embed_corpus(db)
        tenant_id = uuid4()
        keyword = "invordering"

        async def search(as_of: str) -> list[str]:
            async with db.transaction():
                await bind_tenant(db, tenant_id)
                token = set_tenant_context(
                    TenantContext(tenant_id=tenant_id, user_id=None, run_id=None)
                )
                try:
                    result = await retrieve(
                        db,
                        query=keyword,
                        scope=SearchScope(jurisdictions=("NL",), as_of=as_of, top_k=10),
                        embedder=None,
                    )
                finally:
                    reset_tenant_context(token)
            return [item.article.article_id for item in result.articles]

        # 语料里荷兰法律的生效日期都在 1990 年之后
        ancient = await search("1970-01-01")
        recent = await search("2026-09-30")
        assert ancient == []
        assert recent, "当前时点应当能召回条文"

    async def test_rerank_miss_degrades_instead_of_failing(self, db) -> None:
        """回放模式未录制该查询时，必须退回 RRF 次序并声明降级。"""
        from uuid import uuid4

        from app.chains.replay import ReplayReranker
        from app.core.tenant import bind_tenant

        await embed_corpus(db)
        tenant_id = uuid4()

        async with db.transaction():
            await bind_tenant(db, tenant_id)
            token = set_tenant_context(
                TenantContext(tenant_id=tenant_id, user_id=None, run_id=None)
            )
            try:
                result = await retrieve(
                    db,
                    query="invordering van rijksbelastingen",
                    scope=SearchScope(jurisdictions=("NL",), top_k=5),
                    embedder=None,
                    reranker=ReplayReranker([]),  # 空 fixture → 必然未命中
                )
            finally:
                reset_tenant_context(token)

        assert result.articles
        assert any("重排未命中" in note for note in result.degraded)
        assert all(item.rerank_score is None for item in result.articles)
