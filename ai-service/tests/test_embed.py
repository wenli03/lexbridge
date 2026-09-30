"""法条向量化的测试。

**关于测试用的向量，有一件事必须先说清楚**：这里用的是 `_pseudo_vector` 生成的
确定性伪向量（与 `MODEL_MODE=mock` 同源），它们**不携带任何语义**。
因此本文件验证的是**存储与重算逻辑**——向量有没有写进去、模型换了会不会重算、
维度不对会不会在写库前就报错；它**不验证检索质量**，也不构成"语义检索可用"的证据。
检索质量要有真实向量与标注集才能谈，那是评测的事。

需要数据库的用例由 `requires_db` 门控，用 `MODEL_MODE=mock` 的等价物
（伪向量）跑，**不联网、不需密钥**。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from langchain_core.embeddings import Embeddings

from app.chains.replay import _pseudo_vector
from app.indexing.embed import (
    MAX_EMBED_CHARS,
    _to_pg_vector,
    embed_pending_articles,
    embedding_text,
)
from app.indexing.seed_db import import_groups
from conftest import load_corpus_groups, requires_db

DIM = 1024
MODEL = "test-embedding-model"


class RecordingEmbeddings(Embeddings):
    """记录收到的文本，返回确定性伪向量。用来断言"送进去的是什么"。"""

    def __init__(self, dim: int = DIM) -> None:
        self.dim = dim
        self.texts: list[str] = []

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        self.texts.extend(texts)
        return [_pseudo_vector(text, self.dim) for text in texts]

    def embed_query(self, text: str) -> list[float]:
        return _pseudo_vector(text, self.dim)


# =============================================================================
# 纯逻辑
# =============================================================================
class TestEmbeddingText:
    def test_text_carries_the_statute_name_and_article_number(self) -> None:
        """正文里常常不出现法律名称，所以名称必须由我们拼进去。"""
        text = embedding_text(
            statute_title="荷兰《1990 年税收征收法》",
            article_no="Artikel 1",
            content="1 Deze wet geldt bij de invordering van rijksbelastingen.",
        )
        assert "荷兰《1990 年税收征收法》" in text
        assert "Artikel 1" in text
        assert "Deze wet geldt" in text


class TestVectorLiteral:
    def test_literal_has_all_components(self) -> None:
        literal = _to_pg_vector([0.0] * DIM)
        assert literal.startswith("[") and literal.endswith("]")
        assert len(literal.strip("[]").split(",")) == DIM

    def test_literal_is_parseable_by_postgres_notation(self) -> None:
        assert _to_pg_vector([1.5, -2.25]) == "[1.5,-2.25]"


class TestDimensionGuard:
    async def test_wrong_dimension_is_rejected_before_writing(self) -> None:
        """维度不符必须在写库前报错，并指出是配置问题。

        若放到 INSERT 阶段才失败，错误会是一句 psycopg 的转换异常，
        看不出"模型配置与表定义不一致"这个真实原因。
        """
        from app.indexing.embed import _embed

        with pytest.raises(RuntimeError) as excinfo:
            await _embed(
                ["文本"], embeddings=RecordingEmbeddings(dim=768), model=MODEL, expected_dim=DIM
            )
        message = str(excinfo.value)
        assert "768" in message and str(DIM) in message
        assert MODEL in message

    async def test_length_mismatch_is_rejected(self) -> None:
        """向量与文本数量错位时宁可报错，也不要错位写库。

        错位的后果是把 A 条的向量挂到 B 条上：检索照常返回结果，
        只是指向了错误的法条——典型的"看起来对"的错误。
        """
        from app.indexing.embed import _embed

        class DroppingEmbeddings(RecordingEmbeddings):
            def embed_documents(self, texts: list[str]) -> list[list[float]]:
                return [_pseudo_vector(t, self.dim) for t in texts[:-1]]

        with pytest.raises(RuntimeError, match="期望"):
            await _embed(
                ["一", "二"], embeddings=DroppingEmbeddings(), model=MODEL, expected_dim=DIM
            )


# =============================================================================
# 真实数据库
# =============================================================================
async def insert_article(conn, *, content: str, article_no: str = "Section 99") -> None:
    """插入一条平台级的法条，用于构造语料里没有的边界情况。"""
    async with conn.cursor() as cur:
        await cur.execute(
            """
            INSERT INTO kb.statute (jurisdiction_code, title_zh, statute_no)
            VALUES ('IE', '爱尔兰《测试法》', 'TEST-1') RETURNING id
            """
        )
        statute_id = (await cur.fetchone())["id"]
        await cur.execute(
            """
            INSERT INTO kb.statute_version
                (statute_id, tenant_scope, version_label, effective_from,
                 content_hash, publish_status, source_url)
            VALUES (%s, 'PLATFORM', 'enacted', DATE '2020-01-01', 'test-hash',
                    'PUBLISHED', 'https://example.test/act')
            RETURNING id
            """,
            (statute_id,),
        )
        version_id = (await cur.fetchone())["id"]
        await cur.execute(
            """
            INSERT INTO kb.article
                (statute_version_id, tenant_scope, jurisdiction_code, article_no,
                 hierarchy_path, content, publish_status, effective_from)
            VALUES (%s, 'PLATFORM', 'IE', %s, %s, %s, 'PUBLISHED', DATE '2020-01-01')
            """,
            (version_id, article_no, ["Test Act", article_no], content),
        )


@requires_db
class TestEmbedAgainstDatabase:
    async def test_all_published_articles_get_a_vector(self, db) -> None:
        groups, expected_rows = load_corpus_groups()
        await import_groups(db, groups)

        embeddings = RecordingEmbeddings()
        summary = await embed_pending_articles(
            db, embeddings=embeddings, model=MODEL, expected_dim=DIM
        )

        assert summary.candidates == expected_rows
        assert summary.embedded == expected_rows
        assert summary.pending_remaining == 0
        assert len(embeddings.texts) == expected_rows

        async with db.cursor() as cur:
            await cur.execute("SELECT count(*) AS n FROM kb.article_vector")
            row = await cur.fetchone()
        assert row is not None and row["n"] == expected_rows

    async def test_vectors_record_which_model_produced_them(self, db) -> None:
        groups, _ = load_corpus_groups()
        await import_groups(db, groups)
        await embed_pending_articles(
            db, embeddings=RecordingEmbeddings(), model=MODEL, expected_dim=DIM
        )

        async with db.cursor() as cur:
            await cur.execute("SELECT DISTINCT embedding_model FROM kb.article_vector")
            models = {row["embedding_model"] for row in await cur.fetchall()}
        assert models == {MODEL}

    async def test_second_run_is_a_no_op(self, db) -> None:
        groups, expected_rows = load_corpus_groups()
        await import_groups(db, groups)
        await embed_pending_articles(
            db, embeddings=RecordingEmbeddings(), model=MODEL, expected_dim=DIM
        )

        again = await embed_pending_articles(
            db, embeddings=RecordingEmbeddings(), model=MODEL, expected_dim=DIM
        )
        assert again.candidates == 0
        assert again.embedded == 0
        assert again.chunks_committed == 0
        assert again.pending_remaining == 0

        async with db.cursor() as cur:
            await cur.execute("SELECT count(*) AS n FROM kb.article_vector")
            row = await cur.fetchone()
        assert row is not None and row["n"] == expected_rows

    async def test_changing_the_model_forces_reembedding(self, db) -> None:
        """换模型必须重算全量。

        不同模型的向量不在同一空间里。若沿用旧向量，距离比较仍然会返回
        一组看起来正常的排序——**这是本项目里最隐蔽的一类错误**：
        没有任何报错，检索质量却已经失效。
        """
        groups, expected_rows = load_corpus_groups()
        await import_groups(db, groups)
        await embed_pending_articles(
            db, embeddings=RecordingEmbeddings(), model="model-a", expected_dim=DIM
        )

        summary = await embed_pending_articles(
            db, embeddings=RecordingEmbeddings(), model="model-b", expected_dim=DIM
        )
        assert summary.candidates == expected_rows
        assert summary.reembedded_stale == expected_rows
        assert summary.pending_remaining == 0

        async with db.cursor() as cur:
            await cur.execute("SELECT DISTINCT embedding_model FROM kb.article_vector")
            models = {row["embedding_model"] for row in await cur.fetchall()}
        assert models == {"model-b"}

    async def test_vectors_are_queryable_by_pgvector(self, db) -> None:
        """证明向量列真的能被 pgvector 用来算距离。

        **只断言"能查、行数对、距离递增"，不断言排序正确性**——
        伪向量没有语义，谁排第一是任意的。断言排序正确会是一个假的通过。
        """
        groups, expected_rows = load_corpus_groups()
        await import_groups(db, groups)
        await embed_pending_articles(
            db, embeddings=RecordingEmbeddings(), model=MODEL, expected_dim=DIM
        )

        probe = _to_pg_vector(_pseudo_vector("荷兰 税收 征收", DIM))
        async with db.cursor() as cur:
            await cur.execute(
                """
                SELECT article_id, embedding <=> %s::vector AS distance
                FROM kb.article_vector
                ORDER BY distance
                LIMIT 5
                """,
                (probe,),
            )
            hits = await cur.fetchall()
        assert len(hits) == 5
        distances = [hit["distance"] for hit in hits]
        assert all(distance >= 0 for distance in distances)
        assert distances == sorted(distances)
        assert len({hit["article_id"] for hit in hits}) == 5

    async def test_wrong_dimension_writes_nothing(self, db) -> None:
        """维度不符时不能留下半批数据。"""
        groups, _ = load_corpus_groups()
        await import_groups(db, groups)

        with pytest.raises(RuntimeError):
            await embed_pending_articles(
                db, embeddings=RecordingEmbeddings(), model=MODEL, expected_dim=768
            )

        async with db.cursor() as cur:
            await cur.execute("SELECT count(*) AS n FROM kb.article_vector")
            row = await cur.fetchone()
        assert row is not None and row["n"] == 0

    async def test_long_clause_is_truncated_before_embedding(self, db) -> None:
        """超长条文必须在调用前截断，且截断被计入统计。

        不截断的后果是模型端报错或静默截断——后者更难查，
        因为流水线会正常完成，只是那条的向量只反映了开头一部分。

        断言写成「基线 + 增量」而不是绝对值：**真实语料本身就有大量超长条文**
        （实测 2,563 条里 570 条超过默认上限），写死"只有 1 条被截断"会直接失败。
        这个写法既精确又不依赖语料的当前规模。
        """
        groups, _ = load_corpus_groups()
        await import_groups(db, groups)
        # 先把真实语料处理掉，让下一轮只剩新插入的那一条
        baseline = await embed_pending_articles(
            db, embeddings=RecordingEmbeddings(), model=MODEL, expected_dim=DIM
        )
        assert baseline.truncated > 0, "真实语料里应当存在超长条文（实测 557 条）"

        await insert_article(db, content="长" * (MAX_EMBED_CHARS + 500), article_no="Section 98")

        embeddings = RecordingEmbeddings()
        summary = await embed_pending_articles(
            db, embeddings=embeddings, model=MODEL, expected_dim=DIM
        )

        assert summary.candidates == 1
        assert summary.truncated == 1
        assert summary.embedded == 1
        assert all(len(text) <= MAX_EMBED_CHARS for text in embeddings.texts)

    async def test_max_chars_is_configurable(self, db) -> None:
        """上限可调，且调低后确实生效——这是调整检索覆盖面的入口。"""
        groups, _ = load_corpus_groups()
        await import_groups(db, groups)

        embeddings = RecordingEmbeddings()
        summary = await embed_pending_articles(
            db, embeddings=embeddings, model=MODEL, expected_dim=DIM, max_chars=200
        )
        assert all(len(text) <= 200 for text in embeddings.texts)
        assert summary.truncated > summary.candidates / 2

    async def test_whitespace_only_article_is_skipped_not_embedded(self, db) -> None:
        """空白正文不应产生向量。

        空白嵌出来的是一个"什么都不像"的向量，它会在**任何**查询里
        以中等相似度被召回——污染结果且没有任何迹象。
        """
        groups, _ = load_corpus_groups()
        await import_groups(db, groups)
        await insert_article(db, content="   \n\t  ", article_no="Section 97")

        embeddings = RecordingEmbeddings()
        summary = await embed_pending_articles(
            db, embeddings=embeddings, model=MODEL, expected_dim=DIM
        )

        assert summary.skipped_empty == 1
        assert summary.embedded == summary.candidates - 1
        # 它仍然算"待处理"，因为它是平台库里一条已发布但没有向量的法条
        assert summary.pending_remaining == 1


@requires_db
class TestLimit:
    async def test_limit_leaves_the_rest_pending(self, db) -> None:
        """limit 截断时 `pending_remaining` 必须如实反映还有多少没做。

        这个数字是"要不要再跑一次"的唯一依据；
        它若恒为 0，管理员会以为向量化已经完成。
        """
        groups, expected_rows = load_corpus_groups()
        await import_groups(db, groups)

        summary = await embed_pending_articles(
            db, embeddings=RecordingEmbeddings(), model=MODEL, expected_dim=DIM, limit=100
        )
        assert summary.embedded == 100
        assert summary.pending_remaining == expected_rows - 100


def test_corpus_dir_is_reachable() -> None:
    """语料目录必须存在——它是向量化与导入的共同输入。"""
    corpus = Path(__file__).resolve().parents[2] / "deploy" / "seed" / "corpus"
    assert corpus.is_dir()
    assert list(corpus.glob("*.jsonl"))
