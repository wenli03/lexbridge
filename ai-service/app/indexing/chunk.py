"""条款切分：把一份法规正文切成可引用的「条」。

**为什么切分值得单独一个模块**：这个系统对外的每个结论都必须挂到具体的条上
（G1.3「无引用不出结论」）。切错一条，下游的检索、引用校验、差异矩阵会一起错，
而且错得很隐蔽——引用看起来是有效的，只是指向了隔壁那条。

切分因此遵循两条原则：

1. **只认行首锚点。** 正文里「见第 12 条」这种引用也会包含「第 12 条」，
   若不加行首约束，一份法规会被切出比实际多几倍的碎片。
2. **层级靠上下文累积。** 「条」归属的编/章/节来自它前面的标题行，
   因此切分是有状态的顺序扫描，而不是纯正则分割。

设计取向：宁可切出层级缺失的条，也不要切出**内容错误**的条——
前者可以在复核队列里被人工修，后者会被当成事实引用出去。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# 中文数字（含〇与两）
_CN_NUM = "〇零一二三四五六七八九十百千两"


@dataclass(frozen=True)
class Clause:
    """一条法条。字段与 `kb.article` 对齐。"""

    article_no: str
    content: str
    hierarchy_path: list[str] = field(default_factory=list)

    @property
    def char_length(self) -> int:
        return len(self.content)


# =============================================================================
# 中文：第X编 / 第X章 / 第X节 / 第X条
# =============================================================================
_CN_STRUCT_RE = re.compile(rf"^第[{_CN_NUM}]+(编|章|节)\s*\S*")
_CN_ARTICLE_RE = re.compile(rf"^第[{_CN_NUM}]+条")
# 「目录」块里的条目形如「第一章 基本规定 ......... 1」
_CN_TOC_LINE_RE = re.compile(r"\.{3,}\s*\d+\s*$")
_CN_TOC_HEAD_RE = re.compile(r"^\s*目\s*录\s*$")


# =============================================================================
# 英文：PART / CHAPTER / Section N
# =============================================================================
_EN_STRUCT_RE = re.compile(r"^(PART|Chapter|CHAPTER|Part)\s+[IVXLC0-9]+\b")
# 「12.—Some heading」与「Section 12. Some heading」两种排版都常见
_EN_ARTICLE_RE = re.compile(
    r"^(?:Section\s+(?P<num1>\d+[A-Z]?)\s*[.．]|(?P<num2>\d+[A-Z]?)\s*[.．]\s*[—\-–])"
)


def _normalize(text: str) -> str:
    """统一换行与空白，但**不**动正文里的字词。"""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("\u3000", " ").replace("\xa0", " ")
    lines = [line.rstrip() for line in text.split("\n")]
    return "\n".join(lines)


def _strip_toc(lines: list[str]) -> list[str]:
    """丢掉开头的目录块。

    目录里同样有「第一章」「第一条」这样的行，不丢掉的话，
    切分结果的前半部分会是目录条目，而且它们会被当成真正的条。
    """
    start = 0
    for index, line in enumerate(lines):
        stripped = line.strip()
        if _CN_TOC_HEAD_RE.match(stripped):
            start = index + 1
            continue
        if start and _CN_TOC_LINE_RE.search(stripped):
            start = index + 1
            continue
        if start and not stripped:
            start = index + 1
            continue
        if start:
            break
    return lines[start:] if start else lines


def _detect_language(lines: list[str]) -> str:
    """按「出现得更早的锚点」判定语言，而不是按字符数量。

    中英混排的法规（例如香港条例）常常是中英对照，此时**正文开头**的语言
    才代表这份文件是按哪种格式切分的。
    """
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        if _CN_ARTICLE_RE.match(stripped) or _CN_STRUCT_RE.match(stripped):
            return "zh"
        if _EN_ARTICLE_RE.match(stripped) or _EN_STRUCT_RE.match(stripped):
            return "en"
    return "zh"


def _split_zh(lines: list[str]) -> list[Clause]:
    clauses: list[Clause] = []
    hierarchy: list[str] = []
    current_no: str | None = None
    buffer: list[str] = []

    def flush() -> None:
        if current_no is None:
            return
        content = "\n".join(part for part in buffer if part.strip()).strip()
        if content:
            clauses.append(Clause(current_no, content, [*hierarchy]))

    for line in lines:
        stripped = line.strip()
        if not stripped:
            buffer.append("")
            continue
        if _CN_STRUCT_RE.match(stripped):
            # 新的章/节：先把上一条收尾，再重置层级
            flush()
            current_no = None
            buffer = []
            # 编 > 章 > 节：遇到同级别或更高级别的标题时，丢弃更低级别的那一层
            hierarchy = [part for part in hierarchy if not _same_or_lower(part, stripped)]
            hierarchy.append(stripped)
            continue
        if _CN_ARTICLE_RE.match(stripped):
            flush()
            current_no = stripped.split()[0].split("\u3000")[0]
            rest = stripped[len(current_no) :].strip()
            buffer = [rest] if rest else []
            continue
        if current_no is not None:
            buffer.append(stripped)

    flush()
    return clauses


_LEVEL_RE = re.compile(r"(编|章|节)")
_LEVEL_ORDER = {"编": 0, "章": 1, "节": 2}


def _level(title: str) -> int:
    """取层级标题的级别：编 0 < 章 1 < 节 2，未知为最低。

    **不能用位置索引取这个字**：`"第一章"[1:2]` 得到的是 "一"，
    于是"章"被当成未知级别，"节"反而变成更高级别，层级会被整段覆盖掉。
    这类错误在测试里表现为"层级少了一层"，在生产里表现为引用路径错位。
    """
    match = _LEVEL_RE.search(title)
    return _LEVEL_ORDER.get(match.group(1), 3) if match else 3


def _same_or_lower(existing: str, candidate: str) -> bool:
    """判断已有层级标题是否应被新标题替换（同级别或更低级别）。"""
    return _level(existing) >= _level(candidate)


def _split_en(lines: list[str]) -> list[Clause]:
    clauses: list[Clause] = []
    hierarchy: list[str] = []
    current_no: str | None = None
    buffer: list[str] = []

    def flush() -> None:
        if current_no is None:
            return
        content = "\n".join(part for part in buffer if part.strip()).strip()
        if content:
            clauses.append(Clause(current_no, content, [*hierarchy]))

    for line in lines:
        stripped = line.strip()
        if not stripped:
            buffer.append("")
            continue
        if _EN_STRUCT_RE.match(stripped):
            flush()
            current_no = None
            buffer = []
            hierarchy = [stripped]
            continue
        match = _EN_ARTICLE_RE.match(stripped)
        if match:
            flush()
            number = match.group("num1") or match.group("num2") or ""
            current_no = f"Section {number}"
            rest = stripped[match.end() :].strip(" .—–-")
            buffer = [rest] if rest else []
            continue
        if current_no is not None:
            buffer.append(stripped)

    flush()
    return clauses


def split_articles(text: str, *, min_length: int = 10) -> list[Clause]:
    """把法规正文切成条。

    Args:
        text: 法规正文（纯文本，可含标题与目录）。
        min_length: 小于该长度的条被视为切分噪声丢弃。
            阈值存在的理由：`Section 12.` 单独成行时后面可能没有正文，
            这种空条如果入库，检索命中它会得到一条"看起来有引用但读不出内容"的结果。

    Returns:
        按原文顺序排列的条。切不出条时返回空列表——
        **不要**退化成"把整篇当作一条"，那会让引用粒度失效，比切不出来更糟。
    """
    if not text or not text.strip():
        return []

    lines = _normalize(text).split("\n")
    lines = _strip_toc(lines)
    language = _detect_language(lines)
    clauses = _split_zh(lines) if language == "zh" else _split_en(lines)
    return [clause for clause in clauses if clause.char_length >= min_length]
