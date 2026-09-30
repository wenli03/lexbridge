"""文档解析的测试。

重点不在"能读出文字"，而在**读不出文字时的行为**：
扫描件、空文件、坏编码都必须落到 `NeedsManualReview`，而不是返回一个空字符串让
流水线"成功"地入库 0 条法条。因此下面每个"失败分支"都有专门用例。
"""

from __future__ import annotations

import pytest

from app.indexing.chunk import split_articles
from app.indexing.parse import (
    NeedsManualReview,
    ParsedDocument,
    html_to_text,
    parse_document,
)

CHINESE_LAW = """中华人民共和国企业所得税法

第一章 总则

第一条 在中华人民共和国境内，企业和其他取得收入的组织为企业所得税的纳税人。

第二条 企业分为居民企业和非居民企业。本法所称居民企业，是指依法在中国境内成立的企业。
"""


def fake_pdf_reader(text: str, pages: int):
    """构造一个返回固定文本与页数的假 PDF reader。"""

    def reader(_data: bytes) -> tuple[str, int]:
        return text, pages

    return reader


class TestPlainText:
    def test_markdown_is_parsed(self) -> None:
        outcome = parse_document(CHINESE_LAW.encode("utf-8"), "law.md")
        assert isinstance(outcome, ParsedDocument)
        assert outcome.source_format == "md"
        assert "第一条" in outcome.text

    def test_parsed_text_is_chunkable(self) -> None:
        """解析与切分的衔接：解析出来的文本必须真的能切出条。

        这是两个模块之间的接缝。只测解析会漏掉"文本读出来了但切分器认不出"的情况，
        而那正是最常见的集成失败。
        """
        outcome = parse_document(CHINESE_LAW.encode("utf-8"), "law.txt")
        assert isinstance(outcome, ParsedDocument)
        clauses = split_articles(outcome.text)
        assert [c.article_no for c in clauses] == ["第一条", "第二条"]

    def test_gbk_encoded_chinese_is_decoded(self) -> None:
        """中文法规很可能是 GBK 编码，用 utf-8 强解会得到乱码却不报错。"""
        outcome = parse_document(CHINESE_LAW.encode("gb18030"), "law.txt")
        assert isinstance(outcome, ParsedDocument)
        assert "企业所得税" in outcome.text
        # 乱码的典型形态：出现替换字符
        assert "\ufffd" not in outcome.text

    def test_unreadable_encoding_goes_to_manual(self) -> None:
        """无法解码时宁可转人工，也不要产出乱码正文。"""
        outcome = parse_document(b"\xff\xff\xff\xff", "law.txt")
        assert isinstance(outcome, NeedsManualReview)
        assert outcome.reason_code == "UNREADABLE_ENCODING"

    def test_utf8_bom_is_stripped(self) -> None:
        """带 BOM 的 utf-8 若不处理，首个「第一条」前面会多一个不可见字符，
        行首锚点匹配失败，第一条会丢失。"""
        outcome = parse_document(CHINESE_LAW.encode("utf-8-sig"), "law.txt")
        assert isinstance(outcome, ParsedDocument)
        clauses = split_articles(outcome.text)
        assert clauses and clauses[0].article_no == "第一条"


class TestManualReview:
    def test_empty_file(self) -> None:
        outcome = parse_document(b"", "law.txt")
        assert isinstance(outcome, NeedsManualReview)
        assert outcome.reason_code == "EMPTY_FILE"

    def test_whitespace_only(self) -> None:
        outcome = parse_document(b"   \n\t\n  ", "law.txt")
        assert isinstance(outcome, NeedsManualReview)
        assert outcome.reason_code == "EMPTY_DOCUMENT"

    def test_unsupported_suffix_lists_supported_types(self) -> None:
        outcome = parse_document(b"whatever", "scan.docx")
        assert isinstance(outcome, NeedsManualReview)
        assert outcome.reason_code == "UNSUPPORTED_FORMAT"
        # 提示必须说清支持什么，否则管理员只能靠猜
        assert ".pdf" in outcome.reason_text


