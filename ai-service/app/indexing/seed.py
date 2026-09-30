"""种子语料导入：把 `deploy/seed/corpus/*.jsonl` 规范化成可入库的结构。

**为什么导入路径值得单独一个模块，而不是跟上传路径共用一份代码。**
两者的输入形态根本不同：上传路径拿到的是原始文件，需要解析、切分、抽取、算置信度；
种子语料是**已经结构化好的**（`scripts/fetch_corpus.py` 采集时就把条号与层级切好了），
直接是「一行一条法条」。硬要统一，就得给种子数据反向伪造一个原始文件再解析一遍——
凭空多出一堆失败点，而收益只是"少了一个模块"。

本模块只做规范化与校验，**不碰数据库**。理由同上：校验必须能在没有数据库的情况下
跑，否则"这批语料能不能入库"这个问题只能靠真的去入库一次才知道，
而失败会发生在**中途**——部分法规已写入，任务却失败了。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path

logger = logging.getLogger(__name__)

#: 语料目录（相对仓库根）。采集脚本写入这里，本模块只读。
CORPUS_DIR = Path("deploy/seed/corpus")

#: 允许的日期精度。与 `kb.statute_version.date_precision` 的约束一致。
ALLOWED_PRECISIONS = frozenset({"DAY", "YEAR"})

REQUIRED_FIELDS = (
    "jurisdiction_code",
    "statute_title",
    "version_label",
    "effective_from",
    "source_url",
    "article_no",
    "hierarchy_path",
    "content",
)


@dataclass(frozen=True)
class CorpusRow:
    """一行 = 一条法条。字段名与采集脚本的输出一一对应。"""

    jurisdiction_code: str
    statute_title: str
    statute_title_original: str | None
    statute_no: str | None
    version_label: str
    effective_from: date
    effective_to: date | None
    date_precision: str
    source_url: str
    source_fetched_at: str | None
    article_no: str
    hierarchy_path: tuple[str, ...]
    content: str

    @property
    def version_key(self) -> tuple[str, str, str]:
        """一个法规版本的唯一标识：法域 + 名称 + 版本标签。

        用它分组，而不是用文件名——同一部法规可能来自多个文件（分次采集），
        按文件分组会为同一版本建出多条 `statute_version`。
        """
        return (self.jurisdiction_code, self.statute_title, self.version_label)


@dataclass(frozen=True)
class StatuteVersionGroup:
    """一个法规版本及其全部法条。"""

    key: tuple[str, str, str]
    statute_title_original: str | None
    statute_no: str | None
    effective_from: date
    effective_to: date | None
    date_precision: str
    source_url: str
    source_fetched_at: str | None
    rows: tuple[CorpusRow, ...]


def load_corpus(paths: Sequence[Path]) -> tuple[list[CorpusRow], list[str]]:
    """读取并规范化语料文件。

    Returns:
        (解析成功的行, 问题清单)。**问题不抛异常而是返回**：
        导入前需要先把全部问题一次看清（例如 3 个法域的日期格式都不对），
        而不是修一个跑一次。
    """
    rows: list[CorpusRow] = []
    problems: list[str] = []

    for path in paths:
        if not path.exists():
            problems.append(f"{path}：文件不存在")
            continue
        for line_number, raw_line in enumerate(path.read_text("utf-8").splitlines(), start=1):
            line = raw_line.strip()
            if not line:
                continue
            where = f"{path.name}:{line_number}"
            try:
                payload = json.loads(line)
            except json.JSONDecodeError as exc:
                problems.append(f"{where}：JSON 解析失败（{exc.msg}）")
                continue
            row, row_problems = _to_row(payload, where)
            problems.extend(row_problems)
            if row is not None:
                rows.append(row)

    return rows, problems


def _to_row(payload: dict, where: str) -> tuple[CorpusRow | None, list[str]]:
    problems: list[str] = []

    missing = [field for field in REQUIRED_FIELDS if not payload.get(field)]
    if missing:
        return None, [f"{where}：缺少必填字段 {', '.join(missing)}"]

    try:
        effective_from = date.fromisoformat(str(payload["effective_from"])[:10])
    except ValueError:
        return None, [f"{where}：effective_from 不是合法日期（{payload['effective_from']!r}）"]

    effective_to: date | None = None
    raw_to = payload.get("effective_to")
    if raw_to:
        try:
            effective_to = date.fromisoformat(str(raw_to)[:10])
        except ValueError:
            problems.append(
                f"{where}：effective_to 不是合法日期（{raw_to!r}），已按「仍然有效」处理"
            )
    if effective_to is not None and effective_to <= effective_from:
        problems.append(
            f"{where}：effective_to（{effective_to}）不晚于 effective_from（{effective_from}），"
            "已按「仍然有效」处理——区间倒置会让「按版本回答」失效"
        )
        effective_to = None

    precision = str(payload.get("date_precision") or "DAY").upper()
    if precision not in ALLOWED_PRECISIONS:
        problems.append(f"{where}：date_precision={precision!r} 不在允许集合内，已按 DAY 处理")
        precision = "DAY"

    hierarchy = payload["hierarchy_path"]
    if not isinstance(hierarchy, list) or not hierarchy:
        return None, [f"{where}：hierarchy_path 必须是非空数组"]

    row = CorpusRow(
        jurisdiction_code=str(payload["jurisdiction_code"]),
        statute_title=str(payload["statute_title"]),
        statute_title_original=payload.get("statute_title_original") or None,
        statute_no=(str(payload["statute_no"]) if payload.get("statute_no") else None),
        version_label=str(payload["version_label"]),
        effective_from=effective_from,
        effective_to=effective_to,
        date_precision=precision,
        source_url=str(payload["source_url"]),
        source_fetched_at=payload.get("source_fetched_at"),
        article_no=str(payload["article_no"]),
        hierarchy_path=tuple(str(part) for part in hierarchy),
        content=str(payload["content"]),
    )
    return row, problems


def group_by_version(rows: Iterable[CorpusRow]) -> list[StatuteVersionGroup]:
    """按法规版本分组。

    组内的版本级字段（生效区间、来源 URL）**必须一致**；不一致说明采集脚本
    或数据本身有问题，此时取首行的值并记录下来——静默取首行会让"这一版到底
    什么时候生效"变成一个说不清的问题。
    """
    buckets: dict[tuple[str, str, str], list[CorpusRow]] = {}
    for row in rows:
        buckets.setdefault(row.version_key, []).append(row)

    groups: list[StatuteVersionGroup] = []
    for key, bucket in buckets.items():
        first = bucket[0]
        for other in bucket[1:]:
            if other.effective_from != first.effective_from:
                logger.warning(
                    "同一版本内 effective_from 不一致：%s 为 %s，%s 为 %s（按前者处理）",
                    first.article_no,
                    first.effective_from,
                    other.article_no,
                    other.effective_from,
                )
                break
        groups.append(
            StatuteVersionGroup(
                key=key,
                statute_title_original=first.statute_title_original,
                statute_no=first.statute_no,
                effective_from=first.effective_from,
                effective_to=first.effective_to,
                date_precision=first.date_precision,
                source_url=first.source_url,
                source_fetched_at=first.source_fetched_at,
                rows=tuple(bucket),
            )
        )
    return groups


def find_duplicate_article_numbers(rows: Iterable[CorpusRow]) -> list[str]:
    """找出同一版本内重复的条号。

    **必须在写入前查出来。** `kb.article` 上有 `uq_article_no
    (statute_version_id, tenant_scope, article_no)`，重复条号会让整批 INSERT
    失败——而如果按法规分批提交，失败会停在中间某部法规上，
    前面几部已入库、后面几部没有，知识库进入一个"看起来正常但不完整"的状态。
    """
    seen: set[tuple[tuple[str, str, str], str]] = set()
    duplicates: list[str] = []
    for row in rows:
        marker = (row.version_key, row.article_no)
        if marker in seen:
            duplicates.append(f"{row.statute_title} / {row.article_no}（版本 {row.version_label}）")
        seen.add(marker)
    return duplicates
