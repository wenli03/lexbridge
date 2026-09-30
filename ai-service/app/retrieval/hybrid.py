"""混合检索：三路召回 → RRF 融合 → 重排。

**为什么不是"向量检索"了事。** 三类问题需要三种召回方式：

1. **意图型**（"我们这样安排会不会被认定为常设机构"）只有向量能召回——
   问句里没有任何与法条原文重合的词。
2. **术语型**（"withholding tax on dividends"）关键词更稳——
   向量在这一类上偶尔会漏掉精确措辞的条文，而漏掉一条税率条款的代价很大。
3. **中文短语型**：`simple` 词典**不能切分中文**，整句会成为一个 token，
   `tsvector` 对它几乎无效。所以第三路用 `ILIKE` 子串匹配 + `pg_trgm`
   相似度排序——中文查询能靠这一路召回。

三条路各有所长且互相补位，因此融合它们而不是选一条。

**为什么用 RRF 而不是加权求和。** 向量相似度在 [0,1]，`ts_rank_cd` 是无界值，
直接相加需要先校准，而校准系数会随语料分布漂移——语料一换就得重调。
RRF 只用**排名**，天然免疫量纲问题。常数 60 取原论文经验值。

**租户过滤在这里是强制的，不是参数**（详细设计 §6.4 / 4+1 场景 S4）。
本模块的函数签名里没有 tenant 参数：它只能从 `require_current_tenant()` 取，
取不到就抛。这不是为了调用方便，而是**为了让"忘记加过滤"在语法上不可能发生**。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date

from langchain_core.embeddings import Embeddings
from psycopg import AsyncConnection
from psycopg.rows import DictRow

from app.core.tenant import require_current_tenant
from app.retrieval.citation import CandidateArticle

logger = logging.getLogger(__name__)

#: RRF 常数（原论文经验值）
RRF_K = 60

VECTOR_LIMIT = 100
LEXICAL_LIMIT = 100
SUBSTRING_LIMIT = 50

#: **全部路由与取行查询共用这一段过滤条件。**
#: 四处各写一遍的后果是迟早有一处漏掉，而漏掉的那一处不会报错——
#: 它只会让某一条路径悄悄多返回一些行。放在一个字面量里，
#: "有没有漏"就退化成一个可以直接看见的问题。
_FILTER = """
      a.tenant_scope IN ('PLATFORM', %(tenant)s)
  AND a.publish_status = 'PUBLISHED'
  AND %(as_of)s::date BETWEEN a.effective_from
                          AND coalesce(a.effective_to, DATE '9999-12-31')
  AND a.jurisdiction_code = ANY(%(jurisdictions)s)
"""

_VECTOR_SQL = f"""
    SELECT a.id
    FROM kb.article a
    JOIN kb.article_vector v ON v.article_id = a.id
    WHERE {_FILTER}
    ORDER BY v.embedding <=> %(embedding)s::vector
    LIMIT %(limit)s
"""  # noqa: S608 - 拼进去的只有模块常量 _FILTER，全部取值走 psycopg 占位符

_LEXICAL_SQL = f"""
    SELECT a.id
    FROM kb.article a
    WHERE {_FILTER}
      AND a.content_tsv @@ websearch_to_tsquery('simple', %(query)s)
    ORDER BY ts_rank_cd(a.content_tsv, websearch_to_tsquery('simple', %(query)s)) DESC
    LIMIT %(limit)s
"""  # noqa: S608 - 同上

_SUBSTRING_SQL = f"""
    SELECT a.id
    FROM kb.article a
    WHERE {_FILTER}
      AND a.content ILIKE %(pattern)s
    ORDER BY similarity(a.content, %(query)s) DESC
    LIMIT %(limit)s
"""  # noqa: S608 - 同上

_ROW_SQL = f"""
    SELECT a.id,
           a.article_no,
           a.content,
           a.tenant_scope,
           a.effective_from,
           a.effective_to,
           a.hierarchy_path,
           s.title_zh AS statute_title
    FROM kb.article a
    JOIN kb.statute_version v ON v.id = a.statute_version_id
    JOIN kb.statute s ON s.id = v.statute_id
    WHERE a.id = ANY(%(ids)s::uuid[]) AND {_FILTER}
