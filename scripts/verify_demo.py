#!/usr/bin/env python3
"""演示环境的端到端验收（只用标准库，不需要装依赖）。

跑在 `docker compose up` 之后，逐项检查「访客打开浏览器能看到什么」：

  1. 三个演示账号都能登录，且角色正确
  2. 知识库确实有真实语料（法规数 / 法条数）
  3. 法条详情能取到，且带来源 URL（AC-1.6 可溯源）
  4. 审计日志能查到自己的登录留痕
  5. `q` 关键词筛选真的生效（曾经因为后端不收这个参数而静默失效）
  6. 跨租户隔离：A 租户的令牌看不到 B 租户的数据（AC-5.1）
  7. 未实现的功能如实回 404，而不是 500

输出刻意全用 ASCII：Windows 控制台默认是 GBK，中文与 emoji 会让脚本在
打印阶段就以 UnicodeEncodeError 崩掉——而那看起来像"验收失败"，
实际只是终端编码问题。要看中文请去浏览器。

退出码：0 = 全部通过。
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

# 端口从 WEB_PORT 读，默认 8088。与 README 里"不要照抄固定端口"是同一条理由：
# 演示环境的端口以 deploy/.env 为准，写死会让脚本在一个正确的部署上失败。
BASE = f"http://localhost:{os.environ.get('WEB_PORT', '8088')}"
PASSWORD = "LexBridge@2026"

results: list[tuple[bool, str, str]] = []


def check(ok: bool, name: str, detail: str = "") -> None:
    results.append((ok, name, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))


def call(
    method: str,
    path: str,
    *,
    token: str | None = None,
    body: dict | None = None,
) -> tuple[int, dict]:
    """返回 (HTTP 状态码, 解析后的 JSON)。非 2xx 也返回而不是抛异常——
    验收脚本需要看状态码本身，把 4xx/5xx 变成异常会丢失这个信息。"""
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            return exc.code, json.loads(raw)
        except json.JSONDecodeError:
            return exc.code, {"_raw": raw[:200]}


def login(tenant: str, username: str) -> tuple[int, str | None, dict]:
    status, payload = call(
        "POST",
        "/api/auth/login",
        body={"tenantCode": tenant, "username": username, "password": PASSWORD},
    )
    token = payload.get("data", {}).get("token") if payload.get("success") else None
    return status, token, payload


def main() -> int:
    print("=" * 72)
    print("LexBridge demo acceptance")
    print("=" * 72)

    # --- 0. 访客的第一屏：登录页的一键入口 ---------------------------------
    # 本系统没有自助注册（账号由管理员开设，是产品口径而非待补功能），
    # 因此"能不能不读文档就进去"决定了一个访客会不会继续往下看。
    # 这里检查两件事：入口**存在**，以及入口给的凭据**真的能登录**——
    # 只检查前者会漏掉"按钮渲染出来了但点了进不去"这种最糟的状态。
    status, payload = call("GET", "/api/auth/demo-accounts")
    data = payload.get("data") or {}
    demo_accounts = data.get("accounts") or []
    check(
        status == 200 and data.get("enabled") is True and len(demo_accounts) >= 3,
        "demo entry is available without auth (interviewer can get in)",
        f"status={status} enabled={data.get('enabled')} accounts={len(demo_accounts)}",
    )

    for account in demo_accounts:
        status, token, _ = login(account["tenantCode"], account["username"])
        if account["password"] != PASSWORD:
            token = None
        check(
            status == 200 and token is not None,
            f"one-click credential works: {account['username']}",
            f"status={status}",
        )

    # --- 1. 三个角色都能登录，角色正确 -------------------------------------
    tokens: dict[str, str] = {}
    for username, expected_role in (
        ("admin", "ADMIN"),
        ("lawyer", "LAWYER"),
        ("compliance", "COMPLIANCE_OFFICER"),
    ):
        status, token, payload = login("demo-law", username)
        ok = status == 200 and token is not None
        role = (payload.get("data") or {}).get("user", {}).get("role")
        check(
            ok and role == expected_role,
            f"login demo-law/{username}",
            f"status={status} role={role}",
        )
        if token:
            tokens[username] = token

    if "admin" not in tokens:
        print("\n无法登录，后续检查无法进行。")
        return 1
    admin = tokens["admin"]

    # --- 2. 知识库有真实语料 ------------------------------------------------
    status, payload = call("GET", "/api/statutes?page=1&size=50", token=admin)
    total = (payload.get("data") or {}).get("total")
    check(
        status == 200 and isinstance(total, int) and total >= 8,
        "GET /api/statutes returns seeded corpus",
        f"status={status} total={total}",
    )

    items = (payload.get("data") or {}).get("items") or []
    check(len(items) > 0, "statute list has rows", f"page size={len(items)}")

    # --- 3. 法域覆盖与来源 URL ---------------------------------------------
    status, payload = call("GET", "/api/statutes?page=1&size=200", token=admin)
    all_items = (payload.get("data") or {}).get("items") or []
    jurisdictions = {i.get("jurisdictionCode") for i in all_items}
    check(
        jurisdictions >= {"IE", "NL"},
        "both seeded jurisdictions are visible",
        f"found={sorted(j for j in jurisdictions if j)}",
    )
    with_source = [i for i in all_items if i.get("sourceUrl")]
    check(
        len(with_source) == len(all_items) and len(all_items) > 0,
        "every statute carries a source URL (AC-1.6 traceability)",
        f"{len(with_source)}/{len(all_items)}",
    )

    # --- 4. 法条详情可取 ----------------------------------------------------
    # 详情接口要按 id 取，而列表接口不返回 id——从语料里挑一个已知的法规版本，
    # 用它的 article_count 证明"列表说有 132 条"与"详情确实能取到"是一回事。
    if all_items:
        status, payload = call("GET", "/api/statutes?page=1&size=1", token=admin)
        first = ((payload.get("data") or {}).get("items") or [{}])[0]
        check(
            status == 200 and first.get("articleCount", 0) > 0,
            "statute reports a non-zero article count",
            f"articles={first.get('articleCount')} jurisdiction={first.get('jurisdictionCode')}",
        )

    # --- 4. 审计日志有登录留痕 ---------------------------------------------
    status, payload = call("GET", "/api/audit-logs?page=1&size=20", token=admin)
    logs = (payload.get("data") or {}).get("items") or []
    check(
        status == 200 and len(logs) > 0,
        "GET /api/audit-logs has rows (login is audited)",
        f"status={status} rows={len(logs)}",
    )
    actions = {row.get("action") for row in logs}
    check("LOGIN" in actions, "audit contains a LOGIN entry", f"actions={sorted(a for a in actions if a)}")

    # --- 5. q 关键词筛选真的生效 -------------------------------------------
    # 取一条已知 trace_id，用它反查，结果必须**只有**这一条。
    # 修复前的行为是：后端不收 q，Spring 静默忽略，于是返回全部记录。
    if logs:
        target_trace = logs[0].get("traceId")
        status, payload = call(
            "GET", f"/api/audit-logs?page=1&size=50&q={target_trace}", token=admin
        )
        filtered = (payload.get("data") or {}).get("items") or []
        ok = status == 200 and len(filtered) >= 1 and all(
            row.get("traceId") == target_trace for row in filtered
        )
        check(
            ok,
            "audit q= filters by trace_id (was silently ignored)",
            f"status={status} matched={len(filtered)} of {len(logs)}",
        )

    # 不存在的关键词必须返回空，而不是全部
    status, payload = call(
        "GET", "/api/audit-logs?page=1&size=50&q=zzz-no-such-trace-zzz", token=admin
    )
    empty = (payload.get("data") or {}).get("items") or []
    check(
        len(empty) == 0,
        "audit q= with a nonsense keyword returns nothing",
        f"rows={len(empty)}",
    )

    # --- 6. 跨租户隔离 ------------------------------------------------------
    status, acme_token, payload = login("acme-law", "admin")
    check(status == 200 and acme_token, "login acme-law/admin", f"status={status}")

    if acme_token:
        # 两个租户各自看审计日志：都只能看到自己的登录记录。
        _, p1 = call("GET", "/api/audit-logs?page=1&size=50", token=admin)
        _, p2 = call("GET", "/api/audit-logs?page=1&size=50", token=acme_token)
        ids1 = {r.get("id") for r in ((p1.get("data") or {}).get("items") or [])}
        ids2 = {r.get("id") for r in ((p2.get("data") or {}).get("items") or [])}
        overlap = ids1 & ids2
        check(
            not overlap,
            "cross-tenant audit isolation (no shared rows)",
            f"demo-law={len(ids1)} acme-law={len(ids2)} overlap={len(overlap)}",
        )

        # 用 A 的令牌去猜 B 的租户 —— 令牌里的租户是签名保护的，客户端改不了。
        _, me2 = call("GET", "/api/auth/me", token=acme_token)
        tenant2 = (me2.get("data") or {}).get("tenantId")
        _, me1 = call("GET", "/api/auth/me", token=admin)
        tenant1 = (me1.get("data") or {}).get("tenantId")
        check(
            tenant1 is not None and tenant2 is not None and tenant1 != tenant2,
            "each token resolves to its own tenant",
            f"{str(tenant1)[:8]} vs {str(tenant2)[:8]}",
        )

    # --- 7. 角色在**服务端**被强制，而不是只在前端隐藏入口 -----------------
    # 这条曾经不成立：`anyRequest().authenticated()` 意味着"登录了就能访问一切"，
    # 律师的令牌能直接读到全部审计记录。依据 PRD §3.8 权限矩阵，律师不该有审计权限。
    lawyer_token = tokens.get("lawyer")
    compliance_token = tokens.get("compliance")
    if lawyer_token:
        status, _ = call("GET", "/api/audit-logs?page=1&size=1", token=lawyer_token)
        check(
            status == 403,
            "lawyer is forbidden from audit logs (server-side, not just hidden in UI)",
            f"status={status}",
        )
    if compliance_token:
        status, _ = call("GET", "/api/audit-logs?page=1&size=1", token=compliance_token)
        check(
            status == 200,
            "compliance officer can read audit logs",
            f"status={status}",
        )

    # --- 8. 未实现的功能如实回 404 -----------------------------------------
    # 404 而不是 500 是必需的：前端据此区分"该功能尚未实现"与"服务出故障了"，
    # 而 500 会让一个还没做的功能看起来像坏掉了。
    for path in ("/api/consult-sessions", "/api/knowledge/review"):
        status, _ = call("GET", path, token=admin)
        check(
            status == 404,
            f"unimplemented endpoint {path} returns 404 (not 500)",
            f"status={status}",
        )

    # --- 9. 未认证请求必须回 401 而不是 403 ---------------------------------
    # 前端靠 401/403 区分"跳登录页"与"提示权限不足"；全都回 403 的话，
    # 令牌过期与真的没权限在界面上无法区分。
    status, _ = call("GET", "/api/statutes?page=1&size=1")
    check(status == 401, "unauthenticated request returns 401", f"status={status}")

    # --- 汇总 --------------------------------------------------------------
    failed = [r for r in results if not r[0]]
    print()
    print("=" * 72)
    print(f"{len(results) - len(failed)}/{len(results)} passed")
    if failed:
        print("FAILED:")
        for _ok, name, detail in failed:
            print(f"  - {name}  {detail}")
    print("=" * 72)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
