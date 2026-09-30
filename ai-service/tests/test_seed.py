"""种子语料导入的测试。

分两层：

1. **逻辑层**——用临时文件构造各种坏数据，验证问题能被**报出来**而不是被吞掉。
2. **真实语料层**——直接对仓库里已采集的 `deploy/seed/corpus/*.jsonl` 断言。
   这一层的价值在于：它验证的是"这批真数据能不能入库"，而不是"我的假数据能不能过"。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.indexing.seed import (
    CorpusRow,
    find_duplicate_article_numbers,
    group_by_version,
    load_corpus,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
CORPUS_DIR = REPO_ROOT / "deploy" / "seed" / "corpus"


def write_corpus(tmp_path: Path, rows: list[dict], name: str = "test.jsonl") -> Path:
    path = tmp_path / name
    path.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in rows), encoding="utf-8"
    )
    return path


def valid_row(**overrides: object) -> dict:
    row: dict = {
        "jurisdiction_code": "IE",
        "statute_title": "爱尔兰《测试法》",
        "statute_title_original": "Test Act",
        "statute_no": "Act No. 1 of 2020",
        "version_label": "enacted",
        "effective_from": "2020-01-01",
        "effective_to": None,
        "date_precision": "DAY",
        "source_url": "https://example.test/act",
        "source_fetched_at": "2026-09-30T00:00:00Z",
        "article_no": "Section 1",
        "hierarchy_path": ["Test Act", "Section 1"],
        "content": "这一条的内容足够长，不会被当成切分噪声。",
    }
    row.update(overrides)
    return row


class TestLoad:
    def test_valid_row_is_loaded(self, tmp_path: Path) -> None:
        rows, problems = load_corpus([write_corpus(tmp_path, [valid_row()])])
        assert problems == []
        assert len(rows) == 1
        assert rows[0].article_no == "Section 1"
        assert rows[0].hierarchy_path == ("Test Act", "Section 1")

    def test_missing_required_field_is_reported_with_line_number(self, tmp_path: Path) -> None:
        row = valid_row()
        del row["source_url"]
        rows, problems = load_corpus([write_corpus(tmp_path, [row])])
        assert rows == []
        assert len(problems) == 1
        # 必须带行号：语料文件上千行，没有行号的报错等于没报
        assert "test.jsonl:1" in problems[0]
        assert "source_url" in problems[0]

    def test_missing_file_is_reported_not_raised(self, tmp_path: Path) -> None:
        rows, problems = load_corpus([tmp_path / "nope.jsonl"])
        assert rows == []
        assert any("不存在" in problem for problem in problems)

    def test_broken_json_line_does_not_kill_the_batch(self, tmp_path: Path) -> None:
        path = tmp_path / "mixed.jsonl"
        path.write_text(
            json.dumps(valid_row(), ensure_ascii=False) + "\n{不是合法 JSON\n",
            encoding="utf-8",
        )
        rows, problems = load_corpus([path])
        # 坏行被跳过，好行仍然可用——否则一行脏数据会让整批语料无法导入
        assert len(rows) == 1
        assert any("JSON" in problem for problem in problems)

    def test_invalid_effective_from_rejects_the_row(self, tmp_path: Path) -> None:
        rows, problems = load_corpus(
            [write_corpus(tmp_path, [valid_row(effective_from="1997 年")])]
        )
        assert rows == []
        assert any("effective_from" in problem for problem in problems)

    def test_inverted_range_is_reported_and_treated_as_open_ended(self, tmp_path: Path) -> None:
        """区间倒置必须报出来，并退回「仍然有效」。

        静默保留倒置区间会让「按 2021 年回答」这类查询永远匹配不到这条，
        而使用者只会以为这条法规不存在。
        """
        rows, problems = load_corpus(
            [
                write_corpus(
                    tmp_path, [valid_row(effective_from="2020-05-01", effective_to="2019-01-01")]
                )
            ]
        )
        assert len(rows) == 1
        assert rows[0].effective_to is None
        assert any("不晚于" in problem for problem in problems)

    def test_unknown_date_precision_falls_back_to_day_with_problem(self, tmp_path: Path) -> None:
        rows, problems = load_corpus([write_corpus(tmp_path, [valid_row(date_precision="MONTH")])])
        assert rows[0].date_precision == "DAY"
        assert any("date_precision" in problem for problem in problems)

    def test_empty_hierarchy_path_rejects_the_row(self, tmp_path: Path) -> None:
        rows, problems = load_corpus([write_corpus(tmp_path, [valid_row(hierarchy_path=[])])])
        assert rows == []
        assert any("hierarchy_path" in problem for problem in problems)

    def test_blank_lines_are_ignored(self, tmp_path: Path) -> None:
        path = tmp_path / "blanks.jsonl"
        path.write_text(
            "\n" + json.dumps(valid_row(), ensure_ascii=False) + "\n\n", encoding="utf-8"
        )
        rows, problems = load_corpus([path])
        assert len(rows) == 1
        assert problems == []


class TestGrouping:
    def test_rows_of_same_version_are_grouped(self, tmp_path: Path) -> None:
        rows, _ = load_corpus(
            [
                write_corpus(
                    tmp_path,
                    [
                        valid_row(article_no="Section 1"),
                        valid_row(article_no="Section 2"),
                        valid_row(article_no="Section 1", version_label="2024 修订版"),
                    ],
                )
            ]
        )
        groups = group_by_version(rows)
        # 两个版本 → 两组，而不是三组
        assert len(groups) == 2
        sizes = sorted(len(group.rows) for group in groups)
        assert sizes == [1, 2]

    def test_version_level_fields_come_from_the_group(self, tmp_path: Path) -> None:
        rows, _ = load_corpus([write_corpus(tmp_path, [valid_row()])])
        group = group_by_version(rows)[0]
        assert group.key == ("IE", "爱尔兰《测试法》", "enacted")
        assert group.source_url == "https://example.test/act"


class TestDuplicateDetection:
    def test_duplicate_article_number_within_version_is_found(self, tmp_path: Path) -> None:
        """重复条号会让整批 INSERT 撞上 `uq_article_no`。

        必须在写入前发现：否则失败点落在中间某部法规上，
        前面几部已入库、后面几部没有，知识库进入"看起来正常但不完整"的状态。
        """
        rows, _ = load_corpus(
            [
                write_corpus(
                    tmp_path,
                    [valid_row(article_no="Section 7"), valid_row(article_no="Section 7")],
                )
            ]
        )
        duplicates = find_duplicate_article_numbers(rows)
        assert len(duplicates) == 1
        assert "Section 7" in duplicates[0]

    def test_same_article_number_in_different_versions_is_not_a_duplicate(
        self, tmp_path: Path
    ) -> None:
        rows, _ = load_corpus(
            [
                write_corpus(
                    tmp_path,
                    [
                        valid_row(article_no="Section 7"),
                        valid_row(article_no="Section 7", version_label="2024 修订版"),
                    ],
                )
            ]
        )
        assert find_duplicate_article_numbers(rows) == []


# =============================================================================
# 真实语料：断言的是仓库里那批真数据
# =============================================================================
corpus_files = sorted(CORPUS_DIR.glob("*.jsonl")) if CORPUS_DIR.exists() else []


@pytest.mark.skipif(not corpus_files, reason="语料文件不在工作区（可能是部分检出）")
class TestRealCorpus:
    def test_real_corpus_loads_without_problems(self) -> None:
        rows, problems = load_corpus(corpus_files)
        assert problems == [], f"真实语料存在 {len(problems)} 个问题：{problems[:5]}"
        assert len(rows) >= 2000, f"真实语料只加载出 {len(rows)} 条"

    def test_real_corpus_covers_the_two_reachable_jurisdictions(self) -> None:
        rows, _ = load_corpus(corpus_files)
        assert {row.jurisdiction_code for row in rows} == {"IE", "NL"}

    def test_every_real_clause_is_traceable(self) -> None:
        """AC-1.6：每条法条都能回溯到公开来源。"""
        rows, _ = load_corpus(corpus_files)
        untraceable = [row for row in rows if not row.source_url.startswith("http")]
        assert untraceable == []

    def test_every_real_clause_has_content(self) -> None:
        rows, _ = load_corpus(corpus_files)
        assert all(row.content.strip() for row in rows)

    def test_real_corpus_has_no_duplicate_article_numbers(self) -> None:
        rows, _ = load_corpus(corpus_files)
        assert find_duplicate_article_numbers(rows) == []

    def test_irish_corpus_is_year_precision(self) -> None:
        """爱尔兰站点不提供机器可读的通过日期，精度必须如实标为 YEAR。

        这条断言是在守护一个诚实性约束：如果哪天它变成 DAY，
        要么是站点改了（那要更新记录），要么是有人给它编了个具体日期。
        """
        rows, _ = load_corpus(corpus_files)
        irish: list[CorpusRow] = [row for row in rows if row.jurisdiction_code == "IE"]
        assert irish, "语料里应当有爱尔兰的数据"
        assert {row.date_precision for row in irish} == {"YEAR"}

    def test_real_corpus_groups_into_versions(self) -> None:
        rows, _ = load_corpus(corpus_files)
        groups = group_by_version(rows)
        # 组数应远小于条数（多条文归到同一版本），否则说明版本标签没起到分组作用
        assert 2 <= len(groups) < len(rows) / 10
