#!/usr/bin/env python3
"""生成随仓库提交的**预计算向量**（只需跑一次，产物入库）。

## 这个脚本解决什么

公开仓库的演示形态是「clone → docker compose up → 能玩」，不能要求使用者
先申请一个 embedding API key。因此向量在**开发机上算一次**、按内容哈希固化成
`deploy/seed/vectors/<model>.jsonl` 提交进仓库，之后灌库只查表。

跑这个脚本需要 `SILICONFLOW_API_KEY`，会产生少量费用（当前子集约 320 条）。
日常演示与 CI **都不需要**它。

## 覆盖范围：若干部完整法规，而不是所有法规的前 N 条

按「整部法规」选取的理由：截断式的子集会让同一部法规里一半的条文有向量、
另一半没有，检索命中哪一条取决于运气。整部选取至少让覆盖范围是可描述的。

选哪些法规由 `SUBSET_MAX_ARTICLES` 控制：按法域、法规名排序后依次整部收入，
直到再加入一部就会超出上限为止。**结果确定、可复现**，
因此重跑本脚本得到的文件内容一致（除了向量本身的浮点值）。

## 用法

    # 宿主机（需要 ai-service 的依赖环境）
    cd ai-service && uv run python ../scripts/build_demo_vectors.py

    # 先看看会选哪些法规、多少条，不实际调用接口
    cd ai-service && uv run python ../scripts/build_demo_vectors.py --dry-run
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import struct
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_AI_SERVICE = _ROOT / "ai-service"
if str(_AI_SERVICE) not in sys.path:
    sys.path.insert(0, str(_AI_SERVICE))

from app.chains.model_factory import get_embeddings  # noqa: E402
from app.indexing.seed import CorpusRow, load_corpus  # noqa: E402
from app.indexing.seed_vectors import prepare_text, text_hash  # noqa: E402

CORPUS_DIR = _ROOT / "deploy" / "seed" / "corpus"
VECTOR_DIR = _ROOT / "deploy" / "seed" / "vectors"

#: 子集上限。约 320 条 1024 维向量 ≈ 1.8MB（base64 float32），
#: 是"仓库不至于臃肿"与"覆盖得像个真知识库"之间的取舍。
SUBSET_MAX_ARTICLES = 320


def _load_env() -> None:
    """从 deploy/.env 读取配置。

    与其它冒烟脚本同一做法：宿主机的真实口令与 key 只存在于 deploy/.env，
    读它比让使用者手工 export 一堆变量可靠，也避免密钥落进 shell 历史。
    """
    import os

    env_path = _ROOT / "deploy" / ".env"
    if not env_path.exists():
        return
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


def select_subset(rows: list[CorpusRow]) -> tuple[list[CorpusRow], list[tuple[str, str, int]]]:
    """按整部法规选取子集。

    Returns:
        (选中的行, [(法域, 法规名, 条数), ...]) —— 后者用于写进 README，
        让"到底覆盖了哪些法规"是可查的，而不是要人去读 jsonl。
    """
    by_statute: dict[tuple[str, str, str], list[CorpusRow]] = {}
    for row in rows:
        by_statute.setdefault(row.version_key, []).append(row)

    # 排序键：法域 → 条数（少的先，让子集能装下更多部法规）→ 法规名。
    # 用条数升序是为了在同样 320 条的预算下覆盖尽量多的法规，
    # 而不是被一部 1,104 条的巨法吃掉全部额度。
    ordered = sorted(
        by_statute.items(),
        key=lambda kv: (kv[0][0], len(kv[1]), kv[0][1]),
    )

    selected: list[CorpusRow] = []
    chosen: list[tuple[str, str, int]] = []
    for key, group in ordered:
        if selected and len(selected) + len(group) > SUBSET_MAX_ARTICLES:
            continue
        selected.extend(group)
        chosen.append((key[0], key[1], len(group)))

    return selected, chosen


async def build(*, dry_run: bool) -> int:
    _load_env()

    from app.core.config import get_settings

    settings = get_settings()
    model = settings.embedding_model

    corpus_files = sorted(CORPUS_DIR.glob("*.jsonl"))
    if not corpus_files:
        print(f"[错误] 语料目录为空：{CORPUS_DIR}", file=sys.stderr)
        return 1

    rows, problems = load_corpus(corpus_files)
    for problem in problems:
        print(f"[警告] 语料问题：{problem}")

    subset, chosen = select_subset(rows)
    print(f"语料共 {len(rows)} 条；子集选中 {len(subset)} 条，覆盖 {len(chosen)} 部法规：")
    for jurisdiction, title, count in chosen:
        print(f"   {jurisdiction}  {count:5d}  {title}")

    if dry_run:
        print("\n--dry-run：未调用 embedding 接口，未写文件。")
        return 0

    texts: list[str] = []
    for row in subset:
        texts.append(
            prepare_text(
                statute_title=row.statute_title,
                article_no=row.article_no,
                content=row.content,
            )
        )

    print(f"\n开始调用 embedding 接口（{len(texts)} 条，模型 {model}）…")
    embeddings = get_embeddings(settings=settings)
    vectors = await embeddings.aembed_documents(texts)
    if len(vectors) != len(texts):
        print(f"[错误] 返回 {len(vectors)} 条向量，期望 {len(texts)} 条", file=sys.stderr)
        return 1

    expected_dim = settings.embedding_dim
    bad = [i for i, v in enumerate(vectors) if len(v) != expected_dim]
    if bad:
        print(
            f"[错误] 有 {len(bad)} 条向量维度不是 {expected_dim}（首条维度 "
            f"{len(vectors[bad[0]])}）。kb.article_vector 声明的是 vector({expected_dim})，"
            f"维度不符会让灌库时的 INSERT 直接失败。",
            file=sys.stderr,
        )
        return 1

    VECTOR_DIR.mkdir(parents=True, exist_ok=True)
    out_path = VECTOR_DIR / f"{model.replace('/', '__')}.jsonl"
    with out_path.open("w", encoding="utf-8") as fh:
        for text, vector in zip(texts, vectors, strict=True):
            fh.write(
                json.dumps(
                    {
                        "text_hash": text_hash(text),
                        "model": model,
                        "dim": len(vector),
                        # 存 base64 的 float32，而不是 JSON 的浮点数组。
                        #
                        # 两个理由，第二个更重要：
                        #   1. 体积。JSON 会按 repr 写出最多 17 位有效数字，
                        #      1024 维一条约 19KB；float32 + base64 是 5.5KB。
                        #      实测 319 条从 6.9MB 降到 1.8MB。
                        #   2. **保真。** pgvector 的 `vector` 类型底层就是 float4，
                        #      走 JSON 是先降到 float64 再由数据库降到 float4；
                        #      直接存 float32 是同一个精度，少一次无意义的往返。
                        # 显式小端：文件要跨机器读，不能依赖本机字节序。
                        "vector": base64.b64encode(
                            struct.pack(f"<{len(vector)}f", *vector)
                        ).decode("ascii"),
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                + "\n"
            )

    size_mb = out_path.stat().st_size / 1024 / 1024
    print(f"\n[完成] 已写入 {out_path.relative_to(_ROOT)}（{len(vectors)} 条，{size_mb:.2f} MB）")
    print("       记得同步更新 deploy/seed/vectors/README.md 里的覆盖清单与条数。")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="生成随仓库提交的预计算向量")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只报告子集构成，不调用接口、不写文件",
    )
    args = parser.parse_args()
    return asyncio.run(build(dry_run=args.dry_run))


if __name__ == "__main__":
    sys.exit(main())