"""  # noqa: S608 - 同上


@dataclass(frozen=True)
class SearchScope:
    """一次检索的边界。"""

    jurisdictions: tuple[str, ...]
    as_of: str = field(default_factory=lambda: date.today().isoformat())
    top_k: int = 12
    #: 交给重排的条数。比重排结果多留一些，让 reranker 有挑选余地。
    rerank_pool: int = 40


@dataclass
class RetrievedArticle:
    article: CandidateArticle
    rrf_score: float
    #: 该条在各路上的名次（1 起）。保留它是为了在 trace 里回答
    #: "这一条是靠哪一路召回的"——排查检索质量时这比最终排名有用得多。
    route_ranks: dict[str, int] = field(default_factory=dict)
    rerank_score: float | None = None


@dataclass
class SearchResult:
    articles: list[RetrievedArticle]
    #: 降级说明。**有内容不代表失败**，但调用方应当把它透出到响应里，
    #: 而不是自己吞掉——"结果没重排"与"结果重排过"对使用者的意义不同。
    degraded: list[str] = field(default_factory=list)
    route_sizes: dict[str, int] = field(default_factory=dict)

    @property
    def candidates(self) -> list[CandidateArticle]:
        """交给引用校验的候选集。"""
        return [item.article for item in self.articles]


def escape_like(text: str) -> str:
    """转义 `LIKE` 的通配符。

    **不转义的后果不是报错，而是召回一堆无关条文。** 法令文本里 `%` 很常见
    （"税率为 25%"），不转义时它会被当成"任意字符"，
    于是"税率为 25%"变成"税率为 25 后接任意内容"——匹配面被放大，
    而使用者只会觉得"检索结果有点不相关"。

    反斜杠必须**先**处理：否则它会把后面新加的转义符再转义一次。
    """
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def fuse_rrf(
    rankings: dict[str, list[str]], *, k: int = RRF_K
) -> list[tuple[str, float, dict[str, int]]]:
    """按 RRF 融合多路排名。

    纯函数，不碰数据库：融合是这个模块里唯一有"算法"的部分，
    把它隔离出来才能穷尽地测（多路重叠、单路独有、名次相差很大等）。

    Args:
        rankings: 路由名 → 按名次排列的 id 列表（名次从 0 开始）。
        k: RRF 常数。越大则"排名靠前"的优势越平缓。

    Returns:
        (id, 融合分, {路由名: 名次}) 按融合分降序。名次从 1 开始计。
    """
    scores: dict[str, float] = {}
    ranks: dict[str, dict[str, int]] = {}

    for route, ids in rankings.items():
        for position, article_id in enumerate(ids, start=1):
            scores[article_id] = scores.get(article_id, 0.0) + 1.0 / (k + position)
            ranks.setdefault(article_id, {})[route] = position

    # 同分时按"最好名次"再排一次：RRF 分数是离散的（1/(60+n) 的组合），
    # 撞分很常见，而稳定且可解释的次序比任意次序好。
    def sort_key(item: tuple[str, float]) -> tuple[float, int, str]:
        article_id, score = item
        best = min(ranks[article_id].values())
        return (-score, best, article_id)

    return [
        (article_id, score, ranks[article_id])
        for article_id, score in sorted(scores.items(), key=sort_key)
    ]


async def retrieve(
    conn: AsyncConnection[DictRow],
    *,
    query: str,
    scope: SearchScope,
    embedder: Embeddings | None = None,
    reranker: object | None = None,
) -> SearchResult:
    """三路召回并融合。

    Args:
        conn: 已绑定租户的事务连接（RLS 与这里的 SQL 过滤是两层互补的防线）。
        query: 用户的原始问题。
        scope: 法域、检索时点与条数上限。
        embedder: 查询向量化用。为 None 或调用失败时**跳过向量路并标注降级**，
            而不是整体失败——中文法条仍可由子串路召回，这比返回空结果有用。
        reranker: 有 `rerank(query, documents, top_n)` 的对象（见 §6.3）。

    Raises:
        RuntimeError: 当前任务没有租户上下文。
    """
    tenant = require_current_tenant()
    params = {
        "tenant": str(tenant.tenant_id),
        "as_of": scope.as_of,
        "jurisdictions": list(scope.jurisdictions),
    }
    rankings: dict[str, list[str]] = {}
    degraded: list[str] = []

    if embedder is None:
        degraded.append("未提供向量模型，本次仅用关键词与子串召回")
    else:
        embedding = await _safe_query_embedding(embedder, query, degraded)
        if embedding is not None:
            rankings["vector"] = await _fetch_ids(
                conn,
                _VECTOR_SQL,
                {**params, "embedding": _to_pg_vector(embedding), "limit": VECTOR_LIMIT},
            )

    if query.strip():
        rankings["lexical"] = await _fetch_ids(
            conn, _LEXICAL_SQL, {**params, "query": query, "limit": LEXICAL_LIMIT}
        )
        rankings["substring"] = await _fetch_ids(
            conn,
            _SUBSTRING_SQL,
            {
                **params,
                "query": query,
                "pattern": f"%{escape_like(query)}%",
                "limit": SUBSTRING_LIMIT,
            },
        )
    else:
        degraded.append("查询为空，已跳过关键词与子串召回")

    fused = fuse_rrf({route: ids for route, ids in rankings.items() if ids})
    if not fused:
        return SearchResult(
            articles=[],
            degraded=degraded,
            route_sizes={route: len(ids) for route, ids in rankings.items()},
        )

    pool_size = max(scope.top_k, scope.rerank_pool)
    head = fused[:pool_size]
    articles = await _load_articles(conn, [article_id for article_id, _, _ in head], params)

    ordered: list[RetrievedArticle] = []
    for article_id, score, ranks in head:
        article = articles.get(article_id)
        if article is None:
            # 融合后取行时被过滤掉，说明两次查询之间数据变了（发布/回滚）。
            # 丢掉它比留下一条没有正文的候选好——引用校验需要正文比对。
            logger.info("候选 %s 在取行时不再可见（可能刚发生发布或回滚）", article_id)
            continue
        ordered.append(RetrievedArticle(article=article, rrf_score=score, route_ranks=ranks))

    if reranker is not None and ordered:
        ordered = await _rerank(
            reranker, query=query, pool=ordered, top_k=scope.top_k, degraded=degraded
        )
    else:
        ordered = ordered[: scope.top_k]

    return SearchResult(
        articles=ordered,
        degraded=degraded,
        route_sizes={route: len(ids) for route, ids in rankings.items()},
    )


async def _safe_query_embedding(
    embedder: Embeddings, query: str, degraded: list[str]
) -> list[float] | None:
    """查询向量化失败时降级，不中断整个检索。"""
    try:
        return await embedder.aembed_query(query)
    except Exception as exc:  # noqa: BLE001 - 降级路径要能接住任何失败
        logger.warning("查询向量化失败，降级为关键词与子串召回：%s", type(exc).__name__)
        degraded.append(
            f"查询向量化失败（{type(exc).__name__}），本次未使用语义召回，"
            "结果可能漏掉措辞不同的相关条文"
        )
        return None


async def _fetch_ids(conn: AsyncConnection[DictRow], sql: str, params: dict) -> list[str]:
    async with conn.cursor() as cur:
        await cur.execute(sql, params)
        return [str(row["id"]) for row in await cur.fetchall()]


async def _load_articles(
    conn: AsyncConnection[DictRow], ids: list[str], params: dict
) -> dict[str, CandidateArticle]:
    """取候选法条的完整行。

    **这里再次套用同一段过滤条件**，而不是"既然已经按 id 取了就免了"：
    取行与召回之间可能发生发布或回滚，重放同一段条件才能保证
    "交出去的候选一定是当前可见且当前有效的"。
    """
    if not ids:
        return {}
    async with conn.cursor() as cur:
        await cur.execute(_ROW_SQL, {**params, "ids": ids})
        rows = await cur.fetchall()

    return {
        str(row["id"]): CandidateArticle(
            article_id=str(row["id"]),
            article_no=row["article_no"],
            content=row["content"],
            tenant_scope=row["tenant_scope"],
            effective_from=str(row["effective_from"]),
            effective_to=str(row["effective_to"]) if row["effective_to"] else None,
            statute_title=row["statute_title"] or "",
            hierarchy_path=tuple(row["hierarchy_path"] or ()),
        )
        for row in rows
    }


async def _rerank(
    reranker: object,
    *,
    query: str,
    pool: list[RetrievedArticle],
    top_k: int,
    degraded: list[str],
) -> list[RetrievedArticle]:
    """重排。**RRF 分数保留不动，只调整次序**（§6.3）。

    保留 RRF 分数是为了让 trace 能回答"这条是靠哪一路召回的"；
    若用重排分数把它覆盖掉，检索质量问题就只能靠猜。
    """
    documents = [item.article.content for item in pool]
    try:
        results = await reranker.rerank(query, documents, top_n=top_k)  # type: ignore[attr-defined]
    except Exception as exc:  # noqa: BLE001 - 重排失败不应让整个检索失败
        logger.warning("重排失败，保留 RRF 次序：%s", type(exc).__name__)
        degraded.append(f"重排失败（{type(exc).__name__}），结果按融合分数排序")
        return pool[:top_k]

    scores: dict[int, float | None] = {
        int(result["index"]): result.get("relevance_score") for result in results
    }
    if all(score is None for score in scores.values()) and scores:
        # 回放模式下未录制该查询时会走到这里。此时宁可退回 RRF 次序并声明，
        # 也不要给用户一个看起来正常、实则随机排序的结果。
        degraded.append("重排未命中（离线回放未录制该查询），结果按融合分数排序")
        return pool[:top_k]

    order = sorted(
        range(len(pool)),
        key=lambda index: (
            scores.get(index) is not None,
            scores.get(index) or 0.0,
        ),
        reverse=True,
    )
    ordered: list[RetrievedArticle] = []
    for index in order[:top_k]:
        item = pool[index]
        item.rerank_score = scores.get(index)
        ordered.append(item)
    return ordered


def _to_pg_vector(vector: list[float]) -> str:
    """格式化成 pgvector 字面量。与 `indexing.embed` 的做法一致、理由相同。"""
    return "[" + ",".join(f"{value:.7g}" for value in vector) + "]"
