#!/usr/bin/env python3
"""可执行位检查：带 shebang 的文件必须是 100755。

## 为什么需要这个检查

**Windows 上 git 看不见可执行位。** `core.filemode` 默认为 false，
于是 `git add` 一律记成 `100644`——不管文件在磁盘上是不是可执行。
提交时没有任何提示，本机运行也一切正常（Windows 不靠权限位决定能不能跑），
所以这个问题在 Windows 开发者的机器上**不可能被发现**。

它只在 Linux 上暴露，而且是以一种误导性的方式：

    /home/runner/work/_temp/xxxx.sh: line 1: ./mvnw: Permission denied
    错误：进程以退出码 126 结束

看起来像 CI runner 出了问题或路径写错，而真正的原因是**仓库里少了一个权限位**。
实测就栽在这里一次：Java job 8 秒红掉，而本机 `mvnw verify` 完全正常。

## 判据为什么是 shebang

`#!` 是一句声明：「这个文件是用来直接执行的」。既然如此，它在仓库里就必须可执行——
两者不一致时，一定是有人的环境没有把这件事记下来，而不是有人故意要这么配。

反过来说，不带 shebang 的脚本（例如 `capture_screenshots.mjs`，约定用 `node` 调用）
不受这条约束，它们保持 644 是正确的。

## 修法

    git update-index --chmod=+x <path>

注意它改的是**索引**里的模式位，不依赖文件系统——这正是 Windows 上唯一可行的办法。

退出码：0 = 全部正确。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

EXEC_MODE = "100755"


def run_git(*args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=ROOT, capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )
    if result.returncode != 0:
        raise SystemExit(f"git {' '.join(args)} 失败：{result.stderr.strip()}")
    return result.stdout


def main() -> int:
    print("=" * 74)
    print("可执行位检查")
    print("=" * 74)

    # `git ls-files -s` 的每一行：<mode> <object> <stage>\t<path>
    entries: list[tuple[str, str]] = []
    for line in run_git("ls-files", "-s").splitlines():
        meta, _, path = line.partition("\t")
        parts = meta.split()
        if len(parts) >= 3 and path:
            entries.append((parts[0], path))

    problems: list[str] = []
    checked = 0

    for mode, path in entries:
        file_path = ROOT / path
        try:
            with file_path.open("rb") as fh:
                if fh.read(2) != b"#!":
                    continue
        except OSError:
            continue  # 索引里有、工作区没有（例如被删除但未提交）——不是本检查的职责

        checked += 1
        if mode != EXEC_MODE:
            problems.append(f"{path}  (索引里是 {mode}，应为 {EXEC_MODE})")

    print(f"\n带 shebang 的受控文件：{checked} 个")

    if problems:
        print(f"\n[失败] {len(problems)} 个文件没有可执行位：\n")
        for item in problems:
            print(f"  - {item}")
        print(
            "\n修法：\n"
            "  git update-index --chmod=+x <path>\n"
            "\n"
            "Windows 上 git 看不见权限位（core.filemode=false），"
            "必须用上面这条命令改索引，而不是改文件系统属性。\n"
            "不修的话，Linux 上的 CI 会以 './xxx: Permission denied' 失败——"
            "那条错误看起来像 runner 的问题。"
        )
        return 1

    print("[通过] 所有带 shebang 的文件都已标记为可执行")
    return 0


if __name__ == "__main__":
    sys.exit(main())
