"""LangSmith 装配。

对应 PRD 的 AC-6.1（节点级 / 模型调用级 / 检索级 trace 覆盖率 100%）。

**关键点：用环境变量而不是代码装配。** LangChain 与 LangGraph 只要检测到
`LANGSMITH_TRACING=true` 就会自动上报，无需在代码里包任何装饰器。
这意味着图里的每个节点、每次模型调用天然带 trace——覆盖率是"默认就有"的，
而不是靠开发者记得加。

**一个容易踩的坑**：变量名是 `LANGSMITH_TRACING`，
不是旧版文档里的 `LANGCHAIN_TRACING_V2`。用错名字不会有任何报错，
只是 trace 静默消失——这类问题最难查，因此在此显式说明。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any, Literal, TypeVar, cast

from app.core.config import Settings

logger = logging.getLogger(__name__)

#: 这里把 run_type 的取值范围写死，而不是从 langsmith 内部导入它的同名类型。
#: 那个类型住在 `langsmith.client` 里、无稳定性承诺，跟着它改名会让升级变成一个
#: 与业务无关的编译错误。取值范围本身是 LangSmith 的公开 API 面，变化只会是新增；
#: 真的新增时，这里"没提供"是安全的降级，而不是错误。
RunType = Literal["tool", "chain", "llm", "retriever", "embedding", "prompt", "parser"]

#: 被装饰的函数。保留原签名——装饰器不该改变调用方看到的类型。
_F = TypeVar("_F", bound=Callable[..., Any])


def configure_langsmith(settings: Settings) -> bool:
    """把配置写进环境变量。返回是否启用了追踪。

    必须在导入 langchain 相关模块**之前**调用才最稳妥，
    因此放在应用启动的最前面。
    """
    import os

    if not settings.langsmith_tracing:
        # 显式关闭，避免宿主机上残留的变量意外打开
        os.environ["LANGSMITH_TRACING"] = "false"
        logger.info("LangSmith 追踪未启用")
        return False

    if not settings.langsmith_api_key:
        logger.warning(
            "LANGSMITH_TRACING=true 但 LANGSMITH_API_KEY 为空，追踪将被跳过。"
            "这不会影响服务功能，但 AC-6.1 的 trace 覆盖率无法验收。"
        )
        return False

    os.environ["LANGSMITH_TRACING"] = "true"
    os.environ["LANGSMITH_API_KEY"] = settings.langsmith_api_key
    os.environ["LANGSMITH_PROJECT"] = settings.langsmith_project
    os.environ["LANGSMITH_ENDPOINT"] = settings.langsmith_endpoint
    logger.info("LangSmith 追踪已启用，项目=%s", settings.langsmith_project)
    return True


def traced(name: str, run_type: RunType = "chain") -> Callable[[_F], _F]:
    """检索函数的 trace 装饰器。

    检索用 `run_type="tool"`：这样命中的法条 ID 与相似度会作为 tool span 的属性
    出现在 trace 里，验收 AC-6.1 的"检索级"要求时可以直接看到。

    未安装 langsmith 时退化为无操作装饰器，保证服务在无该依赖时仍可运行。
    """
    try:
        from langsmith import traceable
    except ImportError:  # pragma: no cover

        def _noop(fn: _F) -> _F:
            return fn

        return _noop

    # langsmith 把自己的装饰器标注成返回 `SupportsLangsmithExtra`（它额外挂了
    # `.with_config` 等属性），因此与"原样返回同一个签名"不兼容。运行时被装饰的
    # 函数确实保留原签名，所以这里的 cast 是把 stub 的过度收紧说回实话，
    # 而不是在掩盖类型错误——用 `# type: ignore` 会让真正的签名变化也被吞掉。
    return cast("Callable[[_F], _F]", traceable(run_type=run_type, name=name))
