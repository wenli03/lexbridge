"""回放机制：把真实模型响应录制成 fixture，之后离线重放。

**为什么需要它**（决策记录 B-1）：

本项目是作品集项目，交付形态必须满足三个约束，而这三个约束恰好指向同一个方案：

  1. CI 不能花钱，也不能把 API Key 放进 GitHub Secrets
     → 测试与冒烟必须能脱离真实 API 运行
  2. 面试现场不能因网络抖动或模型产品线变动而翻车
     → 演示结果必须确定性可复现
  3. **面试官 clone 仓库后 `docker compose up` 就应该能完整演示，不需要任何密钥**
     → 这是作品集项目最强的加分项之一

回放方案同时满足三者：真实调用一次，把响应（含 tool_calls 结构、embedding 向量、
rerank 分数）录成 JSONL 提交进仓库，之后无限次离线重放。

**关于检索的一个诚实说明**：

回放 embedding 只能命中「录过的文本」。因此演示用的固定问题会被完整录下来，
检索链路可以端到端重放；而用户临时输入的任意问题会命中缓存未命中路径，
退化为确定性伪向量——此时语义检索质量无意义。
这种情况下系统会显式返回降级提示（而非静默给出坏结果）。
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
from pathlib import Path
from typing import Any, cast

from langchain_core.embeddings import Embeddings
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, UsageMetadata
from langchain_core.outputs import ChatGeneration, ChatResult

logger = logging.getLogger(__name__)


class FixtureNotFoundError(RuntimeError):
    """回放时找不到对应记录。

    刻意设计成显式异常而非静默兜底：演示链路一旦与录制时不一致，
    应当立刻炸出来，而不是悄悄返回一个假的响应让排查变得困难。
    """


def _hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]


class FixtureStore:
    """一个场景一个 JSONL 文件。

    用追加写入而非整体重写：录制过程可能中途失败，已录成的部分应当保留。
    """

    def __init__(self, directory: Path, scenario: str) -> None:
        self.directory = Path(directory)
        self.scenario = scenario
        self.path = self.directory / f"{scenario}.jsonl"
        self._lock = threading.Lock()

    def load(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            # 抛 `FixtureNotFoundError` 而不是 `FileNotFoundError`：
            # 「文件不在」与「记录用尽」对调用方是同一件事——你要的回放记录拿不到。
            # 分裂成两种异常时，`except FixtureNotFoundError` 兜不住前者，
            # 于是"fixture 没录"会以 `FileNotFoundError` 的面目冒到上层，
            # 而它看起来像磁盘问题，排查方向一开始就是错的。
            raise FixtureNotFoundError(
                f"回放 fixture 不存在：{self.path}\n"
                f"请先用 MODEL_MODE=real 跑一次该场景以录制，"
                f"或确认 REPLAY_FIXTURE_DIR 指向正确。"
            )
        records: list[dict[str, Any]] = []
        with self.path.open("r", encoding="utf-8") as fh:
            for lineno, line in enumerate(fh, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError as e:
                    raise ValueError(f"fixture 格式损坏 {self.path}:{lineno}: {e}") from e
        return records

    def append(self, record: dict[str, Any]) -> None:
        with self._lock:
            # 建的是 **path 的父目录**，不是 directory 本身。
            # 场景名按详细设计 §7.4 用「图节点路径」（如 `tax_planning/extract_facts`），
            # 于是磁盘上会多一层子目录；只建 directory 的话，
            # 录制会在 `open("a")` 处抛 FileNotFoundError——
            # 而录制失败的表现是"演示时才发现没有 fixture"。
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")


class ReplayChatModel(BaseChatModel):
    """按顺序回放录制的对话响应。

    顺序回放而非按请求哈希匹配：演示路径是固定的，顺序回放更简单也更容易推理。
    代价是**提示词一改，fixture 就会错位**——这是刻意接受的取舍，
    因为错位时会在第一个不匹配处抛错，比静默用错响应容易发现得多。
    """

    records: list[dict[str, Any]] = []
    cursor: int = 0
    scenario: str = "unnamed"

    @property
    def _llm_type(self) -> str:
        return "replay"

    def bind_tools(
        self,
        tools: Any,
        *,
        tool_choice: str | None = None,
        **kwargs: Any,
    ) -> Any:
        """让 replay / mock 模式支持结构化输出。

        **不实现这个方法，两条离线路径就废了一半。**
        `model_factory.structured()` 走的是
        `with_structured_output(method="function_calling")`，它在内部调用 `bind_tools`；
        而 `BaseChatModel.bind_tools` 默认抛 `NotImplementedError`。
        实测（`structured(ReplayChatModel(...), Schema)`）确实直接抛错。

        后果不是"少一个便利方法"，而是：**replay 与 mock 模式下无法做任何结构化抽取**
        ——本体抽取、事实槽位、候选方案生成全都依赖它。而这两条路径恰好是
        CI 与"面试官 clone 下来即可完整演示"所依赖的（决策记录 B-1）。
        真实调用模式下这个问题不存在，所以它只会在离线路径上暴露。

        实现成"记录绑定参数后返回自身"：回放依旧按录制顺序取响应，
        区别只是现在录制的 `tool_calls` 能被 `with_structured_output` 解析。
        """
        return self.bind(tools=tools, tool_choice=tool_choice, **kwargs)

    @property
    def _identifying_params(self) -> dict[str, Any]:
        return {"scenario": self.scenario, "records": len(self.records)}

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        exchanges = [r for r in self.records if r.get("type") == "chat"]
        if self.cursor >= len(exchanges):
            raise FixtureNotFoundError(
                f"场景 {self.scenario!r} 的录制已用尽"
                f"（第 {self.cursor + 1} 次调用，共录 {len(exchanges)} 次）。\n"
                f"通常是提示词或节点顺序改动导致调用次数变化。"
                f"请用 MODEL_MODE=real 重新录制该场景。"
            )
        rec = exchanges[self.cursor]
        self.cursor += 1

        tool_calls = [
            {
                "name": tc["name"],
                "args": tc["args"],
                "id": tc.get("id", f"call_{i}"),
                "type": "tool_call",
            }
            for i, tc in enumerate(rec.get("tool_calls") or [])
        ]
        message = AIMessage(content=rec.get("content") or "", tool_calls=tool_calls)
        # fixture 里的 `usage` 是自由格式 JSON，而 `usage_metadata` 是有键名的
        # TypedDict，因此这里必须做一次断言。用 `cast` 而不是 `# type: ignore`：
        # 它把"我在断言什么"写在代码上。这个信任是有依据的——录制文件由本仓库
        # 的 recorder 写出；若哪天改为回放外部来源的 fixture，这里就是该加校验的地方。
        message.usage_metadata = cast(
            "UsageMetadata",
            rec.get("usage")
            or {
                "input_tokens": 0,
                "output_tokens": 0,
                "total_tokens": 0,
            },
        )
        return ChatResult(generations=[ChatGeneration(message=message)])


class ReplayEmbeddings(Embeddings):
    """按文本哈希回放录制的向量；未命中时退化为确定性伪向量并告警。"""

    def __init__(self, records: list[dict[str, Any]], scenario: str) -> None:
        self.scenario = scenario
        self._by_hash = {
            r["text_hash"]: r["vector"]
            for r in records
            if r.get("type") == "embedding" and r.get("text_hash")
        }
        self.misses: list[str] = []

    def _lookup(self, text: str) -> list[float]:
        vec = self._by_hash.get(_hash_text(text))
        if vec is not None:
            return vec
        self.misses.append(text[:80])
        logger.warning(
            "回放向量未命中（第 %d 次）：%.60s… —— 该文本未在录制时出现过，语义检索对本条无意义。",
            len(self.misses),
            text,
        )
        return _pseudo_vector(text, dim=1024)

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._lookup(t) for t in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._lookup(text)


def _pseudo_vector(text: str, dim: int) -> list[float]:
    """确定性伪向量。仅用于让回放模式不崩，不具任何语义。"""
    digest = hashlib.sha256(text.encode("utf-8")).digest()
    raw = (digest * ((dim // len(digest)) + 1))[:dim]
    # 映射到 [-1, 1] 并归一化，避免检索时出现数值异常
    vec = [(b - 127.5) / 127.5 for b in raw]
    norm = sum(v * v for v in vec) ** 0.5 or 1.0
    return [v / norm for v in vec]


# =============================================================================
# 录制
# =============================================================================
def record_chat_exchange(
    store: FixtureStore,
    *,
    request_summary: str,
    message: AIMessage,
) -> None:
    """把一次真实对话响应写进 fixture。

    `request_summary` 只存摘要不存全文——fixture 会入仓，而提示词里可能带上
    用户案情；摘要够用来定位即可。
    """
    store.append(
        {
            "type": "chat",
            "request_summary": request_summary[:200],
            "content": message.content
            if isinstance(message.content, str)
            else str(message.content),
            "tool_calls": [
                {"name": tc.get("name"), "args": tc.get("args"), "id": tc.get("id")}
                for tc in (getattr(message, "tool_calls", None) or [])
            ],
            "usage": getattr(message, "usage_metadata", None),
        }
    )


def record_embedding(store: FixtureStore, *, text: str, vector: list[float]) -> None:
    store.append(
        {
            "type": "embedding",
            "text_hash": _hash_text(text),
            "text": text[:200],
            "vector": vector,
        }
    )


def record_rerank(
    store: FixtureStore, *, query: str, documents: list[str], results: list[dict[str, Any]]
) -> None:
    store.append(
        {
            "type": "rerank",
            "query_hash": _hash_text(query),
            "query": query[:200],
            "documents": documents,
            "results": results,
        }
    )


class ReplayReranker:
    """回放录制的重排结果。

    与 embedding 的未命中处理策略不同：这里**不做伪分数兜底**，
    而是保留候选的原有顺序。

    理由：重排的伪分数会直接改变最终答案的引用顺序，而顺序是用户可见的。
    宁可退化为「未重排」并在响应里标注降级，也不要给用户一个看起来正常、
    实则随机排序的结果。检索的伪向量只影响召回集合，危害小一档——
    这个区别值得区别对待。
    """

    def __init__(self, records: list[dict[str, Any]]) -> None:
        self._by_query = {
            r["query_hash"]: r["results"]
            for r in records
            if r.get("type") == "rerank" and r.get("query_hash")
        }
        self.misses: list[str] = []

    async def rerank(self, query: str, documents: list[str], top_n: int) -> list[dict[str, Any]]:
        recorded = self._by_query.get(_hash_text(query))
        if recorded is not None:
            return recorded[:top_n]

        self.misses.append(query[:80])
        logger.warning(
            "回放重排未命中（第 %d 次）：%.60s… —— 保留原始顺序，结果未经重排。",
            len(self.misses),
            query,
        )
        # 原序返回，relevance_score 置空以表明「未重排」而非「分数为 0」
        return [
            {"index": i, "relevance_score": None, "document": doc}
            for i, doc in enumerate(documents[:top_n])
        ]
