"""结构化输出在**离线路径**上是否可用。

这个文件存在的理由是一次实测：`ReplayChatModel` 原先不实现 `bind_tools`，
于是 `structured(replay_model, Schema)` 直接抛 `NotImplementedError`——
**replay 与 mock 模式下无法做任何结构化抽取**。而本体抽取、事实槽位、
候选方案生成全都依赖它，这两条路径又恰好是 CI 与一键演示所依赖的。

真实调用模式下从不会暴露这个问题，所以它只会出现在离线路径上——
这正是需要一条测试守着它的原因。
"""

from __future__ import annotations

from typing import Any

import pytest
from langchain_core.embeddings import Embeddings
from pydantic import BaseModel

from app.chains.model_factory import structured
from app.chains.replay import ReplayChatModel


class Intent(BaseModel):
    intent: str
    confidence: float


def chat_record(
    *,
    name: str | None = None,
    args: dict[str, Any] | None = None,
    content: str = "",
) -> dict[str, Any]:
    tool_calls = [{"name": name, "args": args or {}, "id": "call_1"}] if name is not None else []
    return {
        "type": "chat",
        "request_summary": "test",
        "content": content,
        "tool_calls": tool_calls,
        "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
    }


class TestReplayStructuredOutput:
    def test_bind_tools_is_implemented(self) -> None:
        """曾经这里抛 NotImplementedError，两条离线路径因此无法做结构化抽取。"""
        model = ReplayChatModel(records=[], scenario="mock")
        bound = model.bind_tools([Intent])
        assert bound is not None

    def test_structured_parses_recorded_tool_call(self) -> None:
        """录制的 tool_calls 应当能被解析成 schema 实例。"""
        model = ReplayChatModel(
            records=[
                chat_record(name="Intent", args={"intent": "TAX_PLANNING", "confidence": 0.9})
            ],
            scenario="mock",
        )
        parser = structured(model, Intent)
        result = parser.invoke("这段文字是本测试的提示词")

        assert isinstance(result, Intent)
        assert result.intent == "TAX_PLANNING"
        assert result.confidence == pytest.approx(0.9)

    def test_records_are_consumed_in_order(self) -> None:
        """回放按顺序推进：第二次调用取第二条录制。"""
        model = ReplayChatModel(
            records=[
                chat_record(name="Intent", args={"intent": "DIVERGENCE", "confidence": 0.8}),
                chat_record(name="Intent", args={"intent": "OTHER", "confidence": 0.1}),
            ],
            scenario="mock",
        )
        parser = structured(model, Intent)
        first = parser.invoke("第一次")
        second = parser.invoke("第二次")
        assert (first.intent, second.intent) == ("DIVERGENCE", "OTHER")

    def test_exhausted_records_fail_loudly(self) -> None:
        """录制用尽时抛错，而不是返回一个空壳对象。

        静默返回空壳会让"提示词改了导致调用次数变化"这个问题
        伪装成"抽取结果为空"，而后者会被当成数据质量问题去查。
        """
        from app.chains.replay import FixtureNotFoundError

        model = ReplayChatModel(
            records=[chat_record(name="Intent", args={"intent": "OTHER", "confidence": 0.1})],
            scenario="mock",
        )
        parser = structured(model, Intent)
        parser.invoke("第一次")
        with pytest.raises(FixtureNotFoundError):
            parser.invoke("第二次")


class TestMockModeWiring:
    def test_get_chat_model_in_mock_mode_supports_structured_output(self, monkeypatch) -> None:
        """走工厂的 mock 模式也必须能拿到结构化输出——否则 mock 无用于契约测试。"""
        from app.chains import model_factory
        from app.core.config import ModelMode, Settings

        settings = Settings(model_mode=ModelMode.MOCK, siliconflow_api_key="")
        model = model_factory.get_chat_model(settings=settings)
        # mock 的合成响应不含 tool_calls，因此这里只验证"能构造出解析器"。
        # 能构造本身就是这次修复的目标：此前它在此处就会抛 NotImplementedError。
        assert structured(model, Intent) is not None


class TestKeyStore:
    """顺带守住一点：回放 embedding 的未命中要有迹可循。"""

    def test_embedding_miss_is_recorded(self) -> None:
        from app.chains.replay import ReplayEmbeddings

        embeddings: Embeddings = ReplayEmbeddings([], scenario="mock")
        vector = embeddings.embed_query("一条从未录制过的文本")
        assert len(vector) == 1024
        assert embeddings.misses  # type: ignore[attr-defined]
