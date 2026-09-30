"""把**随仓库提交的预计算向量**灌进 `kb.article_vector`，全程不调用 embedding 接口。

## 为什么需要这个模块

`embed.py` 走的是真实 embedding 接口，需要 `SILICONFLOW_API_KEY`。而本仓库是公开的
作品集项目，交付形态写死为「clone 下来一条命令跑起来，不需要任何密钥」——
把向量算在别人的机器上就等于把演示变成了"先申请一个 API key"。

做法：在开发机上用真实接口算**一次**，把结果按「嵌入文本的哈希」固化成
`deploy/seed/vectors/<model>.jsonl` 提交进仓库。之后灌库只做查表，
零网络、零成本、结果确定。

这与 `app/chains/replay.py` 是同一个思路（真实调用一次、离线无限重放），
区别只在于回放的是模型响应、这里回放的是向量——因此两者共用同一种
"按内容哈希寻址"的约定。

## 覆盖范围是**子集**，这是有意的

全量 2,562 条的向量约 14MB，会让 clone 变慢。当前提交的是若干部完整法规
（约 300 条，见 `deploy/seed/vectors/README.md`）。

**未被覆盖的法条不是错误状态**：`kb.article_vector` 是可选行，`article` 没有向量
仍然照常展示与关键词检索（`kb.article.content_tsv` 的 GIN 索引与向量无关）。
`_count_pending` 本来就会把它们计为"待向量化"。
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import logging
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from psycopg import AsyncConnection
from psycopg.rows import DictRow

from app.indexing.embed import (
    MAX_EMBED_CHARS,
    PLATFORM_SCOPE,
    _upsert_vectors,
    embedding_text,
)

logger = logging.getLogger(__name__)

#: 哈希长度。32 个十六进制字符（128 位）下，即使 2,562 条也远不到碰撞风险，
#: 而短哈希让文件小一截、肉眼对照时也读得动。
HASH_CHARS = 32


def text_hash(text: str) -> str:
    """嵌入文本 → 寻址键。

    **生成侧与消费侧必须用同一个函数**，因此它只在这里定义一次：
    `scripts/build_demo_vectors.py` 写文件时用它，本模块读文件时也用它。
    各写一份的后果是"写入用的是 A 算法、查找用的是 B 算法"——
    表现是查表全部未命中，而它看起来像"向量文件没生成对"。
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:HASH_CHARS]


def prepare_text(*, statute_title: str, article_no: str, content: str) -> str:
    """法条 → 实际被嵌入的文本。

    与 `embed.py` 用同一套拼装与截断规则。截断必须一致：生成侧按截断后的文本
    算哈希，消费侧若用未截断的原文算，长条文会全部查不到——
    而短条文都正常，于是问题看起来像"某几部法规的数据有问题"。
    """
    text = embedding_text(statute_title=statute_title, article_no=article_no, content=content)
    return text[:MAX_EMBED_CHARS]


