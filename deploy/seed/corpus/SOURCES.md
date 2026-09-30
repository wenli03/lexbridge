# 语料来源与可得性实测

本文件记录**六法域公开法规语料的采集结论**：哪些源能拿到、哪些拿不到、拿不到的确切原因。

为什么要写这份文件：AC-1.6 要求六法域合计 ≥5,000 条法条。当前只达成 2 个法域、2,562 条。
与其把差距藏起来，不如把**每个法域卡在哪里**写成可复核的证据——下一个人接着做时，
不必重复探测这六个站点。

- 采集工具：`scripts/fetch_corpus.py`（只用标准库，无需安装依赖）
- 校验工具：`scripts/verify_corpus.py`
- 复现命令：`python scripts/fetch_corpus.py fetch all --limit 4` 然后 `python scripts/verify_corpus.py`

---

## 汇总

| 法域 | 状态 | 条数 | 法规数 | 阻断原因 |
| --- | --- | ---: | ---: | --- |
| CN 中国内地 | ❌ 未采集 | 0 | 0 | 正文存于站点内网 OBS，外部不可达 |
| HK 香港 | ❌ 未采集 | 0 | 0 | 站点为 JS 单页应用，HTTP 只返回外壳 |
| SG 新加坡 | ❌ 未采集 | 0 | 0 | AWS WAF JavaScript 挑战 |
| IE 爱尔兰 | ✅ 已采集 | 1,508 | 4 | — |
| NL 荷兰 | ✅ 已采集 | 1,054 | 4 | — |
| KY 开曼群岛 | ❌ 未采集 | 0 | 0 | 返回 202 前端渲染，法规以 PDF 为主 |
| **合计** |  | **2,562** | **8** | 目标 5,000 条 / 6 法域 |

日期精度：IE 全部为 `YEAR`（站点不提供机器可读的通过日期），NL 全部为 `DAY`。

---

## 已修复缺陷：IE 最后一条吞掉附表（残留 2 条待查）

### 缺陷与修复

**症状**：条文本该是"条"的粒度，但个别行的长度是正常值的两个数量级。

| 法规 | 修复前最长 | 修复后最长 |
| --- | ---: | ---: |
| Taxes Consolidation Act, 1997（1,104 条） | **376,126** | **24,309** ✅ |
| Finance Act, 1997（166 条） | **104,527** | **13,406** ✅ |
| Finance Act, 1999（217 条） | 72,821 | 72,821 ⚠️ |
| Houses of the Oireachtas Commission Act 2003（21 条） | 9,611 | 9,611 ⚠️ |
| NL 四部法律 | ≤ 12,976 | ≤ 12,976（无此问题） |

**原因（已定位）**：`_ie_sections` 按 `<a name="secN">` 锚点切分，**最后一条的结束位置
取的是整个 body 的末尾**；而正文之后还有附表（Schedules），附表里没有 `secN` 锚点，
于是被整块并入了最后一条。实测《税收合并法》`Section 1104` 的 376,126 字符里，
真正属于该条的只有开头约 8,700 字符。

**修复**：把 `<a name="sched…">` 锚点也当作切分边界（`scripts/fetch_corpus.py`）。
附表**不被采集**——它是"表"而不是"条"，粒度与引用模型不同；这一取舍是显式的，
写在 `_ie_sections` 的 docstring 里。

**回归防护**：`python scripts/fetch_corpus.py selftest`（离线、零依赖、一秒）。
它把"附表不得并入条文"固化成一条断言——这个缺陷第一次是**靠事后统计发现的**，
而不是靠测试拦住的。

**未采用**"直接删掉这几条"的做法：那会把"附表没被正确切分"这个事实一起删掉。

### 残留：两条仍然偏长

`Finance Act, 1999` 的 `Section 27`（72,821 字符）与
`Houses of the Oireachtas Commission Act 2003` 的 `Section 4`（9,611 字符）在修复后不变，
说明它们**不是同一个原因**：

- 前者不是该法案的最后一条，所以"附表边界"这条规则不适用于它——
  更可能是页面里缺少某个 `secN` 锚点，导致一条吞掉了后续若干条；
- 后者（9,611 字符，约为中位数的 7 倍）**可能本来就是一条长条文**，
  把它当成缺陷需要先核对来源原文。

