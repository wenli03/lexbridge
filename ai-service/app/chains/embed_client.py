"""硅基流动的 embedding 与 rerank 客户端。

**参数取值来自实测，不是拍脑袋**（docs/decision-record.md §3）：

  - 批量 128、并发 8 —— 唯一在所有运行中保持 100% 成功率的组合
  - 真实批量上限是 512，不是官方文档说的 32
  - **重试是必需品**：冷启动耗时实测在 0.70s–65.85s 之间波动，
    失败与冷启动状态相关而与并发度不直接相关。同一组参数跑两次，
    一次 384/512 失败、一次 512/512 成功。

最后一条尤其重要：如果按「并发太高被限流」的直觉去调，会得出错误结论
并白白降低吞吐。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx
from langchain_core.embeddings import Embeddings

from app.core.config import Settings

logger = logging.getLogger(__name__)

# 可重试的 HTTP 状态：429 限流、5xx 服务端
_RETRYABLE = {429, 500, 502, 503, 504}


class SiliconFlowError(RuntimeError):
    def __init__(self, status: int, body: str) -> None:
        self.status = status
        self.body = body
        if status == 402:
            hint = (
                "（账户余额不足。这是计费失败而非能力失败——"
                "充值与调用能力是两回事，不要据此判断模型不支持某功能。）"
            )
        else:
            hint = ""
        super().__init__(f"硅基流动返回 HTTP {status}: {body[:300]}{hint}")


async def _post_with_retry(
    client: httpx.AsyncClient,
    path: str,
    payload: dict[str, Any],
    *,
    max_retries: int,
    semaphore: asyncio.Semaphore,
) -> dict[str, Any]:
    """带指数退避的 POST。退避上限 30s，避免冷启动期把队列拖死。"""
    delay = 1.0
    last: Exception | None = None

    async with semaphore:
        for attempt in range(max_retries + 1):
            try:
                resp = await client.post(path, json=payload)
                if resp.status_code == 200:
                    return resp.json()
                if resp.status_code in _RETRYABLE and attempt < max_retries:
                    logger.warning(
                        "硅基流动 %s 返回 %d，%.1fs 后重试（%d/%d）",
                        path,
                        resp.status_code,
                        delay,
                        attempt + 1,
                        max_retries,
                    )
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, 30.0)
                    continue
                raise SiliconFlowError(resp.status_code, resp.text)
            except (httpx.TimeoutException, httpx.TransportError) as e:
                last = e
                if attempt < max_retries:
                    logger.warning(
                        "硅基流动 %s 网络异常 %s，%.1fs 后重试（%d/%d）",
                        path,
                        type(e).__name__,
                        delay,
                        attempt + 1,
                        max_retries,
                    )
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, 30.0)
                    continue
                raise
    raise last or RuntimeError("重试逻辑异常退出")


class SiliconFlowEmbeddings(Embeddings):
    """批量 + 并发 + 退避的 embedding 客户端。"""

    def __init__(self, settings: Settings) -> None:
        self.cfg = settings
        self._client: httpx.AsyncClient | None = None
        self._semaphore = asyncio.Semaphore(settings.embedding_concurrency)

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.cfg.siliconflow_base_url,
                headers={"Authorization": f"Bearer {self.cfg.require_api_key()}"},
                timeout=self.cfg.llm_timeout_seconds,
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def warmup(self) -> None:
        """预热。

        冷启动实测 0.70s–65.85s。不预热的话，入库流水线的第一批会慢得离谱，
        而且更容易失败。在开始批量之前调一次，成本一条文本。
        """
        try:
            await self._embed_one_batch(["预热"])
            logger.info("embedding 模型预热完成")
        except Exception as e:  # 预热失败不应阻断启动
            logger.warning("embedding 预热失败（不阻断）：%s", e)

    async def _embed_one_batch(self, texts: list[str]) -> list[list[float]]:
        body = await _post_with_retry(
            self._get_client(),
            "/embeddings",
            {
                "model": self.cfg.embedding_model,
                "input": texts,
                "dimensions": self.cfg.embedding_dim,
            },
            max_retries=self.cfg.llm_max_retries,
            semaphore=self._semaphore,
        )
        data = sorted(body.get("data") or [], key=lambda d: d.get("index", 0))
        return [d["embedding"] for d in data]

    async def aembed_documents(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []

        batch = self.cfg.embedding_batch_size
        batches = [texts[i : i + batch] for i in range(0, len(texts), batch)]
        logger.info(
            "embedding：%d 条 → %d 批（每批 %d，并发 %d）",
            len(texts),
            len(batches),
            batch,
            self.cfg.embedding_concurrency,
        )

        results = await asyncio.gather(
            *(self._embed_one_batch(b) for b in batches), return_exceptions=True
        )

        vectors: list[list[float]] = []
        # 捕获 `BaseException` 而不是 `Exception`：`asyncio.gather` 在任务被取消时
        # 返回的是 `CancelledError`，它不是 `Exception` 的子类。只挡 `Exception`
        # 会让取消走进 `else` 分支、被当成一批向量去 `extend`——
        # 报出来的错是"list 不能 extend CancelledError"，与真正的原因（请求被取消）
        # 毫无关系，排查方向一开始就是错的。
        errors: list[BaseException] = []
        for r in results:
            if isinstance(r, BaseException):
                errors.append(r)
            else:
                vectors.extend(r)

        if errors:
            # 部分失败时明确抛出，不返回长度不符的结果——
            # 静默的长度错位会在写入向量表时表现为难以定位的错乱。
            raise RuntimeError(
                f"{len(errors)}/{len(batches)} 个 batch 失败，首个错误：{errors[0]}"
            ) from errors[0]

        if len(vectors) != len(texts):
            raise RuntimeError(f"返回 {len(vectors)} 条向量，期望 {len(texts)} 条")
        return vectors

    async def aembed_query(self, text: str) -> list[float]:
        vectors = await self._embed_one_batch([text])
        return vectors[0]

    # --- LangChain 同步接口 ------------------------------------------------
    # 流水线全程异步，这两个只在同步上下文（如脚本）里用。
    # 在已运行的事件循环里调用会抛错，这是刻意的：静默地起新循环更容易出问题。
    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return _run_sync(self.aembed_documents(texts))

    def embed_query(self, text: str) -> list[float]:
        return _run_sync(self.aembed_query(text))


class SiliconFlowReranker:
    """`/v1/rerank` 薄客户端。返回结果按 relevance_score 降序。"""

    def __init__(self, settings: Settings) -> None:
        self.cfg = settings
        self._client: httpx.AsyncClient | None = None
        self._semaphore = asyncio.Semaphore(settings.embedding_concurrency)

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.cfg.siliconflow_base_url,
                headers={"Authorization": f"Bearer {self.cfg.require_api_key()}"},
                timeout=self.cfg.llm_timeout_seconds,
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def rerank(
        self, query: str, documents: list[str], top_n: int = 10
    ) -> list[dict[str, Any]]:
        if not documents:
            return []
        body = await _post_with_retry(
            self._get_client(),
            "/rerank",
            {
                "model": self.cfg.rerank_model,
                "query": query,
                "documents": documents,
                "top_n": min(top_n, len(documents)),
            },
            max_retries=self.cfg.llm_max_retries,
            semaphore=self._semaphore,
        )
        results = body.get("results") or []
        results.sort(key=lambda r: r.get("relevance_score", 0.0), reverse=True)
        return results


def _run_sync(coro: Any) -> Any:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    raise RuntimeError("在已运行的事件循环中调用了同步接口。流水线应统一使用 aembed_* 异步方法。")
