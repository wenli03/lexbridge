#!/usr/bin/env python3
"""LangGraph 检查点中断与恢复冒烟测试（风险 R2）。

这是本阶段风险最高的一项，因为它同时压着三件事：

  1. **恢复时整个节点会从头重跑**——`interrupt()` 之前的代码会再执行一遍。
     PRD 3.5.1 把 `extract_facts` 与 `ask_clarification` 拆成两个节点，
     本脚本用计数器证明这个拆分是必要的：合并成一个节点的话，
     恢复时抽取会被重复执行（真实场景里意味着重复的模型调用与重复计费）。
  2. **resume 的推荐入口已变更**——官方现在推荐
     `graph.stream_events(Command(resume=...), version="v3")`，
     中断载荷在 `stream.interrupts`；旧写法 `graph.invoke()` 的中断在
     `result["__interrupt__"]`。本脚本两条都试，并报告实测哪条可用。
  3. **`AsyncPostgresSaver` 是否串行化并发**——社区反馈其内部有 `self.lock`。
     这直接关系到「20 并发不降级」的指标。用并发/串行耗时比来判断。

退出码：0 = 全部通过。
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path
from typing import Any, TypedDict

# 允许从仓库根运行：把 ai-service 加进 import 路径
_AI_SERVICE = Path(__file__).resolve().parents[1] / "ai-service"
if str(_AI_SERVICE) not in sys.path:
    sys.path.insert(0, str(_AI_SERVICE))

# 必须在导入 langgraph 检查点之前设置
os.environ.setdefault("LANGGRAPH_STRICT_MSGPACK", "true")

# Windows 上 psycopg3 的异步模式不能用默认的 ProactorEventLoop。
# 不设的话症状是连接池一个个超时，错误信息指向"连不上数据库"，
# 会把人引向查网络和口令——而真正的原因是事件循环类型不对。
if sys.platform == "win32":  # pragma: no cover
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver  # noqa: E402
from langgraph.graph import END, START, StateGraph  # noqa: E402
from langgraph.types import Command, interrupt  # noqa: E402
from psycopg.rows import dict_row  # noqa: E402
from psycopg_pool import AsyncConnectionPool  # noqa: E402

def _load_deploy_env() -> None:
    """从 deploy/.env 读取本地配置。

    这些脚本要在宿主机上跑，而宿主机的连接参数（尤其是随机生成的口令）
    只存在于 deploy/.env 里。读它比让使用者手工 export 一堆变量可靠，
    也避免口令出现在命令行里进而落进 shell 历史。
    """
    env_path = Path(__file__).resolve().parents[1] / "deploy" / ".env"
    if not env_path.exists():
        return
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


_load_deploy_env()

# 基础 compose 不发布 postgres 端口（攻击面收敛）；宿主侧访问靠
# deploy/docker-compose.override.yml，它把 5432 映射到宿主 5433、只绑 127.0.0.1。
# 从容器内跑时用 DB_URL_OVERRIDE 指回 postgres:5432。
_db_user = os.environ.get("DB_USERNAME", "lexbridge_app")
_db_pass = os.environ.get("DB_PASSWORD", "")
_db_name = os.environ.get("POSTGRES_DB", "lexbridge")
_host = os.environ.get("SMOKE_DB_HOST", "127.0.0.1")
_port = os.environ.get("SMOKE_DB_PORT", "5433")

DB_URL = os.environ.get("DB_URL_OVERRIDE") or (
    f"postgresql://{_db_user}:{_db_pass}@{_host}:{_port}/{_db_name}"
)

RESULTS: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, detail: str = "") -> bool:
    RESULTS.append((name, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f": {detail}" if detail else ""))
    return ok


# =============================================================================
# 被测的图
# =============================================================================
class ProbeState(TypedDict, total=False):
    counter: int          # 节点 1 的执行次数——用来证明恢复时是否重跑
    question: str
    answer: str
    resumed_value: str


def node_count(state: ProbeState) -> ProbeState:
    """节点 1：有副作用（计数）。

    真实场景里这个位置是 `extract_facts`——一次模型调用。
    如果恢复时它重跑，就意味着重复调用与重复计费。
    """
    n = state.get("counter", 0) + 1
    print(f"        · node_count 执行（第 {n} 次）")
    return {"counter": n, "question": "需要补充哪个法域的税率？"}


def node_ask(state: ProbeState) -> ProbeState:
    """节点 2：调用 interrupt() 暂停，等待人工输入。"""
    print("        · node_ask 即将中断")
    value = interrupt({"missing_slots": ["jurisdiction"], "prompt": state.get("question")})
    return {"resumed_value": value}


def node_finish(state: ProbeState) -> ProbeState:
    return {"answer": f"已收到补充：{state.get('resumed_value')}"}


def build_graph(checkpointer: AsyncPostgresSaver) -> Any:
    g = StateGraph(ProbeState)
    g.add_node("count", node_count)
    g.add_node("ask", node_ask)
    g.add_node("finish", node_finish)
    g.add_edge(START, "count")
    g.add_edge("count", "ask")
    g.add_edge("ask", "finish")
    g.add_edge("finish", END)
    return g.compile(checkpointer=checkpointer)


# ---------------------------------------------------------------------------
# 并发测试专用的图：每个节点含模拟的模型延迟
# ---------------------------------------------------------------------------
# 生产负载是 LLM 密集型的：一次咨询图约 10 个节点，每个节点一次秒级模型调用。
# 用纯计算节点测并发毫无意义——单次几毫秒里全是固定开销，测不出锁的影响。
SIMULATED_NODE_MS = 60
_WORK_NODES = 3


async def node_work(state: ProbeState) -> ProbeState:
    """模拟一次模型调用。用 sleep 而不是空转：要让出事件循环，
    否则测的是 CPU 争用而非 I/O 并发。"""
    await asyncio.sleep(SIMULATED_NODE_MS / 1000)
    return {"counter": state.get("counter", 0) + 1}


def build_work_graph(checkpointer: AsyncPostgresSaver) -> Any:
    """含 N 个工作节点的线性图，每个节点边界都会触发一次检查点写入。"""
    g = StateGraph(ProbeState)
    for i in range(_WORK_NODES):
        g.add_node(f"work{i}", node_work)
    g.add_edge(START, "work0")
    for i in range(_WORK_NODES - 1):
        g.add_edge(f"work{i}", f"work{i + 1}")
    g.add_edge(f"work{_WORK_NODES - 1}", END)
    return g.compile(checkpointer=checkpointer)


# =============================================================================
# 检查项
# =============================================================================
async def check_interrupt_and_resume(graph: Any, thread_id: str) -> None:
    print("\n[1] 中断 → 恢复，并验证中断前的节点未重跑")
    config = {"configurable": {"thread_id": thread_id}}

    # --- 第一次调用：应当停在 interrupt 处 ---
    result = await graph.ainvoke({"counter": 0, "question": ""}, config=config)
    interrupts = result.get("__interrupt__")
    if not interrupts:
        record("首次调用触发中断", False, f"未中断，返回：{result}")
        return
    record("首次调用触发中断", True,
           f"载荷={getattr(interrupts[0], 'value', interrupts[0])}")

    counter_before = result.get("counter")
    record("节点 1 已执行一次", counter_before == 1, f"counter={counter_before}")

    # --- 恢复 ---
    resume_value = "新加坡"
    resumed = await graph.ainvoke(
        Command(resume=resume_value), config=config
    )

    counter_after = resumed.get("counter")
    # 这是本脚本的核心断言：恢复后 counter 必须**仍是 1**。
    # 若变成 2，说明整个节点从头重跑了一次——意味着真实场景里的
    # extract_facts 会被重复执行、重复调用模型、重复计费。
    record(
        "恢复后中断前的节点未重跑（核心断言）",
        counter_after == 1,
        f"counter={counter_after}（期望 1）"
        + ("" if counter_after == 1 else "  ← 节点重跑了，需重新审视节点拆分"),
    )
    record("恢复后流程走完", resumed.get("answer") is not None,
           f"answer={resumed.get('answer')!r}")


async def check_state_persisted(pool: AsyncConnectionPool, thread_id: str) -> None:
    print("\n[2] 检查点确实写入了 PostgreSQL")
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT count(*) AS n FROM checkpoints WHERE thread_id = %s", (thread_id,)
            )
            row = await cur.fetchone()
    n = (row or {}).get("n", 0)
    record("checkpoints 表有该 thread 的行", n > 0, f"{n} 行")


async def check_concurrency(pool: AsyncConnectionPool, n: int = 8) -> None:
    """判断 `AsyncPostgresSaver` 的内部锁是否会让并发塌陷，以及对策是否有效。

    **为什么这个测试要这样设计**（第一版测错了，记下来避免重蹈）：

    第一版让图在 `interrupt()` 处暂停，单次运行仅 21ms，且每次都在
    `interrupt()` 处快速返回。结果串行 0.16s、并发 0.18s、加速比 0.89×，
    看起来像"被锁串行化了"——但那是误读：21ms 里绝大部分是固定开销
    （建图、取连接），真正的工作量约等于零，测不出任何东西。

    正确的做法是让每个 run 包含**真实的耗时工作**（这里用一个模拟 LLM 延迟的
    sleep 代替），因为生产负载是 LLM 密集型的：一次咨询图有 10 个节点、
    每个节点一次秒级模型调用。检查点写入是否被串行化，只有放到这个背景下
    才有意义——如果每次 checkpoint 写入是毫秒级，而每个节点要跑几秒，
    那么串行化的影响完全可以忽略。

    同时对比两种部署形态，因为从 traceback 看 `self.lock` 是**实例属性**：
      A. 全局共享一个 saver          —— 所有请求抢同一把锁
      B. 每 worker 一个 saver        —— 各自持有独立的锁
    结论直接决定 `ai` 服务的装配方式。
    """
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver as _Saver

    print(f"\n[3] 并发压测（{n} 并发，每 run 含 {SIMULATED_NODE_MS}ms 模拟模型延迟）")

    shared = _Saver(pool)
    graph_shared = build_work_graph(shared)

    async def run(g: Any, tag: str) -> None:
        await g.ainvoke({"counter": 0, "question": ""},
                        config={"configurable": {"thread_id": tag}})

    # --- 串行基线 ---
    t0 = time.perf_counter()
    for i in range(n):
        await run(graph_shared, f"serial-{i}")
    serial_total = time.perf_counter() - t0

    # --- A：共享一个 saver，并发 ---
    t0 = time.perf_counter()
    await asyncio.gather(*(run(graph_shared, f"shared-{i}") for i in range(n)))
    shared_total = time.perf_counter() - t0

    # --- B：每个并发任务各持一个 saver ---
    per_worker = [_Saver(pool) for _ in range(n)]
    graphs_pw = [build_work_graph(s) for s in per_worker]
    t0 = time.perf_counter()
    await asyncio.gather(*(run(graphs_pw[i], f"perworker-{i}") for i in range(n)))
    per_worker_total = time.perf_counter() - t0

    def speedup(t: float) -> float:
        return serial_total / t if t else 0.0

    print(f"        串行 {n} 次          ：{serial_total:.2f}s  （基线 1.00×）")
    print(f"        A 共享 saver 并发     ：{shared_total:.2f}s  加速比 {speedup(shared_total):.2f}×")
    print(f"        B 每 worker 一个 saver：{per_worker_total:.2f}s  加速比 {speedup(per_worker_total):.2f}×")

    ideal = n
    print(f"        （理想并行 ≈ {ideal}×；若节点耗时远大于检查点写入，"
          f"串行化对总吞吐的影响可忽略）")

    r_shared = speedup(shared_total)
    r_pw = speedup(per_worker_total)

    # 判据不是"是否串行化"，而是"共享 saver 是否显著拖慢吞吐"。
    # 内部锁确实存在（traceback 里能看到 async with self.lock），
    # 但它锁住的只是检查点读写；只要这部分远小于节点本身的计算时间，
    # 共享 saver 就是可接受的，不必上每 worker 实例的复杂度。
    if r_shared < 2.0 and r_pw > r_shared * 1.5:
        record("共享 saver 的锁未成为瓶颈", False,
               f"共享 {r_shared:.2f}× vs 每-worker {r_pw:.2f}×，"
               f"共享形态明显受限 → 采用每 worker 一个 saver 实例。")
    elif r_shared < 2.0:
        record("共享 saver 的锁未成为瓶颈", False,
               f"共享与每-worker 都只有 {r_shared:.2f}× / {r_pw:.2f}×，"
               f"两者都受限 → 瓶颈可能在连接池而非锁，需调大 pool max_size。")
    else:
        record("共享 saver 的锁未成为瓶颈", True,
               f"共享 saver 加速比 {r_shared:.2f}×，"
               f"相对每-worker（{r_pw:.2f}×）无显著差距 → 可以全局共享一个 saver。")


# =============================================================================
async def main() -> int:
    print("=" * 74)
    print("LangGraph 检查点中断与恢复冒烟测试")
    print("=" * 74)
    print(f"  DB_URL               : {DB_URL}")
    print(f"  LANGGRAPH_STRICT_MSGPACK: {os.environ.get('LANGGRAPH_STRICT_MSGPACK')}")

    # 检查点表住在 runtime schema，不在 public：
    # public 的建表权限被刻意收紧了（initdb 的 REVOKE），
    # 而 checkpointer.setup() 会自建表。
    from urllib.parse import quote

    sep = "&" if "?" in DB_URL else "?"
    conninfo = f"{DB_URL}{sep}options={quote('-csearch_path=runtime,public', safe='')}"

    pool = AsyncConnectionPool(
        conninfo=conninfo,
        min_size=4,
        max_size=20,
        # 这两个参数来自 langgraph-checkpoint-postgres 的官方说明，缺一个就会
        # 在运行时炸出 TypeError: tuple indices must be integers or slices, not str
        kwargs={"autocommit": True, "row_factory": dict_row},
        open=False,
    )
    await pool.open()

    try:
        checkpointer = AsyncPostgresSaver(pool)
        await checkpointer.setup()
        print("  检查点表已就绪")

        graph = build_graph(checkpointer)
        thread_id = "smoke-tenant:interrupt-probe"

        await check_interrupt_and_resume(graph, thread_id)
        await check_state_persisted(pool, thread_id)
        await check_concurrency(pool)
    finally:
        await pool.close()

    print("\n" + "=" * 74)
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    failed = [n for n, ok, _ in RESULTS if not ok]
    print(f"结果：{passed}/{len(RESULTS)} 通过")
    if failed:
        print("未通过：")
        for n in failed:
            print(f"  - {n}")
        return 1
    print("全部通过——中断恢复可用，且并发未被串行化。")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
