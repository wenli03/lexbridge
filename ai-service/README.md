# LexBridge AI 服务

Python 侧运行时，承载 LangChain / LangGraph / DeepAgent / LlamaIndex。

## 为什么存在这个服务

技术栈里 Spring Boot（JVM）与 LangChain、LangGraph、DeepAgent、LlamaIndex（均为
Python 原生库）无法共处一个进程。项目约束要求不新增未列出的技术，因此采用双运行时：
Spring Boot `api` 承担业务主干（租户、权限、审计、会话、发布），本服务承担 AI 链路。
两者通过内网 HTTP 通信，`ai` 不发布宿主端口。

## 三模式：real / replay / mock

这是本服务最值得说明的设计（详见 `docs/decision-record.md` D-09 与《详细设计》§11.4）。

| 模式 | 行为 | 需要密钥 | 用途 |
| --- | --- | --- | --- |
| `real` | 真实调用硅基流动 | 是 | 开发与真实演示 |
| `replay` | 回放仓库内录制的真实响应 | **否** | **`docker compose up` 的默认值**、CI |
| `mock` | 合成响应 | 否 | 契约测试 |

**`replay` 模式让面试官 clone 仓库后无需任何密钥即可完整演示**，
同时使 CI 不必把 API Key 放进 GitHub Secrets。

所有需要模型的地方都经 `app/chains/model_factory.py`，任何地方都不直接
`ChatOpenAI(...)`——这条纪律是三模式能成立的前提。

## 目录

```
app/
├─ api/            内部 HTTP 接口（供 api 服务调用）
├─ graph/          LangGraph 图定义与检查点装配
├─ agents/         DeepAgent 封装（长报告路径）
├─ chains/         LangChain：模型工厂、提示、解析、工具
├─ indexing/       LlamaIndex：解析、切分、索引
├─ retrieval/      路由式混合检索与重排
├─ ontology/       本体 schema、抽取与查询
├─ wiki/           LLM wiki 生成与引用校验
├─ redline/        红线规则引擎（不得依赖 chains —— DR-4）
├─ observability/  LangSmith 装配
└─ core/           配置、日志、异常、数据库
```

## 架构约束由 CI 强制

`pyproject.toml` 里的 `[tool.importlinter]` 把架构规则变成了可执行的门禁。
最关键的一条是 **DR-4：`redline` 不得 import `chains`**——红线判定若依赖模型生成，
就存在「模型被说服后绕过红线」的风险。把它固化到代码结构里，
比写在文档里靠人自觉可靠得多。

## 开发

```bash
uv sync --extra dev

# 静态检查与架构门禁
uv run ruff check .
uv run mypy app
uv run lint-imports

# 测试（默认不含需要真实 API 的用例）
uv run pytest
uv run pytest -m real_api          # 需要余额与密钥
```
