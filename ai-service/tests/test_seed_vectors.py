"""预计算向量的测试。

分两层，与 `test_seed.py` 同样的思路：

1. **逻辑层**——构造各种坏文件，验证能被**报出来**而不是被吞掉。
   这一层尤其重要，因为向量文件的消费者是灌库服务：它读坏了却不报错时，
   表现是"知识库里没有向量"，而这看起来像"向量文件没生成"。
2. **真实产物层**——直接对仓库里已提交的 `deploy/seed/vectors/*.jsonl` 断言。
   验证的是"这批提交进仓库的文件能不能用"，而不是"我造的数据能不能过"。
"""

from __future__ import annotations

import base64
import json
import struct
from pathlib import Path

import pytest

from app.indexing.seed_vectors import (
    HASH_CHARS,
    load_vectors,
    prepare_text,
    text_hash,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
VECTOR_DIR = REPO_ROOT / "deploy" / "seed" / "vectors"


def encode(vector: list[float]) -> str:
    """与 `scripts/build_demo_vectors.py` 相同的编码：小端 float32 + base64。"""
    return base64.b64encode(struct.pack(f"<{len(vector)}f", *vector)).decode("ascii")


def write_vectors(tmp_path: Path, records: list[dict], name: str = "v.jsonl") -> Path:
    path = tmp_path / name
    path.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n",
        encoding="utf-8",
    )
    return path


# =============================================================================
# 寻址键
# =============================================================================
class TestTextHash:
    def test_is_stable_and_fixed_length(self) -> None:
        assert text_hash("荷兰 1990 年税收征收法") == text_hash("荷兰 1990 年税收征收法")
        assert len(text_hash("任意文本")) == HASH_CHARS

    def test_differs_on_a_single_character(self) -> None:
        # 哈希是用来寻址的，两个不同文本碰撞会让 A 条拿到 B 条的向量——
        # 而它会以一个"语义上不太对"的检索结果出现，几乎不可能被发现。
        assert text_hash("Artikel 1") != text_hash("Artikel 2")


class TestPrepareText:
    def test_matches_the_shape_embed_py_produces(self) -> None:
        """拼装规则必须与 `embed.py:embedding_text()` 一致。

        两边不一致的后果是**查表全部未命中**，而它看起来像"向量文件没生成对"——
        排查方向一开始就是错的。
        """
        text = prepare_text(
            statute_title="Invorderingswet 1990", article_no="Artikel 1", content="正文"
        )
        assert text == "Invorderingswet 1990 Artikel 1：正文"

    def test_truncates_consistently_with_embedding(self) -> None:
        from app.indexing.embed import MAX_EMBED_CHARS

        long_content = "x" * (MAX_EMBED_CHARS * 2)
        text = prepare_text(statute_title="T", article_no="1", content=long_content)
        assert len(text) == MAX_EMBED_CHARS


# =============================================================================
# 读文件
# =============================================================================
class TestLoadVectors:
    def test_round_trips_a_vector(self, tmp_path: Path) -> None:
        path = write_vectors(
            tmp_path,
            [{"text_hash": "abc", "model": "m", "dim": 3, "vector": encode([0.5, -1.25, 3.0])}],
        )
        vectors = load_vectors(path)
        assert set(vectors) == {"abc"}
        assert vectors["abc"] == pytest.approx([0.5, -1.25, 3.0])

    def test_missing_file_says_how_to_generate_it(self, tmp_path: Path) -> None:
        """缺文件时要指出生成方式。

        只说"文件不存在"会让人去翻目录树猜这个文件本该从哪来。
        """
        with pytest.raises(FileNotFoundError, match="build_demo_vectors"):
            load_vectors(tmp_path / "nope.jsonl")

    def test_rejects_non_base64(self, tmp_path: Path) -> None:
        path = write_vectors(
            tmp_path, [{"text_hash": "abc", "model": "m", "dim": 3, "vector": "不是 base64!!"}]
        )
        with pytest.raises(ValueError, match="base64"):
            load_vectors(path)

    def test_rejects_truncated_payload(self, tmp_path: Path) -> None:
        """长度不是 4 的倍数时要说清原因。

        让它走到 `struct.unpack` 的话，报出来的是
        "unpack requires a buffer of N bytes"，看不出是文件坏了。
        """
        path = write_vectors(
            tmp_path,
            [
                {
                    "text_hash": "abc",
                    "model": "m",
                    "dim": 3,
                    "vector": base64.b64encode(b"12345").decode(),
                }
            ],
        )
        with pytest.raises(ValueError, match="不是 4 的倍数"):
            load_vectors(path)

    def test_rejects_dim_mismatch(self, tmp_path: Path) -> None:
        """声明的 dim 与实际解出的维度不一致时必须报错。

        放任它在灌库时才炸的话，报出来的是 pgvector 的
        "expected 1024 dimensions, not 3"——指向数据库而不是这个文件。
        """
        path = write_vectors(
            tmp_path,
            [{"text_hash": "abc", "model": "m", "dim": 1024, "vector": encode([0.5, 1.0, 2.0])}],
        )
        with pytest.raises(ValueError, match="dim=1024"):
            load_vectors(path)

    def test_missing_fields_are_rejected(self, tmp_path: Path) -> None:
        path = write_vectors(
            tmp_path, [{"model": "m", "dim": 3, "vector": encode([1.0, 2.0, 3.0])}]
        )
        with pytest.raises(ValueError, match="text_hash"):
            load_vectors(path)


