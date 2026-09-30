"""图的真实接线：把模型、检索与结构化参数接到 `ConsultDeps` 上。

**这个模块是把"图能跑"与"图真的能用"分开的那一层。** 图本身只依赖注入的
可调用对象，因此它的正确性可以在没有网络、没有数据库的情况下被验证；
而这里集中处理所有外部依赖，好处是"一次咨询到底碰了哪些外部系统"
在这一个文件里就能看全。

三条贯穿全模块的取向：

1. **所有模型调用经 `model_factory`**，且 replay 模式下必须给出 `scenario`。
   场景名用「图节点路径」（如 `tax_planning/extract_facts`），
   这样 fixture 的粒度与代码结构对得上，改一个节点不会牵连整个场景。
2. **结构化参数只从 `kb.wiki_section.key_parameters` 读，不从正文里猜。**
   同一个键在两个词条里给出不同值时**丢弃该键**而不是挑一个——
   挑出来的那个会被当成查到的值一路算进结论里，而它其实是抛硬币。
3. 缺参数、缺引用、缺模型的场合一律**降级并留痕**，不编造。
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable, Mapping
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.retrieval.citation import CandidateArticle

logger = logging.getLogger(__name__)

#: 场景名前缀。完整场景名形如 `tax_planning/extract_facts`，
#: 落到磁盘上是 `fixtures/replay/tax_planning/extract_facts.jsonl`（一层子目录）。
#: 这正是详细设计 §7.4 建议的"用图的节点路径命名"——场景与代码结构对得上，
#: 改一个节点不会牵连整个场景。
TAX_PLANNING = "tax_planning"

#: 从词条里读结构化参数时，限定为平台公共库 + 当前租户。
#: 与检索层同一口径（详细设计 §6.4）。
_PARAM_SQL = """
    SELECT ws.jurisdiction_code, ws.key_parameters
    FROM kb.wiki_section ws
    JOIN kb.wiki_entry we ON we.id = ws.wiki_entry_id
    WHERE ws.jurisdiction_code = ANY(%(jurisdictions)s)
      AND we.status = 'PUBLISHED'
      AND we.tenant_scope IN ('PLATFORM', %(tenant)s)
