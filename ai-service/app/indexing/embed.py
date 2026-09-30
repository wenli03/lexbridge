"""向量化：把已发布的法条写成 `kb.article_vector` 的行。

没有向量，法条只能靠关键词检索命中；有向量，才谈得上"问一个意图、命中一批条文"。

**这个模块处理三件容易被忽略的事**：

1. **已有向量但模型变了，必须重算。** 不同模型的向量不在同一个空间里，
   把它们混在同一列里做距离比较，得到的是**看起来正常的错误结果**——
   检索能返回行、分数也在合理范围，只是排序毫无意义。
   因此判据是 `embedding_model`，不是"有没有向量"。
2. **写入前校验维度。** `kb.article_vector.embedding` 是 `vector(1024)`。
   若配置换了模型而维度不同，向量会在 INSERT 时才被 pgvector 拒绝，
   错误信息是一句 psycopg 的转换失败，看不出是模型配置的问题。
   在调用点显式比对维度，能把错误变成"配置了 X 维、表要求 1024 维"。
3. **按批提交，而不是攒到最后。** 2,500 条向量一次写进去，中途一次网络抖动
   就要从头再来；按批提交最多损失一批。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from langchain_core.embeddings import Embeddings
from psycopg import AsyncConnection
from psycopg.rows import DictRow

logger = logging.getLogger(__name__)

PLATFORM_SCOPE = "PLATFORM"

#: 单次提交的条数。**不取模型的 128 批量**，取它的两倍：128 次的节奏下
#: 每 128 条就要往返一次数据库提交，而提交本身比多攒 128 条的开销更大。
#: 也不取更大：这批是"失败时重做的单位"，2,500 条里做丢 256 条是可以接受的，
#: 做丢 2,500 条不是。
COMMIT_BATCH = 256

#: 送进 embedding 的文本上限（字符）。
#: 超长条文（部分爱尔兰法案的单条包含整张附表）会超出模型的上下文窗口，
#: 表现是接口报错或服务端静默截断——两者都比我们自己截断更难查。
#: 截断只影响**语义**检索：`kb.article.content` 是完整的，
#: 关键词检索（`content_tsv`）仍然能命中被截掉的尾部。
#:
#: **这个值是被实测修正过的**：初次设为 3000 时，真实语料里 2,563 条中有 570 条
#: （约 22%）被截断——比我原先估的高一个量级。它现在既是默认值也是可传参数，
#: 且截断比例会被记进摘要并告警，因为"两成条文只嵌了开头"是会影响检索质量的事实，
#: 不该只出现在实现者的脑子里。
MAX_EMBED_CHARS = 3000

#: 截断比例超过这个值时告警。取 5%：低于它不值得打断日志，
#: 高于它说明上限或语料有一方需要人工看一眼。
TRUNCATION_WARN_RATIO = 0.05

#: 向量以 `'[0.1,0.2,…]'` 文本形式传参并显式 `::vector` 转换。
#: 不用 psycopg 的 pgvector 适配器：那需要额外注册类型，
#: 而注册发生在连接创建时——漏注册的表现是"本地能跑、某个入口报转换错误"。
#: 文本形式零依赖，代价是每行多几百字节的网络传输。
_VECTOR_PLACEHOLDER = "%s::vector"


@dataclass
class EmbedSummary:
    """一次向量化的结果。所有字段都是实测数字。"""

    model: str = ""
    candidates: int = 0
    embedded: int = 0
    reembedded_stale: int = 0
    skipped_empty: int = 0
    truncated: int = 0
    chunks_committed: int = 0
    #: 跑完后仍待向量的条数。**这个数字是本模块最该被看的一个**：
    #: 它 >0 意味着调用方传了 limit 而语料还没处理完，
    #: 或者有别的写入路径同时在插法条。
    pending_remaining: int = 0


def embedding_text(*, statute_title: str, article_no: str, content: str) -> str:
    """构造送进 embedding 的文本。

    **为什么不直接嵌 `content`。** 大量法条的正文以指示代词开头，例如荷兰的
    "1 Deze wet geldt bij de invordering van rijksbelastingen."（本法适用于……），
    整条里不出现一次法律名称。只嵌正文的话，一个问"荷兰税收征收法怎么规定"的用户
    在向量空间里几乎碰不到它——而这条恰恰是最该被命中的。

    因此把「法规名 + 条号」拼在正文前面，让每条向量自带上下文。
    这是**文档侧**的增强，查询侧照常嵌入，不需要配套改动
    （同空间模型下文档前缀不会把查询推远）。
    """
    return f"{statute_title} {article_no}：{content}".strip()


async def embed_pending_articles(
    conn: AsyncConnection[DictRow],
    *,
    embeddings: Embeddings,
    model: str,
    expected_dim: int,
    tenant_scope: str = PLATFORM_SCOPE,
    limit: int | None = None,
    max_chars: int = MAX_EMBED_CHARS,
) -> EmbedSummary:
    """为缺向量或模型已过期的法条生成向量。

    `embeddings` 由调用方传入（来自 `model_factory.get_embeddings`），
    本模块不决定用哪个模型——那是配置与运行模式的事。

    `max_chars` 可调的原因见 `MAX_EMBED_CHARS` 的说明：真实语料里有约两成条文
    超过默认上限，调整它需要一份实测数据来支撑，而不是改代码常量。

    调用方负责不设置租户上下文（与 `seed_db` 同理：平台行只能在平台上下文写入）。
    """
    summary = EmbedSummary(model=model)
    rows = await _fetch_candidates(conn, tenant_scope=tenant_scope, model=model, limit=limit)
    summary.candidates = len(rows)

    if not rows:
        logger.info("没有需要向量化的法条（模型 %s）", model)
        summary.pending_remaining = await _count_pending(conn, tenant_scope, model)
        return summary

    for start in range(0, len(rows), COMMIT_BATCH):
        batch = rows[start : start + COMMIT_BATCH]
        texts: list[str] = []
        payloads: list[tuple[object, str, bool]] = []

        for row in batch:
            content = (row["content"] or "").strip()
            if not content:
                # 空正文嵌出来的是一个"什么都不像"的向量，它会在检索时
                # 以中等相似度出现在任何查询里——污染召回而无人察觉。
                summary.skipped_empty += 1
                continue
            text = embedding_text(
                statute_title=row["statute_title"] or "",
                article_no=row["article_no"] or "",
                content=content,
            )
            truncated = len(text) > max_chars
            if truncated:
                text = text[:max_chars]
                summary.truncated += 1
            texts.append(text)
            payloads.append((row["id"], row["existing_model"], truncated))

        if not texts:
            continue

        vectors = await _embed(texts, embeddings=embeddings, model=model, expected_dim=expected_dim)

        await _upsert_vectors(
            conn,
            [
                (article_id, vector, model)
                for (article_id, _existing, _truncated), vector in zip(
                    payloads, vectors, strict=True
                )
            ],
        )
        summary.chunks_committed += 1

        for _article_id, existing_model, _truncated in payloads:
            summary.embedded += 1
            if existing_model:
                # 有旧向量却被重算 → 它属于"模型变更导致的过期"
                summary.reembedded_stale += 1

        logger.info(
            "向量化进度 %d/%d（本批 %d 条，模型 %s）",
            min(start + COMMIT_BATCH, len(rows)),
            len(rows),
            len(texts),
            model,
        )

    summary.pending_remaining = await _count_pending(conn, tenant_scope, model)

    # 截断比例必须显式告警，而不是只躺在摘要里。
    # 依据：真实语料首次跑出 570/2563（22%）的截断率，而当时没有任何地方提示，
    # 是测试断言把它顶出来的。这类"质量下降但不报错"的情况需要一个出口。
    embedded_total = summary.embedded
    if embedded_total and summary.truncated / embedded_total > TRUNCATION_WARN_RATIO:
        logger.warning(
            "截断比例偏高：%d/%d（%.0f%%）条法条的嵌入文本被截到 %d 字符。"
            "被截掉的尾部仍可被关键词检索命中，但语义检索对其无覆盖。"
            "若需完整覆盖，请上调 max_chars 并确认模型上下文窗口足够。",
            summary.truncated,
            embedded_total,
            100 * summary.truncated / embedded_total,
            max_chars,
        )

    return summary


async def _embed(
    texts: list[str],
    *,
    embeddings: Embeddings,
    model: str,
    expected_dim: int,
) -> list[list[float]]:
    """调用模型并校验维度。"""
    vectors = await embeddings.aembed_documents(texts)

    if len(vectors) != len(texts):
        raise RuntimeError(
            f"返回 {len(vectors)} 条向量，期望 {len(texts)} 条。"
            "长度错位若被容忍，写库时会把 A 的向量挂到 B 的条上——"
            "检索结果看起来完全正常，只是指向了错误的法条。"
        )

    # 只抽查首条：模型返回的向量维度是一致的，逐条查是白费 CPU
    actual = len(vectors[0]) if vectors else expected_dim
    if actual != expected_dim:
        raise RuntimeError(
            f"模型 {model} 返回 {actual} 维向量，但 kb.article_vector.embedding 是 "
            f"vector({expected_dim})。请把 EMBEDDING_DIM（或模型配置）与迁移对齐后重跑；"
            "维度不匹配时 pgvector 会在 INSERT 阶段才报错，届时看不出是配置问题。"
        )
    return vectors


def _to_pg_vector(vector: list[float]) -> str:
    """把浮点列表格式化成 pgvector 的字面量。"""
    return "[" + ",".join(f"{value:.7g}" for value in vector) + "]"


async def _fetch_candidates(
    conn: AsyncConnection[DictRow],
    *,
    tenant_scope: str,
    model: str,
    limit: int | None,
) -> list[dict]:
    sql = """
        SELECT a.id,
               a.article_no,
               a.content,
               a.jurisdiction_code,
               s.title_zh AS statute_title,
               av.embedding_model AS existing_model
        FROM kb.article a
        JOIN kb.statute_version v ON v.id = a.statute_version_id
        JOIN kb.statute s ON s.id = v.statute_id
        LEFT JOIN kb.article_vector av ON av.article_id = a.id
        WHERE a.tenant_scope = %s
          AND a.publish_status = 'PUBLISHED'
          AND (av.article_id IS NULL OR av.embedding_model IS DISTINCT FROM %s)
        ORDER BY a.created_at, a.id
    """
    params: list[object] = [tenant_scope, model]
    if limit is not None:
        sql += " LIMIT %s"
        params.append(limit)

    async with conn.cursor() as cur:
        await cur.execute(sql, params)
        return list(await cur.fetchall())


async def _count_pending(conn: AsyncConnection[DictRow], tenant_scope: str, model: str) -> int:
    async with conn.cursor() as cur:
        await cur.execute(
            """
            SELECT count(*) AS n
            FROM kb.article a
            LEFT JOIN kb.article_vector av ON av.article_id = a.id
            WHERE a.tenant_scope = %s
              AND a.publish_status = 'PUBLISHED'
              AND (av.article_id IS NULL OR av.embedding_model IS DISTINCT FROM %s)
            """,
            (tenant_scope, model),
        )
        row = await cur.fetchone()
    return int(row["n"]) if row else 0


async def _upsert_vectors(
    conn: AsyncConnection[DictRow],
    payloads: list[tuple[object, list[float], str]],
) -> None:
    async with conn.cursor() as cur:
        await cur.executemany(
            f"""
            INSERT INTO kb.article_vector (article_id, embedding, embedding_model, updated_at)
            VALUES (%s, {_VECTOR_PLACEHOLDER}, %s, now())
            ON CONFLICT (article_id) DO UPDATE
               SET embedding = EXCLUDED.embedding,
                   embedding_model = EXCLUDED.embedding_model,
                   updated_at = now()
            """,  # noqa: S608 - 占位符是常量，无外部输入
            [(article_id, _to_pg_vector(vector), model) for article_id, vector, model in payloads],
        )