# =============================================================================
# 真实产物
# =============================================================================
class TestCommittedArtifact:
    """对仓库里实际提交的向量文件断言。

    这一层守的是"提交进仓库的东西确实可用"。它失败时**不要**改断言——
    那是产物本身出了问题，重跑 `scripts/build_demo_vectors.py` 并更新
    `deploy/seed/vectors/README.md` 的覆盖清单。
    """

    def artifacts(self) -> list[Path]:
        return sorted(VECTOR_DIR.glob("*.jsonl"))

    def test_at_least_one_artifact_is_committed(self) -> None:
        assert self.artifacts(), (
            f"{VECTOR_DIR} 下没有 .jsonl。预计算向量是「clone 后零密钥可玩」的一环，"
            f"缺了它灌库会跳过向量（不报错），而知识库的语义检索就没有覆盖。"
        )

    def test_all_artifacts_load_and_are_1024_dimensional(self) -> None:
        from app.indexing.embed import MAX_EMBED_CHARS  # noqa: F401  仅确认模块可导入

        for path in self.artifacts():
            vectors = load_vectors(path)
            assert vectors, f"{path.name} 里没有任何向量"
            dims = {len(v) for v in vectors.values()}
            assert dims == {1024}, f"{path.name} 的向量维度不齐：{dims}"

    def test_hashes_are_unique(self) -> None:
        """重复哈希意味着两条法条抢同一份向量。

        它不会报错，只会让其中一条拿到另一条的向量——检索结果"有点不对"，
        而这种错几乎不可能靠肉眼发现。
        """
        for path in self.artifacts():
            raw = [
                json.loads(line) for line in path.read_text("utf-8").splitlines() if line.strip()
            ]
            hashes = [r["text_hash"] for r in raw]
            assert len(hashes) == len(set(hashes)), f"{path.name} 里有重复的 text_hash"

    def test_hashes_match_the_current_corpus(self) -> None:
        """**最关键的一条**：向量文件里的哈希必须能在当前语料里找到对应法条。

        它守的是"语料改了、向量忘了重新生成"这种不同步。
        没有这条测试时，症状是灌库日志里一句 WARNING（很容易被忽略），
        而演示时表现为语义检索少覆盖了一部分法条。
        """
        from app.indexing.seed import load_corpus

        corpus_dir = REPO_ROOT / "deploy" / "seed" / "corpus"
        rows, _problems = load_corpus(sorted(corpus_dir.glob("*.jsonl")))
        assert rows, "语料为空，无法比对"

        corpus_hashes = {
            text_hash(
                prepare_text(
                    statute_title=row.statute_title,
                    article_no=row.article_no,
                    content=row.content,
                )
            )
            for row in rows
        }

        for path in self.artifacts():
            vectors = load_vectors(path)
            orphaned = set(vectors) - corpus_hashes
            assert not orphaned, (
                f"{path.name} 里有 {len(orphaned)}/{len(vectors)} 条向量在语料中找不到对应法条。"
                f"通常意味着语料已更新而向量未重新生成——请重跑 "
                f"scripts/build_demo_vectors.py。"
            )
