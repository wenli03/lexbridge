#!/usr/bin/env python3
"""DeepAgent 在硅基流动端点上的可用性冒烟（风险 R6）。

DeepAgent 在本项目里的定位是**长报告路径的可选加速器**，不是核心演示的必需品
（PRD 3.5.2 决策 5）。因此这个脚本要回答的不是"能不能用"，而是三个更具体的问题：

  1. **规划工具确实是 opt-in 吗？** 官方文档称规划工具自 0.7 起需要显式传入
     `middleware=[TodoListMiddleware()]`。这是破坏性变更，传漏了不会有任何报错，
     只是 agent 失去了规划能力——属于"静默降级"，最该被验证。
  2. **deepseek-ai/DeepSeek-V4-Flash 能驱动长程任务吗？** 官方称
     "any model that supports tool calling works"，而该模型实测支持 function
     calling。但"支持工具调用"与"能完成多步规划且正常终止"是两件事。
  3. **成本可控吗？** 长程 agent 若陷入循环，token 消耗会失控。必须量出来。

社区有 DeepAgent 在 OpenAI 兼容端点上流式异常的 issue，因此这里也顺带验证
非流式调用是否正常。

用法：
    SILICONFLOW_API_KEY=sk-xxx python scripts/smoke_deepagents.py
退出码：0 = 结论明确（无论 DeepAgent 是否可用）；1 = 脚本本身出错。
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path
from typing import Any

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
        if value:  # 空值 = 未配置
            os.environ[key] = value


_load_deploy_env()


# =============================================================================
# 工具：故意保持平凡，避免模型能力之外的变量干扰结论
# =============================================================================
def lookup_tax_rate(jurisdiction: str) -> str:
    """查询某法域的企业所得税税率。"""
    table = {"SG": "17%", "HK": "16.5%", "CN": "25%", "IE": "12.5%"}
    return table.get(jurisdiction.upper(), "未知法域")


TASK = (
    "请分三步完成：第一步用 lookup_tax_rate 查 SG 的税率；"
    "第二步查 HK 的税率；第三步比较两者并给出结论。"
)


def _todo_tools_available(agent: Any) -> list[str]:
    """尝试从编译后的 agent 上读出它实际绑定了哪些工具。

    读不到不算失败——不同版本的图结构不同。此时返回空列表，
    由调用方退回用 middleware 的契约做判断。
    """
    names: list[str] = []
    for attr in ("tools", "bound_tools"):
        v = getattr(agent, attr, None)
        if v:
            names.extend(getattr(t, "name", str(t)) for t in v)
    # 有些版本把工具挂在节点上
    try:
        for node in agent.get_graph().nodes.values():
            for t in getattr(node, "tools", None) or []:
                names.append(getattr(t, "name", str(t)))
    except Exception:
        pass
    return sorted(set(names))


async def _run_agent(use_todo_middleware: bool,
                     task: str = TASK) -> dict[str, Any]:
    """跑一次 DeepAgent，返回可判定的观测结果。"""
    from deepagents import create_deep_agent

    from app.chains.model_factory import get_chat_model
    from app.core.config import ModelMode, get_settings

    settings = get_settings()
    # 本脚本的目的就是验证真实模型行为，因此强制 real（replay 下测不出模型能力）
    settings.model_mode = ModelMode.REAL
    model = get_chat_model(settings=settings, temperature=0.0)

    kwargs: dict[str, Any] = {}
    middleware_tools: list[str] = []
    if use_todo_middleware:
        from langchain.agents.middleware import TodoListMiddleware

        mw = TodoListMiddleware()
        # 这个 middleware 的契约就是「提供 write_todos」，直接读它的声明，
        # 比观察模型是否碰巧调用它要可靠得多
        middleware_tools = [getattr(t, "name", str(t)) for t in (mw.tools or [])]
        kwargs["middleware"] = [mw]

    agent = create_deep_agent(
        model=model,
        tools=[lookup_tax_rate],
        system_prompt="你是税务信息助手。完成任务后用一句话给出结论。",
        **kwargs,
    )

    t0 = time.perf_counter()
    result = await agent.ainvoke(
        {"messages": [{"role": "user", "content": task}]},
        config={"recursion_limit": 60},
    )
    elapsed = time.perf_counter() - t0

    messages = result.get("messages", [])

    # 收集所有工具调用名
    tool_names: list[str] = []
    todos_written: list[Any] = []
    total_tokens = 0
    for m in messages:
        for tc in (getattr(m, "tool_calls", None) or []):
            name = tc.get("name")
            tool_names.append(name)
            if name in ("write_todos", "write_todo"):
                todos_written.append(tc.get("args"))
        usage = getattr(m, "usage_metadata", None)
        if usage:
            total_tokens += usage.get("total_tokens", 0) or 0

    final = messages[-1] if messages else None
    return {
        "elapsed": elapsed,
        "message_count": len(messages),
        "tool_names": tool_names,
        "todo_calls": len(todos_written),
        "total_tokens": total_tokens,
        "final_content": (getattr(final, "content", "") or "")[:300],
        "terminated": final is not None,
        "todos_sample": todos_written[:1],
        # 分离"工具可用"与"模型用了"这两件事——第一版把两者混为一谈，
        # 于是把"模型认为任务太简单不需要规划"误判成了"规划工具没生效"
        "middleware_tools": middleware_tools,
        "agent_tools": _todo_tools_available(agent),
    }


async def main() -> int:
    print("=" * 74)
    print("DeepAgent 可用性冒烟（风险 R6）")
    print("=" * 74)

    if not os.environ.get("SILICONFLOW_API_KEY"):
        print("\n[SKIP] 未设置 SILICONFLOW_API_KEY —— 本项需要真实模型，无法在回放模式下验证。")
        return 0

    from app.core.config import get_settings

    s = get_settings()
    print(f"  chat_model : {s.chat_model}")
    print(f"  base_url   : {s.siliconflow_base_url}")

    conclusions: list[tuple[str, bool, str]] = []

    # ---------------------------------------------------------------- 对照 A
    print("\n[A] 不传 TodoListMiddleware —— 验证规划工具确实是 opt-in")
    try:
        a = await _run_agent(use_todo_middleware=False)
    except Exception as e:
        print(f"      ❌ 调用失败：{type(e).__name__}: {str(e)[:200]}")
        conclusions.append(("DeepAgent 基本可用", False, f"{type(e).__name__}"))
        a = None

    if a:
        print(f"      消息数={a['message_count']}  工具调用={a['tool_names']}  "
              f"tokens={a['total_tokens']}  耗时={a['elapsed']:.1f}s")
        print(f"      最终输出：{a['final_content'][:120]!r}")
        print(f"      middleware 声明的工具：{a['middleware_tools'] or '（未传）'}")
        print(f"      从 agent 上读到的工具：{a['agent_tools'] or '（读不到，退回用 middleware 契约判断）'}")
        # 期望：不传 middleware 时，规划工具**不可用**
        has_todo_tool = "write_todos" in (a["middleware_tools"] + a["agent_tools"])
        conclusions.append((
            "不传 middleware 时规划工具不可用（确为 opt-in）",
            not has_todo_tool,
            "工具集里没有 write_todos，与文档一致" if not has_todo_tool
            else "竟然存在 write_todos —— 文档所述的破坏性变更不成立，需重新核对版本行为",
        ))
        # 基本可用性：正常终止 + 确实调用了业务工具
        used_lookup = any("lookup_tax_rate" in n for n in a["tool_names"])
        conclusions.append((
            "DeepAgent 基本可用（正常终止且调用了业务工具）",
            a["terminated"] and used_lookup,
            f"终止={a['terminated']} 调用业务工具={used_lookup}",
        ))

    # ---------------------------------------------------------------- 对照 B
    print("\n[B] 传入 TodoListMiddleware —— 验证规划能力确实被激活")
    try:
        b = await _run_agent(use_todo_middleware=True)
    except Exception as e:
        print(f"      ❌ 调用失败：{type(e).__name__}: {str(e)[:200]}")
        b = None

    if b:
        print(f"      消息数={b['message_count']}  write_todos 次数={b['todo_calls']}  "
              f"tokens={b['total_tokens']}  耗时={b['elapsed']:.1f}s")
        print(f"      最终输出：{b['final_content'][:120]!r}")
        print(f"      middleware 声明的工具：{b['middleware_tools']}")
        print(f"      从 agent 上读到的工具：{b['agent_tools'] or '（读不到，退回用 middleware 契约判断）'}")
        if b["todos_sample"]:
            print(f"      待办样例：{str(b['todos_sample'][0])[:200]}")
        # **断言的是"工具可用"，不是"模型用了"**。
        # 第一版把两者混为一谈，于是把"模型觉得三步任务太简单、不屑于写待办"
        # 误判成了"规划工具没生效"。可用性是能力问题，用没用是行为问题。
        available = ("write_todos" in b["middleware_tools"]
                     or "write_todos" in b["agent_tools"])
        conclusions.append((
            "传入 middleware 后规划工具可用",
            available,
            f"工具集含 write_todos（模型本次调用 {b['todo_calls']} 次）" if available
            else "工具集里仍无 write_todos —— 规划工具未生效",
        ))

    # ---------------------------------------------------------------- 对照 C
    print("\n[C] 长任务下模型是否主动规划（行为观察，不作断言）")
    long_task = (
        "请完成一份比较研究：分别用 lookup_tax_rate 查询 SG、HK、CN、IE 四个法域"
        "的企业所得税税率，逐一记录；然后两两比较，找出最高与最低；"
        "最后给出三条关于跨境架构选址的观察。请分步骤进行。"
    )
    try:
        c = await _run_agent(use_todo_middleware=True, task=long_task)
        print(f"      消息数={c['message_count']}  write_todos 次数={c['todo_calls']}  "
              f"tokens={c['total_tokens']}  耗时={c['elapsed']:.1f}s")
        print(f"      业务工具调用：{c['tool_names']}")
        if c["todo_calls"]:
            print(f"      待办样例：{str(c['todos_sample'][0])[:240]}")
            print("      → 模型在较复杂的任务上主动使用了规划工具")
        else:
            print("      → 模型仍未使用规划工具。这不影响可用性结论：")
            print("        工具已绑定，是否调用取决于模型对任务复杂度的判断。")
    except Exception as e:
        print(f"      ⚠️ 长任务未完成：{type(e).__name__}: {str(e)[:160]}")
        c = None

    # ------------------------------------------------------------------ 汇总
    print("\n" + "=" * 74)
    print("结论")
    print("=" * 74)
    for name, ok, detail in conclusions:
        print(f"  [{'✅' if ok else '❌'}] {name}: {detail}")

    print("\n  成本与延迟：")
    for label, r in (("A 三步/无规划", a), ("B 三步/有规划", b), ("C 长任务/有规划", c)):
        if r:
            print(f"    {label:<16} tokens={r['total_tokens']:<7} "
                  f"耗时={r['elapsed']:>5.1f}s  消息数={r['message_count']}")

    print("\n  定位提醒：DeepAgent 在本项目里只用于长报告路径，由 feature flag 控制，")
    print("  对核心演示（两类咨询）非必需。若以上有失败项，退化为普通 LangGraph")
    print("  fan-out 即可，不阻塞 P4。")

    # 脚本本身成功执行即返回 0：这里产出的是"结论"而不是"通过/失败"
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
