#!/usr/bin/env python3
"""P0 冒烟总编排。

逐个运行各专项冒烟脚本并汇总结果。设计上有两个刻意的取舍：

  1. **区分「跳过」与「失败」**。缺 API Key 时 `smoke_siliconflow.py` 应被
     标记为"跳过"而不是"失败"——否则在无密钥环境下（CI、面试官第一次
     clone）会看到一片红，而实际上系统是好的。
  2. **慢的脚本默认不跑**。`smoke_embed_throughput.py` 要几分钟且消耗额度，
     只在显式要求时运行。

用法：
    python scripts/smoke_all.py              # 快速集
    python scripts/smoke_all.py --all        # 含吞吐测量
    python scripts/smoke_all.py --only checkpoint
"""

from __future__ import annotations

import argparse
import hashlib
import os
import subprocess
import sys
import time
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"


# 脚本需要区分"密钥无效"和"密钥缺失"，因为这两者的排查方向完全不同。
_CONFLICTS: list[str] = []


def _load_deploy_env() -> None:
    """把 deploy/.env 读进环境。

    语义刻意选成：**.env 里的非空值优先于已有环境变量**，与 `setdefault`
    的"已有值优先"相反。原因是踩过一个很隐蔽的坑：

      机器上存在一个用户级的 SILICONFLOW_API_KEY（陈旧、已失效），
      而 deploy/.env 里那一项是空的。用 setdefault 的话，脚本会静默使用
      那个陈旧的密钥，症状是满屏 401 "Token is invalid"，
      排查的人会先去怀疑代码或网络，而真正的原因在环境变量里。

    空值视为"未配置"而非"强制置空"——否则 .env 里留空的项会把
    使用者有意设置的变量抹掉。
    """
    env_path = ROOT / "deploy" / ".env"
    if not env_path.exists():
        return
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if not value:
            continue  # 空值 = 未配置
        existing = os.environ.get(key)
        if existing and existing != value:
            _CONFLICTS.append(key)
        os.environ[key] = value


_load_deploy_env()

# name -> (脚本, 说明, 是否需要密钥, 是否慢)
SUITES: dict[str, tuple[str, str, bool, bool]] = {
    "siliconflow": ("smoke_siliconflow.py", "模型能力（chat / embedding / rerank）", True, False),
    "checkpoint": ("smoke_checkpoint_interrupt.py", "检查点中断恢复与并发", False, False),
    "deepagents": ("smoke_deepagents.py", "DeepAgent 在硅基流动端点上的可用性", True, False),
    "langsmith": ("smoke_langsmith.py", "LangSmith trace 覆盖", False, False),
    "throughput": ("smoke_embed_throughput.py", "embedding 批量吞吐", True, True),
}

SKIP = "SKIP"
PASS = "PASS"
FAIL = "FAIL"
MISSING = "MISSING"


def run_one(name: str) -> tuple[str, float, str]:
    filename, _desc, needs_key, _slow = SUITES[name]
    path = SCRIPTS / filename

    if not path.exists():
        return MISSING, 0.0, "脚本尚未编写"

    if needs_key and not os.environ.get("SILICONFLOW_API_KEY"):
        return SKIP, 0.0, "未设置 SILICONFLOW_API_KEY"

    env = dict(os.environ)
    # 脚本在宿主上跑，需要连到 compose 暴露的开发端口
    env.setdefault("SMOKE_DB_HOST", "127.0.0.1")
    env.setdefault("SMOKE_DB_PORT", "5433")

    t0 = time.perf_counter()
    # 用 ai-service 的虚拟环境：这些脚本依赖 langgraph 等包
    proc = subprocess.run(
        ["uv", "run", "--directory", str(ROOT / "ai-service"), "python", str(path)],
        capture_output=True, text=True, encoding="utf-8", errors="replace", env=env,
    )
    dt = time.perf_counter() - t0

    if proc.returncode == 0:
        return PASS, dt, ""
    tail = (proc.stderr or proc.stdout or "").strip().splitlines()
    return FAIL, dt, tail[-1][:160] if tail else f"退出码 {proc.returncode}"


