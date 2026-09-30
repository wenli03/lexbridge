"""测试公共设施：路径、Windows 事件循环策略、数据库 fixture。

需要数据库的测试统一用 `db` fixture 与 `requires_db` 标记：
两个测试文件各写一份连接与清理代码的话，它们迟早会漂移
（一处清了 `kb.article_vector`、另一处忘了），而"清理不干净"的表现是
测试之间互相干扰——顺序变了就红，单跑却绿，是最费时间的一类失败。
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest

#: `ai-service/` —— 供 `import app` 使用
PROJECT_ROOT = Path(__file__).resolve().parent.parent
#: 仓库根 —— 语料、迁移等跨服务资源的位置，与上一个不是同一个目录。
#: 这两个必须分开：`PROJECT_ROOT` 用于 sys.path（要的是 `ai-service/`），
#: 而语料在仓库根的 `deploy/` 下；混用一个会让"找不到语料"这种错误
#: 看起来像"语料不存在"，而实际是路径算错了一层。
REPO_ROOT = PROJECT_ROOT.parent

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# Windows 默认的 ProactorEventLoop 不被 psycopg 的异步模式支持，直接用会抛
# `InterfaceError: Psycopg cannot use the 'ProactorEventLoop' to run in async mode`。
#
# 为什么修在这里而不是放弃异步：生产环境是 Linux 容器（走 Selector/Epoll，没有这个问题），
# 受影响的只是"在 Windows 上跑需要数据库的测试"这一条路径。若不修，本机开发时
# 所有涉及数据库的测试都跑不了，于是它们实际上不会被运行——**一个跑不了的测试
# 等于没有测试**。策略必须在事件循环创建之前设置，所以放在 conftest 的导入期。
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


DATABASE_URL = os.environ.get("TEST_DATABASE_URL")

requires_db = pytest.mark.skipif(
    not DATABASE_URL,
    reason="未设置 TEST_DATABASE_URL，跳过需要真实数据库的测试",
)


@pytest.fixture
async def db():
    """一个连到 `TEST_DATABASE_URL` 的连接，用前清空 `kb` 的业务表。

    **必须以非表属主角色连接**（如 `lexbridge_ai`）：表属主绕过 RLS，
    而以属主连接会让所有"租户隔离"相关的断言恒真。
    """
    assert DATABASE_URL is not None
    # 延迟导入：纯逻辑测试不该因为没装 psycopg 而整个 conftest 导入失败
    import psycopg
    from psycopg.rows import dict_row

    conn = await psycopg.AsyncConnection.connect(DATABASE_URL, row_factory=dict_row)
    try:
        # 按外键顺序清理。不用 TRUNCATE：非属主角色没有该权限，
        # 而"为了跑测试给一个服务角色授予 TRUNCATE"是把测试的便利塞进生产权限里。
        for table in ("kb.article_vector", "kb.article", "kb.statute_version", "kb.statute"):
            await conn.execute(f"DELETE FROM {table}")  # noqa: S608 - 表名是常量
        await conn.commit()
        yield conn
    finally:
        await conn.close()


def load_corpus_groups():
    """加载仓库内已采集的真实语料并按版本分组。

    语料文件不在工作区（部分检出）时直接失败而不是跳过：
    需要真实语料的测试若静默跳过，会让人误以为"跑过了"。
    """
    from app.indexing.seed import group_by_version, load_corpus

    files = sorted((REPO_ROOT / "deploy" / "seed" / "corpus").glob("*.jsonl"))
    assert files, "工作区里找不到 deploy/seed/corpus/*.jsonl"
    rows, problems = load_corpus(files)
    assert problems == [], f"真实语料有问题：{problems[:3]}"
    return group_by_version(rows), len(rows)