def load_vectors(path: Path) -> dict[str, list[float]]:
    """读预计算向量文件。返回 `{文本哈希: 向量}`。

    行格式：`{"text_hash": "...", "model": "...", "dim": N, "vector": "<base64>"}`，
    其中 vector 是**小端 float32 的 base64**（编码侧见 `scripts/build_demo_vectors.py`，
    两条理由——体积，以及 pgvector 的 `vector` 底层本来就是 float4）。

    用 JSONL 而不是单个大 JSON：追加写友好，且单行损坏时其余行仍可读。
    """
    vectors: dict[str, list[float]] = {}
    if not path.exists():
        raise FileNotFoundError(
            f"预计算向量文件不存在：{path}\n"
            f"生成方式见 scripts/build_demo_vectors.py（需要 SILICONFLOW_API_KEY，只需跑一次）。"
        )

    with path.open("r", encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{lineno} 不是合法 JSON：{exc}") from exc

            key = record.get("text_hash")
            encoded = record.get("vector")
            if not key or not isinstance(encoded, str) or not encoded:
                raise ValueError(f"{path}:{lineno} 缺少 text_hash 或 vector")

            try:
                raw = base64.b64decode(encoded, validate=True)
            except (binascii.Error, ValueError) as exc:
                raise ValueError(f"{path}:{lineno} 的 vector 不是合法 base64：{exc}") from exc

            if len(raw) % 4 != 0:
                # float32 是 4 字节一组。长度不是 4 的倍数时 struct.unpack 会抛出
                # 一句看不出所以然的话（"unpack requires a buffer of N bytes"），
                # 这里提前把"文件被截断或被文本工具改坏了"说清楚。
                raise ValueError(
                    f"{path}:{lineno} 的 vector 长度为 {len(raw)} 字节，不是 4 的倍数——"
                    f"文件可能被截断或在传输中被改成了文本模式。"
                )

            dim = len(raw) // 4
            declared = record.get("dim")
            if declared is not None and declared != dim:
                raise ValueError(
                    f"{path}:{lineno} 声明 dim={declared}，实际解出 {dim} 维。"
                    f"两者不一致说明记录已经损坏。"
                )

            vectors[key] = list(struct.unpack(f"<{dim}f", raw))

    logger.info("已载入 %d 条预计算向量（%s）", len(vectors), path.name)
    return vectors


@dataclass
class VectorSummary:
    """一次向量灌入的结果。字段是事实，不含推测。"""

    articles_seen: int = 0
    vectors_applied: int = 0
    #: 文件里有、但库里没有对应条文的向量。正常为 0；不为 0 说明语料与向量文件
    #: 不同步（例如语料更新后忘了重新生成向量），这是个需要报出来的信号
    unused_vectors: int = 0


async def apply_vectors(
    conn: AsyncConnection[DictRow],
    vectors: dict[str, list[float]],
    *,
    model: str,
    tenant_scope: str = PLATFORM_SCOPE,
) -> VectorSummary:
    """把向量写入 `kb.article_vector`。

    调用方负责不设置租户上下文（与 `seed_db` 同理：平台行只能在平台上下文写入）。
    """
    summary = VectorSummary()
    rows = await _fetch_articles(conn, tenant_scope)
    summary.articles_seen = len(rows)

    matched: set[str] = set()
    payloads: list[tuple[Any, list[float], str]] = []

    for row in rows:
        content = (row["content"] or "").strip()
        if not content:
            continue
        key = text_hash(
            prepare_text(
                statute_title=row["statute_title"] or "",
                article_no=row["article_no"] or "",
                content=content,
            )
        )
        vector = vectors.get(key)
        if vector is None:
            continue
        matched.add(key)
        payloads.append((row["id"], vector, model))

    if payloads:
        # 分批提交：单条 1024 维向量的写入不便宜，一次 executemany 塞进三千条
        # 会让事务过大，中途失败要全部重来。
        batch = 256
        for start in range(0, len(payloads), batch):
            await _upsert_vectors(conn, payloads[start : start + batch])
        summary.vectors_applied = len(payloads)

    summary.unused_vectors = len(vectors) - len(matched)
    if summary.unused_vectors:
        logger.warning(
            "有 %d 条预计算向量在库中找不到对应法条。"
            "通常意味着语料已更新但向量文件没有重新生成——"
            "这些向量不会生效，请重跑 scripts/build_demo_vectors.py。",
            summary.unused_vectors,
        )

    return summary


async def _fetch_articles(
    conn: AsyncConnection[DictRow], tenant_scope: str
) -> list[dict[str, Any]]:
    """取出所有待匹配的法条。

    **与 `embed._fetch_candidates` 的差异**：那边带 `av.article_id IS NULL OR
    embedding_model IS DISTINCT FROM %s` 的条件，因为它只处理"缺向量或向量过期"的行；
    这里要拿全量去和向量文件比对，因此不带那个条件，也不做 LIMIT。
    """
    async with conn.cursor() as cur:
        await cur.execute(
            """
            SELECT a.id,
                   a.article_no,
                   a.content,
                   s.title_zh AS statute_title
            FROM kb.article a
            JOIN kb.statute_version v ON v.id = a.statute_version_id
            JOIN kb.statute s ON s.id = v.statute_id
            WHERE a.tenant_scope = %s
              AND a.publish_status = 'PUBLISHED'
            ORDER BY a.created_at, a.id
            """,
            (tenant_scope,),
        )
        return list(await cur.fetchall())
