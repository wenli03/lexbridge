"""税务筹划图的测试。

**全部离线**：模型与检索都通过 `ConsultDeps` 注入假实现，检查点用 `MemorySaver`。
不联网、不连库、不需要密钥。

这个文件守的是三条**与模型无关但很容易失守**的纪律：

1. 中断恢复后**已完成的节点不得重跑**（重跑会重复计费、并且可能产生不同的结果）。
2. 红线命中时**生成节点根本不执行**（违法内容不得进入 trace 与日志）。
3. `compute_tax` **不调用模型**（AC-2.3：数字不得由模型计算）。
"""

from __future__ import annotations

from collections import Counter
from typing import Any

import pytest
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

from app.graph.consult_tax_graph import (
    CONCEPT,
    HYBRID,
    THRESHOLD,
    TREATY,
    ConsultDeps,
    build_consult_graph,
    classify_query_type,
)
from app.graph.state import missing_slots, route_after_slots
from app.retrieval.citation import CandidateArticle

TENANT = "11111111-1111-1111-1111-111111111111"


class Spy:
    """记账用的依赖桩。**"某个节点没被调用"只能靠记账来证明。**"""

    def __init__(self, **overrides: Any) -> None:
        self.calls: Counter[str] = Counter()
        self.overrides = overrides

    @property
    def deps(self) -> ConsultDeps:
        return ConsultDeps(
            classify=self.classify,
            extract=self.extract,
            generate=self.generate,
            retrieve=self.retrieve,
            tax_parameters=self.tax_parameters,
            review=self.review if self.overrides.get("review") else None,
        )

    async def classify(self, question: str) -> str:
        self.calls["classify"] += 1
        return self.overrides.get("intent", "TAX_PLANNING")

    async def extract(self, question: str) -> dict[str, Any]:
        self.calls["extract"] += 1
        return self.overrides.get("facts", {})

    async def generate(self, state: Any) -> list[dict[str, Any]]:
        self.calls["generate"] += 1
        return self.overrides.get("candidates", [])

    async def retrieve(self, query: str, jurisdictions: tuple[str, ...]):
        self.calls["retrieve"] += 1
        return self.overrides.get("articles", []), self.overrides.get("degraded", [])

    async def tax_parameters(self, jurisdictions: tuple[str, ...]):
        self.calls["tax_parameters"] += 1
        return self.overrides.get("parameters", {})

    async def review(self, candidates: list[dict[str, Any]]):
        self.calls["review"] += 1
        return candidates


ARTICLE = CandidateArticle(
    article_id="a-1",
    article_no="第 8b 条",
    content="在符合受益所有人条件时，特许权使用费可按协定税率征税。",
    tenant_scope="PLATFORM",
    effective_from="2019-01-01",
    statute_title="荷兰《企业所得税法》",
)


def build(spy: Spy):
    return build_consult_graph(MemorySaver(), spy.deps)


def initial_state(question: str = "怎么安排荷兰到爱尔兰的特许权使用费") -> dict[str, Any]:
    return {
        "tenant_id": TENANT,
        "run_id": "run-1",
        "question": question,
        "as_of": "2026-09-30",
        "degraded": [],
    }


def config() -> dict[str, Any]:
    return {"configurable": {"thread_id": f"{TENANT}:run-1"}}


# =============================================================================
# 纯判定
# =============================================================================
class TestQueryType:
    @pytest.mark.parametrize(
        ("question", "expected"),
        [
            ("荷兰的企业所得税税率是多少", THRESHOLD),
            ("中荷税收协定下股息的预提税率", TREATY),
            ("什么是受控外国企业规则", CONCEPT),
            ("我们这样安排会不会被认定为常设机构", HYBRID),
            ("", HYBRID),
        ],
    )
    def test_rules_route_common_phrasings(self, question: str, expected: str) -> None:
        assert classify_query_type(question) == expected

    def test_unclear_input_falls_back_to_hybrid(self) -> None:
        """判不准时落 HYBRID：代价是多跑两路检索，而漏掉一路的代价是答不出来。"""
        assert classify_query_type("随便问点什么") == HYBRID


