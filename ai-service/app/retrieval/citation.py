"""引用校验：`无引用不出结论` 的执行者。

这条算法是 PRD `G1.3` 的落点，也是这个系统与"通用问答套壳"之间最实在的差别：
模型可以生成一句听起来完全正确的税务结论，并且随手挂上一条**看起来很像**的法条
（条号格式正确、法域正确、语气正确），但它并不存在于库里。

校验做四件事，对应四种失败：

| 检查 | 失败判定 | 为什么需要 |
| --- | --- | --- |
| 引用是否在本次检索的候选集内 | `REJECT_OUT_OF_CANDIDATES` | 挡"凭空出现的法条"——只查库存在性不够，模型可以引用一条它自己刚编进上下文的内容 |
| 租户是否可见 | `REJECT_TENANT` | 挡跨租户串库 |
| `as_of` 时点上是否有效 | `STALE` | 挡"用新法条回答旧问题"（AC-1.4） |
| 引文是否为原文子串 | `REJECT_QUOTE_MISMATCH` | 挡"条号对但内容编"——最隐蔽的一种，条号可查所以看起来最可信 |

处置策略（对应 PRD §6.6 的"删除结论句"而不是"整篇重写"）：
整篇重写会让未被污染的结论也发生变化，用户无法判断哪句变了；
逐句剥离则保证"留下的都是校验过的"。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum

_WS_RE = re.compile(r"\s+")


def _normalize(text: str) -> str:
    """压缩空白后再比较。

    法规原文在 PDF/HTML 里常带换行与多空格，而模型复述时是单行。
    直接比较会让正确的引文被判为不匹配——这是误杀，比漏放更伤。
    """
    return _WS_RE.sub("", text)


@dataclass(frozen=True)
class CandidateArticle:
    """本次检索命中的法条。字段对应 `kb.article`。"""

    article_id: str
    article_no: str
    content: str
    tenant_scope: str
    effective_from: str
    effective_to: str | None = None
    statute_title: str = ""
    hierarchy_path: tuple[str, ...] = field(default_factory=tuple)

    def covers(self, as_of: str) -> bool:
        """该条在该时点是否生效。日期用 ISO 字符串，可直接字典序比较。"""
        if as_of < self.effective_from:
            return False
        return not (self.effective_to is not None and as_of >= self.effective_to)


class CitationStatus(StrEnum):
    OK = "OK"
    REJECT_OUT_OF_CANDIDATES = "REJECT_OUT_OF_CANDIDATES"
    REJECT_TENANT = "REJECT_TENANT"
    REJECT_QUOTE_MISMATCH = "REJECT_QUOTE_MISMATCH"
    STALE = "STALE"


@dataclass(frozen=True)
class Citation:
    article_id: str
    quoted_text: str = ""


@dataclass(frozen=True)
class Claim:
    """一条结论句及其引用。"""

    text: str
    citations: tuple[Citation, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class CitationCheck:
    citation: Citation
    status: CitationStatus
    article: CandidateArticle | None = None
    detail: str = ""


@dataclass(frozen=True)
class ClaimVerdict:
    claim: Claim
    kept: bool
    checks: tuple[CitationCheck, ...]
    #: 保留但有失效引用时，指向上位法条的提示
    stale_note: str | None = None

    @property
    def valid_citations(self) -> tuple[CitationCheck, ...]:
        return tuple(c for c in self.checks if c.status is CitationStatus.OK)


@dataclass(frozen=True)
class CitationReport:
    verdicts: tuple[ClaimVerdict, ...]

    @property
    def kept(self) -> tuple[ClaimVerdict, ...]:
        return tuple(v for v in self.verdicts if v.kept)

    @property
    def stripped(self) -> tuple[ClaimVerdict, ...]:
        return tuple(v for v in self.verdicts if not v.kept)

    @property
    def stale(self) -> tuple[ClaimVerdict, ...]:
        return tuple(
            v for v in self.verdicts if any(c.status is CitationStatus.STALE for c in v.checks)
        )

    @property
    def must_refuse(self) -> bool:
        """**全部结论都被剥离**时，不输出任何结论。

        这条约束的意义在于：一个"检索到法条但没有一条能支撑结论"的回答，
        最诚实的形态是承认依据不足，而不是换一套说法把话说圆。
        """
        return bool(self.verdicts) and not self.kept

    def summary(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for verdict in self.verdicts:
            for check in verdict.checks:
                counts[check.status.value] = counts.get(check.status.value, 0) + 1
        counts["claims_kept"] = len(self.kept)
        counts["claims_stripped"] = len(self.stripped)
        return counts


def _check_one(
    citation: Citation,
    candidates_by_id: dict[str, CandidateArticle],
    tenant_id: str,
    as_of: str,
) -> CitationCheck:
    article = candidates_by_id.get(citation.article_id)
    if article is None:
        return CitationCheck(
            citation,
            CitationStatus.REJECT_OUT_OF_CANDIDATES,
            detail="引用的法条不在本次检索候选集内",
        )
    if article.tenant_scope not in ("PLATFORM", tenant_id):
        return CitationCheck(
            citation,
            CitationStatus.REJECT_TENANT,
            detail="引用的法条不属于当前租户可见范围",
        )
    if not article.covers(as_of):
        return CitationCheck(
            citation,
            CitationStatus.STALE,
            article,
            detail=f"该条在 {as_of} 时点不生效（生效区间 {article.effective_from} ~ "
            f"{article.effective_to or '至今'}）",
        )
    if citation.quoted_text and _normalize(citation.quoted_text) not in _normalize(article.content):
        return CitationCheck(
            citation,
            CitationStatus.REJECT_QUOTE_MISMATCH,
            article,
            detail="引文不是该法条原文的子串",
        )
    return CitationCheck(citation, CitationStatus.OK, article)


def verify_claims(
    claims: list[Claim],
    candidates: list[CandidateArticle],
    *,
    tenant_id: str,
    as_of: str,
) -> CitationReport:
    """校验一组结论句。

    Args:
        claims: 待校验的结论句。**没有引用**的结论一律剥离。
        candidates: 本次检索的候选法条（校验的封闭集合）。
        tenant_id: 当前租户，用于可见性判定。
        as_of: 回答所依据的时点（`AC-1.4` 的"按版本回答"）。
    """
    candidates_by_id = {article.article_id: article for article in candidates}
    verdicts: list[ClaimVerdict] = []

    for claim in claims:
        if not claim.citations:
            verdicts.append(
                ClaimVerdict(claim, kept=False, checks=(), stale_note="结论未附任何引用，已剥离")
            )
            continue

        checks = tuple(
            _check_one(citation, candidates_by_id, tenant_id, as_of) for citation in claim.citations
        )
        # `STALE`（引用真实存在、只是在该时点不生效）算"有支撑"，保留但标注；
        # `REJECT_*` 一律剥离。这两类的处置刻意不同：说错版本仍然是一句有依据的话，
        # 而引用一条不存在的法条是一句没有依据的话。
        has_support = any(
            check.status in (CitationStatus.OK, CitationStatus.STALE) for check in checks
        )
        stale_checks = [c for c in checks if c.status is CitationStatus.STALE]
        note = None
        if stale_checks:
            note = "；".join(check.detail for check in stale_checks)
        verdicts.append(ClaimVerdict(claim, kept=has_support, checks=checks, stale_note=note))

    return CitationReport(tuple(verdicts))
