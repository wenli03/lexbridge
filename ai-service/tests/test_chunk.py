"""条款切分的测试。

切分错误的代价是引用指向隔壁那条——看起来有效、实际错位，
所以这里的用例主要针对"容易切错"的地方，而不是happy path。
"""

from __future__ import annotations

from app.indexing.chunk import split_articles

CN_LAW = """中华人民共和国示例税法

目录
第一章 基本规定 ................ 1
第一条 立法目的 ................ 1
第二章 税率 ................ 3

第一章 基本规定
第一条 为了规范税收征收管理，保护纳税人的合法权益，制定本法。
第二条 本法所称纳税人，是指依照法律负有纳税义务的单位和个人，
包括企业、事业单位以及其他取得收入的组织。
第二节 税率
第三条 企业所得税的税率为百分之二十五。非居民企业取得本法规定所得的，
适用百分之二十的税率。
"""


class TestChinese:
    def test_articles_are_split(self) -> None:
        clauses = split_articles(CN_LAW)
        assert [c.article_no for c in clauses] == ["第一条", "第二条", "第三条"]

    def test_table_of_contents_is_dropped(self) -> None:
        """目录里的条目不能变成条——否则前几条的内容是"................ 1"。"""
        clauses = split_articles(CN_LAW)
        assert all("......" not in c.content for c in clauses)

    def test_hierarchy_accumulates_and_resets(self) -> None:
        clauses = split_articles(CN_LAW)
        path = {c.article_no: c.hierarchy_path for c in clauses}
        assert path["第一条"] == ["第一章 基本规定"]
        assert path["第二条"] == ["第一章 基本规定"]
        # 第二节 在第一章 之后出现，章节层级应叠加而不是替换
        assert path["第三条"] == ["第一章 基本规定", "第二节 税率"]

    def test_multiline_body_is_kept_together(self) -> None:
        """条内的换行不能把同一条切断。"""
        clauses = split_articles(CN_LAW)
        second = next(c for c in clauses if c.article_no == "第二条")
        assert "包括企业、事业单位" in second.content

    def test_inline_reference_does_not_split(self) -> None:
        """正文里"依照第 12 条规定"不是新的一条。"""
        text = "第一条 企业依照第 12 条规定计算应纳税所得额，并按月申报缴纳。"
        clauses = split_articles(text)
        assert len(clauses) == 1
        assert clauses[0].article_no == "第一条"


EN_ACT = """PART 1
Preliminary and General

Section 1. Short title and commencement.
This Act may be cited as the Sample Tax Act 2026 and comes into operation on 1 January 2026.

Section 2.—Interpretation.
In this Act "tax" means income tax charged under this Act.

12.—Rates of tax.
The rate of tax shall be 25 per cent.
"""


class TestEnglish:
    def test_section_heading_styles_are_both_supported(self) -> None:
        """「Section 1.」与「12.—」两种排版都要能切。"""
        clauses = split_articles(EN_ACT)
        assert [c.article_no for c in clauses] == ["Section 1", "Section 2", "Section 12"]

    def test_part_becomes_hierarchy(self) -> None:
        clauses = split_articles(EN_ACT)
        assert clauses[0].hierarchy_path == ["PART 1"]

    def test_dash_style_body_is_captured(self) -> None:
        clauses = split_articles(EN_ACT)
        last = next(c for c in clauses if c.article_no == "Section 12")
        assert "25 per cent" in last.content


class TestDegradation:
    def test_empty_input_returns_empty(self) -> None:
        assert split_articles("") == []
        assert split_articles("   \n  ") == []

    def test_unparseable_text_returns_empty_rather_than_one_big_chunk(self) -> None:
        """切不出来时返回空，**不要**退化成"整篇当成一条"。

        整篇一条会让引用粒度失效：所有结论都指向同一个"条"，
        校验无法发现错位，这比切不出条更糟。
        """
        assert split_articles("这是一段没有条号的说明文字，长约二十个字。") == []

    def test_short_clauses_are_dropped(self) -> None:
        text = "第一条 短\n第二条 这一条有足够长的正文内容用于通过最小长度过滤。"
        clauses = split_articles(text)
        assert [c.article_no for c in clauses] == ["第二条"]
