"""演示语料的灌库入口（`python -m app.indexing.seed_demo`）。

## 为什么需要这个入口

`seed_db.import_groups` 与 `seed_vectors.apply_vectors` 在此之前**只被测试调用**，
没有任何运行时路径能把 `deploy/seed/corpus/*.jsonl` 变成库里的行。后果是：
`docker compose up` 之后 `kb` 是空的，`GET /api/statutes` 返回 0 条——
界面能打开，但知识库页什么都没有，而这恰恰是演示最主要的一页。

本模块把「读语料 → 建法规/版本/法条 → 贴预计算向量」串成一条命令，
由 compose 的 `seed` 一次性服务调用。

## 三条约束

1. **不设置租户上下文。** 平台公共法条库（`tenant_scope='PLATFORM'`）只能在平台
   上下文下写入，这是 `kb` 的 RLS 策略的设计意图（见 `seed_db` 模块 docstring）。
2. **幂等。** 每次 `docker compose up` 都会跑一次。重复执行必须是无害的：
   已存在的版本整组跳过，向量走 `ON CONFLICT DO UPDATE`。
3. **失败要大声。** 退出码非 0，且原因写在日志里。compose 里 `api` 依赖本服务
   `service_completed_successfully`，因此灌库失败会让 api 不启动——
   这比"启动成功但知识库是空的"要好，后者会把问题一直藏到演示现场。
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

from psycopg import AsyncConnection
from psycopg.rows import DictRow, dict_row
from psycopg_pool import AsyncConnectionPool

from app.core.config import get_settings
from app.indexing.seed import group_by_version, load_corpus
from app.indexing.seed_db import import_groups
from app.indexing.seed_vectors import apply_vectors, load_vectors

logger = logging.getLogger(__name__)

#: 容器内语料与向量文件的挂载点。compose 把 `deploy/seed` 挂到 `/seed`（只读）。
DEFAULT_SEED_DIR = Path("/seed")

#: 宿主机上直接跑时的默认位置（相对仓库根）。
LOCAL_SEED_DIR = Path(__file__).resolve().parents[3] / "deploy" / "seed"


async def seed(
    *,
    seed_dir: Path,
    vector_model: str | None,
    skip_vectors: bool,
) -> int:
    """执行一次灌库。返回进程退出码。"""
    corpus_dir = seed_dir / "corpus"
    corpus_files = sorted(corpus_dir.glob("*.jsonl"))

    if not corpus_files:
        logger.error("语料目录里没有 .jsonl 文件：%s", corpus_dir)
        return 1

    logger.info("语料文件：%s", ", ".join(p.name for p in corpus_files))

    rows, problems = load_corpus(corpus_files)
    if problems:
        # 有问题不代表不能导入——问题时清单可能包含"某个法域整体缺失"这类
        # 不影响其余数据的情况。但必须逐条打出来：静默导入一半语料，
        # 表现是"知识库里少了些东西"，而没人知道少了什么。
        for problem in problems:
            logger.warning("语料问题：%s", problem)
    if not rows:
        logger.error("语料解析后没有任何行，终止。")
        return 1

    groups = group_by_version(rows)
    logger.info("解析出 %d 条法条、%d 个法规版本", len(rows), len(groups))

    settings = get_settings()
    pool: AsyncConnectionPool[AsyncConnection[DictRow]] = AsyncConnectionPool(
        conninfo=settings.dsn,
        min_size=1,
        max_size=4,
        kwargs={"autocommit": True, "row_factory": dict_row},
        open=False,
    )
    await pool.open()
    try:
        async with pool.connection() as conn:
            summary = await import_groups(conn, groups)
            logger.info(
                "法规：新建 %d / 复用 %d ｜ 版本：新建 %d / 跳过 %d ｜ 法条：新建 %d",
                summary.statutes_created,
                summary.statutes_reused,
                summary.versions_created,
                summary.versions_skipped,
                summary.articles_created,
            )

            if skip_vectors or not vector_model:
                logger.info("已跳过向量灌入（skip_vectors=%s）", skip_vectors)
                return 0

            vector_path = seed_dir / "vectors" / f"{_safe_name(vector_model)}.jsonl"
            if not vector_path.exists():
                # **不是错误。** 向量是可选增强：没有它，法条照常展示与关键词检索，
                # 只是语义检索覆盖不到。让它成为硬失败会让"向量文件暂时缺失"
                # 变成"整个演示起不来"，代价远大于收益。
                logger.warning(
                    "未找到预计算向量文件 %s，跳过。法条仍可展示与关键词检索，仅语义检索无覆盖。",
                    vector_path,
                )
                return 0

            vectors = load_vectors(vector_path)
            vector_summary = await apply_vectors(conn, vectors, model=vector_model)
            logger.info(
                "向量：库中法条 %d 条，命中并写入 %d 条，文件内未使用 %d 条",
                vector_summary.articles_seen,
                vector_summary.vectors_applied,
                vector_summary.unused_vectors,
            )
    finally:
        await pool.close()

    return 0


def _safe_name(model: str) -> str:
    """模型 ID → 文件名。

    `Qwen/Qwen3-Embedding-8B` 里带斜杠，直接当文件名会在多数文件系统上
    被当成路径分隔符，于是写文件时因目录不存在而失败——
    而错误信息是"找不到目录"，看起来像挂载问题。统一替换成 `__`。
    """
    return model.replace("/", "__")


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="把演示语料灌入 kb schema")
    parser.add_argument(
        "--seed-dir",
        type=Path,
        default=None,
        help=f"种子数据根目录（含 corpus/ 与 vectors/）。容器内默认 {DEFAULT_SEED_DIR}",
    )
    parser.add_argument(
        "--vector-model",
        default=None,
        help="预计算向量对应的 embedding 模型 ID。默认取配置里的 EMBEDDING_MODEL",
    )
    parser.add_argument(
        "--skip-vectors",
        action="store_true",
        help="只灌法条，不灌向量",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    )
    args = _parse_args(argv)

    seed_dir = args.seed_dir
    if seed_dir is None:
        seed_dir = DEFAULT_SEED_DIR if DEFAULT_SEED_DIR.exists() else LOCAL_SEED_DIR

    vector_model = args.vector_model or get_settings().embedding_model

    return asyncio.run(
        seed(seed_dir=seed_dir, vector_model=vector_model, skip_vectors=args.skip_vectors)
    )


if __name__ == "__main__":
    sys.exit(main())
