"""接线层的测试。

`app/graph/deps.py` 是把"图能跑"与"图真的能用"分开的那一层，所以它的测试
也分两类，各自守不同的东西：

1. **纯函数**（`facts_to_dict` / `merge_key_parameters`）——不碰数据库、
   不碰模型，把策略分支测穷尽。合并策略尤其重要：它在"两个词条给出不同税率"
   时的选择会一路算进综合税负率，而算出来的数字看起来和真的一样。
2. **replay 模式下的端到端接线**——`build_deps` 造出来的 `extract` 真的能
   经 `model_factory`（回放）→ `structured()` → `facts_to_dict` 走通一遍。
   这条链之前断过一次（`ReplayChatModel` 不支持 `bind_tools`），而断点
   发生在离线路径上，只有跑一遍才会发现。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.chains.replay import FixtureStore
from app.core.config import ModelMode, Settings
from app.graph.deps import TAX_PLANNING, FactItem, build_deps, facts_to_dict, merge_key_parameters

TENANT = "11111111-1111-1111-1111-111111111111"


def chat_record(name: str, args: dict) -> dict:
    """一条录制的模型响应。形状与 `ReplayChatModel` 读取的一致。"""
    return {
        "type": "chat",
        "request_summary": "deps-test",
        "content": "",
        "tool_calls": [{"name": name, "args": args, "id": "call_1"}],
        "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
    }


# =============================================================================
# facts_to_dict
# =============================================================================
class TestFactsToDict:
    def test_jurisdictions_are_split_into_a_list(self) -> None:
        """模型倾向写 `"NL,IE"`，而下游的法域过滤要的是列表。

        不在这一处切开的话，`"NL,IE"` 会被当成**一个**法域代码传给 SQL，
        查询返回空集——而空集看起来像"这些法域都没有相关规定"，
        正好是 `divergence` 模块极力要避免的那种误读。
        """
        facts = facts_to_dict([FactItem(key="jurisdictions", value="NL, IE")])
        assert facts["jurisdictions"] == ["NL", "IE"]

    @pytest.mark.parametrize("raw", ["NL，IE", "NL;IE", "NL/IE", "nl ie", "NL、IE"])
    def test_common_separators_all_work(self, raw: str) -> None:
        """中文逗号、顿号、分号、斜杠都要能切——模型会用其中的任何一种。"""
        facts = facts_to_dict([FactItem(key="jurisdictions", value=raw)])
        assert facts["jurisdictions"] == ["NL", "IE"]

    def test_blank_value_is_not_written(self) -> None:
        """空值不写进 facts：写了会让"抽到了空串"通过槽位判定。

        这是 `missing_slots` 那套"空值即缺"的口径在上游的对应动作。
        """
        facts = facts_to_dict(
            [FactItem(key="amount", value="   "), FactItem(key="income_type", value="股息")]
        )
        assert facts == {"income_type": "股息"}

    def test_blank_key_is_skipped(self) -> None:
        assert facts_to_dict([FactItem(key="  ", value="有值但键为空")]) == {}

    def test_empty_jurisdiction_list_is_not_written(self) -> None:
        """全是分隔符时切出空列表，此时应当视为没抽到，而不是 `[]`。"""
        assert facts_to_dict([FactItem(key="jurisdictions", value=" , ; ")]) == {}

    def test_later_duplicate_key_wins(self) -> None:
        facts = facts_to_dict(
            [FactItem(key="amount", value="100 万"), FactItem(key="amount", value="200 万")]
        )
        assert facts["amount"] == "200 万"


# =============================================================================
# merge_key_parameters
# =============================================================================
def row(code: str, **params: str) -> dict:
    return {"jurisdiction_code": code, "key_parameters": params}


class TestMergeKeyParameters:
    def test_same_value_in_two_rows_is_kept(self) -> None:
        """两处一致 → 保留。这是正常情形，说明抽取稳定。"""
        merged, conflicts = merge_key_parameters(
            [row("IE", corporate_tax_rate="12.5%"), row("IE", corporate_tax_rate="12.5%")]
        )
        assert merged == {"IE": {"corporate_tax_rate": "12.5%"}}
        assert conflicts == []

    def test_conflicting_values_drop_the_key(self) -> None:
        """**不同值 → 丢弃该键，而不是取首个或最后一个。**

        取任何一个都会让一次"抛硬币的结果"看起来像查到的值，
        而它会一路算进综合税负率——那正是 `tax_calc` 反复要防的错误，
        只是发生在更早的一步。
        """
        merged, conflicts = merge_key_parameters(
            [row("IE", corporate_tax_rate="12.5%"), row("IE", corporate_tax_rate="15%")]
        )
        assert "corporate_tax_rate" not in merged.get("IE", {})
        assert conflicts == ["IE.corporate_tax_rate"]

    def test_a_third_row_cannot_vote_the_conflict_away(self) -> None:
        """第三个词条重复了第一个的值，冲突**依然存在**。

        若允许"再出现一次就解掉冲突"，那么两个矛盾值里出现两次的那个会赢，
        而它并没有比另一个更可信——只是被重复了一遍。
        """
        merged, conflicts = merge_key_parameters(
            [
                row("IE", corporate_tax_rate="12.5%"),
                row("IE", corporate_tax_rate="15%"),
                row("IE", corporate_tax_rate="12.5%"),
            ]
        )
        assert "corporate_tax_rate" not in merged.get("IE", {})
        assert conflicts == ["IE.corporate_tax_rate"]

    def test_other_keys_survive_a_conflict(self) -> None:
        """一个键冲突不该连坐同一法域的其它键——那样会损失本来可用的参数。"""
        merged, conflicts = merge_key_parameters(
            [
                row("IE", corporate_tax_rate="12.5%", wht_dividend="0%"),
                row("IE", corporate_tax_rate="15%", wht_dividend="0%"),
            ]
        )
        assert merged["IE"] == {"wht_dividend": "0%"}
        assert conflicts == ["IE.corporate_tax_rate"]

    def test_jurisdictions_are_independent(self) -> None:
        """不同法域的同名键互不影响。"""
        merged, conflicts = merge_key_parameters(
            [row("IE", corporate_tax_rate="12.5%"), row("NL", corporate_tax_rate="25.8%")]
        )
        assert merged == {
            "IE": {"corporate_tax_rate": "12.5%"},
            "NL": {"corporate_tax_rate": "25.8%"},
        }
        assert conflicts == []

    def test_non_string_and_blank_values_are_skipped(self) -> None:
        merged, _ = merge_key_parameters(
            [
                {
                    "jurisdiction_code": "IE",
                    "key_parameters": {"good": "12.5%", "blank": "  ", "nested": {"a": 1}},
                }
            ]
        )
        assert merged["IE"] == {"good": "12.5%"}

    @pytest.mark.parametrize(
        "bad_row",
        [
            {"key_parameters": {"a": "1%"}},  # 无法域
            {"jurisdiction_code": "IE", "key_parameters": None},
            {"jurisdiction_code": "IE", "key_parameters": ["不是字典"]},
        ],
    )
    def test_malformed_rows_are_ignored(self, bad_row: dict) -> None:
        """形状不对的行跳过，而不是抛错——一条坏行不该让整次咨询失败。"""
        merged, conflicts = merge_key_parameters([bad_row])
        assert merged == {}
        assert conflicts == []


# =============================================================================
# replay 模式下的端到端接线
# =============================================================================
class TestReplayWiring:
    def settings(self, tmp_path: Path) -> Settings:
        return Settings(
            model_mode=ModelMode.REPLAY,
            replay_fixture_dir=str(tmp_path),
            siliconflow_api_key="",
        )

    def test_extract_round_trips_through_the_factory(self, tmp_path: Path) -> None:
        """`extract` 必须真的经工厂→structured→facts_to_dict 走通一遍。

        这条测试覆盖的是**接线**，不是解析：它证明 `build_deps` 用的场景名
        与 fixture 的文件名确实对得上。此前这类耦合出过一次事——
        `ReplayChatModel` 不支持 `bind_tools`，于是离线路径上的结构化抽取
        整体不可用，而它在真实调用模式下从不暴露。
        """
        FixtureStore(tmp_path, f"{TAX_PLANNING}/extract_facts").append(
            chat_record(
                "FactsResult",
                {
                    "facts": [
                        {"key": "jurisdictions", "value": "IE, NL"},
                        {"key": "income_type", "value": "特许权使用费"},
                        {"key": "amount", "value": "   "},
                    ],
                    "notes": "金额未提及",
                },
            )
        )
        deps = build_deps(
            conn=None, tenant_id=TENANT, settings=self.settings(tmp_path), with_review=False
        )

        facts = _run(deps.extract("爱尔兰向荷兰支付特许权使用费，税务上怎么安排"))
        assert facts == {"jurisdictions": ["IE", "NL"], "income_type": "特许权使用费"}

    def test_missing_fixture_fails_instead_of_returning_empty(self, tmp_path: Path) -> None:
        """缺 fixture 必须报错，不能悄悄返回 `{}`。

        返回空字典会让"没录制"表现为"模型什么都没抽到"，
        于是问题被当成抽取质量差去查，而实际是 fixture 没准备好。
        """
        from app.chains.replay import FixtureNotFoundError

        deps = build_deps(
            conn=None, tenant_id=TENANT, settings=self.settings(tmp_path), with_review=False
        )
        with pytest.raises(FixtureNotFoundError):
            _run(deps.extract("任何问题"))


def _run(coro):
    """跑一个协程。

    不用 `pytest.mark.asyncio`：这几条测试只验证"调用能走通"，
    与事件循环无关，因此也就不需要 asyncio 插件在场。
    """
    import asyncio

    return asyncio.run(coro)
