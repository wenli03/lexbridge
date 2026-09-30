"""税务测算：**确定性代码，模型不得参与算数**（AC-2.3）。

为什么这条要被写成验收标准，而不是"注意一下"：

一个语言模型可以写出完全通顺、格式正确、还带着百分号和小数位的税负测算，
而那个数字是它"读起来合理"地拼出来的。这类错误**没有任何外部特征**——
它不像幻觉引用那样能靠查库发现，也不像格式错误那样能被解析器拦住。
一个错了 3 个百分点的综合税负率，从文本上看起来与正确的那个一模一样。

因此本模块的边界划得很硬：

- 税率、阈值一律来自 `kb.wiki_section.key_parameters`（结构化字段），
  不来自任何自然语言文本，也不来自模型的算术。
- **算不出来就说算不出来。** 缺参数、参数解析不出数字、参数越界，
  一律返回"拒绝给出数字 + 原因"，而不是用默认值兜底。
  默认值的问题在于它会被当成事实引用出去，而使用者无法分辨
  这个 25% 是查到的还是系统猜的。
- 每个中间量都保留下来（`components`），因为叙述层要能把
  "这个数字是怎么来的"讲清楚；讲不清的数字等于没有依据。

参数键名由调用方从 wiki 词条里读出来传入，本模块不维护"键名别名表"——
猜键名与猜税率是同一类错误，只是发生得更早。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

#: 允许的税率范围。超出即视为数据问题而非业务情况。
MIN_RATE = Decimal("0")
MAX_RATE = Decimal("1")

#: 「25%」「25 %」「0.25」「12.5％」（全角百分号）都能解析。
_RATE_RE = re.compile(r"^\s*([0-9]+(?:\.[0-9]+)?)\s*(%|％)?\s*$")


@dataclass(frozen=True)
class TaxLayer:
    """架构里的一层。

    例如「荷兰控股 → 爱尔兰运营」是两层：运营层先缴当地企业所得税，
    分配时缴预提税；控股层再把利润分配出去，再缴一次预提税。

    两个参数键都可以为 None，因为"这一层征什么税"本身就是要查的事实：
    终极母公司所在层通常没有企业所得税，也可能所在法域对汇出不再征预提税。
    **把某一层写成"不征税"是调用方的判断，本模块只负责按它算，
    并在结果里记下这一层被跳过了**——默默跳过与"免税"在结果上无法区分。
    """

    jurisdiction_code: str
    #: 企业所得税率的参数键名（`key_parameters` 里的键）；None = 该层不征此税
    corporate_param: str | None = None
    #: 分配环节预提税率的参数键名；None = 该层不再向外分配（或不再征）
    withholding_param: str | None = None


@dataclass(frozen=True)
class TaxResult:
    """测算结果。**`rate` 为 None 时表示"拒绝给出数字"**，原因在 `blocked_by`。"""

    rate: Decimal | None
    components: tuple[tuple[str, Decimal], ...] = ()
    blocked_by: tuple[str, ...] = ()
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def ok(self) -> bool:
        return self.rate is not None

    def as_percent(self) -> str | None:
        """交给叙述层展示。**格式化也在这里做**，免得模型自己拼百分号。

        显式指定 `ROUND_HALF_UP`：`Decimal.quantize` 的默认舍入是
        银行家舍入（half-even），于是 25.625% 会显示成 25.62% ——
        在财务报表与统计口径里那是对的，但在一句"综合税负率为 X%"里
        它看起来就是算错了。这类差异不会报错，只会让人怀疑整个数字。
        """
        if self.rate is None:
            return None
        value = (self.rate * 100).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        return f"{value}%"


def parse_rate(raw: str | None) -> Decimal | None:
    """把 `key_parameters` 里的字符串值解析成小数形式的税率。

    解析不出数字（例如"视情况而定"、"依协定"）时返回 None——
    **返回 None 是正确行为，不是失败。** 结构化字段里出现自然语言，
    说明这一格的抽取质量不够，此时唯一诚实的动作是承认它不可用。

    百分号形式按百分数解释（"25%" → 0.25），纯小数按小数解释（"0.25" → 0.25）。
    """
    if raw is None:
        return None
    match = _RATE_RE.match(str(raw))
    if not match:
        return None
    try:
        value = Decimal(match.group(1))
    except InvalidOperation:  # pragma: no cover - 正则已限制形态
        return None
    if match.group(2):
        value = value / Decimal(100)
    return value if MIN_RATE <= value <= MAX_RATE else None


def compute_effective_tax(
    layers: list[TaxLayer],
    parameters: dict[str, dict[str, str]],
) -> TaxResult:
    """按层级链计算综合税负率（税后留存比例的补数）。

    模型：利润在每层先缴企业所得税，分配时再缴预提税，因此

        税后留存 = Π (1 − 企业所得税率_i) × (1 − 预提税率_i)
        综合税负率 = 1 − 税后留存

    Args:
        layers: 从**利润产生地**到**最终母公司**的顺序，不能颠倒。
        parameters: 法域代码 → `key_parameters`。

    Returns:
        `TaxResult`。任何一层缺参数或参数不可用，都返回 `rate=None` 并说明原因——
        绝不是"跳过这一层继续算"，那会得到一个偏低的、看起来更"优惠"的数字，
        而这恰好是使用者最愿意相信的方向。
    """
    if not layers:
        return TaxResult(rate=None, blocked_by=("架构为空，没有可计算的层级",))

    remaining = Decimal(1)
    components: list[tuple[str, Decimal]] = []
    blocked: list[str] = []
    notes: list[str] = []

    for index, layer in enumerate(layers, start=1):
        if layer.corporate_param is None and layer.withholding_param is None:
            # 该层没有声明任何税种（终极了）。记一条说明而不是静默跳过：
            # "这一层不征税"与"我们忘了看这一层"在结果里长得一样。
            notes.append(f"第 {index} 层 {layer.jurisdiction_code} 未声明税率参数，按不征税处理")
            continue

        scope = parameters.get(layer.jurisdiction_code)
        if scope is None:
            blocked.append(f"第 {index} 层 {layer.jurisdiction_code}：没有该法域的结构化参数")
            continue

        if layer.corporate_param is not None:
            corporate = _lookup(scope, layer.corporate_param)
            if corporate is None:
                blocked.append(
                    f"第 {index} 层 {layer.jurisdiction_code}：参数 "
                    f"{layer.corporate_param!r} 缺失或无法解析为税率"
                )
            else:
                remaining *= Decimal(1) - corporate
                components.append((f"{layer.jurisdiction_code}.{layer.corporate_param}", corporate))

        if layer.withholding_param is None:
            continue
        withholding = _lookup(scope, layer.withholding_param)
        if withholding is None:
            blocked.append(
                f"第 {index} 层 {layer.jurisdiction_code}：参数 "
                f"{layer.withholding_param!r} 缺失或无法解析为税率"
            )
            continue
        remaining *= Decimal(1) - withholding
        components.append((f"{layer.jurisdiction_code}.{layer.withholding_param}", withholding))

    if blocked:
        # 拒绝时也把已算出的中间量带回去：排查"为什么算不出来"时，
        # "哪一层缺"比"整体失败"有用得多。
        return TaxResult(rate=None, components=tuple(components), blocked_by=tuple(blocked))

    rate = Decimal(1) - remaining
    if len(layers) > 2:
        notes.append("层级超过两层时未计入受控外国企业规则与实质经营要求的影响")
    return TaxResult(rate=rate, components=tuple(components), notes=tuple(notes))


def _lookup(scope: dict[str, str], key: str) -> Decimal | None:
    """在一层的参数里按键取值并解析。键不存在与值解析不出，都返回 None。

    这两种情况对使用者意味着同一件事：**这一格没有可用的数字**。
    """
    if key not in scope:
        return None
    return parse_rate(scope[key])
