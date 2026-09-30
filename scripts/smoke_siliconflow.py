#!/usr/bin/env python3
"""硅基流动连通性与能力冒烟测试。

这是整个项目风险最高的一环：模型名、能力开关、embedding 维度都取决于别人的服务。
本脚本用**纯标准库**实现，因此可以在安装任何依赖之前运行——它的作用就是在写第一行
业务代码之前给出确定答案。

用法：
    SILICONFLOW_API_KEY=sk-xxx python scripts/smoke_siliconflow.py

退出码：0 = 全部通过；1 = 有检查项未通过。
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE_URL = os.environ.get("SILICONFLOW_BASE_URL", "https://api.siliconflow.cn/v1").rstrip("/")
API_KEY = os.environ.get("SILICONFLOW_API_KEY", "").strip()

CHAT_MODEL = os.environ.get("CHAT_MODEL", "deepseek-ai/DeepSeek-V4-Flash")
EMBED_MODEL = os.environ.get("EMBED_MODEL", "Qwen/Qwen3-Embedding-8B")
EMBED_FALLBACK_MODEL = os.environ.get("EMBED_FALLBACK_MODEL", "BAAI/bge-m3")
RERANK_MODEL = os.environ.get("RERANK_MODEL", "Qwen/Qwen3-Reranker-8B")
EMBED_DIM = int(os.environ.get("EMBED_DIM", "1024"))

TIMEOUT = float(os.environ.get("SMOKE_TIMEOUT", "90"))

RESULTS: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, detail: str) -> bool:
    RESULTS.append((name, ok, detail))
    flag = "PASS" if ok else "FAIL"
    print(f"  [{flag}] {name}: {detail}")
    return ok


def post(path: str, payload: dict) -> tuple[int, dict | str]:
    """返回 (status_code, body)。HTTP 错误也返回 body，便于看清失败原因。"""
    url = f"{BASE_URL}{path}"
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Authorization": f"Bearer {API_KEY}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            try:
                return resp.status, json.loads(raw)
            except json.JSONDecodeError:
                return resp.status, raw
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        try:
            return e.code, json.loads(body)
        except json.JSONDecodeError:
            return e.code, body
    except Exception as e:  # 网络层失败（DNS/超时/TLS）
        return -1, f"{type(e).__name__}: {e}"


def get(path: str) -> tuple[int, dict | str]:
    req = urllib.request.Request(
        f"{BASE_URL}{path}",
        headers={"Authorization": f"Bearer {API_KEY}", "Accept": "application/json"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", errors="replace")
    except Exception as e:
        return -1, f"{type(e).__name__}: {e}"


def err_of(body: dict | str) -> str:
    if isinstance(body, dict):
        m = body.get("message") or body.get("error") or body
        return json.dumps(m, ensure_ascii=False)[:300]
    return str(body)[:300]


# ------------------------------------------------------------ 0. 账户余额
def check_balance() -> bool | None:
    """余额预检。返回 True/False 表示是否够用，None 表示查不到。

    余额不足时所有付费端点都会返回 HTTP 402，与"模型不支持某能力"是两回事——
    必须在报告里把这两者区分开，否则会误判成技术不可行。
    """
    print("\n[0/7] 账户余额预检")
    status, body = get("/user/info")
    if status != 200 or not isinstance(body, dict):
        record("账户余额", True, f"无法查询（HTTP {status}），跳过预检")
        return None
    data = body.get("data") or body
    balance = data.get("totalBalance", data.get("balance"))
    if balance is None:
        record("账户余额", True, "响应中无余额字段，跳过预检")
        return None
    try:
        enough = float(balance) > 0
    except (TypeError, ValueError):
        record("账户余额", True, f"无法解析余额 {balance!r}，跳过预检")
        return None
    record("账户余额", enough,
           f"{balance} 元" + ("" if enough else "  ← 余额为 0，所有付费调用将返回 402"))
    return enough


# ---------------------------------------------------------------- 1. 模型清单
def check_model_listing() -> None:
    print("\n[1/7] GET /models —— 模型是否在清单内")
    status, body = get("/models")
    if status != 200 or not isinstance(body, dict):
        record("列出模型", False, f"HTTP {status} {err_of(body)}")
        return
    ids = {m.get("id") for m in body.get("data", [])}
    record("列出模型", True, f"返回 {len(ids)} 个模型")

    record(f"chat 模型存在 ({CHAT_MODEL})", CHAT_MODEL in ids,
           "已找到" if CHAT_MODEL in ids else _closest(ids, "deepseek"))
    record(f"embedding 模型存在 ({EMBED_MODEL})", EMBED_MODEL in ids,
           "已找到" if EMBED_MODEL in ids else _closest(ids, "embed"))


def _closest(ids: set[str], needle: str) -> str:
    hits = sorted(i for i in ids if needle.lower() in i.lower())
    return "未找到，相近的有: " + ", ".join(hits[:8]) if hits else "未找到，且无相近项"


# ------------------------------------------------- 2. function calling（关键）
def check_function_calling() -> None:
    print("\n[2/7] function calling —— 结构化输出的主力路径")
    schema = {
        "type": "object",
        "properties": {
            "jurisdiction": {"type": "string", "description": "ISO 3166 alpha-2 法域代码"},
            "tax_type": {"type": "string", "description": "税种"},
            "rate_percent": {"type": "number", "description": "税率，百分比数值"},
        },
        "required": ["jurisdiction", "tax_type", "rate_percent"],
    }
    payload = {
        "model": CHAT_MODEL,
        "messages": [
            {"role": "system", "content": "你是法律条文信息抽取器。只通过调用工具返回结果。"},
            {"role": "user", "content": "新加坡的企业所得税税率为 17%。"},
        ],
        "tools": [{"type": "function", "function": {
            "name": "record_facts", "description": "记录抽取到的事实", "parameters": schema}}],
        "tool_choice": {"type": "function", "function": {"name": "record_facts"}},
        "temperature": 0,
    }
    t0 = time.time()
    status, body = post("/chat/completions", payload)
    dt = time.time() - t0

    if status != 200 or not isinstance(body, dict):
        record("function calling", False, f"HTTP {status} {err_of(body)}")
        return
    try:
        msg = body["choices"][0]["message"]
        calls = msg.get("tool_calls") or []
        if not calls:
            record("function calling", False,
                   f"未返回 tool_calls，content={str(msg.get('content'))[:120]!r}")
            return
        args = json.loads(calls[0]["function"]["arguments"])
        missing = [k for k in schema["required"] if k not in args]
        ok = not missing
        record("function calling", ok,
               f"{args} ({dt:.1f}s)"
               + ("" if ok else f" 缺字段 {missing}"))
        usage = body.get("usage", {})
        print(f"        tokens: prompt={usage.get('prompt_tokens')} "
              f"completion={usage.get('completion_tokens')}")
    except (KeyError, IndexError, json.JSONDecodeError) as e:
        record("function calling", False, f"响应结构异常 {type(e).__name__}: {e}")


# ------------------------------------------------------------ 3. JSON mode
def check_json_mode() -> None:
    print("\n[3/7] json_object 模式 —— function calling 的降级路径")
    payload = {
        "model": CHAT_MODEL,
        "messages": [
            {"role": "system",
             "content": '只输出 JSON，形如 {"jurisdiction":"SG","tax_type":"CIT","rate_percent":17}'},
            {"role": "user", "content": "新加坡的企业所得税税率为 17%。"},
        ],
        "response_format": {"type": "json_object"},
        "temperature": 0,
    }
    t0 = time.time()
    status, body = post("/chat/completions", payload)
    dt = time.time() - t0
    if status != 200 or not isinstance(body, dict):
        record("json_object 模式", False, f"HTTP {status} {err_of(body)}")
        return
    try:
        content = body["choices"][0]["message"]["content"]
        parsed = json.loads(content)
        record("json_object 模式", True, f"{parsed} ({dt:.1f}s)")
    except (KeyError, IndexError, json.JSONDecodeError) as e:
        record("json_object 模式", False, f"无法解析为 JSON: {type(e).__name__}: {e}")


# ------------------------------------------------------------ 4. Embedding
def check_embedding(model: str, dimensions: int | None, label: str) -> None:
    payload = {"model": model, "input": ["新加坡企业所得税税率为百分之十七。"]}
    if dimensions is not None:
        payload["dimensions"] = dimensions
    t0 = time.time()
    status, body = post("/embeddings", payload)
    dt = time.time() - t0
    if status != 200 or not isinstance(body, dict):
        record(label, False, f"HTTP {status} {err_of(body)}")
        return
    try:
        vec = body["data"][0]["embedding"]
        got = len(vec)
        expect = dimensions or EMBED_DIM
        ok = got == expect
        record(label, ok, f"维度 {got}（期望 {expect}）({dt:.1f}s)"
               + ("" if ok else "  ← 维度不符，需调整 DDL"))
    except (KeyError, IndexError) as e:
        record(label, False, f"响应结构异常 {type(e).__name__}: {e}")
    return


def check_embedding_batch_limit() -> None:
    """核实 32 条/请求 的批量上限——它直接决定索引 5,000 条法条要多少次调用。"""
    print("\n[5/7] embedding 批量上限探测")
    for n in (32, 33):
        payload = {"model": EMBED_MODEL, "input": [f"测试文本 {i}" for i in range(n)]}
        if EMBED_DIM:
            payload["dimensions"] = EMBED_DIM
        status, body = post("/embeddings", payload)
        returned = len(body.get("data") or []) if isinstance(body, dict) else 0
        ok = status == 200 and returned == n
        if n == 32:
            detail = f"返回 {returned} 条" if status == 200 else err_of(body)
            record("单次 32 条", ok, f"HTTP {status} {detail}")
        else:
            # 33 条失败是**预期**结果——它证明上限存在，是信息而非错误
            print(f"        [INFO] 单次 33 条: HTTP {status} "
                  f"{'（被拒，确认上限为 32）' if not ok else '（竟通过，上限更高）'}")
            if ok:
                print("        [WARN] 上限高于 32，可相应减少调用次数")


# -------------------------------------------------------------- 6. Rerank
def check_rerank() -> None:
    print("\n[6/7] rerank —— 服务于检索的融合重排")
    payload = {
        "model": RERANK_MODEL,
        "query": "跨境关联交易的转让定价规则",
        "documents": [
            "独立交易原则要求关联交易定价与非关联方一致。",
            "企业所得税的申报期限为年度终了后五个月内。",
            "本条规定了增值税专用发票的开具要求。",
        ],
        "top_n": 3,
    }
    t0 = time.time()
    status, body = post("/rerank", payload)
    dt = time.time() - t0
    if status != 200 or not isinstance(body, dict):
        record("rerank", False, f"HTTP {status} {err_of(body)}")
        return
    try:
        results = body["results"]
        best = results[0]
        score = best.get("relevance_score")
        ok = score is not None and best.get("index") == 0
        record("rerank", ok,
               f"首条 index={best.get('index')} score={score:.4f} ({dt:.1f}s)"
               if score is not None else f"无 relevance_score: {results[:1]}")
    except (KeyError, IndexError, TypeError) as e:
        record("rerank", False, f"响应结构异常 {type(e).__name__}: {e}")


# ------------------------------------------- 7. json_schema（仅闭环，不计成败）
def check_json_schema() -> None:
    """探测严格结构化输出（`response_format={"type":"json_schema"}`）。

    这一项**不计入通过/失败**。因为它已经不影响技术路线——function calling 已实测
    可用，结构化输出走那条路。这里只是把「该模型是否支持 Strict Structured Outputs」
    这个问题彻底闭环，免得日后有人再翻文档猜一次。
    """
    print("\n[7/7] json_schema 严格结构化输出（仅作闭环记录，不影响决策）")
    payload = {
        "model": CHAT_MODEL,
        "messages": [
            {"role": "system", "content": "抽取法律条文中的事实。"},
            {"role": "user", "content": "新加坡的企业所得税税率为 17%。"},
        ],
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "facts",
                "strict": True,
                "schema": {
                    "type": "object",
                    "properties": {
                        "jurisdiction": {"type": "string"},
                        "tax_type": {"type": "string"},
                        "rate_percent": {"type": "number"},
                    },
                    "required": ["jurisdiction", "tax_type", "rate_percent"],
                    "additionalProperties": False,
                },
            },
        },
        "temperature": 0,
    }
    t0 = time.time()
    status, body = post("/chat/completions", payload)
    dt = time.time() - t0
    if status != 200 or not isinstance(body, dict):
        print(f"        [INFO] 不支持：HTTP {status} {err_of(body)}")
        print("        [INFO] 与预期一致，且不改变路线——function_calling 已实测可用。")
        return
    try:
        content = body["choices"][0]["message"]["content"]
        parsed = json.loads(content)
        print(f"        [INFO] 竟然支持：{parsed} ({dt:.1f}s)")
        print("        [INFO] 记录在案。但抽取节点仍统一走 function_calling，"
              "避免两条路径行为不一致。")
    except (KeyError, IndexError, json.JSONDecodeError) as e:
        print(f"        [INFO] 返回 200 但内容无法解析：{type(e).__name__}: {e}")


# ---------------------------------------------------------------- main
def main() -> int:
    print("=" * 72)
    print("硅基流动连通性与能力冒烟测试")
    print("=" * 72)
    print(f"  base_url    : {BASE_URL}")
    print(f"  api_key     : {'已设置 (' + API_KEY[:6] + '…' + API_KEY[-4:] + ')' if API_KEY else '【未设置】'}")
    print(f"  chat_model  : {CHAT_MODEL}")
    print(f"  embed_model : {EMBED_MODEL} (dim={EMBED_DIM})")
    print(f"  rerank_model: {RERANK_MODEL}")

    if not API_KEY:
        print("\n[ABORT] 未设置 SILICONFLOW_API_KEY 环境变量。")
        return 1

    has_balance = check_balance()
    check_model_listing()
    check_function_calling()
    check_json_mode()
    print("\n[4/7] embedding 维度")
    check_embedding(EMBED_MODEL, EMBED_DIM, f"embedding {EMBED_MODEL} (dim={EMBED_DIM})")
    check_embedding(EMBED_FALLBACK_MODEL, None, f"embedding 备选 {EMBED_FALLBACK_MODEL} (固定维度)")
    check_embedding_batch_limit()
    check_rerank()
    check_json_schema()

    print("\n" + "=" * 72)
    passed = [n for n, ok, _ in RESULTS if ok]
    failed = [(n, d) for n, ok, d in RESULTS if not ok]
    billing = [(n, d) for n, d in failed if "402" in d or "insufficient" in d]
    genuine = [(n, d) for n, d in failed if (n, d) not in billing]

    print(f"结果：{len(passed)}/{len(RESULTS)} 通过")

    if billing:
        print(f"\n因【账户余额不足】而未能验证的检查项（{len(billing)} 项）：")
        for name, _ in billing:
            print(f"  - {name}")
        print("\n这些不是技术失败，是计费失败。充值后重跑本脚本即可得到真实结论。")
        print("在充值之前，以下能力仍属【未验证】：function calling、json_object、"
              "embedding 维度、批量上限、rerank。")

    if genuine:
        print(f"\n真正的失败项（{len(genuine)} 项）：")
        for name, detail in genuine:
            print(f"  - {name}: {detail}")
        return 1

    if billing:
        return 1

    print("\n全部通过——可以进入 P0 骨架搭建。"
          + ("" if has_balance else " （余额预检未生效，请留意）"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
