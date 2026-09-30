#!/usr/bin/env python3
"""语料校验：对着 AC-1.6 逐条核对，并**如实报告差距**。

这个脚本的设计取向与项目其它部分一致：宁可报告"没达标"，
也不要让一个漂亮的指标掩盖事实。它做两件事：

1. **质量校验**（必须全过）：溯源字段完整、条号不重复、正文非空、日期格式合法。
2. **规模与覆盖报告**（当前不达标）：六法域 ≥5,000 条。

用法：
    python scripts/verify_corpus.py            # 报告模式，恒以 0 退出
    python scripts/verify_corpus.py --strict   # 不达标即非 0 退出（用于明确验收）
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CORPUS_DIR = REPO_ROOT / "deploy" / "seed" / "corpus"

# AC-1.6 的目标值
TARGET_ARTICLES = 5000
TARGET_JURISDICTIONS = ["CN", "HK", "SG", "IE", "NL", "KY"]

REQUIRED_FIELDS = (
    "jurisdiction_code", "statute_title", "statute_no", "version_label",
    "effective_from", "source_url", "source_fetched_at", "article_no",
    "hierarchy_path", "content",
)

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def load(code: str) -> list[dict]:
    path = CORPUS_DIR / f"{code}.jsonl"
    if not path.exists():
        return []
    rows: list[dict] = []
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise SystemExit(f"{path.name}:{line_no} JSON 解析失败: {exc}") from exc
    return rows


def check_quality(code: str, rows: list[dict]) -> list[str]:
    """返回问题清单，空清单表示通过。"""
    problems: list[str] = []
    if not rows:
        return problems
    missing = Counter()
    for row in rows:
        for field in REQUIRED_FIELDS:
            if not row.get(field):
                missing[field] += 1
    for field, count in missing.items():
        problems.append(f"{count} 条缺少字段 {field}")

    bad_dates = sum(
        1 for row in rows
        if not DATE_RE.match(str(row.get("effective_from", "")))
        or (row.get("effective_to") and not DATE_RE.match(str(row["effective_to"])))
    )
    if bad_dates:
        problems.append(f"{bad_dates} 条生效日期格式不是 YYYY-MM-DD")

    short = sum(1 for row in rows if len(row.get("content", "")) < 10)
    if short:
        problems.append(f"{short} 条正文长度 <10 字符")

    no_url = sum(1 for row in rows if not str(row.get("source_url", "")).startswith("http"))
    if no_url:
        problems.append(f"{no_url} 条来源 URL 不是 http(s) 链接")

    # 同一版本内条号必须唯一——重复意味着切分逻辑把同一条切成了两半
    keys = Counter(
        (row["statute_no"], row["version_label"], row["article_no"]) for row in rows
    )
    dupes = [key for key, count in keys.items() if count > 1]
    if dupes:
        problems.append(f"{len(dupes)} 组重复条号，例如 {dupes[0]}")

    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description="校验六法域语料")
    parser.add_argument("--strict", action="store_true",
                        help="不满足 AC-1.6 时以非 0 退出")
    args = parser.parse_args()

    print("=" * 68)
    print("语料质量校验（必须全过）")
    print("=" * 68)
    quality_failed = False
    totals: dict[str, int] = {}
    statues: dict[str, int] = {}
    precisions: Counter = Counter()

    for code in TARGET_JURISDICTIONS:
        rows = load(code)
        totals[code] = len(rows)
        statues[code] = len({(r["statute_title"], r["version_label"]) for r in rows})
        precisions.update(str(r.get("date_precision", "DAY")) for r in rows)
        problems = check_quality(code, rows)
        if not rows:
            print(f"  {code}: 无语料")
            continue
        if problems:
            quality_failed = True
            print(f"  {code}: 不通过")
            for problem in problems:
                print(f"      - {problem}")
        else:
            print(f"  {code}: 通过（{len(rows)} 条）")

    print()
    print("=" * 68)
    print("规模与覆盖（对照 AC-1.6 / D-02）")
    print("=" * 68)
    print(f"  {'法域':<6}{'法条数':>9}{'法规数':>8}")
    for code in TARGET_JURISDICTIONS:
        note = "" if totals[code] else "   ← 未采集"
        print(f"  {code:<6}{totals[code]:>9}{statues[code]:>8}{note}")
    total = sum(totals.values())
    covered = sum(1 for code in TARGET_JURISDICTIONS if totals[code] > 0)
    print(f"  {'合计':<6}{total:>9}{sum(statues.values()):>8}")

    print()
    print(f"  法域覆盖：{covered} / {len(TARGET_JURISDICTIONS)}   目标 {len(TARGET_JURISDICTIONS)}")
    print(f"  法条总数：{total} / {TARGET_ARTICLES}")
    if precisions:
        detail = "，".join(f"{name} 精度 {count} 条" for name, count in precisions.items())
        print(f"  日期精度：{detail}")

    print()
    print("=" * 68)
    print("结论")
    print("=" * 68)
    meets = covered == len(TARGET_JURISDICTIONS) and total >= TARGET_ARTICLES
    if quality_failed:
        print("  语料质量校验未通过——这是缺陷，必须修复。")
    if meets and not quality_failed:
        print("  ✅ 满足 AC-1.6。")
        return 0

    gaps = TARGET_ARTICLES - total
    print(f"  ❌ 未满足 AC-1.6：缺 {max(0, gaps)} 条法条，"
          f"缺 {len(TARGET_JURISDICTIONS) - covered} 个法域。")
    print("     按 D-02 / D-17，此差距必须如实记录在 README「已知边界」，")
    print("     并通过扩充可访问的数据源来缩小，不得改写指标。")
    print("     各法域不可达的具体原因见 deploy/seed/corpus/SOURCES.md。")
    if args.strict:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
