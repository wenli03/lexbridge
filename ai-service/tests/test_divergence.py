"""差异矩阵的测试。

两个政策各有专门的用例组：

- `TestNoInference` —— 主张了"许可/禁止/附条件"却拿不出依据时，必须降级。
  这是 AC-3.1 的核心。
- `TestStability` —— 稳定性评级与依据的不对称要求（AC-3.2）。
"""

from __future__ import annotations

import pytest

from app.graph.divergence import (
    STATE_LABELS,
    CellProposal,
    ObligationState,
    StabilityLevel,
    StabilityProposal,
    build_matrix,
)
from app.retrieval.citation import CandidateArticle

SG = "SG"
NL = "NL"
IE = "IE"
BEHAVIOR = "跨境特许权使用费支付"


def article(
    article_id: str, *, title: str = "《所得税法》", no: str = "第 13(1)(9) 条"
) -> CandidateArticle:
    return CandidateArticle(
        article_id=article_id,
        article_no=no,
        content="特许权使用费在符合受益所有人条件时可按协定税率征税。",
        tenant_scope="PLATFORM",
        effective_from="2019-01-01",
        statute_title=title,
    )


CANDIDATES = [
    article("a-sg", title="新加坡《所得税法》", no="第 13(1)(9) 条"),
    article("a-nl", title="荷兰《企业所得税法》", no="第 8b 条"),
]


class TestCompleteness:
    """AC-3.1：4 个法域全部有值，不得缺项。"""

    def test_every_combination_gets_a_row_even_without_proposals(self) -> None:
        matrix = build_matrix(
            behaviors=[BEHAVIOR],
            jurisdictions=[SG, NL, IE],
            candidates=CANDIDATES,
        )
        assert matrix.is_complete()
        assert len(matrix.rows) == 3
        assert all(row.cell.state is ObligationState.NOT_FOUND for row in matrix.rows)

    def test_multiple_behaviors_times_jurisdictions(self) -> None:
        matrix = build_matrix(
            behaviors=["跨境特许权使用费支付", "跨境股息分配"],
            jurisdictions=[SG, NL],
            candidates=CANDIDATES,
        )
        assert matrix.is_complete()
        assert len(matrix.rows) == 4

    def test_partial_proposals_do_not_shrink_the_matrix(self) -> None:
        """只给一个法域建议时，其余法域照旧成行。"""
        matrix = build_matrix(
            behaviors=[BEHAVIOR],
            jurisdictions=[SG, NL, IE],
            proposals=[
                CellProposal(BEHAVIOR, SG, ObligationState.PERMITTED, ("a-sg",)),
            ],
            candidates=CANDIDATES,
        )
        assert matrix.is_complete()
        assert matrix.get(BEHAVIOR, SG).cell.state is ObligationState.PERMITTED
        assert matrix.get(BEHAVIOR, IE).cell.state is ObligationState.NOT_FOUND

    def test_empty_input_gives_empty_matrix(self) -> None:
        matrix = build_matrix(behaviors=[], jurisdictions=[], candidates=[])
        assert matrix.rows == ()
        assert matrix.is_complete()


