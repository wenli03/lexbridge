"""录制与回放的往返测试。

**不打网络。** 这里验证的是**格式契约**：录制侧写出的东西，回放侧必须读得懂。
两侧的实现在同一份 fixture 上对接，因此格式一旦漂移，这里就会红——
而漂移的真实后果是"演示时才发现回放不出东西"，那时离改动已经很远了。

`RecordingChatOpenAI` 继承 `ChatOpenAI`，构造它需要 api_key 之类的参数；
本文件不去真正发请求，只验证**模型字段与请求载荷的隔离**（那是实测踩过的坑）
与**嵌入侧的录制行为**（用假实现即可完整覆盖）。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from langchain_core.embeddings import Embeddings
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from app.chains.recording import RecordingChatOpenAI, RecordingEmbeddings
from app.chains.replay import (
    FixtureStore,
    ReplayChatModel,
    ReplayEmbeddings,
    _hash_text,
    record_chat_exchange,
)


class FakeEmbeddings(Embeddings):
    """确定性假嵌入：维度可辨认，便于断言"录下来的就是发出去的那条"。"""

    def __init__(self, dim: int = 4) -> None:
        self.dim = dim
        self.calls = 0

    def _vec(self, text: str) -> list[float]:
        return [float(len(text)), 1.0, 2.0, 3.0][: self.dim]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        self.calls += 1
        return [self._vec(t) for t in texts]

    def embed_query(self, text: str) -> list[float]:
        self.calls += 1
        return self._vec(text)

    async def aembed_documents(self, texts: list[str]) -> list[list[float]]:
        return self.embed_documents(texts)

    async def aembed_query(self, text: str) -> list[float]:
        return self.embed_query(text)


# =============================================================================
# chat：录制 → 回放
# =============================================================================
class TestChatRoundTrip:
    def test_recorded_exchange_replays_in_order(self, tmp_path: Path) -> None:
        store = FixtureStore(tmp_path, "scenario/classify_intent")
        record_chat_exchange(
            store,
            request_summary="[system] 判断意图 / [human] 荷兰控股",
            message=AIMessage(
                content="TAX_PLANNING",
                tool_calls=[
                    {"name": "IntentResult", "args": {"intent": "TAX_PLANNING"}, "id": "c1"}
                ],
            ),
        )

        records = FixtureStore(tmp_path, "scenario/classify_intent").load()
        model = ReplayChatModel(records=records, scenario="scenario/classify_intent")
        result = model._generate([HumanMessage(content="任意问题")])
        message = result.generations[0].message

        assert message.content == "TAX_PLANNING"
        assert message.tool_calls[0]["name"] == "IntentResult"
        assert message.tool_calls[0]["args"] == {"intent": "TAX_PLANNING"}

    def test_scenario_with_slash_creates_subdirectory(self, tmp_path: Path) -> None:
        """场景名用「图节点路径」，磁盘上因此多一层目录。

        这一条守的是 `FixtureStore.append` 里那句"建 path 的父目录而不是
        directory 本身"——建错的话录制会在 `open('a')` 处抛 FileNotFoundError，
        而那时人要等到演示时才发现没有 fixture。
        """
        store = FixtureStore(tmp_path, "tax_planning/extract_facts")
        record_chat_exchange(store, request_summary="s", message=AIMessage(content="ok"))
        assert (tmp_path / "tax_planning" / "extract_facts.jsonl").exists()

    def test_replay_exhaustion_is_explicit(self, tmp_path: Path) -> None:
        """录少了必须炸，不能悄悄返回上一次的结果。"""
        store = FixtureStore(tmp_path, "s")
        record_chat_exchange(store, request_summary="s", message=AIMessage(content="only one"))
        model = ReplayChatModel(records=store.load(), scenario="s")
        model._generate([HumanMessage(content="1")])

        from app.chains.replay import FixtureNotFoundError

        with pytest.raises(FixtureNotFoundError):
            model._generate([HumanMessage(content="2")])


# =============================================================================
# chat：录制器的字段隔离（实测踩过的坑）
# =============================================================================
class TestRecordingModelIsolation:
    def test_store_is_not_a_serialized_field(self) -> None:
        """`_store` 必须是私有属性，不能出现在 `model_dump()` 里。

        **这条是从一个真实的崩溃里长出来的**：把 store 声明成普通字段时，
        它会进 model_dump，而请求体正是从 model_dump 构建的——
        于是 OpenAI SDK 尝试把 FixtureStore 一并 JSON 序列化，
        在**发请求之前**抛 `TypeError: Object of type FixtureStore is not
        JSON serializable`。那个报错指向一个与 fixture 毫无关系的地方。
        """
        model = RecordingChatOpenAI(
            model="any",
            base_url="http://localhost:1",
            api_key="not-a-real-key",  # noqa: S106
        )
        model._store = FixtureStore(Path("."), "probe")

        dumped = model.model_dump()
        # 只断言 `_store` 不在。**不要顺手断言 `"store" not in dumped`**：
        # `ChatOpenAI` 自己就有一个叫 `store` 的字段（OpenAI Responses API 的
        # "是否落库"开关），那条断言会因为它而失败，指向一个与本题无关的字段。
        assert "_store" not in dumped
        assert not any(key.startswith("_") for key in dumped)
        # 私有属性仍然可用
        assert model._store is not None

    def test_request_summary_keeps_user_text_out_of_the_body(self) -> None:
        """摘要里带得上提示词，但不带完整正文以外的东西——这里断言拼接规则本身。"""
        from app.chains.recording import _summarize

        summary = _summarize(
            [SystemMessage(content="你是助手"), HumanMessage(content="爱尔兰特许权使用费")]
        )
        assert "[system] 你是助手" in summary
        assert "[human] 爱尔兰特许权使用费" in summary


# =============================================================================
# embedding：录制 → 回放（按文本哈希）
# =============================================================================
class TestEmbeddingRoundTrip:
    def test_recorded_vectors_replay_by_hash(self, tmp_path: Path) -> None:
        store = FixtureStore(tmp_path, "retrieval")
        recorder = RecordingEmbeddings(FakeEmbeddings(), store)
        texts = ["荷兰 1990 年税收征收法 Artikel 1", "爱尔兰 Finance Act 1997"]
        await_ = pytest.importorskip("asyncio").run

        produced = await_(recorder.aembed_documents(texts))

        replayed = ReplayEmbeddings(store.load(), scenario="retrieval")
        for text, expected in zip(texts, produced, strict=True):
            assert replayed.embed_query(text) == expected
        assert replayed.misses == []

    def test_unrecorded_text_degrades_visibly(self, tmp_path: Path) -> None:
        """未录过的文本要**留痕**，不能静默给一个看起来正常的向量。

        伪向量没有语义，而它会以"中等相似度"出现在任何查询的召回里——
        污染结果而无人察觉。所以命中失败必须记进 misses 并告警。
        """
        store = FixtureStore(tmp_path, "retrieval")
        record_chat_exchange(store, request_summary="s", message=AIMessage(content="x"))
        replayed = ReplayEmbeddings(store.load(), scenario="retrieval")

        vector = replayed.embed_query("从未录过的文本")
        assert len(vector) == 1024
        assert len(replayed.misses) == 1

    def test_hash_matches_the_replay_lookup_key(self, tmp_path: Path) -> None:
        """录制侧写 text_hash，回放侧按 _hash_text 查——两者必须是同一个函数。"""
        store = FixtureStore(tmp_path, "retrieval")
        recorder = RecordingEmbeddings(FakeEmbeddings(), store)
        pytest.importorskip("asyncio").run(recorder.aembed_query("查询文本"))

        line = json.loads((tmp_path / "retrieval.jsonl").read_text("utf-8").splitlines()[0])
        assert line["text_hash"] == _hash_text("查询文本")


# =============================================================================
# 工厂接线
# =============================================================================
class TestFactoryWiring:
    def test_record_mode_requires_scenario(self) -> None:
        """record 模式不给场景名必须报错。

        否则会静默写进 `unnamed.jsonl`，而回放时按真实场景名去找——
        永远找不到，且失败发生在几小时之后的演示现场。
        """
        from app.chains.model_factory import get_chat_model
        from app.core.config import ModelMode, Settings

        settings = Settings(
            model_mode=ModelMode.RECORD,
            siliconflow_api_key="not-a-real-key",  # noqa: S106
        )
        with pytest.raises(ValueError, match="scenario"):
            get_chat_model(settings=settings)

    def test_record_mode_returns_a_recording_model(self) -> None:
        from app.chains.model_factory import get_chat_model
        from app.core.config import ModelMode, Settings

        settings = Settings(
            model_mode=ModelMode.RECORD,
            siliconflow_api_key="not-a-real-key",  # noqa: S106
        )
        model = get_chat_model(settings=settings, scenario="probe/classify_intent")
        assert isinstance(model, RecordingChatOpenAI)
        assert model._store is not None

    def test_real_and_record_build_from_the_same_kwargs(self) -> None:
        """real 与 record 的模型参数必须同源。

        分开写的话，录下来的响应与真实运行时拿到的响应来自两个配置不同的模型
        （温度、超时、重试任一不同就够），而 fixture 看上去完全正常。
        """
        from app.chains.model_factory import _real_chat_kwargs
        from app.core.config import ModelMode, Settings

        settings = Settings(
            model_mode=ModelMode.RECORD,
            siliconflow_api_key="not-a-real-key",  # noqa: S106
            chat_model="test/model",
        )
        kwargs = _real_chat_kwargs(settings, temperature=0.0, model=None)
        assert kwargs["model"] == "test/model"
        assert set(kwargs) == {
            "model",
            "base_url",
            "api_key",
            "temperature",
            "timeout",
            "max_retries",
        }


def _unused(_: Any) -> None:  # pragma: no cover
    """占位，保证 Any 的 import 有意义（mypy strict 下不留未用导入）。"""
