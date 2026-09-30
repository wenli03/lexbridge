"""引用校验的测试。

四种失败各一个用例——它们的失败方式不同，修法也不同，
混在一起测会让人以为"引用校验坏了"而不是"哪一类坏了"。
"""

from __future__ import annotations

from app.retrieval.citation import (
    CandidateArticle,
    Citation,
    CitationStatus,
    Claim,
    verify_claims,
)

TENANT = "11111111-1111-1111-1111-111111111111"
OTHER_TENANT = "22222222-2222-2222-2222-222222222222"
AS_OF = "2026-09-30"

ARTICLE = CandidateArticle(
    article_id="art-001",
    article_no="第三条",
    content="企业所得税的税率为百分之二十五。非居民企业取得本法规定所得的，适用百分之二十的税率。",
    tenant_scope="PLATFORM",
    effective_from="2024-01-01",
    statute_title="示例税法",
)


def test_valid_citation_is_kept() -> None:
    report = verify_claims(
        [Claim("企业所得税税率为 25%。", (Citation("art-001", "税率为百分之二十五"),))],
        [ARTICLE],
        tenant_id=TENANT,
        as_of=AS_OF,
    )
    assert len(report.kept) == 1
    assert report.kept[0].checks[0].status is CitationStatus.OK
    assert report.kept[0].checks[0].article is ARTICLE


def test_claim_without_citation_is_stripped() -> None:
    """无引用不出结论——这是 G1.3，不是可选增强。"""
    report = verify_claims(
        [Claim("建议采用双层控股架构。", ())],
        [ARTICLE],
        tenant_id=TENANT,
        as_of=AS_OF,
    )
    assert not report.kept
    assert report.must_refuse


def test_citation_out_of_candidates_is_rejected() -> None:
    """凭空出现的法条：条号格式完全正确，但不在本次检索候选集内。"""
    report = verify_claims(
        [Claim("依据第八十八条，可以递延纳税。", (Citation("art-999", "可以递延纳税"),))],
        [ARTICLE],
        tenant_id=TENANT,
        as_of=AS_OF,
    )
    assert not report.kept
    assert report.stripped[0].checks[0].status is CitationStatus.REJECT_OUT_OF_CANDIDATES


def test_cross_tenant_citation_is_rejected() -> None:
    foreign = CandidateArticle(
        article_id="art-002",
        article_no="第五条",
        content="私有知识库中的内部口径。",
        tenant_scope=OTHER_TENANT,
        effective_from="2024-01-01",
    )
    report = verify_claims(
        [Claim("依据内部口径应这样处理。", (Citation("art-002", "内部口径"),))],
        [ARTICLE, foreign],
        tenant_id=TENANT,
        as_of=AS_OF,
    )
    assert not report.kept
    assert report.stripped[0].checks[0].status is CitationStatus.REJECT_TENANT


def test_quote_mismatch_is_rejected() -> None:
    """条号对、内容编——最隐蔽的一种，因为条号可查所以看起来最可信。"""
    report = verify_claims(
        [Claim("该法条规定税率为百分之十五。", (Citation("art-001", "税率为百分之十五"),))],
        [ARTICLE],
        tenant_id=TENANT,
        as_of=AS_OF,
    )
    assert not report.kept
    assert report.stripped[0].checks[0].status is CitationStatus.REJECT_QUOTE_MISMATCH


def test_stale_citation_is_kept_but_flagged() -> None:
    """说错版本仍然是一句有依据的话——保留并标注，而不是剥离。

    与 REJECT_* 的处置刻意不同：前者是"引用不成立"，后者是"引用成立但不适用"。
    """
    report = verify_claims(
        [Claim("2021 年的税率为百分之二十五。", (Citation("art-001", ""),))],
        [ARTICLE],
        tenant_id=TENANT,
        as_of="2021-06-01",
    )
    assert len(report.kept) == 1  # 保留
    assert len(report.stale) == 1  # 但被标为失效
    assert "不生效" in (report.kept[0].stale_note or "")


def test_partial_failure_strips_only_the_bad_claim() -> None:
    """部分失败只删坏句——整篇重写会让未被污染的结论也变化，用户无从判断。"""
    report = verify_claims(
        [
            Claim("企业所得税税率为 25%。", (Citation("art-001", "税率为百分之二十五"),)),
            Claim("可以完全免税。", (Citation("art-404", "完全免税"),)),
        ],
        [ARTICLE],
        tenant_id=TENANT,
        as_of=AS_OF,
    )
    assert len(report.kept) == 1
    assert len(report.stripped) == 1
    assert not report.must_refuse


def test_whitespace_differences_do_not_break_matching() -> None:
    """原文带换行、模型复述是单行——这是常态，不能判为不匹配。"""
    multiline = CandidateArticle(
        article_id="art-003",
        article_no="第九条",
        content="纳税人应当\n    自月份终了之日起十五日内\n申报缴纳。",
        tenant_scope="PLATFORM",
        effective_from="2024-01-01",
    )
    report = verify_claims(
        [Claim("应在十五日内申报。", (Citation("art-003", "自月份终了之日起十五日内申报缴纳"),))],
        [multiline],
        tenant_id=TENANT,
        as_of=AS_OF,
    )
    assert len(report.kept) == 1


def test_summary_counts_every_status() -> None:
    report = verify_claims(
        [
            Claim("A", (Citation("art-001", ""),)),
            Claim("B", ()),
        ],
        [ARTICLE],
        tenant_id=TENANT,
        as_of=AS_OF,
    )
    summary = report.summary()
    assert summary["OK"] == 1
    assert summary["claims_kept"] == 1
    assert summary["claims_stripped"] == 1