class TestNoInference:
    """AC-3.1：检索不到就写检索不到，不得推断。"""

    def test_claim_without_any_citation_is_downgraded(self) -> None:
        """结论有、依据没有 —— 这正是"推断"的形状。"""
        matrix = build_matrix(
            behaviors=[BEHAVIOR],
            jurisdictions=[SG],
            proposals=[CellProposal(BEHAVIOR, SG, ObligationState.PERMITTED)],
            candidates=CANDIDATES,
        )
        cell = matrix.get(BEHAVIOR, SG).cell
        assert cell.state is ObligationState.NOT_FOUND
        assert cell.downgraded_from is ObligationState.PERMITTED
        assert not cell.has_basis
        assert any("降级" in note for note in matrix.notes)

    def test_citation_outside_the_candidate_set_is_not_accepted(self) -> None:
        """引一条本次检索没召回的条文，等同于没有依据。

        只查"这条法条在库里存在吗"是不够的——模型可以引用一条它自己
        写进上下文的、恰好真实存在的条文，而那个条文与本次问题是无关的。
        """
        matrix = build_matrix(
            behaviors=[BEHAVIOR],
            jurisdictions=[SG],
            proposals=[
                CellProposal(BEHAVIOR, SG, ObligationState.PROHIBITED, ("a-does-not-exist",)),
            ],
            candidates=CANDIDATES,
        )
        cell = matrix.get(BEHAVIOR, SG).cell
        assert cell.state is ObligationState.NOT_FOUND
        assert cell.downgraded_from is ObligationState.PROHIBITED

    def test_partially_valid_citations_keep_the_cell_with_a_note(self) -> None:
        matrix = build_matrix(
            behaviors=[BEHAVIOR],
            jurisdictions=[SG],
            proposals=[
                CellProposal(
                    BEHAVIOR, SG, ObligationState.CONDITIONAL, ("a-sg", "a-does-not-exist")
                ),
            ],
            candidates=CANDIDATES,
        )
        cell = matrix.get(BEHAVIOR, SG).cell
        assert cell.state is ObligationState.CONDITIONAL
        assert len(cell.citations) == 1
        assert any("不在本次检索候选集内" in note for note in matrix.notes)

    def test_basis_text_is_built_from_article_fields_not_from_a_model_string(self) -> None:
        """依据栏由本模块拼出。若让模型写，最像可信的那一栏就成了最易编造的地方。"""
        matrix = build_matrix(
            behaviors=[BEHAVIOR],
            jurisdictions=[SG],
            proposals=[
                CellProposal(BEHAVIOR, SG, ObligationState.PERMITTED, ("a-sg",)),
            ],
            candidates=CANDIDATES,
        )
        cell = matrix.get(BEHAVIOR, SG).cell
        assert cell.basis == "新加坡《所得税法》 第 13(1)(9) 条"

    def test_duplicate_proposals_keep_the_first_and_record_a_note(self, caplog) -> None:
        """同一格两个互相矛盾的建议不合并成并集——那会掩盖矛盾。"""
        matrix = build_matrix(
            behaviors=[BEHAVIOR],
            jurisdictions=[SG],
            proposals=[
                CellProposal(BEHAVIOR, SG, ObligationState.PERMITTED, ("a-sg",)),
                CellProposal(BEHAVIOR, SG, ObligationState.PROHIBITED, ("a-nl",)),
            ],
            candidates=CANDIDATES,
        )
        assert matrix.get(BEHAVIOR, SG).cell.state is ObligationState.PERMITTED
        assert len(matrix.rows) == 1


class TestLabels:
    def test_not_found_never_displays_as_no_provision(self) -> None:
        """「未检索到明确规定」不能显示成「无规定」。

        后者主张的是"法律里没有这条规则"——系统只能知道自己检索了什么，
        不知道法律里没有什么。漏检与真的没有，在系统内部长得一模一样。
        """
        assert STATE_LABELS[ObligationState.NOT_FOUND] == "未检索到明确规定"
        assert "无规定" not in STATE_LABELS[ObligationState.NOT_FOUND]

        matrix = build_matrix(behaviors=[BEHAVIOR], jurisdictions=[SG])
        assert matrix.as_dicts()[0]["状态"] == "未检索到明确规定"

    def test_table_columns_match_the_prd(self) -> None:
        matrix = build_matrix(behaviors=[BEHAVIOR], jurisdictions=[SG])
        assert list(matrix.as_dicts()[0]) == ["行为", "法域", "状态", "依据条款", "稳定性", "说明"]

    def test_basis_column_says_no_basis_when_absent(self) -> None:
        """AC-3.1：每格附依据条款**或明确标注无依据**。空着不算标注。"""
        matrix = build_matrix(behaviors=[BEHAVIOR], jurisdictions=[SG])
        assert matrix.as_dicts()[0]["依据条款"] == "无依据"

    def test_without_basis_lists_the_unknown_cells(self) -> None:
        matrix = build_matrix(
            behaviors=[BEHAVIOR],
            jurisdictions=[SG, NL],
            proposals=[
                CellProposal(BEHAVIOR, SG, ObligationState.PERMITTED, ("a-sg",)),
            ],
            candidates=CANDIDATES,
        )
        unknown = matrix.without_basis()
        assert len(unknown) == 1
        assert unknown[0].jurisdiction_code == NL


