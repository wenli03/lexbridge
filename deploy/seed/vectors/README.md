# 预计算向量

`Qwen__Qwen3-Embedding-8B.jsonl` —— 319 条法条的 embedding 向量，**随仓库提交**。

## 为什么把它提交进仓库

演示形态是「clone → `docker compose up` → 能玩」，不能要求使用者先申请一个
embedding API key。因此向量在开发机上用真实接口算**一次**，按内容哈希固化，
之后灌库只查表：零网络、零成本、结果确定。

这与 `ai-service/app/chains/replay.py` 是同一个思路——真实调用一次、离线无限重放；
区别只在于回放的是模型响应、这里回放的是向量，因此两者共用同一种
「按内容哈希寻址」的约定。

## 格式

JSONL，一行一条：

```json
{"text_hash":"<sha256(嵌入文本) 前 32 位十六进制>","model":"Qwen/Qwen3-Embedding-8B","dim":1024,"vector":"<base64>"}
```

- **`vector` 是 base64 编码的小端 float32**，不是 JSON 浮点数组。
  这样存有两个理由：体积（一条约 5.5KB 而不是 19KB），以及**保真**——
  pgvector 的 `vector` 类型底层就是 float4，走 JSON 会先降到 float64 再由数据库降回来，
  白跑一趟。
- **寻址键是「实际被嵌入的文本」的哈希**，不是法条 ID。被嵌入的文本由
  `app/indexing/embed.py:embedding_text()` 拼成（`法规名 条号：正文`）并按
  `MAX_EMBED_CHARS` 截断。因此**改拼装规则或改截断长度都会让全部哈希失配**，
  必须重跑生成脚本。
- 生成侧与消费侧共用 `app/indexing/seed_vectors.py` 的 `text_hash()` / `prepare_text()`。
  各写一份的后果是查表全部未命中，而它看起来像"向量文件没生成对"。

## 覆盖范围：3 部完整法规、319 条

| 法域 | 法规 | 条数 |
| --- | --- | ---: |
| IE | Houses of the Oireachtas Commission Act 2003 | 21 |
| IE | Finance Act, 1997 | 166 |
| NL | 1990 年税收征收法（Invorderingswet 1990） | 132 |
| | **合计** | **319** |

语料总共 2,562 条，这里覆盖 319 条（12.5%）。

**按「整部法规」选取，而不是所有法规的前 N 条。** 截断式子集会让同一部法规里
一半条文有向量、另一半没有，检索命中哪一条取决于运气；整部选取至少让覆盖范围
是可描述的。选取规则是「按法域、条数升序、法规名排序，依次整部收入，
直到再加入一部就超出 `SUBSET_MAX_ARTICLES`」——结果确定、可复现。

为什么不全量：2,562 条的向量约 13MB，会让 `git clone` 明显变慢。319 条是
1.69MB，是"仓库不臃肿"与"覆盖得像个真知识库"之间的取舍。

**未被覆盖的法条不是错误状态。** `kb.article_vector` 是可选行：没有向量的法条
照常展示、照常被关键词检索（`kb.article.content_tsv` 的 GIN 索引与向量无关），
只是语义检索覆盖不到它。`embed.py:_count_pending` 本来就会把它们计为"待向量化"。

## 重新生成

```bash
# 先看看会选哪些法规、多少条，不调用接口
cd ai-service && uv run python ../scripts/build_demo_vectors.py --dry-run

# 实际生成（需要 deploy/.env 里的 SILICONFLOW_API_KEY，会产生少量费用）
cd ai-service && MODEL_MODE=real uv run python ../scripts/build_demo_vectors.py
```

生成后请同步更新本文档的覆盖清单与条数。

> **什么时候必须重跑**：语料更新、`embedding_text()` 的拼装规则变化、
> `MAX_EMBED_CHARS` 调整、或换 embedding 模型。
> 灌库时如果发现文件里有向量在库中找不到对应法条，会打 WARNING 提示——
> 那正是"语料与向量不同步"的信号。