**当前处置**：如实保留，未做任何裁剪。两者的共同点是都还没有核对过来源原文——
在核对之前，任何"修掉它"的动作都是猜测。核对方法见下节的可复现命令。

---

## IE 爱尔兰 — irishstatutebook.ie

**可用。** 每个法案的 print 视图一次请求即含全部条文。

- 列表视图：`/eli/{year}/act/{num}/enacted/en/html`（约 0.8MB，用于取法规名与元数据）
- 全文视图：`/eli/{year}/act/{num}/enacted/en/print`（约 8.7MB/法案）
- 条文锚点：`<a name="secN">`；段级锚点为 `name="sN_pM"`（形如 `s832_p2`），
  因此切分正则为 `name="sec(\d+[A-Z]?)"` 不会误切段落。

**踩过的坑**：

1. **最早的实现把法规名写死在代码里，写错了。** ELI `1997/act/39` 被标成「Finance Act 1997」，
   实际页面 `<title>` 是 **Taxes Consolidation Act, 1997**。
   现在法规名一律从页面 `<title>` 读取，并校验长度与内容。
2. **站点不提供机器可读的通过日期。** `<meta>` 里只有 `DC.Title/Creator/Subject/Publisher/Language`，
   没有 `DC.Date`；正文里的日期是"Acts Referred to"表格中**其它**法案的日期，不能当作本法案的。
   因此 IE 记录标注 `date_precision = "YEAR"`，`effective_from` 取 ELI 路径中的年份的 1 月 1 日。
   这是**精度损失，不是估算**——引用里会带上"精度：年"，读者知道我们只知道到年。

已采集法规：

| 法案 | 条数 |
| --- | ---: |
| Taxes Consolidation Act, 1997 | 1,104 |
| Finance Act, 1999 | 217 |
| Finance Act, 1997 | 166 |
| Houses of the Oireachtas Commission Act 2003 | 21 |

> `2003/act/28` 是柯林斯式误打误撞：它确实是「Houses of the Oireachtas Commission Act 2003」，
> 21 条——说明"拿得到但内容可能不是税法"。验证脚本因此不只看条数，也记录法规名供人工复核。

---

## NL 荷兰 — wetten.overheid.nl

**可用，且是本项目质量最好的源。** 服务端渲染，单篇法律一次请求即含全部条文，
并直接给出生效区间。

- 页面：`https://wetten.overheid.nl/{BWB编号}`
- 条文容器：`<div class="artikel" id="HoofdstukI_Artikel1">`
- 条号：容器内 `<h4 id="...">Artikel 1</h4>`
- 正文：`<p class="lid labeled">`（款级），避开"打印/导出/永久链接"等操作链接
- 生效区间：页头 `Geldend van 01-07-2026 t/m heden.`（**DD-MM-YYYY**，不是 ISO）

已采集法规：

| Wet | BWB | 条数 | 生效起 |
| --- | --- | ---: | --- |
| Invorderingswet 1990 | BWBR0004770 | 132 | 2026-07-01 |
| Algemene wet inzake rijksbelastingen | BWBR0002320 | 238 | 2026-04-11 |
| Wet inkomstenbelasting 2001 | BWBR0011353 | 514 | 2026-02-21 |
| Wet op de vennootschapsbelasting 1969 | BWBR0002672 | 170 | 2026-01-01 |

**候选编号的运行期校验**：BWB 编号在代码里只是"候选"，适配器会实际抓取并检查页面
是否含 ≥5 条条文、是否给出生效区间；不满足就丢弃该候选。这样即使某个编号记错，
也不会把错的内容写进语料。

---

## CN 中国内地 — flk.npc.gov.cn（国家法律法规数据库）

**接口可用，但正文不可达。**

逆向过程（`python scripts/fetch_corpus.py probe CN` 的 `JS[law-search]` 输出）：

- 站点是 Vite SPA，`/api/...` 这类猜测路径一律返回前端页面（不是 JSON）。
- 从 `/assets/index-*.js` 中实读到三个端点：
  - `POST /law-search/search/list` —— 检索（请求体未逆出，加任意参数返回 `{"code":500,"msg":"系统异常"}`）
  - `GET  /law-search/search/flfgDetails?bbbs={id}` —— **详情，可用**
  - `GET  /law-search/amazonFile/previewLink?filePath=...` —— 预览链接