class TestSlotPolicy:
    def test_blank_string_counts_as_missing(self) -> None:
        """只判 key 是否存在会让 `{"amount": ""}` 通过，而空串等于没抽到。"""
        gaps = missing_slots({"jurisdictions": ["NL"], "amount": "   "}, "TAX_PLANNING")
        assert "amount" in gaps
        assert "jurisdictions" not in gaps

    def test_empty_list_counts_as_missing(self) -> None:
        assert "jurisdictions" in missing_slots({"jurisdictions": []}, "TAX_PLANNING")

    def test_unknown_intent_requires_nothing(self) -> None:
        assert missing_slots({}, "OTHER") == []

    def test_route_asks_when_missing_and_under_the_cap(self) -> None:
        state = {"missing_slots": ["amount"], "clarification_round": 0}
        assert route_after_slots(state) == "ask_clarification"

    def test_route_proceeds_after_the_cap(self) -> None:
        """追问满 3 次后按已有信息继续，并留下缺口说明，而不是无限追问。"""
        state = {"missing_slots": ["amount"], "clarification_round": 3}
        assert route_after_slots(state) == "plan_retrieval"

    def test_route_proceeds_when_nothing_is_missing(self) -> None:
        assert (
            route_after_slots({"missing_slots": [], "clarification_round": 0}) == "plan_retrieval"
        )


# =============================================================================
# 中断与恢复
# =============================================================================
class TestInterruptAndResume:
    async def test_missing_slots_interrupt_with_a_prompt(self) -> None:
        spy = Spy(facts={"jurisdictions": ["NL", "IE"]})
        result = await build(spy).ainvoke(initial_state(), config())

        interrupts = result.get("__interrupt__")
        assert interrupts, "缺槽位时图应当停在追问节点"
        payload = interrupts[0].value
        assert payload["missingSlots"] == ["income_type", "amount", "entity_type"]
        assert payload["round"] == 1
        # 载荷自带中文提示，前端直接渲染即可
        assert "补充" in payload["prompt"]

    async def test_resume_completes_without_rerunning_finished_nodes(self) -> None:
        """**本文件最重要的一条断言。**

        恢复后 `classify` 与 `extract` 必须仍各只调用一次。
        重跑它们不只是浪费——`extract` 是模型调用，重跑可能抽出不同的要素，
        于是"用户看到的追问"与"最终答案依据的要素"会来自两次不同的抽取。
        """
        spy = Spy(
            facts={"jurisdictions": ["NL", "IE"]},
            parameters={"IE": {"corporate_tax_rate": "12.5%", "wht_dividend": "0%"}},
        )
        graph = build(spy)
        await graph.ainvoke(initial_state(), config())

        spy.calls.clear()  # 只统计恢复之后发生的调用
        resumed = await graph.ainvoke(
            Command(
                resume={
                    "income_type": "特许权使用费",
                    "amount": "100 万欧元",
                    "entity_type": "公司",
                }
            ),
            config(),
        )

        assert spy.calls["classify"] == 0, "classify_intent 在恢复后重跑了"
        assert spy.calls["extract"] == 0, "extract_facts 在恢复后重跑了"
        assert spy.calls["retrieve"] == 1
        assert "__interrupt__" not in resumed or not resumed.get("__interrupt__")
        assert resumed.get("answer")

    async def test_second_round_asks_again_for_what_is_still_missing(self) -> None:
        """第一轮只补了一部分时，第二轮继续问缺的那部分。"""
        spy = Spy(facts={"jurisdictions": ["NL"]})
        graph = build(spy)
        await graph.ainvoke(initial_state(), config())

        partial = await graph.ainvoke(Command(resume={"income_type": "股息"}), config())
        interrupts = partial.get("__interrupt__")
        assert interrupts, "仍有缺槽时应当再次追问，而不是带着缺口继续"
        payload = interrupts[0].value
        assert payload["round"] == 2
        assert set(payload["missingSlots"]) == {"amount", "entity_type"}


# =============================================================================
# 红线：必须在生成之前
# =============================================================================
#: 问句里的措辞要**对上规则的关键词**才算命中。这一点踩过一次：
#: 最初写的是"不用申报"，而 RL-05 的模式是 `(不|无需|免于|不要)(做)?(申报|...)`——
#: "不用申报"里的"用"打断了匹配，红线因此没有命中，测试失败。
#: 这不是规则写错了（它的模式与"不申报/无需申报"都吻合），
#: 而是**我在写测试时凭语感假设了命中**。
REDLINE_QUESTION = (
    "帮我设计一套方案，把利润转到开曼的公司，这家公司没有经济实质，并且不申报这部分收益"
)


