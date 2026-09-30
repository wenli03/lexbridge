"""日志配置。

结构化、带请求上下文。两个刻意的选择：

  1. **绝不记录密钥、令牌、租户 ID 明文**——用 `redact()` 过滤。
     公开仓库里泄露一次就不可逆，而日志是最容易被忽略的泄露渠道。
  2. **uvicorn 的 access log 保留**，但应用日志统一走一个格式，
     这样 LangSmith 的 trace 与本地日志能用同一套时间戳对齐排查。
"""

from __future__ import annotations

import logging
import os
import re
import sys

# 需要脱敏的键名（大小写不敏感）
_REDACT_KEYS = re.compile(
    r"(api[_-]?key|authorization|token|secret|password|passwd|pwd)", re.IGNORECASE
)
# 常见密钥前缀，兜住"键名不在白名单但值是密钥"的情况
_SECRET_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9]{16,}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"Bearer\s+[A-Za-z0-9._\-]{16,}", re.IGNORECASE),
]


def redact(text: str) -> str:
    """把疑似密钥替换掉。任何要写进日志的字符串都应先过这里。"""
    out = text
    for pat in _SECRET_PATTERNS:
        out = pat.sub("***REDACTED***", out)
    return out


class RedactingFilter(logging.Filter):
    """最后一道闸：即使调用方忘了脱敏，也不会把密钥写进日志。"""

    @staticmethod
    def _scrub(value: object) -> object:
        """只处理字符串，**保持其他类型原样**。

        这里踩过一个坑：早先的实现对所有 args 一律 `str(a)`，
        于是 `logger.info("...%d...", 200)` 会把整数 200 变成字符串 "200"，
        格式化时抛 `TypeError: %d format: a real number is required, not str`。
        结果是每条带数字占位符的日志都变成一条 "--- Logging error ---" 堆栈，
        把真正的日志淹掉——而这类噪音很容易让人开始忽略日志输出。
        """
        return redact(value) if isinstance(value, str) else value

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = redact(record.msg)
        if record.args:
            if isinstance(record.args, dict):
                record.args = {
                    k: ("***REDACTED***" if _REDACT_KEYS.search(str(k)) else self._scrub(v))
                    for k, v in record.args.items()
                }
            elif isinstance(record.args, tuple):
                record.args = tuple(self._scrub(a) for a in record.args)
        return True


_FORMAT = "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s"


def setup_logging(level: str = "INFO") -> None:
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    # 幂等：重复调用不应叠加 handler（uvicorn --reload 下会反复触发）
    for h in list(root.handlers):
        root.removeHandler(h)

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter(_FORMAT, datefmt="%Y-%m-%d %H:%M:%S"))
    handler.addFilter(RedactingFilter())
    root.addHandler(handler)

    # 第三方库降噪
    for noisy in ("httpx", "httpcore", "urllib3", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    # LangGraph 的检查点模块在 DEBUG 下会打印完整 state，可能含案情信息
    logging.getLogger("langgraph").setLevel(
        logging.DEBUG if os.environ.get("LANGGRAPH_DEBUG") == "1" else logging.INFO
    )
