#!/usr/bin/env python3
"""Embedding 批量吞吐实测。

为什么需要单独一个脚本：V1.1 的实测发现 `Qwen3-Embedding-8B` 首次调用 12.6s、
紧随其后 0.4s——差距 30 倍，说明存在冷启动/模型装载。**单次调用的耗时不能外推**，
而 G1.1 的「200 页法规 ≤5 分钟」指标完全取决于批量吞吐。

本脚本回答三个问题：
  1. 真实的批量上限是多少？（官方文档称 32，但实测 33 通过）
  2. 在并发下，每秒能处理多少条？
  3. 按此速率，索引 1,500 条 / 10,000 条各需多久？

用法：
    SILICONFLOW_API_KEY=sk-xxx python scripts/smoke_embed_throughput.py
    SILICONFLOW_API_KEY=sk-xxx SMOKE_N=1000 python scripts/smoke_embed_throughput.py

成本提示：默认 512 条短文本，embedding 单价极低，一次运行成本可忽略。
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE_URL = os.environ.get("SILICONFLOW_BASE_URL", "https://api.siliconflow.cn/v1").rstrip("/")
API_KEY = os.environ.get("SILICONFLOW_API_KEY", "").strip()
EMBED_MODEL = os.environ.get("EMBED_MODEL", "Qwen/Qwen3-Embedding-8B")
EMBED_DIM = int(os.environ.get("EMBED_DIM", "1024"))
ALT_MODEL = os.environ.get("EMBED_ALT_MODEL", "BAAI/bge-m3")

N_ITEMS = int(os.environ.get("SMOKE_N", "512"))
CONCURRENCY = int(os.environ.get("SMOKE_CONCURRENCY", "8"))
TIMEOUT = float(os.environ.get("SMOKE_TIMEOUT", "180"))
RUN_ALT = os.environ.get("SMOKE_ALT", "1") == "1"

_print_lock = threading.Lock()


def log(msg: str) -> None:
    with _print_lock:
        print(msg, flush=True)


def post(path: str, payload: dict) -> tuple[int, dict | str]:
    req = urllib.request.Request(
        f"{BASE_URL}{path}",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {API_KEY}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        try:
            return e.code, json.loads(body)
        except json.JSONDecodeError:
            return e.code, body
    except Exception as e:
        return -1, f"{type(e).__name__}: {e}"


def err_of(body) -> str:
    if isinstance(body, dict):
        return json.dumps(body.get("message") or body.get("error") or body,
                          ensure_ascii=False)[:200]
    return str(body)[:200]


def embed_batch(texts: list[str], model: str, dim: int | None,
                retries: int = 3) -> tuple[bool, int, str]:
    """返回 (成功, 返回条数, 错误信息)。带指数退避。"""
    payload: dict = {"model": model, "input": texts}
    if dim is not None:
        payload["dimensions"] = dim
    delay = 1.0
    for attempt in range(retries):
        status, body = post("/embeddings", payload)
        if status == 200 and isinstance(body, dict):
            got = len(body.get("data") or [])
            if got == len(texts):
                return True, got, ""
            return False, got, f"返回 {got} 条，期望 {len(texts)} 条"
        if status in (429, 500, 502, 503, 504) and attempt < retries - 1:
            time.sleep(delay)
            delay *= 2
            continue
        return False, 0, f"HTTP {status} {err_of(body)}"
    return False, 0, "重试耗尽"


# --------------------------------------------------------- 语料：真实长度分布
_ARTICLE_TEMPLATE = (
    "第{no}条 【{topic}】居民企业应当就其来源于中国境内、境外的所得缴纳企业所得税。"
    "企业所得税的税率为百分之二十五。非居民企业在中国境内未设立机构、场所的，"
    "或者虽设立机构、场所但取得的所得与其所设机构、场所没有实际联系的，"
    "应当就其来源于中国境内的所得缴纳企业所得税，适用税率为百分之二十。"
    "本条所称所得，包括销售货物所得、提供劳务所得、转让财产所得、股息红利等权益性投资所得、"
    "利息所得、租金所得、特许权使用费所得、接受捐赠所得和其他所得。"
    "税务机关在判定关联交易是否符合独立交易原则时，可以采用可比非受控价格法、"
    "再销售价格法、成本加成法、交易净利润法、利润分割法等方法。"
)
_TOPICS = ["应纳税所得额", "税率", "应纳税额", "税收优惠", "源泉扣缴",
           "特别纳税调整", "征收管理", "转让定价", "受控外国企业", "资本弱化"]


def build_corpus(n: int) -> list[str]:
    """构造贴近真实法条长度的中文语料——长度不真实会让吞吐数据失真。"""
    return [_ARTICLE_TEMPLATE.format(no=i + 1, topic=_TOPICS[i % len(_TOPICS)])
            for i in range(n)]


# ------------------------------------------------------------------ 探测上限
def probe_batch_limits(model: str, dim: int | None) -> int:
    """二分式探测真实批量上限。返回已知可用的最大批量。"""
    print("\n[1/3] 探测真实批量上限")
    print(f"      官方文档称 32；上一次实测 33 已通过。现在往上探。")
    known_ok = 32
    for size in (32, 64, 128, 256, 512, 1024):
        texts = [f"上限探测样本 {i}" for i in range(size)]
        t0 = time.time()
        ok, got, err = embed_batch(texts, model, dim, retries=1)
        dt = time.time() - t0
        if ok:
            known_ok = size
            print(f"      {size:>5} 条: ✅ {dt:.2f}s")
        else:
            print(f"      {size:>5} 条: ❌ {err}  ← 上限在 {known_ok} 与 {size} 之间")
            break
    print(f"      → 实际可用最大批量：{known_ok}")
    return known_ok


# ---------------------------------------------------------------- 吞吐测量
def measure(model: str, dim: int | None, batch: int, n: int, label: str) -> dict | None:
    corpus = build_corpus(n)
    batches = [corpus[i:i + batch] for i in range(0, len(corpus), batch)]

    # 预热：把冷启动的成本排除在测量之外，但单独记下来
    t_warm = time.time()
    embed_batch(corpus[:min(batch, 8)], model, dim)
    warm_dt = time.time() - t_warm
    log(f"      预热耗时（冷启动，不计入吞吐）：{warm_dt:.2f}s")

    t0 = time.time()
    done = 0
    failures: list[str] = []
    with ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
        futures = [pool.submit(embed_batch, b, model, dim) for b in batches]
        for fut in as_completed(futures):
            ok, got, err = fut.result()
            if ok:
                done += got
            else:
                failures.append(err)
    elapsed = time.time() - t0

    if failures:
        log(f"      ⚠️ {len(failures)} 个批次失败，首个错误：{failures[0]}")

    rate = done / elapsed if elapsed > 0 else 0
    print(f"\n      {label}")
    print(f"        模型={model}  批量={batch}  并发={CONCURRENCY}  目标={n} 条")
    print(f"        成功 {done}/{n} 条，用时 {elapsed:.2f}s  →  {rate:.1f} 条/秒")
    return {"model": model, "done": done, "elapsed": elapsed, "rate": rate,
            "warm": warm_dt, "failures": len(failures)}


# ---------------------------------------------------------------------- main
def main() -> int:
    print("=" * 74)
    print("Embedding 批量吞吐实测")
    print("=" * 74)
    print(f"  base_url : {BASE_URL}")
    print(f"  model    : {EMBED_MODEL} (dim={EMBED_DIM})")
    print(f"  alt      : {ALT_MODEL}")
    print(f"  N        : {N_ITEMS}  并发: {CONCURRENCY}")

    if not API_KEY:
        print("\n[ABORT] 未设置 SILICONFLOW_API_KEY。")
        return 1

    batch_override = int(os.environ.get("SMOKE_BATCH", "0"))
    if batch_override:
        max_batch, batch = batch_override, batch_override
        print(f"\n[1/3] 跳过上限探测（SMOKE_BATCH={batch}）")
    else:
        max_batch = probe_batch_limits(EMBED_MODEL, EMBED_DIM)
        batch = min(max_batch, 128)  # 大批量单点失败代价高，实用值封顶 128

    print(f"\n[2/3] 吞吐测量（批量 {batch}，并发 {CONCURRENCY}）")
    primary = measure(EMBED_MODEL, EMBED_DIM, batch, N_ITEMS, "主模型")

    alt = None
    if RUN_ALT:
        print(f"\n[3/3] 备选模型对比")
        alt = measure(ALT_MODEL, None, batch, N_ITEMS, "备选模型")

    if not primary or primary["done"] == 0:
        print("\n测量失败，无法给出容量结论。")
        return 1

    print("\n" + "=" * 74)
    print("容量结论")
    print("=" * 74)
    rate = primary["rate"]
    for label, count in (("1 份 200 页法规（约 1,500 条）", 1500),
                         ("AC-1.2 目标（5,000 条）", 5000),
                         ("交付规模（1,400 条）", 1400)):
        secs = count / rate if rate else float("inf")
        verdict = ""
        if count == 1500:
            verdict = "  ← G1.1 的 5 分钟预算" + ("  ✅ 通过" if secs <= 300 else "  ❌ 超时")
        print(f"  {label:<32} {secs:7.1f}s{verdict}")

    if alt and alt["done"]:
        print(f"\n  备选 {ALT_MODEL} 速率 {alt['rate']:.1f} 条/秒"
              f"（主模型 {rate:.1f} 条/秒，比值 {alt['rate']/rate:.2f}×）")
        if alt["rate"] > rate * 1.5:
            print("  → 备选明显更快，且维度同为 1024，DDL 无需修改。值得在 P2 前评估。")

    print(f"\n  冷启动成本：预热一次约 {primary['warm']:.2f}s——"
          f"入库流水线应在第一批之前预热，否则首个批次会拖慢整体。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