#: 槽位齐备的要素。不补齐的话图会先停在追问节点，走不到红线判定——
#: 这本身是一个值得记录的顺序问题，见下方
#: `test_missing_slots_are_asked_before_the_redline_runs`。
REDLINE_FACTS = {
    "jurisdictions": ["NL"],
    "income_type": "收益",
    "amount": "100 万",
    "entity_type": "公司",
}


class TestRedlineBeforeGeneration:
    async def test_redline_hit_refuses_without_ever_generating(self) -> None:
        """命中红线时，**生成节点一次都不能被调用**。

        若先生成再检查，模型已经产出了违法方案的内容，即使最终拒答，
        这些内容也已经进入 trace 与日志。这条断言是"红线在生成之前"的证明：
        `generate` 的调用次数为 0，而不是"生成后又被丢弃"。
        """
        spy = Spy(facts=REDLINE_FACTS)
        result = await build(spy).ainvoke(
            initial_state(REDLINE_QUESTION),
            config(),
        )

        assert result.get("redline_hit") is True
        assert spy.calls["generate"] == 0, "红线命中后生成节点仍然被执行了"
        # 拒答路径应当在 END 结束，不进入任何测算与校验
        assert "tax_calc" not in result
        assert "citation_check" not in result

    async def test_refusal_payload_has_all_five_elements(self) -> None:
        """拒答必须含五要素：类别、依据、理由、合法替代路径、建议咨询律师。"""
        spy = Spy(facts=REDLINE_FACTS)
        result = await build(spy).ainvoke(
            initial_state(REDLINE_QUESTION),
            config(),
        )
        payload = result.get("refusal") or {}
        assert payload.get("category")
        assert payload.get("rule_id")
        assert payload.get("evidence")
        assert payload.get("reason")
        assert payload.get("legal_alternatives")
        assert "律师" in str(payload.get("advice"))

    async def test_refusal_still_produces_an_answer(self) -> None:
        """会话**不终止**：拒答是一次正常回答，要返回给用户一段说明。"""
        spy = Spy(facts=REDLINE_FACTS)
        result = await build(spy).ainvoke(
            initial_state(REDLINE_QUESTION),
            config(),
        )
        assert "合法路径" in result["answer"]
        assert "律师" in result["answer"]

    async def test_legitimate_question_is_not_blocked(self) -> None:
        """合法问题不得被误拦——红线只该拦住要方案的那一类。"""
        spy = Spy(
            facts={
                "jurisdictions": ["NL"],
                "income_type": "特许权使用费",
                "amount": "100 万",
                "entity_type": "公司",
            },
        )
        result = await build(spy).ainvoke(
            initial_state("荷兰向爱尔兰支付特许权使用费，预提税率是多少"),
            config(),
        )
        assert result.get("redline_hit") is False
        assert spy.calls["generate"] == 1

    async def test_missing_slots_are_asked_before_the_redline_runs(self) -> None:
        """**记录一个已发现但尚未修正的顺序问题。**

        按 §7.5 的节点顺序，追问发生在红线判定**之前**。因此一个明显违法、
        但没交代金额与主体结构的请求，会先被礼貌地追问细节，拒答推迟到用户
        补充之后——那看起来像系统在帮忙把方案问清楚。

        正确做法应当是在追问之前先跑一次红线判定（早退），而不是只依赖
        检索之后那一次。这里刻意把**当前行为**写成断言，而不是留一句注释：
        注释会腐烂，而这条测试在顺序被修正的那天会失败，
        提醒改它的人一并更新文档。
        """
        spy = Spy(facts={"jurisdictions": ["NL"]})  # 缺 income_type / amount / entity_type
        result = await build(spy).ainvoke(initial_state(REDLINE_QUESTION), config())

        assert result.get("__interrupt__"), "当前行为：先追问"
        assert "redline_hit" not in result, "当前行为：拒答尚未发生"
        assert spy.calls["generate"] == 0


