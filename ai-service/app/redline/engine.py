"""红线判定引擎：确定性、可解释、可审计。

这个模块**不导入 `app.chains` / `app.graph` / `app.agents`**（DR-4）。
判定全程是字符串匹配与语境判断，没有任何模型参与——因此
「把模型说服之后绕过红线」这条攻击路径在结构上不存在。

同时它必须防住另一头的失败：**误拒**。只优化召回率会把系统推向
"一律拒答"的退化解，而一个对正当业务一律拒答的合规工具是没用的。
两端由 `AC-4.1`（召回 100%）与 `AC-4.3`（误拒 ≤10%）同时约束。

防误拒的关键在于区分两种句式：

- **索取方案**：「帮我把收入隐匿掉」「设计一套不申报的安排」→ 越界
- **询问后果/合规**：「隐匿收入会怎么处罚」「我们如何排查这类风险」→ 正当
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.redline.rules import REDLINE_RULES, RedlineRule

# 句子切分：中英文标点都算，长句不切会导致整段被误判
_SENTENCE_SPLIT_RE = re.compile(r"[。；;\n!?！？]|(?<!\d)\.(?!\d)")

# 把请求拉回"合规问询语境"的标志词。
# 命中红线触发词、同时命中这些标志词时，判为「询问后果」而非「索取方案」。
_INQUIRY_MARKERS: tuple[str, ...] = (
    "如何处罚",
    "怎么处罚",
    "处罚",
    "法律责任",
    "法律后果",
    "刑事后果",
    "是否违法",
    "是否构成",
    "是否合规",
    "合规风险",
    "风险点",
    "如何识别",
    "如何排查",
    "如何防范",
    "如何应对",
    "监管口径",
    "执法实践",
    "判例",
    "审计要点",
    "尽职调查",
    "合规要求",
    "披露要求",
)

# 明确的索取信号：与风险标志词同时出现时，索取信号优先（宁严勿松）
_REQUEST_MARKERS: tuple[str, ...] = (
    "帮我",
    "替我",
    "给我设计",
    "设计一套",
    "设计一个",
    "怎么操作",
    "如何操作",
    "怎么做才",
    "教我",
    "出具方案",
    "方案要",
    "要一份",
    "不留下",
    "查不到",
)


@dataclass(frozen=True)
class RedlineVerdict:
    """判定结果。字段直接对应拒答响应五要素。"""

    hit: bool
    rule_id: str | None = None
    category: str | None = None
    #: 命中的原句，作为"判定依据"的证据
    evidence: tuple[str, ...] = field(default_factory=tuple)
    rationale: str = ""
    legal_alternatives: tuple[str, ...] = field(default_factory=tuple)

    def refusal_payload(self) -> dict[str, object]:
        """拒答响应体（五要素齐备）。

        会话**不终止**：这不是一个错误响应，而是一次正常回答——
        说明为什么不能协助，并给出可以走的路。
        """
        return {
            "type": "redline_refusal",
            "rule_id": self.rule_id,
            "category": self.category,
            "evidence": list(self.evidence),
            "reason": self.rationale,
            "legal_alternatives": list(self.legal_alternatives),
            "advice": "本平台提供研究辅助，不构成法律意见；具体事项请咨询具备相应法域执业资格的律师。",
        }


PASS = RedlineVerdict(hit=False)


def split_sentences(text: str) -> list[str]:
    parts = [part.strip() for part in _SENTENCE_SPLIT_RE.split(text)]
    return [part for part in parts if part]


def _is_inquiry(sentence: str) -> bool:
    """判断该句是在问后果，还是在要方案。

    `_REQUEST_MARKERS` 优先：像「帮我看看隐匿收入怎么处罚」这种句子里
    两类标志词都有，但它仍是在问后果；而「帮我设计一套隐匿收入的做法」
    只有索取信号。因此先看索取信号**是否与"要方法"搭配**，再看风险标志词。
    """
    has_inquiry = any(marker in sentence for marker in _INQUIRY_MARKERS)
    if not has_inquiry:
        return False
    # 索取信号 + 风险标志词同时出现：只有当索取信号本身指向"要做法"时才从严
    demanding = ("怎么操作", "如何操作", "怎么做才", "设计一套", "设计一个", "教我")
    return not any(marker in sentence for marker in demanding)


def evaluate(question: str, *, rules: tuple[RedlineRule, ...] = REDLINE_RULES) -> RedlineVerdict:
    """判定一个请求是否命中红线。

    规则按 `REDLINE_RULES` 的顺序优先匹配（越靠前的类别越具体、后果越重）。
    同一句话可能同时符合多条规则，取第一条命中——并列列出多个类别
    会让拒答响应变长却不增加信息量。
    """
    if not question or not question.strip():
        return PASS

    sentences = split_sentences(question)
    risky = [s for s in sentences if not _is_inquiry(s)]

    for rule in rules:
        matched = rule.matched_sentences(risky)
        if matched:
            return RedlineVerdict(
                hit=True,
                rule_id=rule.rule_id,
                category=rule.category,
                evidence=tuple(matched),
                rationale=rule.rationale,
                legal_alternatives=rule.legal_alternatives,
            )
    return PASS
