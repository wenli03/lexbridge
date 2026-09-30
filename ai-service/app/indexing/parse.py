"""文档解析：把上传的原件变成可切分的纯文本。

**这个模块的核心职责不是"读出文字"，而是"读不出文字时如实说出来"。**

需求 N11 / AC-1.7 规定：扫描件 PDF 不做 OCR，进入 `NEEDS_MANUAL` 队列并给出原因。
为什么值得单列一条验收标准：一份纯扫描的 PDF，用任何文本提取库都会**成功返回空字符串**。
如果流水线把空字符串当成正常结果继续走下去，后续切分会切出 0 条，
于是入库任务显示"成功"、知识库里一片空白——**看起来像成功，实际什么都没进来**。
这类静默失效比直接报错难查得多：任务状态是绿的，没人会去看。

因此解析结果是一个**二选一**的值（`ParsedDocument` 或 `NeedsManualReview`），
而不是"文本 + 一个可能为空的字符串"。调用方必须显式处理两种情况，
编译器/类型检查会替我们看着这件事。
"""

from __future__ import annotations

import io
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

logger = logging.getLogger(__name__)

#: 纯文本类格式：直接按文本读，不需要任何解析库
PLAIN_TEXT_SUFFIXES = frozenset({".txt", ".md", ".markdown", ".text"})
HTML_SUFFIXES = frozenset({".html", ".htm", ".xhtml"})
PDF_SUFFIXES = frozenset({".pdf"})

#: 一页 PDF 至少应有这么多字符才算"有文本层"。
#: 阈值取得很低（20 字符/页）：我们是拿来判定"是不是扫描件"，不是判定"内容是否充实"。
#: 定得太高会把表格密集、图示为主但确实有文本层的 PDF 误判为扫描件。
MIN_CHARS_PER_PAGE = 20


@dataclass(frozen=True)
class ParsedDocument:
    """解析成功的结果。"""

    text: str
    source_format: str
    page_count: int | None = None


@dataclass(frozen=True)
class NeedsManualReview:
    """需要人工介入。

    **这是正常结果，不是异常。** 用返回值而不是 raise，是因为调用方需要把它
    转成 `stage='NEEDS_MANUAL'` 并继续处理下一个任务；异常会让"这份文件需要人看"
    和"程序出错了"混在同一个 except 分支里。
    """

    reason_code: str
    reason_text: str
    page_count: int | None = None


ParseOutcome = ParsedDocument | NeedsManualReview


class PdfTextReader(Protocol):
    """PDF 文本提取的可注入接口。

    做成协议而不是直接调用某个库，有两个具体好处：

    1. **测试不需要真 PDF**。"扫描件返回空文本"这个分支是本模块最关键的逻辑，
       而要构造一份真实的无文本层 PDF 很麻烦（得嵌入一张图片）。
       注入一个返回空串的假 reader，就能把这条件测穿。
    2. **换库不改业务代码**。提取库从 pypdf 换到别的，只影响默认实现。
    """

    def __call__(self, data: bytes) -> tuple[str, int]:
        """返回 (提取到的文本, 页数)。"""
        ...


def parse_document(
    data: bytes,
    filename: str,
    *,
    pdf_reader: PdfTextReader | None = None,
) -> ParseOutcome:
    """解析一份原件。

    Args:
        data: 文件字节。
        filename: 原始文件名，只用它的后缀来判定类型。
        pdf_reader: PDF 提取实现。为 None 时使用默认实现（延迟导入 pypdf）。

    Returns:
        `ParsedDocument`（可继续切分）或 `NeedsManualReview`（转人工队列）。
        **不会抛异常**：任何一个分支都要落成任务状态，让管理员在界面上看到发生了什么。
    """
    suffix = Path(filename).suffix.lower()

    if not data:
        return NeedsManualReview(
            reason_code="EMPTY_FILE",
            reason_text="文件是空的（0 字节），没有可解析的内容。",
        )

    if suffix in PLAIN_TEXT_SUFFIXES:
        return _parse_text(data, suffix)

    if suffix in HTML_SUFFIXES:
        return _parse_html(data)

    if suffix in PDF_SUFFIXES:
        return _parse_pdf(data, pdf_reader)

    # 提示里统一用「扩展名」：前半句说不支持 `.docx`，后半句就该列举支持的扩展名。
    # 混用格式名（"PDF"）与扩展名（".txt"）会让管理员的下一句疑问变成
    # "所以我该传 .pdf 还是 PDF？"，而这种疑问本不该产生。
    return NeedsManualReview(
        reason_code="UNSUPPORTED_FORMAT",
        reason_text=(
            f"不支持的文件类型 {suffix or '（无扩展名）'}。"
            "当前支持：.pdf（需含文本层）、.txt、.md、.html。"
        ),
    )


