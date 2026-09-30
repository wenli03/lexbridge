#!/usr/bin/env python3
"""密钥扫描 —— 对应 AC-7.3「仓库中无任何密钥」。

**为什么这个脚本要早写而不是留到 P5**：项目从一开始就会把真实凭据写进
`deploy/.env`（本地开发必需），而从"文件被 gitignore"到"文件真的不会被提交"
之间有一段靠人记性维持的距离。这类事故一旦发生就是不可逆的——
推到公开仓库后，即使立刻删除，fork 与缓存里仍然留着。

设计上做了两件超出简单 grep 的事：

  1. **只扫 git 会提交的文件**。用 `git ls-files` 加上
     `git ls-files --others --exclude-standard` 求并集，精确等于
     "下次 commit 会带上什么"。扫描被忽略的文件只会产生噪音，
     而噪音会让人开始忽略这个脚本的输出。

  2. **显式断言关键文件确实被忽略**。`deploy/.env` 必须被挡住；
     `.env.example` 必须存在且不含真实值。这两条比模式匹配更重要——
     它们是"配置正确"与"配置看起来正确"的区别。

用法：
    python scripts/verify_no_secrets.py
退出码：0 = 干净；1 = 发现问题。
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[1]

# ---------------------------------------------------------------------------
# 必须被忽略的路径：这些文件**按设计**包含真实凭据
# ---------------------------------------------------------------------------
MUST_BE_IGNORED = ["deploy/.env", "deploy/.env.local"]

# ---------------------------------------------------------------------------
# 必须存在且不含真实值的模板
# ---------------------------------------------------------------------------
TEMPLATES = ["deploy/.env.example"]

# ---------------------------------------------------------------------------
# 密钥模式。宁可多报也不能漏报——漏报的代价是不可逆的。
# ---------------------------------------------------------------------------
PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("硅基流动 API Key", re.compile(r"sk-[A-Za-z0-9]{20,}")),
    ("GitHub PAT (fine-grained)", re.compile(r"github_pat_[A-Za-z0-9_]{20,}")),
    ("GitHub token", re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}")),
    # LangSmith 的密钥形如 lsv2_pt_xxxxx 或 ls__xxxxx —— 前缀后**必须紧跟下划线**。
    #
    # 最初的写法是 r"ls[v_]?[A-Za-z0-9_]{20,}"，结果把
    # `UserDetailsServiceAutoConfiguration` 里的 "ls" + "ServiceAutoConfiguration"
    # 也匹配成了密钥，一次扫描报出 5 个误报。
    #
    # 误报的代价不是"多看一眼"：它会让人开始习惯性地忽略扫描输出，
    # 而真正的泄露就藏在同一份输出里。宁可窄一点。
    ("LangSmith key", re.compile(r"\bls(?:v2)?_[A-Za-z0-9_]{20,}\b")),
    ("AWS Access Key", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("私钥块", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |PGP )?PRIVATE KEY-----")),
    ("JWT", re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")),
    ("OpenAI 风格 key", re.compile(r"(?:api[_-]?key|token|secret)\s*[:=]\s*['\"]?[A-Za-z0-9_-]{32,}")),
]

# 允许出现的例外：示例值、占位符、测试夹具
ALLOWLIST = re.compile(
    r"(your[-_]?key|placeholder|example|changeme|xxx+|\*\*\*|REDACTED|<[^>]+>|\$\{)",
    re.IGNORECASE,
)

# 不扫的路径（二进制、依赖、构建产物）
SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "target", "dist",
             "__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache"}
SKIP_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".ico", ".pdf", ".zip",
                 ".gz", ".whl", ".jar", ".class", ".woff", ".woff2", ".ttf"}

MAX_FILE_BYTES = 2 * 1024 * 1024


def run_git(*args: str) -> tuple[int, str]:
    try:
        p = subprocess.run(["git", *args], cwd=ROOT, capture_output=True,
                           text=True, encoding="utf-8", errors="replace")
        return p.returncode, p.stdout
    except FileNotFoundError:
        return 127, ""


def files_git_would_commit() -> list[Path] | None:
    """返回 git 会提交的文件列表；不是 git 仓库时返回 None。

    这个集合 = 已跟踪文件 ∪ 未跟踪且未被忽略的文件。
    它的补集正是"被 .gitignore 挡住的东西"，也就是我们信任的那部分。
    """
    code, _ = run_git("rev-parse", "--is-inside-work-tree")
    if code != 0:
        return None

    paths: set[str] = set()
    for args in (("ls-files",), ("ls-files", "--others", "--exclude-standard")):
        code, out = run_git(*args)
        if code == 0:
            paths.update(line.strip() for line in out.splitlines() if line.strip())

    return [ROOT / p for p in sorted(paths)]


def all_files_fallback() -> list[Path]:
    """非 git 仓库时的退路：遍历整个目录树。"""
    out: list[Path] = []
    for p in ROOT.rglob("*"):
        if any(part in SKIP_DIRS for part in p.parts):
            continue
        if p.is_file():
            out.append(p)
    return out


def scan_file(path: Path) -> list[tuple[int, str, str]]:
    """返回 [(行号, 模式名, 该行内容截断)]"""
    if path.suffix.lower() in SKIP_SUFFIXES:
        return []
    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            return []
        text = path.read_text(encoding="utf-8", errors="strict")
    except (UnicodeDecodeError, OSError):
        return []

    hits = []
    for lineno, line in enumerate(text.splitlines(), 1):
        for name, pat in PATTERNS:
            if pat.search(line) and not ALLOWLIST.search(line):
                hits.append((lineno, name, line.strip()[:120]))
                break
    return hits


def main() -> int:
    print("=" * 74)
    print("密钥扫描")
    print("=" * 74)

    problems: list[str] = []
    warnings: list[str] = []

    # --- 检查 1：关键文件确实被忽略 -------------------------------------
    print("\n[1/3] 检查含真实凭据的文件是否被 git 挡住")
    is_repo = run_git("rev-parse", "--is-inside-work-tree")[0] == 0
    if not is_repo:
        warnings.append("当前不是 git 仓库，无法验证 .gitignore 是否生效")
        print("      ⚠️  不是 git 仓库，跳过（P6 执行 git init 后此项才会生效）")
    else:
        for rel in MUST_BE_IGNORED:
            p = ROOT / rel
            if not p.exists():
                print(f"      · {rel}: 不存在，跳过")
                continue
            code, _ = run_git("check-ignore", "-q", rel)
            if code == 0:
                print(f"      ✅ {rel}: 已被忽略")
            else:
                problems.append(f"{rel} 含真实凭据，却**没有**被 .gitignore 挡住")
                print(f"      ❌ {rel}: 未被忽略 ← 会被提交")

    # --- 检查 2：模板文件不含真实值 -------------------------------------
    print("\n[2/3] 检查配置模板")
    for rel in TEMPLATES:
        p = ROOT / rel
        if not p.exists():
            problems.append(f"缺少配置模板 {rel}")
            print(f"      ❌ {rel}: 不存在")
            continue
        hits = scan_file(p)
        if hits:
            problems.append(f"{rel} 疑似含真实密钥（第 {hits[0][0]} 行：{hits[0][1]}）")
            print(f"      ❌ {rel}: 疑似含真实密钥 — {hits[0][1]} 在第 {hits[0][0]} 行")
        else:
            print(f"      ✅ {rel}: 只有占位符")

    # --- 检查 3：扫描将被提交的文件 -------------------------------------
    print("\n[3/3] 扫描将被提交的文件")
    files = files_git_would_commit() if is_repo else all_files_fallback()
    if files is None:
        files = all_files_fallback()
        scope = f"整个目录树（{len(files)} 个文件）"
    else:
        scope = f"git 会提交的 {len(files)} 个文件"

    print(f"      作用域：{scope}")
    found: list[tuple[Path, int, str, str]] = []
    for p in files:
        for lineno, name, snippet in scan_file(p):
            found.append((p, lineno, name, snippet))

    if not found:
        print("      ✅ 未发现密钥")
    else:
        for p, lineno, name, snippet in found:
            rel = p.relative_to(ROOT)
            problems.append(f"{rel}:{lineno} 疑似 {name}")
            print(f"      ❌ {rel}:{lineno}  疑似 {name}")
            print(f"           {snippet}")

    # --- 汇总 -----------------------------------------------------------
    print("\n" + "=" * 74)
    if warnings:
        for w in warnings:
            print(f"⚠️  {w}")
    if problems:
        print(f"发现 {len(problems)} 个问题：")
        for pr in problems:
            print(f"  - {pr}")
        print("\n在解决之前不要提交。推到公开仓库后即使删除，fork 与缓存里仍然留着。")
        return 1
    print("✅ 未发现密钥泄露风险")
    return 0


if __name__ == "__main__":
    sys.exit(main())