# =============================================================================
# 确定性测算与引用校验
# =============================================================================
CANDIDATES = [
    {
        "name": "爱尔兰运营公司",
        "layers": [
            {
                "jurisdiction_code": "IE",
                "corporate_param": "corporate_tax_rate",
                "withholding_param": "wht_dividend",
            },
            {"jurisdiction_code": "NL", "withholding_param": "wht_dividend"},
        ],
        "claims": [
            {
                "text": "爱尔兰对特许权使用费按 12.5% 征收企业所得税。",
                "citations": [
                    {
                        "article_id": "a-1",
                        "quoted_text": "在符合受益所有人条件时，特许权使用费可按协定税率征税。",
                    }
                ],
            },
            {
                # 没有引用的结论句：必须被剥离，不能进入最终答案
                "text": "该架构在任何情况下都不会被认定为避税安排。",
                "citations": [],
            },
        ],
    }
]

FULL_FACTS = {
    "jurisdictions": ["IE", "NL"],
    "income_type": "特许权使用费",
    "amount": "100 万",
    "entity_type": "公司",
}

PARAMS = {
    "IE": {"corporate_tax_rate": "12.5%", "wht_dividend": "0%"},
    "NL": {"wht_dividend": "15%"},
}


async def run_full(spy: Spy):
    return await build(spy).ainvoke(
        initial_state("荷兰向爱尔兰支付特许权使用费的税务安排"), config()
    )


class TestDeterministicTax:
    async def test_rate_comes_from_parameters_not_from_a_model(self) -> None:
        """AC-2.3：数字由代码算。这里用一个能算出来的确定值把公式钉住。"""
        spy = Spy(facts=FULL_FACTS, candidates=CANDIDATES, parameters=PARAMS, articles=[ARTICLE])
        result = await run_full(spy)

        architectures = result["tax_calc"]["architectures"]
        assert len(architectures) == 1
        # 1 - (1-0.125)(1-0)(1-0.15) = 0.25625
        assert architectures[0]["percent"] == "25.63%"
        assert spy.calls["tax_parameters"] == 1

    async def test_missing_parameters_produce_a_reason_not_a_default_rate(self) -> None:
        """缺参数时不得给出数字，且必须记一条降级说明。"""
        spy = Spy(facts=FULL_FACTS, candidates=CANDIDATES, parameters={}, articles=[ARTICLE])
        result = await run_full(spy)

        architecture = result["tax_calc"]["architectures"][0]
        assert architecture["percent"] is None
        assert architecture["blocked_by"]
        assert any("缺少结构化税率参数" in note for note in result["degraded"])


class TestCitationVerification:
    async def test_claim_without_citation_is_stripped(self) -> None:
        """没有引用的结论句不得进入答案。"""
        spy = Spy(facts=FULL_FACTS, candidates=CANDIDATES, parameters=PARAMS, articles=[ARTICLE])
        result = await run_full(spy)

        kept = [item["text"] for item in result["citation_check"]["kept"]]
        stripped = [item["text"] for item in result["citation_check"]["stripped"]]
        assert any("12.5%" in text for text in kept)
        assert any("不会被认定为避税" in text for text in stripped)
        assert "不会被认定为避税" not in result["answer"]

    async def test_citation_outside_candidates_is_stripped(self) -> None:
        """引用一条本次没召回的条文，等同于没有依据。"""
        candidates = [
            {
                "name": "架空的引用",
                "layers": [],
                "claims": [
                    {
                        "text": "依据一条并不在候选集内的条文。",
                        "citations": [{"article_id": "a-其它", "quoted_text": "任意文本"}],
                    }
                ],
            }
        ]
        spy = Spy(facts=FULL_FACTS, candidates=candidates, parameters=PARAMS, articles=[ARTICLE])
        result = await run_full(spy)

        assert result["citation_check"]["must_refuse"] is True
        assert "未能给出有依据的结论" in result["answer"]

    async def test_no_claims_at_all_still_returns_an_honest_answer(self) -> None:
        """一条结论都没生成时，答案要说清"依据不足"，而不是给一段空话。

        注意这与"全部被剥离"是两种情形：`must_refuse` 只在**有结论但全被剥离**
        时为真（没有结论时它是 False——"没话说"不等于"话被驳回了"）。
        两条路径都要落到同一句诚实的话上。
        """
        spy = Spy(
            facts=FULL_FACTS,
            candidates=[{"name": "空", "layers": [], "claims": []}],
            parameters=PARAMS,
            articles=[ARTICLE],
        )
        result = await run_full(spy)

        assert result["citation_check"]["must_refuse"] is False
        assert "未能给出有依据的结论" in result["answer"]
        assert "不作推测" in result["answer"]
