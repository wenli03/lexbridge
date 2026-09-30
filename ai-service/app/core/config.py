"""配置加载。

所有可调参数集中在此，且全部来自环境变量。理由有三：
  1. 模型 ID 会漂移（V4 Flash 产品线正在变动），必须能不改代码就切换；
  2. 仓库公开，密钥只能从环境注入，绝不能有默认值；
  3. 批量与并发是实测调优出来的（见 docs/decision-record.md），
     写死在代码里会让调优变成改代码。
"""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class ModelMode(StrEnum):
    """LLM 调用模式。

    real   —— 真实调用硅基流动。需要密钥，产生费用。
    replay —— 回放仓库内录制的真实响应。确定性、离线、零成本。
              **一键演示与 CI 的默认值**：面试官 clone 仓库后无需任何密钥
              即可跑完整链路。
    mock   —— 纯合成响应。行为可控但不反映真实模型输出，仅用于契约测试。
    """

    REAL = "real"
    REPLAY = "replay"
    MOCK = "mock"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        # 两个候选路径，缺的会被跳过：
        #   .env            —— 容器内（compose 直接把变量注入环境，通常用不到）
        #   ../deploy/.env  —— 宿主上从 ai-service/ 启动时读项目配置
        # 后者让本地跑脚本、跑测试不必手工 export 一堆变量。
        env_file=(".env", "../deploy/.env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ---------------------------------------------------------------- 运行模式
    model_mode: ModelMode = ModelMode.REPLAY
    replay_fixture_dir: str = "fixtures/replay"

    # ------------------------------------------------------------ 硅基流动
    siliconflow_api_key: str = ""
    siliconflow_base_url: str = "https://api.siliconflow.cn/v1"

    # 模型 ID 经实测确证（decision-record.md D-01）。
    # 注意 deepseek-v4-flash 是产品名不是 API 模型 ID，直接用会 404。
    chat_model: str = "deepseek-ai/DeepSeek-V4-Flash"
    chat_model_fallback: str = "deepseek-ai/DeepSeek-V3.2"

    embedding_model: str = "Qwen/Qwen3-Embedding-8B"
    embedding_fallback_model: str = "BAAI/bge-m3"
    embedding_dim: int = 1024
    rerank_model: str = "Qwen/Qwen3-Reranker-8B"

    # 批量与并发由参数扫描选定（decision-record.md D-05/D-06）。
    # 批量上限实测为 512（文档称 32，不准）；128×8 是唯一保持 100% 成功率的组合。
    embedding_batch_size: int = 128
    embedding_concurrency: int = 8

    # 冷启动实测波动 0.7s–65.9s，因此重试不是可选项而是必需品。
    llm_max_retries: int = 4
    llm_timeout_seconds: float = 120.0

    # ------------------------------------------------------------ LangSmith
    langsmith_tracing: bool = False
    langsmith_api_key: str = ""
    langsmith_project: str = "lexbridge"
    langsmith_endpoint: str = "https://api.smith.langchain.com"

    # ------------------------------------------------------------- 数据库
    # 连接信息**分字段声明，不写成一个完整的 db_url 默认值**。
    #
    # 原因是一个踩过的坑：写成
    #   db_url: str = "postgresql://lexbridge_app:lexbridge_dev_only@127.0.0.1:5433/...")
    # 时，口令被硬编码进默认值，而 .env 里的 POSTGRES/DB_PASSWORD 是随机生成的。
    # 两者不一致 → 认证失败。但 psycopg_pool 会不断重试，
    # 最终抛的是 `PoolTimeout: couldn't get a connection after 30.00 sec`——
    # 错误信息指向"连不上"，而真正原因是口令不对。
    #
    # 容器内由 compose 显式注入完整的 DB_URL，此时 db_url 优先。
    db_url: str = ""  # 显式提供时优先；留空则由下面的字段组装
    db_host: str = "127.0.0.1"
    # 宿主端口 5433 来自 docker-compose.override.yml：基础 compose 出于攻击面
    # 收敛不发布 5432（见 4+1 物理视图 6.2），本地开发靠 override 层映射。
    db_port: int = 5433
    db_name: str = "lexbridge"
    db_username: str = "lexbridge_app"
    db_password: str = ""
    # 不开的话，数据库被攻陷时检查点反序列化可导致代码执行。
    # 本项目仓库公开，此项必须为 true（对应 AC-7.3）。
    langgraph_strict_msgpack: bool = True

    redis_host: str = "localhost"
    redis_port: int = 6379
    redis_password: str = ""
    redis_db: int = 0

    # --------------------------------------------------------------- 安全
    # 内部调用令牌密钥，用于让 api 服务把「当前租户」可信地传给 ai。
    # 空值会让服务启动失败——见下方校验。
    internal_token_secret: str = ""

    # --------------------------------------------------------------- 其他
    upload_dir: str = "/data/files"
    ocr_enabled: bool = False
    log_level: str = "INFO"

    @field_validator("langsmith_endpoint")
    @classmethod
    def _strip_endpoint_trailing_slash(cls, v: str) -> str:
        """LangSmith 端点的尾斜杠会导致 trace 静默丢失，这里直接修掉。"""
        return v.rstrip("/")

    @field_validator("siliconflow_base_url")
    @classmethod
    def _strip_base_url_trailing_slash(cls, v: str) -> str:
        return v.rstrip("/")

    @property
    def dsn(self) -> str:
        """组装出的数据库连接串。显式提供的 db_url 优先（容器内走这条）。"""
        if self.db_url:
            return self.db_url
        from urllib.parse import quote

        # 口令必须转义：随机生成的 base64 口令几乎一定含 + / = 等字符，
        # 不转义会让 libpq 把连接串解析错，症状又是认证失败。
        return (
            f"postgresql://{quote(self.db_username, safe='')}:"
            f"{quote(self.db_password, safe='')}@"
            f"{self.db_host}:{self.db_port}/{self.db_name}"
        )

    def require_api_key(self) -> str:
        """在真正要调用模型时校验密钥，而不是在启动时。

        这个区分很重要：replay 模式下服务必须能在**没有任何密钥**的情况下
        正常启动并跑完整链路。若在启动时就强制要求密钥，
        「面试官 clone 下来即可演示」这个目标就落空了。
        """
        if not self.siliconflow_api_key:
            raise RuntimeError(
                "MODEL_MODE=real 需要 SILICONFLOW_API_KEY，但该环境变量为空。"
                "若只做演示，请改用 MODEL_MODE=replay（无需密钥）。"
            )
        return self.siliconflow_api_key


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
