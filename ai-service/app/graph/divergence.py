"""差异矩阵：把"各法域对同一行为的态度"拼成一张**不缺项**的表（AC-3.1）。

与 `tax_calc` 是同一件事的两个面：那里守的是"数字不由模型算"，
这里守的是"**检索不到就写检索不到，不得推断**"。

**先说一个容易混的区分。** 术语表里这个格值叫「无规定」，而界面上必须显示
「未检索到明确规定」。两者不能互换，因为它们在主张不同的事：

- 「无规定」是对**法律**的判断——"这个法域没有相关规则"。系统没有能力下这个判断：
  它只能知道自己检索了什么，不能知道法律里没有什么。漏检一条反避税条款
  与"真的没有规定"在系统内部长得一模一样。
- 「未检索到明确规定」是对**检索过程**的如实陈述。它同样有用，而且不会撒谎。

因此枚举值取 `NOT_FOUND`，展示文案固定为「未检索到明确规定」，
本模块不提供任何把它显示成「无规定」的出口。

**第二条纪律**：格值只能由**本次检索到的候选条款**支撑。
一个"许可/禁止/附条件"若拿不出候选集里的引用，就会被降级为 `NOT_FOUND`
并记下降级原因——不是丢弃该格（AC-3.1 要求 4 个法域全部有值），
而是让它退回一个诚实的取值。
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any

from app.retrieval.citation import CandidateArticle

logger = logging.getLogger(__name__)


class ObligationState(StrEnum):
    """义务状态（PRD §3.6.3 的 `ObligationState`）。"""

    PERMITTED = "PERMITTED"
    PROHIBITED = "PROHIBITED"
    CONDITIONAL = "CONDITIONAL"
    NOT_FOUND = "NOT_FOUND"


#: 展示文案。`NOT_FOUND` 的文案是**唯一**的，且刻意不用「无规定」——
#: 理由见模块 docstring。
STATE_LABELS: dict[ObligationState, str] = {
    ObligationState.PERMITTED: "许可",
    ObligationState.PROHIBITED: "禁止",
    ObligationState.CONDITIONAL: "附条件",
    ObligationState.NOT_FOUND: "未检索到明确规定",
}


class StabilityLevel(StrEnum):
    """稳定性评级（详细设计 §7.6）。

    展示文案取 PRD US-C2 的措辞：稳定 / 观察中 / 有收紧趋势。
    """

    STABLE = "STABLE"
    CHANGING = "CHANGING"
    UNCERTAIN = "UNCERTAIN"


STABILITY_LABELS: dict[StabilityLevel, str] = {
    StabilityLevel.STABLE: "稳定",
    StabilityLevel.CHANGING: "有收紧趋势",
    StabilityLevel.UNCERTAIN: "观察中",
}


@dataclass(frozen=True)
class CellProposal:
    """对某一格的**建议**。通常来自抽取或模型，因此必须被校验后才能采用。"""

    behavior: str
    jurisdiction_code: str
    state: ObligationState
    #: 支撑该状态的候选法条 id，必须属于本次检索的候选集
    citation_ids: tuple[str, ...] = ()
    #: 附条件的条件内容等补充说明（由调用方给出，不由模型自由发挥最后拼进结论）
    condition_note: str = ""


@dataclass(frozen=True)
class StabilityProposal:
    """对某一行稳定性的建议及其依据。**没有依据就评不出稳定。**"""

    level: StabilityLevel
    evidence: tuple[str, ...] = ()


@dataclass(frozen=True)
class MatrixCell:
    behavior: str
    jurisdiction_code: str
    state: ObligationState
    #: 支撑该格的法条（只能在候选集内，因此可以直接交给引用校验）
    citations: tuple[CandidateArticle, ...] = ()
    #: 依据条款的展示文本。**由本模块从法条字段拼出**，不采用模型给的自由文本——
    #: 否则"依据"这一栏本身就成了可以编造的地方。
    basis: str = ""
    condition_note: str = ""
    #: 被降级前的取值，便于在 trace 里看到"模型原本想说什么"
    downgraded_from: ObligationState | None = None
    reason: str = ""

    @property
    def label(self) -> str:
        return STATE_LABELS[self.state]

    @property
    def has_basis(self) -> bool:
        """AC-3.1：每格要么附依据条款，要么明确标注无依据。"""
        return bool(self.citations)


@dataclass(frozen=True)
class MatrixRow:
    behavior: str
    jurisdiction_code: str
    cell: MatrixCell
    stability: StabilityLevel
    stability_evidence: tuple[str, ...] = ()
    stability_note: str = ""

    @property
    def stability_label(self) -> str:
        return STABILITY_LABELS[self.stability]


@dataclass(frozen=True)
class DivergenceMatrix:
    behaviors: tuple[str, ...]
    jurisdictions: tuple[str, ...]
    rows: tuple[MatrixRow, ...]
    #: 降级与补齐的说明。**调用方应当把它透出到响应里**——
    #: 一张"有 4 行但其中 3 行没找到依据"的矩阵，与一张 4 行都有依据的矩阵，
    #: 对使用者的意义完全不同。
    notes: tuple[str, ...] = field(default_factory=tuple)

    def get(self, behavior: str, jurisdiction_code: str) -> MatrixRow | None:
        for row in self.rows:
            if row.behavior == behavior and row.jurisdiction_code == jurisdiction_code:
                return row
        return None

    def is_complete(self) -> bool:
        """是否每个 (行为 × 法域) 组合都有行。AC-3.1 的"不缺项"就是这一条。"""
        expected = {(b, j) for b in self.behaviors for j in self.jurisdictions}
        return {(row.behavior, row.jurisdiction_code) for row in self.rows} == expected

    def without_basis(self) -> tuple[MatrixRow, ...]:
        """没有依据的行。界面要能一眼看出哪些格是"不知道"。"""
        return tuple(row for row in self.rows if not row.cell.has_basis)

    def as_dicts(self) -> list[dict[str, str]]:
        """给接口/前端用的形态。列名与 PRD §3.6.2 的表一致。"""
        return [
            {
                "行为": row.behavior,
                "法域": row.jurisdiction_code,
                "状态": row.cell.label,
                "依据条款": row.cell.basis or "无依据",
                "稳定性": row.stability_label,
                "说明": row.cell.condition_note or row.cell.reason or row.stability_note,
            }
            for row in self.rows
        ]


def build_matrix(
    *,
    behaviors: Sequence[str],
    jurisdictions: Sequence[str],
    proposals: Iterable[CellProposal] = (),
    candidates: Sequence[CandidateArticle] = (),
    stability: Mapping[tuple[str, str], StabilityProposal] | None = None,
) -> DivergenceMatrix:
    """拼矩阵。

    Args:
        behaviors: 矩阵要覆盖的行为（行标题之一，通常来自 `Transaction`）。
        jurisdictions: 要对照的法域。
        proposals: 对个别格的建议。**允许缺项**——缺的那一格会以
            `未检索到明确规定` 补齐，而不是从矩阵里消失。
        candidates: 本次检索的候选法条，即"哪些引用是可能的"。
        stability: (行为, 法域) → 稳定性建议。

    Returns:
        `DivergenceMatrix`。**保证 `is_complete()` 为真**：矩阵从不出缺项。
    """
    by_id = {article.article_id: article for article in candidates}
    stability = stability or {}

    # 同一格出现多个建议时取第一个并记一条说明。合并多格（例如"既许可又禁止"）
    # 是个真问题，但把它做成"取并集"会掩盖矛盾；留给人工看更诚实。
    proposal_map: dict[tuple[str, str], CellProposal] = {}
    for proposal in proposals:
        key = (proposal.behavior, proposal.jurisdiction_code)
        if key in proposal_map:
            logger.warning("同一格出现多个建议，取首个：%s", key)
            continue
        proposal_map[key] = proposal

    notes: list[str] = []
    rows: list[MatrixRow] = []

    for behavior in behaviors:
        for jurisdiction in jurisdictions:
            cell = _build_cell(
                behavior=behavior,
                jurisdiction_code=jurisdiction,
                proposal=proposal_map.get((behavior, jurisdiction)),
                by_id=by_id,
                notes=notes,
            )
            assessment = stability.get((behavior, jurisdiction))
            level, evidence, note = _assess_stability(cell, assessment, notes)
            rows.append(
                MatrixRow(
                    behavior=behavior,
                    jurisdiction_code=jurisdiction,
                    cell=cell,
                    stability=level,
                    stability_evidence=evidence,
                    stability_note=note,
                )
            )

    return DivergenceMatrix(
        behaviors=tuple(behaviors),
        jurisdictions=tuple(jurisdictions),
        rows=tuple(rows),
        notes=tuple(notes),
    )


def _build_cell(
    *,
    behavior: str,
    jurisdiction_code: str,
    proposal: CellProposal | None,
    by_id: dict[str, CandidateArticle],
    notes: list[str],
) -> MatrixCell:
    """构造一格。这里的判断顺序就是本模块的全部政策。"""
    if proposal is None:
        return MatrixCell(
            behavior=behavior,
            jurisdiction_code=jurisdiction_code,
            state=ObligationState.NOT_FOUND,
            reason="本次检索没有就该行为在该法域给出明确条款",
        )

    if proposal.state is ObligationState.NOT_FOUND:
        return MatrixCell(
            behavior=behavior,
            jurisdiction_code=jurisdiction_code,
            state=ObligationState.NOT_FOUND,
            reason=proposal.condition_note or "检索未命中明确条款",
        )

    accepted = tuple(
        by_id[article_id] for article_id in proposal.citation_ids if article_id in by_id
    )

    if not accepted:
        # 关键分支：主张了"许可/禁止/附条件"却拿不出候选集里的引用。
        # 这类主张**不能留下**——它正是"推断"的形状：结论有，依据没有。
        notes.append(
            f"{jurisdiction_code} 的「{behavior}」被建议为 {STATE_LABELS[proposal.state]}，"
            "但未能给出本次检索候选集内的依据条款，已降级为"
            f"「{STATE_LABELS[ObligationState.NOT_FOUND]}」"
        )
        return MatrixCell(
            behavior=behavior,
            jurisdiction_code=jurisdiction_code,
            state=ObligationState.NOT_FOUND,
            downgraded_from=proposal.state,
            reason="结论缺少候选集内的依据条款，按不得推断处理",
        )

    if len(accepted) < len(proposal.citation_ids):
        notes.append(
            f"{jurisdiction_code} 的「{behavior}」有 "
            f"{len(proposal.citation_ids) - len(accepted)} 条依据不在本次检索候选集内，已忽略"
        )

    return MatrixCell(
        behavior=behavior,
        jurisdiction_code=jurisdiction_code,
        state=proposal.state,
        citations=accepted,
        basis=_render_basis(accepted),
        condition_note=proposal.condition_note,
    )


def _render_basis(citations: Sequence[CandidateArticle]) -> str:
    """把法条渲染成「《法规名》第X条」形式。

    **由本模块拼，不采用模型给的文本。** 若"依据"这一栏由模型写，
    那么整个矩阵里唯一看起来最可信的部分（依据）就成了最容易编造的地方——
    而且它长得和真的一模一样。
    """
    parts = []
    for article in citations[:3]:
        title = article.statute_title or article.article_no
        parts.append(f"{title} {article.article_no}".strip())
    suffix = f" 等 {len(citations)} 条" if len(citations) > 3 else ""
    return "；".join(parts) + suffix


def _assess_stability(
    cell: MatrixCell,
    assessment: StabilityProposal | None,
    notes: list[str],
) -> tuple[StabilityLevel, tuple[str, ...], str]:
    """定稳定性。**只有「稳定」需要依据，其余评级不需要。**

    AC-3.2 要求评级带依据来源。这里刻意做成不对称，理由是两类评级
    在没有依据时**危害方向相反**：

    - 「稳定」是让人放心的结论。没有依据的「稳定」会诱使使用者把一条
      随时可能被修补的规则当成长期结构，而稳定性恰恰是利用监管差异的前提
      （详细设计 §7.6）。放心的结论必须有证据。
    - 「有收紧趋势」是警示。把一条没有依据的警示降级成「观察中」，
      是在**删掉一条提醒**——错的方向是把人推向风险，比留一句可能多余的
      提醒糟得多。

    所以只对「稳定」做降级。这不是疏漏，是这个模块里唯一一处
    "对结论严格、对警示宽容"的地方。
    """
    if cell.state is ObligationState.NOT_FOUND:
        # 连状态都没查到，谈不上这条规则稳不稳定
        return StabilityLevel.UNCERTAIN, (), "未检索到明确规定，无法评估稳定性"

    if assessment is None:
        return StabilityLevel.UNCERTAIN, (), "没有稳定性评估依据"

    if not assessment.evidence:
        if assessment.level is StabilityLevel.STABLE:
            notes.append(
                f"{cell.jurisdiction_code} 的「{cell.behavior}」被评估为稳定，"
                "但未给出依据，已退回「观察中」"
            )
            return StabilityLevel.UNCERTAIN, (), "稳定性评级缺少依据"
        return assessment.level, (), ""

    return assessment.level, tuple(assessment.evidence), ""


def constrain_citations(
    proposal: CellProposal, allowed_ids: Mapping[str, CandidateArticle]
) -> tuple[CellProposal, int]:
    """把建议的引用限制在**该法域自己的**候选集内，返回（新建议，被剔除条数）。

    `_build_cell` 只检查引用落没落在"本次检索的全部候选"里。而差异图是
    **逐法域检索**的：爱尔兰的条款足以支撑"荷兰允许"这一格通过校验，
    依据栏还会规规矩矩地写着《爱尔兰税法》——**它看起来完全正常**。

    这条约束只有分层调用方施加得了（它手里才有按法域分开的候选集），
    因此放在这里、由差异图的节点调用。
    """
    kept = tuple(cid for cid in proposal.citation_ids if cid in allowed_ids)
    dropped = len(proposal.citation_ids) - len(kept)
    if not dropped:
        return proposal, 0
    return replace(proposal, citation_ids=kept), dropped


def proposal_to_dict(proposal: CellProposal) -> dict[str, Any]:
    """落成可进检查点的 dict。检查点走 msgpack，dataclass 不在白名单里。"""
    return {
        "behavior": proposal.behavior,
        "jurisdiction_code": proposal.jurisdiction_code,
        "state": str(proposal.state),
        "citation_ids": list(proposal.citation_ids),
        "condition_note": proposal.condition_note,
    }


def proposal_from_dict(payload: Mapping[str, Any]) -> CellProposal | None:
    """从检查点还原建议。**形状不对就返回 `None` 并记日志，不抛异常。**

    这个函数只在**恢复**路径上被调用，而那条路径上抛异常意味着整个会话卡死：
    用户已经填完追问表单、点了提交，却拿到一个 500。宁可少一格
    （`build_matrix` 会把它退成「未检索到明确规定」），也不让恢复失败。
    """
    try:
        state = ObligationState(str(payload.get("state")))
    except ValueError:
        logger.warning("格值建议的义务状态无法识别，已忽略该建议：%s", payload.get("state"))
        return None
    return CellProposal(
        behavior=str(payload.get("behavior", "")),
        jurisdiction_code=str(payload.get("jurisdiction_code", "")),
        state=state,
        citation_ids=tuple(str(item) for item in payload.get("citation_ids") or []),
        condition_note=str(payload.get("condition_note", "")),
    )


def stability_from_dict(payload: Mapping[str, Any]) -> StabilityProposal | None:
    """校验一条稳定性建议。**评级无法识别时返回 `None`。**

    `None` 不是"没有稳定性"——`_assess_stability` 会把它变成「观察中」并注明
    "没有稳定性评估依据"。**绝不默认成「稳定」**：默认成稳定等于让一次解析失败
    静默地给出一个让人放心的结论（理由见 `_assess_stability`）。
    """
    try:
        level = StabilityLevel(str(payload.get("level")))
    except ValueError:
        logger.warning("稳定性评级无法识别，已忽略：%s", payload.get("level"))
        return None
    return StabilityProposal(
        level=level, evidence=tuple(str(item) for item in payload.get("evidence") or [])
    )


def matrix_payload(matrix: DivergenceMatrix, *, as_of: str) -> dict[str, Any]:
    """**机读形态**的结构化结出（含引用对象），供 `api` 映射前端 DTO。

    与 `as_dicts()` 的区别是刻意的：`as_dicts()` 是 PRD §3.6.2 的表格视图，
    列名是中文、依据是一行渲染好的文本，给人看；而本函数保留 `article_id`、
    法域态枚举与失效标记，给程序用。前端要把依据条款做成可跳转的引用，
    只拿到"《荷兰所得税法》第 3.1 条"这一行字是不够的。

    Args:
        matrix: 已拼好的矩阵。
        as_of: 结论时点。用来标出"该引用在这个时点已经失效"。

    Returns:
        可 JSON 序列化的 dict。`citations[].stale` 只是**标注**，
        **不改变格值**：把"依据已失效"直接改写成「未检索到明确规定」会丢失
        "曾经检索到过一条、但它过期了"这个信息，而两者要修的东西完全不同
        （前者补语料，后者更新版本）。改判与否留给上层显式决策。
    """
    rows: list[dict[str, Any]] = []
    for row in matrix.rows:
        citations = [
            {
                "article_id": article.article_id,
                "article_no": article.article_no,
                "statute_title": article.statute_title,
                # `covers` 就是引用校验用的那条时点口径，这里不另写一套
                "stale": not article.covers(as_of) if as_of else False,
            }
            for article in row.cell.citations
        ]
        rows.append(
            {
                "behavior": row.behavior,
                "jurisdiction_code": row.jurisdiction_code,
                "state": str(row.cell.state),
                "state_label": row.cell.label,
                "basis": row.cell.basis,
                "condition_note": row.cell.condition_note,
                "reason": row.cell.reason,
                "downgraded_from": (
                    str(row.cell.downgraded_from) if row.cell.downgraded_from else None
                ),
                "stability": str(row.stability),
                "stability_label": row.stability_label,
                "stability_note": row.stability_note,
                "citations": citations,
            }
        )

    return {
        "behaviors": list(matrix.behaviors),
        "jurisdictions": list(matrix.jurisdictions),
        "is_complete": matrix.is_complete(),
        "without_basis_count": len(matrix.without_basis()),
        "notes": list(matrix.notes),
        "rows": rows,
    }