"""


class IntentResult(BaseModel):
    """意图判定结果。"""

    intent: Literal["TAX_PLANNING", "DIVERGENCE", "OTHER"]
    confidence: float = 0.0
    rationale: str = ""


class FactItem(BaseModel):
    """一个槽位。用「键值对列表」而不是 `dict[str, Any]`：

    function calling 的 schema 对 `additionalProperties` 支持参差，
    而槽位清单本身是动态的、不适合写成一堆固定字段。
    """

    key: str = Field(description="槽位名，如 jurisdictions / income_type / amount / entity_type")
    value: str = Field(description="槽位的值；多个法域用逗号分隔")


class FactsResult(BaseModel):
    facts: list[FactItem] = []
    notes: str = ""


class CitationItem(BaseModel):
    article_id: str
    # 字段说明**进的是 function calling 的 schema**，模型实际看得到，
    # 因此它和提示词一样是要求的一部分。把"逐字"写在这里比只写在提示词里
    # 更难被忽略：实测仅写在提示词里时，模型会把引文"顺手改顺"
    # （补一个主语 `The`），而那会让整句结论被判为引文不匹配而丢弃。
    quoted_text: str = Field(
        default="",
        description=(
            "支撑该结论的法条原文片段，**逐字复制**自该法条 content 中连续的一段。"
            "不要改写、补主语、调整标点或跨段拼接——引文会与原文做子串比对，"
            "改写过的会被判为不匹配，并导致该结论被整句丢弃。"
        ),
    )


class ClaimItem(BaseModel):
    text: str
    citations: list[CitationItem] = []


class LayerItem(BaseModel):
    jurisdiction_code: str
    corporate_param: str | None = None
    withholding_param: str | None = None


class CandidateItem(BaseModel):
    name: str
    layers: list[LayerItem] = []
    claims: list[ClaimItem] = []


class CandidateList(BaseModel):
    candidates: list[CandidateItem] = []


class RiskItem(BaseModel):
    name: str
    risk_level: Literal["低", "中", "高", "未评估"] = "未评估"
    reason: str = ""


class RiskReview(BaseModel):
    items: list[RiskItem] = []


# =============================================================================
# 纯转换
# =============================================================================
def facts_to_dict(items: Iterable[FactItem]) -> dict[str, Any]:
    """把键值对列表转成槽位字典。

    `jurisdictions` 额外按分隔符切成列表：模型倾向于写成 `"NL,IE"`，
    而下游的法域过滤要的是列表。**切开只在这一处做**——否则
    `"NL,IE"` 会被当成一个法域代码传给 SQL，查询返回空集，
    而空集看起来像"这些法域都没有相关规定"。

    分隔符取「中英文并列用法」的并集：顿号 `、` 是中文里标准的并列标记，
    漏掉它的话 `"NL、IE"` 会整体当成一个法域代码——而它比逗号写法更容易被忽略，
    因为逗号那条路径是通的，只有中文输入才踩到。
    """
    result: dict[str, Any] = {}
    for item in items:
        key = (item.key or "").strip()
        if not key:
            continue
        value = (item.value or "").strip()
        if not value:
            # 空值不写进 facts：写了会让"抽到了空串"通过槽位判定
            continue
        if key == "jurisdictions":
            parts = [
                part.strip().upper() for part in re.split(r"[,，、;；/\s]+", value) if part.strip()
            ]
            if parts:
                result[key] = parts
            continue
        result[key] = value
    return result


def merge_key_parameters(
    rows: Iterable[dict[str, Any]],
) -> tuple[dict[str, dict[str, str]], list[str]]:
    """把多行词条参数合并成「法域 → 键值」。

    **同一个键在不同词条里给出不同值时，丢弃该键**，而不是取首个或最后一个。
    取任何一个都会让一次"抛硬币的结果"看起来像查到的值，
    而它会一路算进综合税负率——这正是 `tax_calc` 反复要防的那类错误，
    只不过错误发生在更早的一步。

    Returns:
        (合并后的参数, 冲突键清单)。冲突键被丢弃，但仍要报出来，
        因为它是抽取质量的信号：同一个税率抽出了两个值，说明抽取有问题。
    """
    merged: dict[str, dict[str, str]] = {}
    conflicts: set[str] = set()

    for row in rows:
        code = row.get("jurisdiction_code")
        # 这里刻意**不**写 `or {}`：`None` 被兜成空字典后会通过下面的类型判断，
        # 于是 `setdefault` 凭空建出一个 `{"IE": {}}`——即"IE 的参数查到了，
        # 只是没有键"。而真相是这行根本没给出参数，两者在下游同样算缺参数，
        # 但在排查时指向完全不同的结论（词条有问题 vs 这行是坏数据）。
        params = row.get("key_parameters")
        if not code or not isinstance(params, dict):
            continue
        scope = merged.setdefault(code, {})
        for key, value in params.items():
            if not isinstance(value, str) or not value.strip():
                continue
            marker = f"{code}.{key}"
            if marker in conflicts:
                # 已经判定冲突的键不再接受后续值：否则第三个词条会"投票"
                # 把冲突解掉，而它并没有让矛盾消失
                continue
            if key in scope and scope[key] != value.strip():
                conflicts.add(marker)
                scope.pop(key, None)
                continue
            scope[key] = value.strip()

    return merged, sorted(conflicts)


# =============================================================================
# 提示词
# =============================================================================
# 提示词与场景名一起进 fixture：回放模式下录制的就是"这套提示词换来的响应"，
# 因此改提示词必须重录（`ReplayChatModel` 会因录制用尽而报错，
# 而不是静默返回上一次的结果）。
_CLASSIFY_PROMPT = (
    "判断下面的咨询属于哪一类：TAX_PLANNING（跨境税务筹划）、"
    "DIVERGENCE（监管差异分析）、OTHER。只输出结论与置信度。\n\n"
    "咨询：{question}"
)

_EXTRACT_PROMPT = (
    "从下面的咨询里抽取交易要素。需要哪些槽位：jurisdictions（法域代码，多个用逗号分隔）、"
    "income_type、amount、entity_type、behavior。抽不到的槽位不要编造，直接省略。\n\n"
    "咨询：{question}"
)

_GENERATE_PROMPT = (
    "根据已确认的交易要素与检索到的法条，设计 2 至 3 套可选的架构。\n"
    "每一套给出：名称、层级（从利润产生地到最终母公司，逐层标注该层的"
    "企业所得税与预提税参数键名）、以及若干结论句。\n"
    "**每条结论句必须挂上支撑它的引用**（article_id 与原文片段）；"
    "没有法条支撑的话就不要写这一句。\n"
    "**quoted_text 必须逐字复制自该法条，是其中连续的一段。**"
    "不要改写、不要补主语、不要调整标点、不要用省略号、不要跨段拼接——"
    "系统会把引文与法条原文做子串比对，改写过的引文会被判为不匹配，"
    "该结论句会被整句丢弃。宁可引短一点。\n\n"
    "要素：{facts}\n\n可用法条：{articles}"
)

_RISK_PROMPT = "对每一套架构标注反避税风险等级（低/中/高）并给出理由。\n\n架构：{candidates}"


def build_deps(
    *,
    conn: Any,
    tenant_id: str,
    scenario_prefix: str = TAX_PLANNING,
    settings: Any = None,
    embedder: Any = None,
    reranker: Any = None,
    with_review: bool = True,
) -> Any:
    """构造真实接线。

    Args:
        conn: 已绑定租户的连接（检索与参数读取都走它；
            `hybrid.retrieve` 内部会从上下文取租户，不接受参数）。
        tenant_id: 当前租户，用于参数查询的 `tenant_scope` 过滤。
        scenario_prefix: replay fixture 的场景前缀。
        embedder: 查询向量化器。为 None 时检索降级为关键词 + 子串，
            并在响应里标注——**不静默**。
        reranker: 重排器。
        with_review: 是否接线反避税风险复核。关掉时图会标注「未评估」。
    """
    from app.chains.model_factory import get_chat_model, structured
    from app.graph.consult_tax_graph import ConsultDeps
    from app.retrieval.hybrid import SearchScope
    from app.retrieval.hybrid import retrieve as hybrid_retrieve

    def scenario(node: str) -> str:
        return f"{scenario_prefix}/{node}"

    async def classify(question: str) -> str:
        parser = structured(
            get_chat_model(settings=settings, scenario=scenario("classify_intent")), IntentResult
        )
        result = await parser.ainvoke(_CLASSIFY_PROMPT.format(question=question))
        return str(result.intent)

    async def extract(question: str) -> dict[str, Any]:
        parser = structured(
            get_chat_model(settings=settings, scenario=scenario("extract_facts")), FactsResult
        )
        result = await parser.ainvoke(_EXTRACT_PROMPT.format(question=question))
        return facts_to_dict(result.facts)

    async def generate(state: Mapping[str, Any]) -> list[dict[str, Any]]:
        parser = structured(
            get_chat_model(settings=settings, scenario=scenario("generate_candidates")),
            CandidateList,
        )
        prompt = _GENERATE_PROMPT.format(
            facts=state.get("facts") or {},
            articles=[
                {
                    "article_id": item.get("article_id"),
                    "article_no": item.get("article_no"),
                    "content": item.get("content"),
                }
                for item in (state.get("retrieved") or [])[:20]
            ],
        )
        result = await parser.ainvoke(prompt)
        return [candidate.model_dump() for candidate in result.candidates]

    async def retrieve(
        query: str, jurisdictions: tuple[str, ...]
    ) -> tuple[list[CandidateArticle], list[str]]:
        result = await hybrid_retrieve(
            conn,
            query=query,
            scope=SearchScope(jurisdictions=tuple(jurisdictions)),
            embedder=embedder,
            reranker=reranker,
        )
        return result.candidates, list(result.degraded)

    async def tax_parameters(jurisdictions: tuple[str, ...]) -> dict[str, dict[str, str]]:
        if not jurisdictions:
            return {}
        async with conn.cursor() as cur:
            await cur.execute(
                _PARAM_SQL,
                {"jurisdictions": list(jurisdictions), "tenant": tenant_id},
            )
            rows = await cur.fetchall()
        merged, conflicts = merge_key_parameters(rows)
        for marker in conflicts:
            logger.warning("结构化参数存在冲突，已丢弃该键（不猜哪一个对）：%s", marker)
        return merged

    async def review(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
        parser = structured(
            get_chat_model(settings=settings, scenario=scenario("review_risks")), RiskReview
        )
        prompt = _RISK_PROMPT.format(
            candidates=[
                {"name": item.get("name"), "layers": item.get("layers")} for item in candidates
            ]
        )
        result = await parser.ainvoke(prompt)
        levels = {item.name: (item.risk_level, item.reason) for item in result.items}
        merged: list[dict[str, Any]] = []
        for candidate in candidates:
            level, reason = levels.get(candidate.get("name", ""), ("未评估", ""))
            merged.append({**candidate, "risk_level": level, "risk_reason": reason})
        return merged

    return ConsultDeps(
        classify=classify,
        extract=extract,
        generate=generate,
        retrieve=retrieve,
        tax_parameters=tax_parameters,
        review=review if with_review else None,
    )