def main() -> int:
    parser = argparse.ArgumentParser(description="P0 冒烟总编排")
    parser.add_argument("--all", action="store_true", help="包含慢速的吞吐测量")
    parser.add_argument("--only", help="只跑指定的一项")
    args = parser.parse_args()

    if args.only:
        if args.only not in SUITES:
            print(f"未知的冒烟项 {args.only!r}，可选：{', '.join(SUITES)}")
            return 2
        names = [args.only]
    elif args.all:
        names = list(SUITES)
    else:
        names = [n for n, (_, _, _, slow) in SUITES.items() if not slow]

    key = os.environ.get("SILICONFLOW_API_KEY", "")
    print("=" * 74)
    print("LexBridge P0 冒烟")
    print("=" * 74)
    # 只报长度与哈希前缀，不输出密钥的任何字符——CI 日志与 docker logs
    # 都可能被转发或归档，掩码"首尾几位"看起来安全，实则仍在泄露凭据材料。
    if key:
        fp = hashlib.sha256(key.encode()).hexdigest()[:12]
        print(f"  硅基流动密钥: 已设置（sha256:{fp} 长度={len(key)}）")
    else:
        print("  硅基流动密钥: 未设置（相关项将跳过）")
    print(f"  待运行项    : {len(names)}")

    if _CONFLICTS:
        # 这条警告是有价值的：环境变量与 .env 不一致时，Docker Compose 也会
        # 优先用环境变量，于是容器里跑的是另一个密钥——症状是满屏 401，
        # 而排查方向会被引向代码。
        print()
        print("  ⚠️  环境变量与 deploy/.env 冲突（当前以 .env 为准，但 docker compose 相反）：")
        for k in _CONFLICTS:
            print(f"      - {k}")
        print("      docker compose 的优先级是「shell 环境变量 > .env 文件」，")
        print("      所以在容器里跑的会是环境变量里那个值。若它已失效，")
        print("      建议清掉：  [Environment]::SetEnvironmentVariable('SILICONFLOW_API_KEY',$null,'User')")
    print()

    results: list[tuple[str, str, float, str]] = []
    for name in names:
        _file, desc, _k, _s = SUITES[name]
        print(f"── {name}: {desc}")
        status, dt, note = run_one(name)
        results.append((name, status, dt, note))
        marker = {PASS: "✅", FAIL: "❌", SKIP: "⏭", MISSING: "⬜"}[status]
        print(f"   {marker} {status}" + (f"  ({dt:.1f}s)" if dt else "") +
              (f"  — {note}" if note else ""))
        print()

    print("=" * 74)
    n_pass = sum(1 for _, s, _, _ in results if s == PASS)
    n_fail = sum(1 for _, s, _, _ in results if s == FAIL)
    n_skip = sum(1 for _, s, _, _ in results if s == SKIP)
    n_miss = sum(1 for _, s, _, _ in results if s == MISSING)
    print(f"通过 {n_pass} | 失败 {n_fail} | 跳过 {n_skip} | 未编写 {n_miss}")

    if n_fail:
        print("\n失败项：")
        for name, s, _, note in results:
            if s == FAIL:
                print(f"  - {name}: {note}")
    if n_miss:
        print("\n尚未编写的冒烟脚本：")
        for name, s, _, _ in results:
            if s == MISSING:
                print(f"  - {name} ({SUITES[name][0]})")
    if n_skip:
        print("\n跳过项（设置 SILICONFLOW_API_KEY 后可运行）：")
        for name, s, _, _ in results:
            if s == SKIP:
                print(f"  - {name}")

    return 1 if n_fail else 0


if __name__ == "__main__":
    sys.exit(main())