class TestHtml:
    def test_block_tags_become_line_breaks(self) -> None:
        """块级标签必须换成换行。

        若换成空格，原本次次独占一行的法条会连成一整行，行首锚点失效，
        表现为"HTML 版法规一条都切不出来"。
        """
        html = (
            "<html><body>"
            "<div>第一条 企业所得税的税率为百分之二十五。</div>"
            "<div>第二条 非居民企业取得本法第三条规定的所得，适用税率为百分之二十。</div>"
            "</body></html>"
        )
        text = html_to_text(html)
        clauses = split_articles(text)
        assert [c.article_no for c in clauses] == ["第一条", "第二条"]

    def test_script_and_style_are_removed(self) -> None:
        html = (
            "<html><head><style>p{color:red}</style>"
            "<script>var x = '第一条 不应被当成正文';</script></head>"
            "<body><p>第二条 本法的实际条款内容在这里，长度足够。</p></body></html>"
        )
        text = html_to_text(html)
        assert "color:red" not in text
        assert "不应被当成正文" not in text
        assert "第二条" in text

    def test_entities_are_decoded(self) -> None:
        text = html_to_text("<p>税率 &gt; 10%&nbsp;且 &lt; 20%</p>")
        assert ">" in text and "<" in text
        assert "&nbsp;" not in text

    def test_html_without_body_text_goes_to_manual(self) -> None:
        outcome = parse_document(
            b"<html><head><script>var a=1;</script></head><body></body></html>", "law.html"
        )
        assert isinstance(outcome, NeedsManualReview)
        assert outcome.reason_code == "EMPTY_DOCUMENT"


class TestPdf:
    def test_text_pdf_is_parsed(self) -> None:
        outcome = parse_document(
            b"%PDF-fake",
            "law.pdf",
            pdf_reader=fake_pdf_reader(CHINESE_LAW * 3, 2),
        )
        assert isinstance(outcome, ParsedDocument)
        assert outcome.page_count == 2
        assert split_articles(outcome.text)

    def test_scanned_pdf_goes_to_manual_instead_of_silently_succeeding(self) -> None:
        """本文件最重要的一条测试。

        纯图片 PDF 的文本提取会**正常返回空串**（不报错）。若流水线把它当成功，
        入库任务会显示绿色、知识库里 0 条法条，而没有任何地方提示出了问题。
        """
        outcome = parse_document(b"%PDF-fake", "scan.pdf", pdf_reader=fake_pdf_reader("", 12))
        assert isinstance(outcome, NeedsManualReview)
        assert outcome.reason_code == "SCANNED_PDF"
        assert outcome.page_count == 12
        # 原因里要写明"不做 OCR"，否则管理员会以为是我们没做对
        assert "OCR" in outcome.reason_text

    def test_low_density_pdf_is_treated_as_scanned(self) -> None:
        """有零星文字（页眉页码）也仍然是扫描件。"""
        outcome = parse_document(
            b"%PDF-fake", "scan.pdf", pdf_reader=fake_pdf_reader("1\n2\n3\n", 30)
        )
        assert isinstance(outcome, NeedsManualReview)
        assert outcome.reason_code == "SCANNED_PDF"

    def test_pdf_parse_error_goes_to_manual(self) -> None:
        def broken_reader(_data: bytes) -> tuple[str, int]:
            raise ValueError("damaged xref")

        outcome = parse_document(b"%PDF-fake", "broken.pdf", pdf_reader=broken_reader)
        assert isinstance(outcome, NeedsManualReview)
        assert outcome.reason_code == "PDF_PARSE_ERROR"

    def test_missing_pdf_dependency_is_not_masked_as_manual(self) -> None:
        """依赖缺失是部署问题，不能伪装成"需要人工"。

        伪装的具体后果：管理员会去修文件，而真正要做的是装依赖。
        """

        def missing_dep(_data: bytes) -> tuple[str, int]:
            raise RuntimeError("解析 PDF 需要 pypdf")

        with pytest.raises(RuntimeError):
            parse_document(b"%PDF-fake", "law.pdf", pdf_reader=missing_dep)