- `GET /law-search/index/aggregateData` —— **可用**，无需参数即返回法规清单
  （`xfsd` 最新发布、`popularSearch` 热门检索，字段含 `bbbs` 主键、`title`、`gbrq`、`flxz`）。

`flfgDetails?bbbs=ff808081729d1efe01729d50b5c500bf`（民法典）实际返回：

```json
{"data": {
  "title": "中华人民共和国民法典", "gbrq": "2020-05-28", "sxrq": "2021-01-01",
  "flxz": "法律", "zdjgName": "全国人民代表大会",
  "content": {"id": "...", "title": "中华人民共和国民法典", "index": 0,
              "children": [{"title": "第一编 总则", "children": [
                {"title": "第一章 基本规定", "children": [{"title": "第一条"}, ...]}]}]},
  "ossFile": {"ossWordPath": "prod/20200528/827f65fcb68f40cb941eed996c5212b0.docx", ...}
}}
```

**卡点（这就是拿不到 CN 正文的原因）**：

1. `content` 条款树是**结构化导航树**：每个节点只有 `{id, index, parentId, title}`，
   **不含正文**。条号可读，条文内容读不到。
2. 正文在 `ossFile` 指向的对象存储里。取直链需经
   `GET /law-search/amazonFile/ofdGenerateLink?filePath=...`，它返回的 `download_url` 指向
   **`flkoss.obs-bj2-internal.cucloud.cn`**——内网域名，外部不可达，且 URL 带 AWS4 签名，
   改域名即失效。
3. `GET /law-search/amazonFile/previewLink` 返回的阅读器地址同样落在内网
   （`172.16.220.10:8095` / `172.16.220.27:38080`）。
4. `wb.flk.npc.gov.cn`（早期设计的正文文件域）`curl` 返回 `000`，不可达。

**结论**：CN 需要能访问该内网的机器，或改用其它可公开访问的法规正文来源。
`fetch_cn` 因此在检测到"条款树无正文"时**主动抛出**带上述证据的 `RuntimeError`，
而不是静默返回 0 条——静默返回会让人误以为"这个法域没有法条"。

---

## SG 新加坡 — sso.agc.gov.sg

**不可达。** `GET /Act/ITA1947?WholeDoc=1` 返回 **2,427 字节**的 JavaScript 挑战页：

```
<title></title> ... <h1>JavaScript is disabled</h1>
<script src="https://6ae5b796d6c9.817bb49a.us-west-1.token.awswaf.com/.../challenge.js">
```

带浏览器 UA 也一样（`curl` 初测 403，加 UA 后变成挑战页）。需要能执行 JS 的会话。

---

## HK 香港 — elegislation.gov.hk

**不可达（HTTP 层）。** `GET /hk/cap112` 跟随重定向后返回 **7,562 字节**的前端外壳
（`<title>Hong Kong e-Legislation</title>`，正文节点数为 0）。
PDF 端点 `/hk/cap112!en.assist.pdf` 返回**同样大小**的外壳页，说明它也走了前端路由。

补充尝试：`hklii.hk`（香港法律信息港）`/en/legis/ord/112/` 与站点根都返回 2,693 字节的壳页。

---

## KY 开曼群岛 — legislation.gov.ky

**不可达（HTTP 层）。** 请求返回 **202**（前端渲染），且开曼法规以 PDF 为主，
需要先定位稳定的 PDF 直链。未继续深入。

---

## 下一步（如需把 AC-1.6 做实）

按性价比排序：

1. **换源**，而不是继续硬啃上述站点：
   - HK：`elegislation.gov.hk` 有批量下载 / OData 类接口，值得从"数据下载"页面入手，
     而不是从阅读页入手。
   - SG：SSO 有官方 API（需先确认是否免 WAF）；或改走其他公开法律数据库。
   - CN：`www.gov.cn`、`www.npc.gov.cn` 的静态法规页（本次已确认可返回 200 且体量正常），
     需要额外写"列表页 → 法规页"的爬取，是独立的一次工作量。
2. **每个新法域先跑 `probe`**：本文件里所有可用结构都是 `probe` 输出的，
   不要凭经验写解析器——IE 的法规名写错就是凭经验的结果。
3. **新增法域后必须重跑 `verify_corpus.py --strict`**，让指标而不是描述来决定是否达标。
