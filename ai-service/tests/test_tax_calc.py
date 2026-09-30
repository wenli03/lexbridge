"""确定性税务测算的测试。

本文件的重点全在**"算不出来的时候会怎样"**：缺参数、参数是自然语言、参数越界。
这些分支比"算对"更重要，因为算错的那一侧有一个很坏的形状——
**数字偏低的错误恰好是使用者最愿意相信的方向**（"这个架构税负只有 12%"）。
所以下面每一条拒绝路径都有用例。
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.graph.tax_calc import TaxLayer, compute_effective_tax, parse_rate

NL = "NL"
IE = "IE"


def params(**scopes: dict[str, str]) -> dict[str, dict[str, str]]:
    return dict(scopes)


class TestParseRate:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("25%", Decimal("0.25")),
            ("25 %", Decimal("0.25")),
            ("12.5%", Decimal("0.125")),
            ("12.5％", Decimal("0.125")),  # 全角百分号：中文材料里很常见
            ("0.25", Decimal("0.25")),
            ("0", Decimal(0)),
            ("100%", Decimal(1)),
        ],
    )
    def test_parses_common_forms(self, raw: str, expected: Decimal) -> None:
        assert parse_rate(raw) == expected

    @pytest.mark.parametrize(
        "raw",
        [
            None,
            "",
            "视情况而定",
            "依税收协定",
            "twenty-five percent",
            "25.5.5%",
            "-5%",  # 越界
            "150%",  # 越界
        ],
    )
    def test_unusable_values_give_none(self, raw: str | None) -> None:
        """解析不出数字时返回 None，**不是返回 0**。

        返回 0 会让"这一格没抽到"变成"这一格免税"——一个偏低的数字
        披着结构化字段的外衣流到结论里。
        """
        assert parse_rate(raw) is None


class TestComputeEffectiveTax:
    def test_single_layer_without_distribution(self) -> None:
        result = compute_effective_tax(
            [TaxLayer(NL, corporate_param="corporate_tax_rate")],
            params(NL={"corporate_tax_rate": "25%"}),
        )
        assert result.ok
        assert result.rate == Decimal("0.25")
        assert result.as_percent() == "25.00%"

    def test_two_layers_with_withholding(self) -> None:
        """综合税负 = 1 − Π(1 − 各环节税率)。

        这里用独立算出的期望值，而不是用同一个公式再算一遍——
        后者只能证明代码等于它自己。
        """
        result = compute_effective_tax(
            [
                TaxLayer(
                    IE, corporate_param="corporate_tax_rate", withholding_param="wht_dividend"
                ),
                TaxLayer(NL, withholding_param="wht_dividend"),
            ],
            params(
                IE={"corporate_tax_rate": "12.5%", "wht_dividend": "0%"},
                NL={"wht_dividend": "15%"},
            ),
        )
        # 1 - (1-0.125)(1-0)(1-0.15) = 1 - 0.875*0.85 = 0.25625
        assert result.ok
        assert result.rate == Decimal("0.25625")
        assert result.as_percent() == "25.63%"  # 四舍五入到两位

    def test_components_record_every_applied_rate(self) -> None:
        """叙述层要能讲清"这个数字是怎么来的"，因此每一步都要留下痕迹。"""
        result = compute_effective_tax(
            [
                TaxLayer(
                    IE, corporate_param="corporate_tax_rate", withholding_param="wht_dividend"
                ),
                TaxLayer(NL, withholding_param="wht_dividend"),
            ],
            params(
                IE={"corporate_tax_rate": "12.5%", "wht_dividend": "0%"},
                NL={"wht_dividend": "15%"},
            ),
        )
        keys = [key for key, _ in result.components]
        assert keys == [
            "IE.corporate_tax_rate",
            "IE.wht_dividend",
            "NL.wht_dividend",
        ]

    def test_result_is_exact_decimal_not_float(self) -> None:
        """用 Decimal 而不是 float：税负是要写进意见书的数字，
        二进制浮点的 0.1+0.2 问题不该出现在这里。"""
        result = compute_effective_tax(
            [TaxLayer(NL, corporate_param="r")],
            params(NL={"r": "0.1"}),
        )
        assert isinstance(result.rate, Decimal)

    def test_missing_jurisdiction_refuses_to_give_a_number(self) -> None:
        result = compute_effective_tax(
            [TaxLayer(IE, corporate_param="corporate_tax_rate")],
            params(NL={"corporate_tax_rate": "25%"}),
        )
        assert result.rate is None
        assert not result.ok
        assert result.as_percent() is None
        assert any("没有该法域" in reason for reason in result.blocked_by)

    def test_unparseable_parameter_refuses_instead_of_guessing(self) -> None:
        result = compute_effective_tax(
            [TaxLayer(IE, corporate_param="corporate_tax_rate")],
            params(IE={"corporate_tax_rate": "视情况而定"}),
        )
        assert result.rate is None
        assert any("corporate_tax_rate" in reason for reason in result.blocked_by)

    def test_partial_availability_does_not_produce_a_partial_number(self) -> None:
        """第一层参数齐全、第二层缺失时，**不能只按第一层给出数字**。

        只算一层会得到一个偏低的结果（少算了后续环节的税），
        而偏低恰好是使用者最愿意相信的方向。
        """
        result = compute_effective_tax(
            [
                TaxLayer(IE, corporate_param="corporate_tax_rate"),
                TaxLayer(NL, corporate_param="corporate_tax_rate"),
            ],
            params(IE={"corporate_tax_rate": "12.5%"}, NL={}),
        )
        assert result.rate is None
        # 已算出的中间量仍然带回来，便于定位是哪一层缺
        assert result.components == (("IE.corporate_tax_rate", Decimal("0.125")),)

    def test_layer_without_declared_taxes_is_skipped_but_noted(self) -> None:
        """终极层不征企业所得税也不征预提税时，跳过它并留下说明。

        静默跳过与"这一层免税"在结果里长得一样，而后者是一个业务判断。
        """
        result = compute_effective_tax(
            [
                TaxLayer(NL, corporate_param="r"),
                TaxLayer("KY"),  # 终极层，无任何税种
            ],
            params(NL={"r": "25%"}),
        )
        assert result.rate == Decimal("0.25")
        assert any("按不征税处理" in note for note in result.notes)

    def test_empty_architecture_is_refused(self) -> None:
        result = compute_effective_tax([], params())
        assert result.rate is None
        assert result.blocked_by

    def test_deep_chain_is_flagged_as_out_of_scope(self) -> None:
        """>2 层时明确声明未计入受控外国企业规则等影响。

        不写这一句的话，四层架构算出来的漂亮数字会被当成完整结论，
        而它没有考虑最可能推翻它的那条规则。
        """
        layers = [TaxLayer(NL, corporate_param="r") for _ in range(3)]
        result = compute_effective_tax(layers, params(NL={"r": "0%"}))
        assert result.ok
        assert any("受控外国企业" in note for note in result.notes)
