"""图状态与**可独立测试的纯判定函数**。

这里放三类东西：

1. `ConsultState` —— 两张图共用的状态（对应详细设计 §7.4）。
2. 纯函数 —— 槽位判定、路由、取值拆分、状态与对象的互转。它们不碰数据库、不碰模型，
   因此图里最容易出错的那部分（"该追问还是该继续"）可以脱离一切外部依赖被穷尽测试。
3. 注入无关的共用节点见 `app/graph/nodes.py`。

**为什么这些函数在这里而不是在各自的图里。** 两张图共用同一条检索词拼装规则
（`retrieval_query`）与同一套状态还原方式（`articles_of` / `claims_of`）。抄一份的
代价不是多几行，而是两张图会在没人注意的时候对"该检索什么""什么算一条结论"产生分歧，
而这种分歧只会在结论对不上法条时才被发现。

**一条贯穿全文件的约束：状态必须可被检查点序列化。**
检查点走 msgpack 存储，dataclass 与自定义对象不在白名单里。
因此状态里只存 dict / list / str / int / bool；法条在状态中以 dict 形态存在，
需要对象语义时再就地还原（`article_from_dict`）。把 `CandidateArticle` 直接塞进状态
在本地跑 MemorySaver 时看不出问题，换成 Postgres 检查点才炸——而那时的错误信息
会是序列化失败，指向基础设施而不是数据形状。
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from typing import Any, TypedDict

from app.retrieval.citation import CandidateArticle, Citation, Claim

#: 追问次数上限（AC-2.1）。超过后不再追问，按已有信息继续并声明缺口。
MAX_CLARIFICATION_ROUNDS = 3

INTENT_TAX_PLANNING = "TAX_PLANNING"
INTENT_DIVERGENCE = "DIVERGENCE"
INTENT_OTHER = "OTHER"

#: 两类咨询各自必须齐备的槽位。
#:
#: 放在模块级而不是节点内部：槽位清单是**产品口径**，改它应该是一次
#: 有意识的修改，而不是在某个节点里顺手加一个 key。
REQUIRED_SLOTS: dict[str, tuple[str, ...]] = {
    INTENT_TAX_PLANNING: ("jurisdictions", "income_type", "amount", "entity_type"),
    INTENT_DIVERGENCE: ("jurisdictions", "behavior"),
}

#: 追问时给用户看的字段名。追问表单直接由 interrupt 载荷驱动，
#: 标签写在这里而不是让前端自己映射——少一处映射就少一处不一致。
SLOT_LABELS: dict[str, str] = {
    "jurisdictions": "涉及的法域（如 NL、IE、SG）",
    "income_type": "收入类型（股息 / 特许权使用费 / 利息 / 股权转让）",
    "amount": "预计金额或规模",
    "entity_type": "主体类型（公司 / 合伙 / 信托 / 基金 / 常设机构）",
    "behavior": "要评估的具体商业行为",
}

#: 并列值的分隔符。**顿号必须在内**：它是中文里标准的并列标记，
#: 而它比逗号更容易被漏掉——逗号那条路径总是通的，只有中文输入才踩到，
#: 于是 `"NL、IE"` 会被整体当成一个法域代码交给 SQL，查询返回空集，
#: 而空集看起来像"这些法域都没有相关规定"。
_SEPARATORS = r"[,，、;；/]+"
#: 法域代码还多一个空白分隔（`"NL IE"`），因为代码本身是单个词元。
_CODE_SEPARATORS = r"[,，、;；/\s]+"


def _split(value: Any, pattern: str) -> list[str]:
    """按分隔符拆值、去空白、去重并保持首次出现的顺序。

    去重而不是保留重复：`["NL", "NL"]` 会给差异矩阵多出一列完全相同的格，
    而"多出来的那一列"看起来像是另一个法域。
    """
    if value is None:
        return []
    pieces: list[str] = []
    if isinstance(value, str):
        pieces = re.split(pattern, value)
    elif isinstance(value, (list, tuple, set, frozenset)):
        for item in value:
            if item is not None:
                pieces.extend(re.split(pattern, str(item)))
    else:
        pieces = [str(value)]

    result: list[str] = []
    for piece in pieces:
        text = piece.strip()
        if text and text not in result:
            result.append(text)
    return result


def split_values(value: Any) -> list[str]:
    """拆并列值（不按空白切分）。

    **不按空白切分是有意的**：`"cross border royalty payment"` 是一个行为描述，
    按空白切会变成 4 个行为，差异矩阵因此多出 3 行填不出来的东西；
    而法域代码（`split_jurisdictions`）里的空白确实该切——那里每一项都是词元。
    """
    return _split(value, _SEPARATORS)


def split_jurisdictions(value: Any) -> list[str]:
    """拆法域代码并统一成大写。

    **只在这一处做**：法域代码要进 SQL 的过滤条件，大小写不一致会让同一批条款
    在两次查询里落到不同的法域集合上。用户从追问表单手填的 `"nl，ie"` 也走这里。
    """
    return [item.upper() for item in _split(value, _CODE_SEPARATORS)]


class CitationRef(TypedDict):
    article_id: str
    article_no: str
    quoted_text: str


# =============================================================================
# 注入形状
# =============================================================================
# 三个可调用对象的签名。**放在这里而不是各自的图里**：税务筹划图与监管差异图
# 共用它们，抄两份的话，两处签名会在没人注意时分岔——而签名不一致的表现是
# "某一张图跑不通"，不是"两处定义不一样"，排查方向一开始就是错的。
#
# 它们都是异步的：图节点本身是 `async def`，同步实现会阻塞事件循环，
# 而 ai 服务是单进程服务多个并发咨询的。
#
#: 抽取交易要素。入参是用户原始咨询，出参是槽位字典（缺失的槽位不出现）。
ExtractFn = Callable[[str], Awaitable[dict[str, Any]]]

#: 生成候选方案。入参是当前状态（图节点拿得到 facts 与 retrieved），出参是候选列表。
#:
#: 入参写成 `Mapping` 而不是 `dict`：图传进来的是 `ConsultState`，而 TypedDict
#: **不是** `dict` 的子类型（mypy 会拒绝），但它确实是 Mapping。用 `dict` 会把一个
#: 类型问题伪装成"图传错了参数"，而实际两边都是对的。
GenerateFn = Callable[[Mapping[str, Any]], Awaitable[list[dict[str, Any]]]]

#: 检索。出参是 `(候选法条, 降级原因)` —— 降级原因与结果**同时**返回而不是写日志，
#: 理由同 `ConsultState.degraded`：降级必须能一路传到用户眼前。
#:
#: 返回的是 `CandidateArticle` 对象而不是 dict：转成 dict 是**图节点**的职责
#: （见 `consult_tax_graph.retrieve` 里的 `article_to_dict`），因为只有那里知道
#: 状态要过 msgpack 检查点。把降级动作塞进检索层，会让"谁负责序列化"没有答案。
RetrieveFn = Callable[[str, tuple[str, ...]], Awaitable[tuple[list[CandidateArticle], list[str]]]]


class ConsultState(TypedDict, total=False):
    """两张咨询图共用的状态。字段与详细设计 §7.4 对齐。"""

    # --- 输入 ---
    tenant_id: str
    run_id: str
    question: str
    as_of: str

    # --- 意图与槽位 ---
    intent: str
    facts: dict[str, Any]
    missing_slots: list[str]
    clarification_round: int
    clarifications: list[dict[str, Any]]

    # --- 检索 ---
    query_type: str
    retrieved: list[dict[str, Any]]

    # --- 红线 ---
    redline_hit: bool
    redline_rules: list[str]
    redline_category: str
    refusal: dict[str, Any]

    # --- 输出 ---
    candidates: list[dict[str, Any]]
    tax_calc: dict[str, Any]
    citations: list[CitationRef]
    citation_check: dict[str, Any]
    answer: str
    #: 降级原因。**是显式字段而不是日志**——降级必须对用户可见，
    #: 一个静默降级的检索结果会被当作完整结果使用（详细设计 §7.4）。
    degraded: list[str]

    # --- 观测 ---
    errors: list[dict[str, Any]]


# =============================================================================
# 纯判定
# =============================================================================
def is_blank(value: Any) -> bool:
    """这个值算不算"没填"。

    **只判 `key not in facts` 是不够的**：模型抽取出 `""` 是很常见的结果，
    而 `{"amount": ""}` 在"键存在"这个意义上是通过的。空值即缺——
    `None`、空白串、空容器都算没填。
    """
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, (list, dict, tuple, set)):
        return not value
    return False


def missing_slots(facts: dict[str, Any] | None, intent: str) -> list[str]:
    """还缺哪些必填要素（判定口径见 `is_blank`）。"""
    required = REQUIRED_SLOTS.get(intent, ())
    facts = facts or {}
    return [slot for slot in required if is_blank(facts.get(slot))]


def clarify_prompt(slots: list[str]) -> str:
    """把缺失槽位变成一句人话。空列表返回空串，调用方不必先判空。"""
    if not slots:
        return ""
    labels = [SLOT_LABELS.get(slot, slot) for slot in slots]
    return "为了给出可用的结论，还需要你补充：" + "；".join(labels)


def route_after_slots(state: ConsultState, proceed_to: str = "plan_retrieval") -> str:
    """缺槽位且未超上限 → 追问；否则继续。

    **回环由这条条件边实现，不是在节点内循环调用 `interrupt`。**
    LangGraph 按 index 严格匹配中断载荷，一个节点里循环调用会让恢复时错位
    （详细设计 §7.5 的追问回环约束）。

    `proceed_to` 由各图给出自己的下一个节点名：判定口径（`missing_slots` +
    上限）是**产品口径、两张图必须一致**，所以它留在这里；而"接着去哪儿"
    是各图的拓扑，不该抄进这个函数里。差异图用
    `functools.partial(route_after_slots, proceed_to="build_divergence_matrix")`。
    """
    if (
        state.get("missing_slots")
        and state.get("clarification_round", 0) < MAX_CLARIFICATION_ROUNDS
    ):
        return "ask_clarification"
    return proceed_to


def route_after_redline(state: ConsultState, proceed_to: str = "generate_candidates") -> str:
    """命中红线 → 拒答；否则进入生成。

    **红线在生成之前。** 若先生成再检查，模型已经产出了违法方案的内容，
    即使最终拒答，这些内容也已经进入 trace 与日志，并可能被从日志里取走。

    `proceed_to` 的理由同 `route_after_slots`：判定口径共用，拓扑各自声明。
    """
    return "compose_refusal" if state.get("redline_hit") else proceed_to


def with_degraded(state: ConsultState, *notes: str) -> list[str]:
    """累积降级说明，自动去重并保持首次出现的顺序。

    去重是必要的：同一原因（例如"回放模式下重排未命中"）会在多个节点各报一次，
    而重复的提示条会让用户以为出了多个问题。
    """
    existing = list(state.get("degraded", []))
    for note in notes:
        if note and note not in existing:
            existing.append(note)
    return existing


# =============================================================================
# 状态 ↔ 对象 转换
# =============================================================================
def article_to_dict(article: CandidateArticle) -> dict[str, Any]:
    """降到可序列化形态。`hierarchy_path` 是 tuple，列表化以保持往返一致。"""
    return {
        "article_id": article.article_id,
        "article_no": article.article_no,
        "content": article.content,
        "tenant_scope": article.tenant_scope,
        "effective_from": article.effective_from,
        "effective_to": article.effective_to,
        "statute_title": article.statute_title,
        "hierarchy_path": list(article.hierarchy_path),
    }


def article_from_dict(payload: dict[str, Any]) -> CandidateArticle:
    """从状态还原。缺失的可选字段按默认值补，不因为少一个字段就整条丢掉。"""
    return CandidateArticle(
        article_id=payload["article_id"],
        article_no=payload.get("article_no", ""),
        content=payload.get("content", ""),
        tenant_scope=payload.get("tenant_scope", "PLATFORM"),
        effective_from=payload.get("effective_from", "1970-01-01"),
        effective_to=payload.get("effective_to"),
        statute_title=payload.get("statute_title", ""),
        hierarchy_path=tuple(payload.get("hierarchy_path") or ()),
    )


def articles_of(state: ConsultState) -> list[CandidateArticle]:
    """把状态里的候选条款还原成对象。"""
    return [article_from_dict(item) for item in state.get("retrieved") or []]


def claims_of_raw(raw_claims: Iterable[Any]) -> list[Claim]:
    """把 `{"text", "citations"}` 形态的结论句还原成 `Claim`。

    **没有引用的句子同样收进来**：它们会在校验阶段被剥离，而"没有引用"
    正是必须被剥离的情形。在这里过滤掉，就等于把"这条结论没有依据"
    这件事悄悄藏了起来——最终答案会看不出少了什么。
    """
    claims: list[Claim] = []
    for raw in raw_claims:
        if not isinstance(raw, dict) or not raw.get("text"):
            continue
        citations = tuple(
            Citation(
                article_id=str(citation.get("article_id", "")),
                quoted_text=str(citation.get("quoted_text", "")),
            )
            for citation in raw.get("citations") or []
            if isinstance(citation, dict)
        )
        claims.append(Claim(text=str(raw["text"]), citations=citations))
    return claims


def claims_of(candidates: Sequence[dict[str, Any]]) -> list[Claim]:
    """从候选方案/路径里收集结论句（口径见 `claims_of_raw`）。"""
    return claims_of_raw(
        claim for candidate in candidates for claim in (candidate.get("claims") or [])
    )


def retrieval_query(state: ConsultState) -> str:
    """把问题与已知要素拼成检索词。

    把要素也拼进去，是因为追问回来的补充信息往往才是检索的关键
    （"荷兰 爱尔兰 特许权使用费"比原始问句更接近法条措辞）。

    `behavior` 也在其中：监管差异图要检索的是一个**行为**，
    用户原句里可能只是一句业务描述，而条款措辞用的是"支付特许权使用费"这类说法。
    """
    parts = [state.get("question", "")]
    facts = state.get("facts") or {}
    for key in ("jurisdictions", "income_type", "entity_type", "behavior"):
        value = facts.get(key)
        if isinstance(value, (list, tuple, set, frozenset)):
            parts.extend(str(item) for item in value)
        elif value:
            parts.append(str(value))
    return " ".join(part for part in parts if part).strip()
