"""录制：把真实模型响应写进 fixture，供之后离线回放。

## 为什么单独一个模块

`replay.py` 负责**读**，本模块负责**写**，两者共用 `FixtureStore` 与
`record_*` 的落盘格式。放在一起会让"回放"这个词同时承担两个方向的职责，
而它们的失败方式完全不同：回放失败是"找不到记录"，录制失败是"记录写坏了"。

## 三种录制对象

模型侧的调用有三类，各自需要录的东西不一样：

| 调用 | 录制内容 | 回放时怎么用 |
| --- | --- | --- |
| chat | 响应文本、`tool_calls`、用量 | 按**顺序**回放（演示路径固定） |
| embedding | 文本 → 向量 | 按**文本哈希**查表 |
| rerank | 查询 → 排序结果 | 按**查询哈希**查表 |

chat 用顺序、embedding/rerank 用哈希，这个不对称是有意的：
图里的节点执行顺序是确定的，而检索会因语料而访问任意文本，
没法用顺序表达。

## 实现方式：包装而不是子类

`RecordingChatOpenAI` 继承 `ChatOpenAI`，另外两个是纯包装。
**继承 chat 而不是包一层 `BaseChatModel`**，是因为 `with_structured_output`
会走 `bind_tools` → `bind()` → 内部生成路径；自己实现一个 `BaseChatModel`
需要把 `bind_tools`、`_generate`、`_agenerate`、参数透传全部对齐一遍，
任何一处漏掉都会让结构化抽取在这条路径上静默失效——
而它恰好是抽取类节点的唯一入口。继承则天然保留全部行为，
只需要在生成返回后多写一行记录。
"""

from __future__ import annotations

import logging
from typing import Any

from langchain_core.embeddings import Embeddings
from langchain_core.messages import BaseMessage
from langchain_openai import ChatOpenAI
from pydantic import PrivateAttr

from app.chains.replay import FixtureStore, record_chat_exchange, record_embedding, record_rerank

logger = logging.getLogger(__name__)


def _summarize(messages: list[BaseMessage]) -> str:
    """给请求留一句摘要。

    只存摘要不存全文：fixture 会入仓，而提示词里可能带上用户的真实案情。
    摘要的用途是在回放出问题时定位"这是第几次调用、问的是什么"。
    """
    from langchain_core.messages import HumanMessage, SystemMessage

    parts: list[str] = []
    for message in messages:
        if isinstance(message, SystemMessage):
            parts.append(f"[system] {message.content}")
        elif isinstance(message, HumanMessage):
            parts.append(f"[human] {message.content}")
    return " / ".join(str(p) for p in parts)


class RecordingChatOpenAI(ChatOpenAI):
    """真实调用硅基流动，同时把每次响应写进 fixture。

    **同步与异步都要覆写。** 图节点走的是 `ainvoke`（`_agenerate`），
    但 `embed_client` 与部分工具走同步路径；只覆写一个的话，
    另一条路径下的调用不会被录进 fixture，而回放时会在"记录用尽"处报错——
    那个错误指向提示词改动，与真实原因（少录了一条）毫无关系。
    """

    # **必须是 PrivateAttr，不能是普通字段。**
    #
    # `ChatOpenAI` 是 pydantic 模型，普通字段会被纳入 `model_dump()`，
    # 而请求体正是从它构建的——于是 OpenAI SDK 会尝试把 store 一并 JSON 序列化，
    # 报 `TypeError: Object of type FixtureStore is not JSON serializable`。
    # 这个报错发生在**发请求之前**，指向一个与 fixture 毫无关系的地方。
    # 私有属性不参与序列化，也不会被带进请求体。
    _store: Any = PrivateAttr(default=None)

    def _record(self, messages: list[BaseMessage], result: Any) -> None:
        if self._store is None:
            return
        generations = getattr(result, "generations", None) or []
        if not generations:
            return
        record_chat_exchange(
            self._store,
            request_summary=_summarize(messages),
            message=generations[0].message,
        )

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> Any:
        result = super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)
        self._record(messages, result)
        return result

    async def _agenerate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> Any:
        result = await super()._agenerate(messages, stop=stop, run_manager=run_manager, **kwargs)
        self._record(messages, result)
        return result


class RecordingEmbeddings(Embeddings):
    """真实调用 embedding 接口，同时按文本哈希记录向量。"""

    def __init__(self, inner: Embeddings, store: FixtureStore) -> None:
        self._inner = inner
        self._store = store

    async def aembed_documents(self, texts: list[str]) -> list[list[float]]:
        vectors = await self._inner.aembed_documents(texts)
        for text, vector in zip(texts, vectors, strict=True):
            record_embedding(self._store, text=text, vector=vector)
        return vectors

    async def aembed_query(self, text: str) -> list[float]:
        vector = await self._inner.aembed_query(text)
        record_embedding(self._store, text=text, vector=vector)
        return vector

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        vectors = self._inner.embed_documents(texts)
        for text, vector in zip(texts, vectors, strict=True):
            record_embedding(self._store, text=text, vector=vector)
        return vectors

    def embed_query(self, text: str) -> list[float]:
        vector = self._inner.embed_query(text)
        record_embedding(self._store, text=text, vector=vector)
        return vector


class RecordingReranker:
    """真实调用 rerank 接口，同时按查询哈希记录排序结果。"""

    def __init__(self, inner: Any, store: FixtureStore) -> None:
        self._inner = inner
        self._store = store

    async def rerank(self, query: str, documents: list[str], top_n: int) -> list[dict[str, Any]]:
        results = await self._inner.rerank(query, documents, top_n)
        record_rerank(self._store, query=query, documents=documents, results=results)
        return results
