#!/usr/bin/env python3
"""LangSmith trace 覆盖冒烟（对应 AC-6.1）。

AC-6.1 要求**节点级 / 模型调用级 / 检索级** trace 覆盖率 100%。
本脚本不满足于"配置了环境变量所以应该在上报"这种推断，
而是**跑一次图、然后把 trace 查回来**——只有查得到才算数。

三个容易静默失效的点，本脚本逐一对付：

  1. **变量名**。当前是 `LANGSMITH_TRACING`，旧文档里的
     `LANGCHAIN_TRACING_V2` 已经不用了。名字写错不会有任何报错，
     trace 只是安静地不出现——这类问题最难查。
  2. **端点尾斜杠**。`LANGSMITH_ENDPOINT` 末尾多一个 `/` 会让上报失败且无提示。
     `app/core/config.py` 里已有 validator 自动去掉，本脚本也会复核。
  3. **检索函数的 span 类型**。检索必须标成 `run_type="tool"`，
     这样命中的法条 ID 与相似度才会作为属性挂上去。
     若标成默认的 `chain`，trace 里就看不到检索细节，AC-6.1 的"检索级"无从验收。

用法：
    LANGSMITH_API_KEY=lsv_xxx python scripts/smoke_langsmith.py
未配置密钥时优雅跳过（返回 0）——因为缺 LangSmith 不影响系统功能本身。
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path
from typing import Any, TypedDict

_AI_SERVICE = Path(__file__).resolve().parents[1] / "ai-service"
if str(_AI_SERVICE) not in sys.path:
    sys.path.insert(0, str(_AI_SERVICE))


def _load_deploy_env() -> None:
    env_path = Path(__file__).resolve().parents[1] / "deploy" / ".env"
    if not env_path.exists():
        return
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if value:
            os.environ[key] = value


_load_deploy_env()

PROJECT = os.environ.get("LANGSMITH_PROJECT", "lexbridge")
# 本次冒烟用独立的 project 名，避免与真实业务 trace 混在一起难以辨别
SMOKE_PROJECT = f"{PROJECT}-smoke"
QUERY_TIMEOUT_S = float(os.environ.get("LANGSMITH_QUERY_TIMEOUT", "60"))


class ProbeState(TypedDict, total=False):
    query: str
    hits: list[str]
    answer: str


def main() -> int:
    print("=" * 74)
    print("LangSmith trace 覆盖冒烟（AC-6.1）")
    print("=" * 74)

    key = os.environ.get("LANGSMITH_API_KEY", "")
    endpoint = os.environ.get("LANGSMITH_ENDPOINT", "")
    print(f"  project  : {SMOKE_PROJECT}")
    print(f"  endpoint : {endpoint or '(默认)'}")

    if not key:
        print("\n[SKIP] 未配置 LANGSMITH_API_KEY。")
        print("       这不影响系统功能——追踪是观测能力，不是运行前提。")
        print("       但 AC-6.1 的 trace 覆盖率因此**无法验收**。")
        print("       配置后重跑本脚本即可。")
        return 0

    if endpoint.endswith("/"):
        print("\n⚠️  LANGSMITH_ENDPOINT 末尾有斜杠，会导致上报静默失败。")
        print("    app/core/config.py 的 validator 会自动修掉已加载的配置，")
        print("    但环境变量本身仍建议改正。")

    # ------------------------------------------------------------------
    # 1. 打开追踪
    # ------------------------------------------------------------------
    os.environ["LANGSMITH_TRACING"] = "true"
    os.environ["LANGSMITH_API_KEY"] = key
    os.environ["LANGSMITH_PROJECT"] = SMOKE_PROJECT
    if endpoint:
        os.environ["LANGSMITH_ENDPOINT"] = endpoint.rstrip("/")

    from app.observability.langsmith import configure_langsmith, traced
    from app.core.config import get_settings

    settings = get_settings()
    settings.langsmith_tracing = True
    settings.langsmith_project = SMOKE_PROJECT
    enabled = configure_langsmith(settings)
    print(f"\n[1/4] configure_langsmith -> {'已启用' if enabled else '未启用'}")
    if not enabled:
        print("      ❌ 启用失败，后续检查无意义")
        return 1

    # ------------------------------------------------------------------
    # 2. 跑一个带「检索」的图
    # ------------------------------------------------------------------
    print("\n[2/4] 运行探针图（含一个标记为 tool 的检索函数）")

    @traced("probe_retrieval", run_type="tool")
    def fake_retrieval(query: str) -> list[str]:
        """模拟检索。标成 tool 是为了让命中法条 ID 与相似度进入 trace。

        真实实现应在此处记录命中法条的 article_id 与相似度分数——
        只写 `return [...]` 而没有任何可观测属性，trace 里就只剩一个空 span，
        AC-6.1 的"检索级"要求等于没满足。
        """
        from langsmith import get_current_run_tree

        hits = ["SG-ITAA-13", "SG-ITAA-14"]
        run = get_current_run_tree()
        if run is not None:
            # 这些属性会出现在 trace 详情里，验收"检索级"看的就是它们
            run.add_metadata({"hits": hits, "scores": [0.91, 0.87], "top_k": 2})
        return hits

    async def probe_graph() -> dict[str, Any]:
        from langgraph.graph import END, START, StateGraph

        def node_retrieve(state: ProbeState) -> ProbeState:
            return {"hits": fake_retrieval(state.get("query", ""))}

        def node_answer(state: ProbeState) -> ProbeState:
            return {"answer": f"依据 {len(state.get('hits', []))} 条法条作答"}

        g = StateGraph(ProbeState)
        g.add_node("retrieve", node_retrieve)
        g.add_node("answer", node_answer)
        g.add_edge(START, "retrieve")
        g.add_edge("retrieve", "answer")
        g.add_edge("answer", END)
        # 本轮不挂 checkpointer：这里只验证 trace，不验证持久化
        graph = g.compile()
        return await graph.ainvoke({"query": "新加坡企业所得税税率"})

    result = asyncio.run(probe_graph())
    print(f"      图执行完成：{result}")

    # 上报是异步的，给它时间
    print("      等待 trace 上报…")

    # ------------------------------------------------------------------
    # 3. 把 trace 查回来
    # ------------------------------------------------------------------
    print("\n[3/4] 从 LangSmith 查询刚产生的 trace")
    runs: list[Any] = []
    deadline = time.time() + QUERY_TIMEOUT_S
    try:
        from langsmith import Client

        client = Client()
        while time.time() < deadline:
            try:
                runs = list(client.list_runs(project_name=SMOKE_PROJECT, limit=20))
            except Exception as e:
                print(f"      查询异常（将继续重试）：{type(e).__name__}: {str(e)[:120]}")
                runs = []
            if runs:
                break
            time.sleep(3)
    except Exception as e:
        print(f"      ❌ 无法创建 LangSmith 客户端：{type(e).__name__}: {str(e)[:200]}")
        return 1

    if not runs:
        print(f"      ❌ {QUERY_TIMEOUT_S:.0f}s 内未查到任何 run。")
        print("      可能原因：密钥无效 / 端点不对 / 项目名不符 / 网络不通。")
        print("      注意 trace 是异步上报的，刚跑完可能需要等几秒。")
        return 1

    print(f"      查到 {len(runs)} 个 run：")
    kinds: dict[str, int] = {}
    for r in runs:
        rt = getattr(r, "run_type", "?")
        kinds[rt] = kinds.get(rt, 0) + 1
        name = getattr(r, "name", "?")
        print(f"        - {rt:<8} {name}")
    print(f"      类型分布：{kinds}")

    # ------------------------------------------------------------------
    # 4. 判定三层覆盖
    # ------------------------------------------------------------------
    print("\n[4/4] 判定 trace 覆盖层次")
    conclusions: list[tuple[str, bool, str]] = []

    has_chain = any(getattr(r, "run_type", "") in ("chain", "graph") for r in runs)
    conclusions.append((
        "节点级 trace（链/图）", has_chain,
        f"有 {kinds.get('chain', 0) + kinds.get('graph', 0)} 条" if has_chain
        else "未发现链级 run",
    ))

    has_tool = any(getattr(r, "run_type", "") == "tool" for r in runs)
    conclusions.append((
        "检索级 trace（tool 类型）", has_tool,
        f"有 {kinds.get('tool', 0)} 条" if has_tool
        else "未发现 tool 类型 run —— 检索函数可能没标 run_type='tool'",
    ))

    # 模型调用级：本次探针图不含模型调用，如实说明而不是假装覆盖
    has_llm = any(getattr(r, "run_type", "") == "llm" for r in runs)
    print(f"  [—] 模型调用级 trace：本次探针图不含模型调用，"
          f"{'但已观察到 llm run' if has_llm else '故不作判定'}")

    print()
    for name, ok, detail in conclusions:
        print(f"  [{'✅' if ok else '❌'}] {name}: {detail}")

    print(f"\n  项目地址：{os.environ.get('LANGSMITH_ENDPOINT', 'https://api.smith.langchain.com')}"
          f"/projects/p/{SMOKE_PROJECT}")

    return 0 if all(ok for _, ok, _ in conclusions) else 1


if __name__ == "__main__":
    sys.exit(main())