# =============================================================================
# 纯文本
# =============================================================================
def _decode_text(data: bytes) -> str | None:
    """按候选编码依次尝试解码。

    **中文法规很可能是 GBK/GB18030**。用 utf-8 强解会把 GBK 文件解成一堆替换字符，
    而更糟的一种情况是 latin-1 —— 它对任何字节序列都"解码成功"，
    于是中文字符变成乱码却完全不报错，一路切分入库。
    所以编码列表按"最可能且最严格"到"最宽松"排列，且**最后不接受 latin-1**：
    宁可判定为需要人工介入，也不要产出乱码正文。
    """
    for encoding in ("utf-8-sig", "utf-8", "gb18030", "big5"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return None


def _parse_text(data: bytes, suffix: str) -> ParseOutcome:
    text = _decode_text(data)
    if text is None:
        return NeedsManualReview(
            reason_code="UNREADABLE_ENCODING",
            reason_text=(
                "无法确定文本编码（已尝试 UTF-8 / GB18030 / Big5）。"
                "为避免把乱码当成正文入库，转人工确认编码后重传。"
            ),
        )
    if not text.strip():
        return NeedsManualReview(
            reason_code="EMPTY_DOCUMENT",
            reason_text="文件有内容但没有任何非空白字符，没有可入库的正文。",
        )
    return ParsedDocument(text=text, source_format=suffix.lstrip("."))


# =============================================================================
# HTML
# =============================================================================
_SCRIPT_STYLE_RE = re.compile(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>")
_BLOCK_TAG_RE = re.compile(
    r"(?i)</?(p|div|br|li|tr|h[1-6]|section|article|table|ul|ol|blockquote)[^>]*>"
)
_ANY_TAG_RE = re.compile(r"(?s)<[^>]+>")
_ENTITY_MAP = {
    "&nbsp;": " ",
    "&amp;": "&",
    "&lt;": "<",
    "&gt;": ">",
    "&quot;": '"',
    "&#39;": "'",
    "&apos;": "'",
    "&mdash;": "—",
    "&ndash;": "–",
    "&hellip;": "…",
}


def html_to_text(html_text: str) -> str:
    """把 HTML 转成保留段落的纯文本。

    **块级标签换成换行，而不是空格。** 条款切分靠行首锚点（`第X条`、`Section N`）。
    如果把 `</div>` 换成空格，原本各占一行的法条会被连成一整行，
    切分器就再也找不到行首锚点——表现为"HTML 法规一条都切不出来"。
    """
    text = _SCRIPT_STYLE_RE.sub(" ", html_text)
    text = _BLOCK_TAG_RE.sub("\n", text)
    text = _ANY_TAG_RE.sub("", text)
    for entity, replacement in _ENTITY_MAP.items():
        text = text.replace(entity, replacement)
    # 数字实体（&#123; 之类）在法规里少见，但出现了就是正文的一部分
    text = re.sub(r"&#(\d+);", lambda m: chr(int(m.group(1))), text)
    lines = [line.strip() for line in text.split("\n")]
    return "\n".join(line for line in lines if line)


def _parse_html(data: bytes) -> ParseOutcome:
    raw = _decode_text(data)
    if raw is None:
        return NeedsManualReview(
            reason_code="UNREADABLE_ENCODING",
            reason_text="无法确定 HTML 文件的编码，转人工确认后重传。",
        )
    text = html_to_text(raw)
    if not text.strip():
        return NeedsManualReview(
            reason_code="EMPTY_DOCUMENT",
            reason_text="HTML 里没有可提取的正文（可能全是脚本或样式）。",
        )
    return ParsedDocument(text=text, source_format="html")


# =============================================================================
# PDF
# =============================================================================
def _default_pdf_reader(data: bytes) -> tuple[str, int]:
    """默认实现：延迟导入 pypdf。

    延迟导入的理由：pypdf 由 LlamaIndex 的 reader 间接提供，而本模块要能在
    没装它的环境里被导入（例如只跑纯逻辑测试时）。真正的导入失败会在调用时
    以清晰的错误暴露，而不是让 `import app.indexing.parse` 直接失败。
    """
    try:
        from pypdf import PdfReader
    except ImportError as exc:  # pragma: no cover - 取决于运行环境
        raise RuntimeError(
            "解析 PDF 需要 pypdf（LlamaIndex 的 reader 依赖）。"
            "安装：uv sync --extra dev；或改用文本格式上传。"
        ) from exc

    reader = PdfReader(io.BytesIO(data))
    pages = [page.extract_text() or "" for page in reader.pages]
    return "\n".join(pages), len(reader.pages)


def _parse_pdf(data: bytes, pdf_reader: PdfTextReader | None) -> ParseOutcome:
    reader = pdf_reader or _default_pdf_reader
    try:
        text, page_count = reader(data)
    except RuntimeError:
        # 依赖缺失：这是部署问题，原样抛出比伪装成"需要人工"更诚实
        raise
    except Exception as exc:  # noqa: BLE001 - 损坏的 PDF 形态太多，统一转为人工队列
        logger.warning("PDF 解析失败，转人工队列：%s", type(exc).__name__)
        return NeedsManualReview(
            reason_code="PDF_PARSE_ERROR",
            reason_text=f"PDF 无法解析（{type(exc).__name__}）。文件可能损坏或加密。",
        )

    stripped = text.strip()
    density = len(stripped) / page_count if page_count else 0

    # 判定扫描件的核心分支：有页数但几乎没有文字。
    # 这个判断之所以必须显式做，是因为它**不报错**——
    # 提取库对纯图片 PDF 会正常返回空串。
    if page_count > 0 and density < MIN_CHARS_PER_PAGE:
        return NeedsManualReview(
            reason_code="SCANNED_PDF",
            reason_text=(
                f"该 PDF 共 {page_count} 页，但只提取到 {len(stripped)} 个字符，"
                "判断为无文本层的扫描件。系统不做 OCR，因此不会入库"
                "（否则会得到一个内容为空、却在界面上显示成功的知识库条目）。"
                "请提供含文本层的版本，或转人工录入。"
            ),
            page_count=page_count,
        )

    if not stripped:
        return NeedsManualReview(
            reason_code="EMPTY_DOCUMENT",
            reason_text="PDF 没有可提取的文本。",
            page_count=page_count,
        )

    return ParsedDocument(text=text, source_format="pdf", page_count=page_count)
