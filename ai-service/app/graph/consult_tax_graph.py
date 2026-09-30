"""税务筹划图（`tax_planning`）。节点顺序对应详细设计 §7.5。

```
classify_intent -> extract_facts -> check_slots
                                     |-(缺槽且未超 3 次)-> ask_clarification -+
                                     |                       interrupt()      |
                                     |   恢复后回到 check_slots <-------------+
                                     +-> plan_retrieval -> retrieve -> redline_check
                                                                        |-命中-> compose_refusal -> END
                                                                        +-> generate_candidates
                                                                            -> review_anti_avoidance
                                                                            -> compute_tax
                                                                            -> verify_citations
                                                                            -> compose_answer -> END
```

**两个顺序上的决定值得单独说明：**

1. **红线在生成之前**（§7.5）。若先生成再检查，模型已经产出了违法方案的内容，
   即使最终拒答，这些内容也已经进入 trace 与日志。放在检索之后、生成之前，
   违法问题在产生任何内容之前就被拦下。
2. **`compute_tax` 在生成之后、引用校验之前**，且**不调用模型**（AC-2.3）。
   它需要"候选架构"作为输入，因此必须在生成之后；它产出的数字要参与最终答案，
   因此必须在引用校验之前——否则复述这些数字的结论句会因为拿不到候选而全被剥离。

**模型边界是注入的（`ConsultDeps`），不是直接调用 `model_factory`。**
理由不是"方便测试"，而是本图里有几条**与模型无关但必须被验证**的纪律：
红线命中时生成节点不得执行、中断恢复后已完成的节点不得重跑、
`compute_tax` 不得触碰模型。要证明"生成节点没被调用"，最直接的办法是注入一个
会记账的假实现；没有注入点的话，这些纪律只能从真实调用的副作用里去间接推断。
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from langgraph.graph import END, START, StateGraph

from app.graph.nodes import (
    ask_clarification,
    citation_check_payload,
    compose_refusal,
    redline_check,
)
from app.graph.state import (
    INTENT_TAX_PLANNING,
    ConsultState,
    ExtractFn,
    GenerateFn,
    RetrieveFn,
    article_to_dict,
    claims_of,
    missing_slots,
    retrieval_query,
    route_after_redline,
    route_after_slots,
    split_jurisdictions,
    with_degraded,
)
from app.graph.tax_calc import TaxLayer, compute_effective_tax

logger = logging.getLogger(__name__)

#: 检索路由（§6.1）。键名与本体里的 `query_type` 取值一致。
THRESHOLD = "THRESHOLD"
TREATY = "TREATY"
CONCEPT = "CONCEPT"
HYBRID = "HYBRID"

#: 规则优先：先看句式，规则不确定时才该交给模型。这里的规则只覆盖
#: 明确句式；判不准就落 HYBRID，那是三条路都跑、最不容易漏的一档。
_RATE_MARKERS = ("税率", "税负", "多少", "预提", "比例")
_TREATY_MARKERS = ("协定", "条约", "公约", "treaty")
_CONCEPT_MARKERS = ("什么是", "是什么", "定义", "解释", "概念")


def classify_query_type(question: str) -> str:
    """按规则判定查询类型。

    **规则优先而不是直接交给模型**：在"税率是多少"这类明确句式上，
    规则比模型稳定，而且不花钱、不引入延迟。判不准时落 `HYBRID`——
    它的代价是多跑两路检索，而漏掉一路的代价是答不出来。
    """
    text = (question or "").strip()
    if not text:
        return HYBRID
    if any(marker in text for marker in _TREATY_MARKERS):
        return TREATY
    if any(marker in text for marker in _CONCEPT_MARKERS):
        return CONCEPT
    if any(marker in text for marker in _RATE_MARKERS):
        return THRESHOLD
    return HYBRID


# `ExtractFn` / `GenerateFn` / `RetrieveFn` 与差异图共用，定义在 `app.graph.state`
# （理由：签名只有一处定义）。这里只定义本图独有的注入形状。
ClassifyFn = Callable[[str], Awaitable[str]]
ReviewFn = Callable[[list[dict[str, Any]]], Awaitable[list[dict[str, Any]]]]
ParametersFn = Callable[[tuple[str, ...]], Awaitable[dict[str, dict[str, str]]]]


@dataclass
class ConsultDeps:
    """模型与检索的注入点。全部必填，**不给默认实现**。

    不给默认实现是有意的：一个"看起来能跑"的默认实现会让图在缺少真实接线时
    也能启动，问题因此被推迟到演示现场。必填意味着接线必须在构造处完成，
    而默认实现集中放在 `app/graph/deps.py`，那里能看到它确实走了 `model_factory`。
    """

    classify: ClassifyFn
    extract: ExtractFn
    generate: GenerateFn
    retrieve: RetrieveFn
    tax_parameters: ParametersFn
    #: 反避税风险复核。为 None 时该节点标注"未评估"并记降级，而不是假装评估过。
    review: ReviewFn | None = None


def _layers_of(candidate: dict[str, Any]) -> list[TaxLayer]:
    """从候选方案的 dict 还原出 `TaxLayer`。

    键名缺失按 `None` 处理（该层不声明这一税种），**不填默认税率**——
    默认税率会被当成查到的值算进结果里，而它实际是猜的。
    """
    layers: list[TaxLayer] = []
    for raw in candidate.get("layers") or []:
        if not isinstance(raw, dict):
            continue
        layers.append(
            TaxLayer(
                jurisdiction_code=str(raw.get("jurisdiction_code", "")),
                corporate_param=raw.get("corporate_param") or None,
                withholding_param=raw.get("withholding_param") or None,
            )
        )
    return layers


def build_consult_graph(checkpointer: Any, deps: ConsultDeps) -> Any:
    """装配税务筹划图。

    `checkpointer` 由调用方给出：测试用 `MemorySaver`，生产用 Postgres 检查点器
    （`app/graph/checkpoint/postgres.py`）。图本身不关心它存在哪里。
    """

    async def classify_intent(state: ConsultState) -> dict[str, Any]:
        return {"intent": await deps.classify(state["question"])}

    async def extract_facts(state: ConsultState) -> dict[str, Any]:
        return {"facts": await deps.extract(state["question"]) or {}}

    def check_slots(state: ConsultState) -> dict[str, Any]:
        # 追问回环回到这里：**重新判定**而不是沿用上一轮的结论。
        # 沿用的话，第二轮补了 A 却漏了 B 时会直接放行。
        #
        # `intent` 缺失时按本图的意图兜底，而不是兜成 `""`：
        # `missing_slots` 是按意图查表的，空字符串查不到条目就等于**没有必填槽位**，
        # 于是该追问的会直接放行——漏在默认值上的错最难发现，因为它只在
        # 调用方忘了传 `intent` 时出现。
        return {
            "missing_slots": missing_slots(
                state.get("facts"), state.get("intent") or INTENT_TAX_PLANNING
            )
        }

    def plan_retrieval(state: ConsultState) -> dict[str, Any]:
        return {"query_type": classify_query_type(state.get("question", ""))}

    async def retrieve(state: ConsultState) -> dict[str, Any]:
        # `split_jurisdictions` 而不是直接 `tuple(...)`：追问表单回填的
        # `jurisdictions` 是用户手打的字符串（`"NL、IE"`），直接 tuple 会把它
        # 拆成单个字符，于是按法域过滤的查询一个法域都匹配不上，
        # 而结果看起来像"这些法域都没有相关规定"。
        jurisdictions = tuple(split_jurisdictions((state.get("facts") or {}).get("jurisdictions")))
        articles, degraded = await deps.retrieve(retrieval_query(state), jurisdictions)
        return {
            # 状态里存 dict：检查点走 msgpack，dataclass 不在白名单里
            "retrieved": [article_to_dict(article) for article in articles],
            "degraded": with_degraded(state, *degraded),
        }

    async def generate_candidates(state: ConsultState) -> dict[str, Any]:
        """生成候选架构。**在红线之后**，因此违法问题走不到这里。"""
        try:
            candidates = await deps.generate(state)
        except Exception as exc:  # noqa: BLE001 - 生成失败要转成可见的结果，而不是 500
            logger.warning("生成候选方案失败：%s", type(exc).__name__)
            return {
                "candidates": [],
                "errors": [
                    *(state.get("errors") or []),
                    {"node": "generate_candidates", "error": type(exc).__name__},
                ],
                "degraded": with_degraded(state, "候选方案生成失败，本次没有可评估的架构"),
            }
        return {"candidates": candidates or []}

    async def review_anti_avoidance(state: ConsultState) -> dict[str, Any]:
        """逐套标注反避税风险。

        没有复核实现时**标注为未评估并记降级**，而不是跳过这一栏——
        跳过会让界面上少一列，而"这一列没有值"与"这一列是空的"看起来一样，
        使用者会把"未评估"读成"没有风险"。
        """
        candidates = list(state.get("candidates") or [])
        if deps.review is None:
            for candidate in candidates:
                candidate.setdefault("risk_level", "未评估")
            return {
                "candidates": candidates,
                "degraded": with_degraded(
                    state, "未配置反避税风险复核，候选方案的风险等级为「未评估」"
                ),
            }
        reviewed = await deps.review(candidates)
        return {"candidates": list(reviewed or candidates)}

    async def compute_tax(state: ConsultState) -> dict[str, Any]:
        """确定性测算（AC-2.3）。**这个节点不调用模型。**

        结果里的数字全部来自 `kb.wiki_section.key_parameters`，
        算不出来时返回原因而不是一个默认值——理由见 `tax_calc` 的模块说明。
        """
        candidates = list(state.get("candidates") or [])
        if not candidates:
            return {"tax_calc": {"architectures": []}}

        jurisdictions = tuple(
            sorted(
                {
                    layer.jurisdiction_code
                    for candidate in candidates
                    for layer in _layers_of(candidate)
                    if layer.jurisdiction_code
                }
            )
        )
        parameters = await deps.tax_parameters(jurisdictions)

        architectures: list[dict[str, Any]] = []
        blocked_any = False
        for candidate in candidates:
            result = compute_effective_tax(_layers_of(candidate), parameters)
            if not result.ok:
                blocked_any = True
            architectures.append(
                {
                    "name": candidate.get("name", ""),
                    # Decimal 不是 msgpack 可序列化的类型，落状态前转字符串
                    "rate": str(result.rate) if result.rate is not None else None,
                    "percent": result.as_percent(),
                    "components": [[key, str(value)] for key, value in result.components],
                    "blocked_by": list(result.blocked_by),
                    "notes": list(result.notes),
                }
            )

        degraded = list(state.get("degraded") or [])
        if blocked_any:
            degraded = with_degraded(
                state,
                "部分架构因缺少结构化税率参数而未能给出综合税负率，其原因是缺失而非免税",
            )
        return {"tax_calc": {"architectures": architectures}, "degraded": degraded}

    def verify_citations(state: ConsultState) -> dict[str, Any]:
        """引用校验（§6.6）。剥离口径见 `citation_check_payload`。"""
        return citation_check_payload(state, claims_of(list(state.get("candidates") or [])))

    def compose_answer(state: ConsultState) -> dict[str, Any]:
        """拼最终答案。**只使用已经过校验的结论句与确定性算出的数字。**"""
        check = state.get("citation_check") or {}
        kept = check.get("kept") or []
        if check.get("must_refuse") or not kept:
            return {
                "answer": (
                    "本次未能给出有依据的结论：检索到的条款不足以支撑任何一条结论，"
                    "因此不作推测。可以补充法域、收入类型与主体结构后重试。"
                )
            }

        risks = {
            candidate.get("name", ""): candidate.get("risk_level", "")
            for candidate in state.get("candidates") or []
        }
        lines = ["依据检索到的条款："]
        lines.extend(f"- {item['text']}" for item in kept)

        architectures = (state.get("tax_calc") or {}).get("architectures") or []
        if architectures:
            lines.append("")
            lines.append("候选架构与综合税负率（由代码计算，非模型估算）：")
            for item in architectures:
                rate = item.get("percent") or "未给出（缺少结构化税率参数）"
                risk = risks.get(item.get("name", "")) or "未评估"
                lines.append(f"- {item['name']}：{rate}；反避税风险：{risk}")

        lines.append("")
        lines.append(
            "本平台提供研究辅助，不构成法律意见；具体事项请咨询具备相应法域执业资格的律师。"
        )
        return {"answer": "\n".join(lines)}

    graph = StateGraph(ConsultState)
    for name, node in (
        ("classify_intent", classify_intent),
        ("extract_facts", extract_facts),
        ("check_slots", check_slots),
        ("ask_clarification", ask_clarification),
        ("plan_retrieval", plan_retrieval),
        ("retrieve", retrieve),
        ("redline_check", redline_check),
        ("compose_refusal", compose_refusal),
        ("generate_candidates", generate_candidates),
        ("review_anti_avoidance", review_anti_avoidance),
        ("compute_tax", compute_tax),
        ("verify_citations", verify_citations),
        ("compose_answer", compose_answer),
    ):
        graph.add_node(name, node)

    graph.add_edge(START, "classify_intent")
    graph.add_edge("classify_intent", "extract_facts")
    graph.add_edge("extract_facts", "check_slots")
    graph.add_conditional_edges(
        "check_slots",
        route_after_slots,
        {"ask_clarification": "ask_clarification", "plan_retrieval": "plan_retrieval"},
    )
    # 追问后回到 check_slots **重新判定**，而不是直接继续——
    # 回环在这里，而不是在节点内循环 interrupt。
    graph.add_edge("ask_clarification", "check_slots")
    graph.add_edge("plan_retrieval", "retrieve")
    graph.add_edge("retrieve", "redline_check")
    graph.add_conditional_edges(
        "redline_check",
        route_after_redline,
        {"compose_refusal": "compose_refusal", "generate_candidates": "generate_candidates"},
    )
    graph.add_edge("compose_refusal", END)
    graph.add_edge("generate_candidates", "review_anti_avoidance")
    graph.add_edge("review_anti_avoidance", "compute_tax")
    graph.add_edge("compute_tax", "verify_citations")
    graph.add_edge("verify_citations", "compose_answer")
    graph.add_edge("compose_answer", END)

    return graph.compile(checkpointer=checkpointer)