class TestStability:
    """AC-3.2：稳定性评级必须带依据。"""

    def test_rating_without_assessment_is_uncertain(self) -> None:
        matrix = build_matrix(
            behaviors=[BEHAVIOR],
            jurisdictions=[SG],
            proposals=[CellProposal(BEHAVIOR, SG, ObligationState.PERMITTED, ("a-sg",))],
            candidates=CANDIDATES,
        )
        row = matrix.get(BEHAVIOR, SG)
        assert row.stability is StabilityLevel.UNCERTAIN
        assert row.stability_label == "观察中"

    def test_stable_without_evidence_is_downgraded(self) -> None:
        """没有依据的「稳定」是最危险的一格——它让人把可能被修补的规则当长期结构。"""
        matrix = build_matrix(
            behaviors=[BEHAVIOR],
            jurisdictions=[SG],
            proposals=[CellProposal(BEHAVIOR, SG, ObligationState.PERMITTED, ("a-sg",))],
            candidates=CANDIDATES,
            stability={(BEHAVIOR, SG): StabilityProposal(StabilityLevel.STABLE)},
        )
        row = matrix.get(BEHAVIOR, SG)
        assert row.stability is StabilityLevel.UNCERTAIN
        assert any("退回「观察中」" in note for note in matrix.notes)

    def test_stable_with_evidence_is_kept(self) -> None:
        matrix = build_matrix(
            behaviors=[BEHAVIOR],
            jurisdictions=[SG],
            proposals=[CellProposal(BEHAVIOR, SG, ObligationState.PERMITTED, ("a-sg",))],
            candidates=CANDIDATES,
            stability={
                (BEHAVIOR, SG): StabilityProposal(StabilityLevel.STABLE, evidence=("近五年无修订",))
            },
        )
        row = matrix.get(BEHAVIOR, SG)
        assert row.stability is StabilityLevel.STABLE
        assert row.stability_label == "稳定"
        assert row.stability_evidence == ("近五年无修订",)

    def test_warning_without_evidence_is_kept(self) -> None:
        """刻意的不对称：警示类评级缺依据时**不降级**。

        把一条没有依据的警示降级成「观察中」，等于删掉一条提醒；
        错的方向是把人推向前述风险，比留一句可能多余的提醒糟得多。
        """
        matrix = build_matrix(
            behaviors=[BEHAVIOR],
            jurisdictions=[SG],
            proposals=[CellProposal(BEHAVIOR, SG, ObligationState.PERMITTED, ("a-sg",))],
            candidates=CANDIDATES,
            stability={(BEHAVIOR, SG): StabilityProposal(StabilityLevel.CHANGING)},
        )
        assert matrix.get(BEHAVIOR, SG).stability is StabilityLevel.CHANGING

    def test_not_found_cell_cannot_be_rated_stable(self) -> None:
        """连状态都没查到，谈不上这条规则稳不稳定。"""
        matrix = build_matrix(
            behaviors=[BEHAVIOR],
            jurisdictions=[SG],
            stability={
                (BEHAVIOR, SG): StabilityProposal(StabilityLevel.STABLE, evidence=("某处依据",))
            },
        )
        row = matrix.get(BEHAVIOR, SG)
        assert row.stability is StabilityLevel.UNCERTAIN
        assert "无法评估稳定性" in row.stability_note

    @pytest.mark.parametrize(
        ("level", "label"),
        [
            (StabilityLevel.STABLE, "稳定"),
            (StabilityLevel.CHANGING, "有收紧趋势"),
            (StabilityLevel.UNCERTAIN, "观察中"),
        ],
    )
    def test_labels_match_the_prd_wording(self, level: StabilityLevel, label: str) -> None:
        matrix = build_matrix(
            behaviors=[BEHAVIOR],
            jurisdictions=[SG],
            proposals=[CellProposal(BEHAVIOR, SG, ObligationState.PERMITTED, ("a-sg",))],
            candidates=CANDIDATES,
            stability={(BEHAVIOR, SG): StabilityProposal(level, evidence=("依据",))},
        )
        assert matrix.get(BEHAVIOR, SG).stability_label == label
