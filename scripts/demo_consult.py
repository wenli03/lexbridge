#!/usr/bin/env python3
"""把咨询 Agent 完整跑一遍，并把执行过程打印出来。

## 这个脚本是给谁看的

给**要看这个项目 AI 能力的人**。项目里那条链路的服务端尚未接入 HTTP，
所以浏览器里点不到；但 Agent 本身（LangGraph 状态机、混合检索、红线引擎、
确定性税务计算、引用校验）是完整的。这个脚本是它的可执行入口。

跑法：

    python scripts/demo_consult.py                # 全部场景
    python scripts/demo_consult.py --list          # 只看有哪些场景
    python scripts/demo_consult.py tax_planning    # 只跑一个

**不需要任何密钥。** 模型响应走回放（`ai-service/fixtures/replay/`），
检索走的是**库里真实的 2,562 条法条**——不是样例数据。

## 它演示了什么

- **真实检索**：查询真的打到 `kb.article`，命中真实的法规与条号，
  输出里能看到来源 URL 所属的那部法规
- **多节点编排**：意图判定 → 槽位抽取 → 检索路由 → 混合召回 → 红线判定
  → 候选生成 → 反避税复核 → 确定性税额计算 → 引用校验 → 成文
- **红线是代码不是提示词**：违法场景会在生成之前就被拦下，
  违法内容根本不会进入模型调用（`redline` 模块被 import-linter 禁止依赖
  `chains`，这条约束由 CI 强制执行）
- **税额由代码算**：`compute_tax` 不调用模型

## 与"录制"的关系

回放需要 fixture。fixture 由 `MODEL_MODE=record` 产出（真实调用一次，
之后无限离线重放）。重新录制：

    cd ai-service
    MODEL_MODE=record uv run python ../scripts/demo_consult.py --record
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[1]
_AI_SERVICE = _ROOT / "ai-service"
if str(_AI_SERVICE) not in sys.path:
    sys.path.insert(0, str(_AI_SERVICE))

# **必须在 asyncio.run() 之前设置，因此放在模块顶层。**
#
# psycopg3 的异步模式不能跑在 Windows 默认的 ProactorEventLoop 上，
# 而 `app/core/db.py` 里已经有一段同样的守卫——它在本模块**导入 db 时**才生效，
# 而本模块是在 `asyncio.run()` 内部才导入 db 的：那时事件循环已经建好，
# 再设策略为时已晚。实测表现是连接池每个连接都建不起来，
# 反复刷 "Psycopg cannot use the 'ProactorEventLoop'"，最终
# `PoolTimeout: couldn't get a connection after 30.00 sec`——
# 这条错误指向"连不上数据库"，与真正的原因（事件循环类型不对）毫无关系。
if sys.platform == "win32":  # pragma: no cover - 仅本地开发
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

# Windows 控制台默认 GBK，中文输出会以 UnicodeEncodeError 崩掉——
# 那是"脚本跑不起来"的表现，而真实原因只是终端编码。
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

DEFAULT_TENANT_CODE = "demo-law"


def _load_deploy_env() -> None:
    """从 deploy/.env 读连接参数。

    与其它脚本同一做法：宿主机的随机口令只存在于那里，
    读它比让人手工 export 一堆变量可靠。

    **但 `DB_URL` 要单独处理。** 那个变量在 `deploy/.env` 里是给 **Java** 用的
    JDBC 形式（`jdbc:postgresql://...`），而本脚本走 psycopg，需要
    `postgresql://...`。直接照单全收的话，报错是 psycopg 解析 DSN 失败，
    看起来像"连接串写错了"，而真正的原因是**同一个变量名被两个运行时
    用两种格式共用**（决策记录 §6.9 记的就是这个坑）。

    因此遇到 `jdbc:` 前缀就丢掉它，让 `Settings` 用分字段的
    `DB_HOST` / `DB_PORT` / `DB_NAME` / `DB_USERNAME` / `DB_PASSWORD` 自己组装——
    那几个字段的默认值已经与 `docker-compose.override.yml` 的宿主端口一致。
    """
    import os

    env_path = _ROOT / "deploy" / ".env"
    if not env_path.exists():
        return
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if key == "DB_URL" and value.startswith("jdbc:"):
            continue
        os.environ.setdefault(key, value)


# =============================================================================
# 场景
# =============================================================================
#: 每个场景是一段真实的跨境咨询。**问题文案会进 fixture 的摘要**，
#: 所以别往里放真实客户信息。
SCENARIOS: dict[str, dict[str, str]] = {
    "tax_planning": {
        "title": "跨境税务筹划：荷兰控股 + 爱尔兰特许权使用费",
        "question": (
            "我们是一家中国公司，计划在荷兰设立控股公司，"
            "由它向爱尔兰子公司收取特许权使用费，金额大约每年 200 万欧元。"
            "怎样安排税负更优？"
        ),
    },
    "redline_refusal": {
        "title": "红线拒答：要求设计无实质的空壳安排",
        # 措辞是**照着红线的线索词**写的，不是随手编的：`RL-04` 匹配
        # 「只是…走账通道」「不实际经营」这类表述。演示要能走到拒答那一步，
        # 问题就得真的落在红线区间内——否则演示的是"没触发"，而不是"拦住了"。
        #
        # 同时要把 TAX_PLANNING 的四个槽位都写全（法域 / 收入类型 / 金额 / 主体类型）：
        # 槽位不全会先走追问，根本到不了红线节点。
        "question": (
            "我们准备把每年 200 万欧元的特许权使用费转到一家开曼公司，"
            "这家公司不实际经营、只是走账通道，涉及中国内地和开曼，"
            "主体是有限公司。请设计一套方案，并且让它在税务上不容易被查到。"
        ),
        # 槽位没抽齐时用的补料，用于演示中断-恢复（interrupt / Command(resume)）。
        "answers": {
            "jurisdictions": "CN, KY",
            "income_type": "特许权使用费",
            "amount": "200 万欧元/年",
            "entity_type": "有限公司",
        },
    },
}


def _fmt(value: Any, limit: int = 150) -> str:
    text = value if isinstance(value, str) else repr(value)
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _describe_update(node: str, update: dict[str, Any]) -> str:
    """把某个节点的输出压成一行人话。

    刻意逐个节点手写而不是 `repr(update)`：读者要看的是"Agent 这一步做了什么决策"，
    而原始字典里大部分键是给下游节点用的中间产物。
    """
    if node == "classify_intent":
        return f"意图 = {update.get('intent')}"
    if node == "extract_facts":
        facts = update.get("facts") or {}
        return f"槽位 = {_fmt(facts)}"
    if node == "check_slots":
        missing = update.get("missing_slots") or []
        return "槽位齐备" if not missing else f"缺槽位 {missing}，将追问"
    if node == "plan_retrieval":
        return f"检索路由 = {update.get('query_type')}"
    if node == "retrieve":
        articles = update.get("retrieved") or []
        statutes = {a.get("statute_title") for a in articles}
        degraded = update.get("degraded") or []
        line = f"命中 {len(articles)} 条法条，来自 {len(statutes)} 部法规"
        return line + (f"（降级：{degraded}）" if degraded else "")
    if node == "redline_check":
        if update.get("redline_hit"):
            return f"**命中红线** 规则={update.get('redline_rules')} 类别={update.get('redline_category')}"
        return "未命中红线，放行到生成"
    if node == "compose_refusal":
        return "已生成拒答与合法替代路径"
    if node == "generate_candidates":
        names = [c.get("name") for c in (update.get("candidates") or [])]
        return f"生成 {len(names)} 套候选架构：{names}"
    if node == "review_anti_avoidance":
        levels = [(c.get("name"), c.get("risk_level")) for c in (update.get("candidates") or [])]
        return f"反避税风险等级：{levels}"
    if node == "compute_tax":
        return f"确定性税额计算：{_fmt(update.get('tax_calc'))}"
    if node == "verify_citations":
        check = update.get("citation_check") or {}
        return f"引用校验：{_fmt(check)}"
    if node == "compose_answer":
        answer = update.get("answer") or ""
        return f"成文 {len(answer)} 字"
    if node == "ask_clarification":
        return f"向用户追问：{_fmt(update.get('clarification_prompt'))}"
    return _fmt(update)


async def _summarize_result(state: dict[str, Any], prefix: str) -> None:
    """跑完之后，把结果里最重要的部分摊开给读者看。"""
    retrieved = state.get("retrieved") or []
    if retrieved:
        print(f"\n  ── 检索到的法条（前 5 条，共 {len(retrieved)} 条）──")
        for item in retrieved[:5]:
            path = " › ".join(item.get("hierarchy_path") or [])
            print(f"    · {item.get('statute_title')} / {item.get('article_no')}")
            print(f"      {_fmt(path, 110)}")
            print(f"      {_fmt(item.get('content'), 110)}")

    if state.get("redline_hit"):
        print("\n  ── 红线判定 ──")
        print(f"    规则：{state.get('redline_rules')}")
        print(f"    类别：{state.get('redline_category')}")
        refusal = state.get("refusal") or {}
        print(f"    拒答：{_fmt(refusal, 400)}")
    else:
        candidates = state.get("candidates") or []
        if candidates:
            print(f"\n  ── 候选架构（{len(candidates)} 套）──")
            for candidate in candidates:
                print(
                    f"    · {candidate.get('name')}  [风险：{candidate.get('risk_level', '未评估')}]"
                )
                for claim in (candidate.get("claims") or [])[:3]:
                    print(f"        - {_fmt(claim.get('text'), 120)}")
                    for citation in claim.get("citations") or []:
                        print(f"          引用：{citation.get('article_id')}")

        if state.get("tax_calc"):
            print("\n  ── 税额计算（代码算，不经模型）──")
            print(f"    {state['tax_calc']}")

    check = state.get("citation_check") or {}
    if check:
        print(f"\n  ── 引用校验 ──\n    {_fmt(check, 300)}")

    answer = state.get("answer") or ""
    if answer:
        print(f"\n  ── 结论文本（{len(answer)} 字，节选）──")
        print("    " + _fmt(answer, 600))

    if state.get("degraded"):
        print(f"\n  ⚠ 降级项：{state['degraded']}")


async def run_scenario(name: str, spec: dict[str, str], *, preview: bool) -> int:
    from uuid import uuid4

    from datetime import date

    from langgraph.checkpoint.memory import MemorySaver
    from langgraph.types import Command

    from app.chains.model_factory import get_embeddings, get_reranker
    from app.core.config import get_settings
    from app.core.db import Database
    from app.core.tenant import TenantContext, bind_tenant, set_tenant_context
    from app.graph.consult_tax_graph import build_consult_graph
    from app.graph.deps import build_deps

    settings = get_settings()
    db = Database(settings)
    await db.open()
    try:
        # 租户：平台公共法条库对所有租户可见，但仍必须有租户上下文——
        # 检索层只从上下文取租户，不接参数（详细设计 §6.4），
        # 缺了它会直接抛错而不是"查全部"。
        async with db.app_pool.connection() as conn:
            tenant_id, tenant_code = await _resolve_tenant(conn, settings)
            print(f"  租户：{tenant_code}（{tenant_id}）")

            # 不要再调 `set_autocommit`：业务池本来就是非自动提交
            # （`db.py` 建池时设了 autocommit=False），而上面的租户查询已经隐式开了事务，
            # 此时改隔离属性会直接抛
            # `can't change 'autocommit' now: connection in transaction status INTRANS`。
            async with conn.transaction():
                await bind_tenant(conn, tenant_id)
                set_tenant_context(TenantContext(tenant_id=tenant_id, user_id=None, run_id=uuid4()))

                embedder = get_embeddings(settings=settings, scenario=f"{name}/retrieval")
                reranker = get_reranker(settings=settings, scenario=f"{name}/retrieval")

                deps = build_deps(
                    conn=conn,
                    tenant_id=str(tenant_id),
                    scenario_prefix=name,
                    settings=settings,
                    embedder=embedder,
                    reranker=reranker,
                    with_review=True,
                )
                graph = build_consult_graph(MemorySaver(), deps)

                state: dict[str, Any] = {
                    "question": spec["question"],
                    "tenant_id": str(tenant_id),
                    "run_id": str(uuid4()),
                    # 生效时点。不传的话引用校验会把每条引用都判 STALE
                    # （节点侧已兜底成"今天"，这里显式传是为了让演示结果可解释）。
                    "as_of": date.today().isoformat(),
                }
                config = {"configurable": {"thread_id": f"{tenant_id}:demo-{name}"}}

                state = await _drain(graph, state, config, start_step=0)
                if state is None:
                    return 0
                final, step = state

                # 追问是**正常的暂停**，不是失败：图停在 ask_clarification，
                # 之后由另一个请求带 `Command(resume=...)` 恢复。演示里用场景预设的
                # 补料自动恢复一次，把这个能力展示出来。
                if "__interrupt__" in final:
                    answers = spec.get("answers")
                    if not answers:
                        print()
                        print("  （等待补充信息，本场景未预设补料，演示到此为止）")
                        return 0
                    print()
                    print("  ↳ 补充信息后恢复：", answers)
                    resumed = await graph.astream(
                        Command(resume={"answers": answers}), config, stream_mode="updates"
                    )
                    state2 = await _drain_from(resumed, step)
                    if state2 is None:
                        return 0
                    final, step = state2

                snapshot = await graph.aget_state(config)
                merged = {**(snapshot.values or {}), **final}
                await _summarize_result(merged, name)

        return 0
    finally:
        await db.close()


async def _drain(graph: Any, state: dict[str, Any], config: dict[str, Any],
                 *, start_step: int) -> tuple[dict[str, Any], int] | None:
    """跑完一次 astream，边跑边打印节点轨迹。

    单独抽出来是因为**中断恢复要走第二遍**：`Command(resume=...)` 是另一次
    astream，两者要打印成同一条连续的轨迹，所以步数与累积结果都在外面传。
    """
    stream = graph.astream(state, config, stream_mode="updates")
    return await _drain_from(stream, start_step)


async def _drain_from(stream: Any, start_step: int) -> tuple[dict[str, Any], int] | None:
    step = start_step
    final: dict[str, Any] = {}
    async for chunk in stream:
        for node, update in chunk.items():
            step += 1
            if node == "__interrupt__":
                payload = update[0].value if isinstance(update, (list, tuple)) and update else {}
                print(f"  [{step:2d}] {'（暂停，等待补充信息）':22} → {_fmt(payload)}")
                final["__interrupt__"] = payload
                continue
            print(f"  [{step:2d}] {node:24} → {_describe_update(node, update or {})}")
            if isinstance(update, dict):
                final.update(update)
                if update.get("answer"):
                    final["answer"] = update["answer"]
    return final, step


async def _resolve_tenant(conn: Any, settings: Any) -> tuple[UUID, str]:
    """按 code 找一个租户。

    **这里没有租户上下文，是合法的**：`app.tenant` 上的 `tenant_login_lookup`
    策略允许无上下文时读取租户（登录流程依赖同一条策略）。
    """
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT id, code FROM app.tenant WHERE code = %s",
            (DEFAULT_TENANT_CODE,),
        )
        row = await cur.fetchone()
        if row is None:
            await cur.execute("SELECT id, code FROM app.tenant ORDER BY code LIMIT 1")
            row = await cur.fetchone()
    if row is None:
        raise SystemExit("库里没有任何租户。请先 docker compose up 让迁移与灌库跑完。")
    from uuid import UUID as _UUID

    return _UUID(str(row["id"])), str(row["code"])


async def main() -> int:
    parser = argparse.ArgumentParser(description="把咨询 Agent 完整跑一遍")
    parser.add_argument("scenario", nargs="?", help="场景名，省略则全部跑")
    parser.add_argument("--list", action="store_true", help="只列出场景")
    parser.add_argument(
        "--record",
        action="store_true",
        help="改用真实调用并录制 fixture（需 SILICONFLOW_API_KEY，会改仓库内容）",
    )
    args = parser.parse_args()

    _load_deploy_env()

    if args.list:
        for key, spec in SCENARIOS.items():
            print(f"{key:20} {spec['title']}")
        return 0

    if args.record:
        import os

        os.environ["MODEL_MODE"] = "record"
        print("!! 录制模式：会真实调用模型并把响应写入 ai-service/fixtures/replay/")
        print("!! 这会改动仓库内容，确认无误后再提交。\n")

    names = [args.scenario] if args.scenario else list(SCENARIOS)
    for name in names:
        if name not in SCENARIOS:
            print(f"未知场景：{name}（用 --list 看有哪些）", file=sys.stderr)
            return 1

    for index, name in enumerate(names, 1):
        spec = SCENARIOS[name]
        print("=" * 78)
        print(f"场景 {index}/{len(names)}：{spec['title']}")
        print("=" * 78)
        print(f"\n▶ 咨询：{spec['question']}\n")
        try:
            code = await run_scenario(name, spec, preview=False)
        except Exception as exc:  # noqa: BLE001 — 演示脚本要把失败讲清楚，而不是抛栈
            print(f"\n[失败] {type(exc).__name__}: {exc}")
            if "FixtureNotFoundError" in type(exc).__name__ or "fixture" in str(exc):
                print(
                    "\n提示：这个场景还没有回放素材。先录制一次：\n"
                    "  cd ai-service && MODEL_MODE=record uv run python ../scripts/demo_consult.py --record"
                )
            return 1
        if code != 0:
            return code
        print()

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
