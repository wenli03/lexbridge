"""模型工厂：real / replay / mock 三模式，同一接口。

**这是本项目最值得说明的一个设计决策**（决策记录 B-1）。

背景：这个项目要交付到公开仓库供面试演示，于是三个约束同时压上来：
CI 不能花钱、演示不能因网络或模型漂移翻车、面试官不该被要求自备密钥。
三者的共同解法是把「模型从哪来」这件事从业务代码里彻底抽走。

所有需要模型的地方一律调用本模块，任何地方都不直接 `ChatOpenAI(...)`。
这样做的直接好处是：`docker compose up` 默认跑 replay，
**clone 下来无需任何密钥即可完整演示**；而把 `MODEL_MODE` 改成 real
就切换到真实调用，业务代码一行不动。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Protocol, TypeVar

from langchain_core.embeddings import Embeddings
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_openai import ChatOpenAI
from pydantic import BaseModel

from app.chains.replay import FixtureStore, ReplayChatModel, ReplayEmbeddings
from app.core.config import ModelMode, Settings, get_settings

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)


# =============================================================================
# Chat
# =============================================================================
def get_chat_model(
    *,
    settings: Settings | None = None,
    scenario: str | None = None,
    temperature: float = 0.0,
    model: str | None = None,
    **kwargs: Any,
) -> BaseChatModel:
    """取得一个 chat 模型。

    `scenario` 只在 replay 模式下有意义：一个场景对应一份录制文件。
    命名建议用「图的节点路径」，例如 `tax_planning/extract_facts`，
    这样 fixture 的粒度与代码结构对得上，改一个节点不会牵连整个场景。
    """
    cfg = settings or get_settings()

    if cfg.model_mode is ModelMode.RECORD:
        if not scenario:
            # 录制没有场景名就无法决定写进哪个文件，会静默写到 unnamed.jsonl，
            # 而回放时按真实场景名去找，永远找不到。
            raise ValueError("record 模式必须提供 scenario，用于决定写入哪个 fixture 文件")
        return _recording_chat_model(cfg, scenario, temperature=temperature, model=model, **kwargs)

    if cfg.model_mode is ModelMode.REAL:
        return _real_chat_model(cfg, temperature=temperature, model=model, **kwargs)

    if cfg.model_mode is ModelMode.REPLAY:
        if not scenario:
            raise ValueError("replay 模式必须提供 scenario，用于定位录制文件")
        store = FixtureStore(_fixture_dir(cfg), scenario)
        records = store.load()
        logger.info("回放模式：场景 %s，已加载 %d 条记录", scenario, len(records))
        return ReplayChatModel(records=records, scenario=scenario)

    # mock —— 复用 ReplayChatModel，只是记录是现场合成的。
    # 复用而非另写一个类，是为了让 mock 与 replay 走完全相同的代码路径：
    # 若 mock 走另一条路径，它就无法证明 replay 路径的正确性。
    return ReplayChatModel(records=_synthetic_records(), scenario="mock")


def _real_chat_kwargs(
    cfg: Settings,
    *,
    temperature: float,
    model: str | None,
    **kwargs: Any,
) -> dict[str, Any]:
    """真实 chat 模型的构造参数。

    **抽成 dict 而不是让两处各写一遍**：`real` 与 `record` 必须构造出
    配置完全一致的模型——否则录制出来的响应和真实运行时拿到的响应
    来自两个不同的模型（温度、超时、重试任一不同就够），
    而 fixture 看上去完全正常，只是"和线上不太一样"。
    """
    return {
        "model": model or cfg.chat_model,
        "base_url": cfg.siliconflow_base_url,
        "api_key": cfg.require_api_key(),
        "temperature": temperature,
        "timeout": cfg.llm_timeout_seconds,
        # 冷启动实测波动 0.7s–65.9s（decision-record.md §3.3），
        # 因此重试不是可选项。SDK 自带的退避覆盖连接层失败，
        # 业务层的 429/5xx 重试在 embed_client 里另做。
        "max_retries": cfg.llm_max_retries,
        **kwargs,
    }


def _real_chat_model(
    cfg: Settings,
    *,
    temperature: float,
    model: str | None,
    **kwargs: Any,
) -> ChatOpenAI:
    return ChatOpenAI(**_real_chat_kwargs(cfg, temperature=temperature, model=model, **kwargs))


def _recording_chat_model(
    cfg: Settings,
    scenario: str,
    *,
    temperature: float,
    model: str | None,
    **kwargs: Any,
) -> BaseChatModel:
    """record 模式：真实调用 + 落盘。

    构造出一个真正的 `ChatOpenAI`（经 `_real_chat_model`），只把类换成会记录的
    子类——这样"录下来的响应"与"real 模式下的响应"必然是同一条代码路径产生的，
    不存在"录制走了一套参数、生产走另一套"的可能。
    """
    from app.chains.recording import RecordingChatOpenAI

    logger.info("录制模式：场景 %s → %s", scenario, _fixture_dir(cfg) / f"{scenario}.jsonl")
    recording = RecordingChatOpenAI(
        **_real_chat_kwargs(cfg, temperature=temperature, model=model, **kwargs)
    )
    # store 是 PrivateAttr，构造后赋值（它不能是普通字段，理由见 recording.py）
    recording._store = FixtureStore(_fixture_dir(cfg), scenario)
    return recording


def _synthetic_records() -> list[dict[str, Any]]:
    """mock 模式的合成响应。

    刻意保持极小：mock 的职责只是让契约测试能在无网络下跑通，
    它不模拟模型的智能。任何需要「像真的」的场合都应该用 replay。
    """
    return [
        {
            "type": "chat",
            "request_summary": "mock",
            "content": "【mock 响应】当前为 mock 模式，未调用真实模型。",
            "tool_calls": [],
            "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
        }
    ]


def _fixture_dir(cfg: Settings) -> Path:
    p = Path(cfg.replay_fixture_dir)
    return p if p.is_absolute() else Path(__file__).resolve().parents[2] / p


# =============================================================================
# 结构化输出
# =============================================================================
def structured(model: BaseChatModel, schema: type[T]) -> Any:
    """取得结构化输出封装。

    **统一走 function_calling，不用 json_schema。**

    实测结论（decision-record.md §2）：两条路在本项目的模型上都可用，
    但选择性来自三点，而不是「json_schema 不可用」这个已被推翻的说法：

      1. function calling 与 LangChain 的 `with_structured_output` 直接对应；
      2. 它能同时表达「调用哪个工具」与「参数结构」，抽取节点需要这个能力
         来区分「抽到了实体」和「抽到了关系」；
      3. 让抽取节点与生成节点走同一条路径，避免两条路径的行为差异
         在联调时变成难查的问题。

    所有需要结构化输出的地方都必须经由此函数，不要各自指定 method。
    """
    return model.with_structured_output(schema, method="function_calling")


# =============================================================================
# Embedding
# =============================================================================
def get_embeddings(
    *,
    settings: Settings | None = None,
    scenario: str | None = None,
) -> Embeddings:
    cfg = settings or get_settings()

    if cfg.model_mode is ModelMode.REPLAY:
        if not scenario:
            raise ValueError("replay 模式必须提供 scenario")
        records = FixtureStore(_fixture_dir(cfg), scenario).load()
        return ReplayEmbeddings(records, scenario=scenario)

    if cfg.model_mode is ModelMode.MOCK:
        return ReplayEmbeddings([], scenario="mock")

    from app.chains.embed_client import SiliconFlowEmbeddings

    real = SiliconFlowEmbeddings(cfg)

    if cfg.model_mode is ModelMode.RECORD:
        if not scenario:
            raise ValueError("record 模式必须提供 scenario，用于决定写入哪个 fixture 文件")
        from app.chains.recording import RecordingEmbeddings

        return RecordingEmbeddings(real, FixtureStore(_fixture_dir(cfg), scenario))

    return real


# =============================================================================
# Rerank
# =============================================================================
class Reranker(Protocol):
    """重排器接口。

    SiliconFlow 的 `/v1/rerank` 不是 OpenAI 标准端点，LangChain 没有内置封装，
    因此自己写一个薄客户端——保持接口一致，换实现时上层无感。

    **写成 `Protocol` 而不是基类**：两个实现（`SiliconFlowReranker`、
    `ReplayReranker`）本来就不继承它，而是各自独立定义同一个方法。
    用基类的话，`get_reranker` 返回它们会被判为类型错误——而它们运行时完全可用，
    于是这个错误只能靠强加一层无意义的继承来消掉。结构化类型描述的是事实：
    "有这个方法就够"，不要求实现者知道这个协议存在。
    """

    async def rerank(
        self, query: str, documents: list[str], top_n: int
    ) -> list[dict[str, Any]]: ...


def get_reranker(*, settings: Settings | None = None, scenario: str | None = None) -> Reranker:
    cfg = settings or get_settings()

    if cfg.model_mode in (ModelMode.REAL, ModelMode.RECORD):
        from app.chains.embed_client import SiliconFlowReranker

        real = SiliconFlowReranker(cfg)
        if cfg.model_mode is ModelMode.RECORD:
            if not scenario:
                raise ValueError("record 模式必须提供 scenario，用于决定写入哪个 fixture 文件")
            from app.chains.recording import RecordingReranker

            return RecordingReranker(real, FixtureStore(_fixture_dir(cfg), scenario))
        return real

    from app.chains.replay import ReplayReranker

    records = (
        FixtureStore(_fixture_dir(cfg), scenario).load()
        if cfg.model_mode is ModelMode.REPLAY and scenario
        else []
    )
    return ReplayReranker(records)
