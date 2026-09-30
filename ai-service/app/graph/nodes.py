"""两张咨询图共用的节点。

**为什么单独一个模块。** 这里每个节点的输出都是**跨图的前端契约**：
追问中断载荷里的字段名、拒答文案里的五要素、引用校验的剥离口径，
`api` 与前端都按它们解析。把它们留在某一张图里、让另一张图去 import，
会让「税务图的内部实现」变成「差异图的依赖」；各抄一份则两处的字段名与口径
迟早不一致，而不一致的那一份会在界面上缺要素——用户不会看到报错，
只会看到一个少了「合法替代路径」的拒答。

它们都不需要注入（不碰模型、不碰数据库、不碰检索），因此共享它们
不会把两张图绑在一起。
"""

from __future__ import annotations

from datetime import date
from typing import Any

from langgraph.types import interrupt

from app.graph.state import ConsultState, articles_of, clarify_prompt, with_degraded
from app.redline.engine import evaluate
from app.retrieval.citation import Claim, verify_claims


def ask_clarification(state: ConsultState) -> dict[str, Any]:
    """调用 `interrupt()` 暂停，等待用户补充（§7.5 的追问回环约束）。

    **本节点内只能有一次 `interrupt()` 调用，且不得包在 try/except 里。**
    LangGraph 按 index 严格匹配中断载荷；在一个节点里循环调用会让恢复时
    匹配错位。追问次数上限由外层条件边回环实现（`route_after_slots`），
    不是在节点内循环。

    载荷里带上中文提示文案，前端直接渲染这个表单即可——
    少一处映射就少一处不一致。
    """
    slots = state.get("missing_slots") or []
    round_no = state.get("clarification_round", 0) + 1
    answers = interrupt(
        {
            "missingSlots": slots,
            "round": round_no,
            "prompt": clarify_prompt(slots),
        }
    )
    return {
        "facts": {**(state.get("facts") or {}), **(answers or {})},
        "clarifications": [
            *(state.get("clarifications") or []),
            {"round": round_no, "answers": answers or {}},
        ],
        "clarification_round": round_no,
    }


def redline_check(state: ConsultState) -> dict[str, Any]:
    """独立规则引擎，**不依赖模型**（DR-4 由 import-linter 在 CI 强制）。

    两张图都用它，且都把它排在「生成」节点之前。放在这里而不是各图各写一份，
    是因为"哪一步之后不能再让模型说话"是合规结论，不是某张图的实现细节。
    """
    verdict = evaluate(state.get("question", ""))
    if not verdict.hit:
        return {"redline_hit": False}
    return {
        "redline_hit": True,
        "redline_rules": [verdict.rule_id] if verdict.rule_id else [],
        "redline_category": verdict.category or "",
        "refusal": verdict.refusal_payload(),
    }


def compose_refusal(state: ConsultState) -> dict[str, Any]:
    """拒答。**会话不终止**——这是一次正常回答，不是错误响应。

    答案文本直接取自拒答载荷的字段，因此五要素一定齐备：
    另写一份文案的话，两者迟早会不一致，而不一致的那一份会缺要素。
    """
    payload = state.get("refusal") or {}
    return {
        "answer": (
            f"{payload.get('reason', '该请求触及平台的服务边界，无法协助。')}\n\n"
            f"判定类别：{payload.get('category', '')}；规则：{payload.get('rule_id', '')}\n\n"
            "可以考虑的合法路径：\n"
            + "\n".join(f"- {item}" for item in payload.get("legal_alternatives") or [])
            + f"\n\n{payload.get('advice', '')}"
        )
    }


def citation_check_payload(state: ConsultState, claims: list[Claim]) -> dict[str, Any]:
    """引用校验（§6.6）。失败的结论句**逐句剥离**，而不是整篇重写。

    整篇重写会让未被污染的结论也发生变化，用户无法判断哪句变了；
    逐句剥离保证"留下的都是校验过的"。

    结论句由调用方给出：税务图只看候选方案，差异图还要把「差异成因」的
    叙述一起送校验——**叙述也是结论**，绕过校验就等于给模型留了一条
    不被引用约束的出口。口径（剥离而非重写、全剥离则按依据不足处理）
    留在这里，各图只决定"哪些句子算结论"。
    """
    report = verify_claims(
        claims,
        articles_of(state),
        tenant_id=state.get("tenant_id", ""),
        # **`as_of` 缺省时必须兜到"今天"，不能是空串。**
        #
        # 空串会让 `CandidateArticle.covers()` 里那句
        # `as_of < effective_from` 恒为真，于是**每一条引用都被判 STALE**。
        # 而 STALE 不是拒绝——它是"引用真实存在、只是在该时点不生效"，
        # 属于**保留但标注**的一档（见 citation.py 的判定说明）。
        # 两件事叠起来的后果是：调用方漏传一个字段，输出看起来完全合理
        # （"引用确实存在，只是时点对不上"），而实际上全部标注都是错的。
        # 实测踩到过：脚本没设 as_of，12 条引用全判 STALE。
        as_of=state.get("as_of") or date.today().isoformat(),
    )

    citations: list[dict[str, str]] = []
    seen: set[str] = set()
    for verdict in report.verdicts:
        for check in verdict.valid_citations:
            if check.article is None or check.article.article_id in seen:
                continue
            seen.add(check.article.article_id)
            citations.append(
                {
                    "article_id": check.article.article_id,
                    "article_no": check.article.article_no,
                    "quoted_text": check.citation.quoted_text,
                }
            )

    degraded = list(state.get("degraded") or [])
    if report.must_refuse:
        degraded = with_degraded(state, "所有结论句均未通过引用校验，本次按依据不足处理")

    return {
        "citation_check": {
            "summary": report.summary(),
            "kept": [{"text": verdict.claim.text} for verdict in report.kept],
            "stripped": [
                {
                    "text": verdict.claim.text,
                    "reason": verdict.stale_note or "引用未通过校验",
                }
                for verdict in report.stripped
            ],
            "must_refuse": report.must_refuse,
        },
        "citations": citations,
        "degraded": degraded,
    }
