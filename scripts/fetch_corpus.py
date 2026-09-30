#!/usr/bin/env python3
"""六法域公开法规语料采集与溯源。

设计约束（与项目既有约定一致）：

1. **只用标准库**——与 `scripts/smoke_*.py` 相同，clone 后无需安装依赖即可运行。
2. **只采集公开发布的法规原文**，逐条记录来源 URL 与获取时间（PRD N5 / R-09）。
3. **绝不生成内容**：任何一条法条都必须来自真实的 HTTP 响应。
   若某个法域拿不到数据，就少一个法域，而不是补上编造的文本。

用法：
    python scripts/fetch_corpus.py probe IE
    python scripts/fetch_corpus.py fetch IE --limit 3
    python scripts/fetch_corpus.py fetch all
    python scripts/fetch_corpus.py stats
"""

from __future__ import annotations

import argparse
import gzip
import io
import json
import re
import sys
import time
import urllib.error
import urllib.request
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
CORPUS_DIR = REPO_ROOT / "deploy" / "seed" / "corpus"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

# 法域代码 -> 中文名 / 英文名（与 kb.jurisdiction 种子一致）
JURISDICTIONS: dict[str, tuple[str, str]] = {
    "CN": ("中国内地", "Mainland China"),
    "HK": ("香港", "Hong Kong"),
    "SG": ("新加坡", "Singapore"),
    "IE": ("爱尔兰", "Ireland"),
    "NL": ("荷兰", "Netherlands"),
    "KY": ("开曼群岛", "Cayman Islands"),
}


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

def http_get(url: str, *, timeout: int = 60, retries: int = 2) -> bytes:
    """GET 一个 URL，返回原始字节。失败重试，最终失败则抛出。"""
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "text/html,application/xhtml+xml,application/xml,application/json;q=0.9,*/*;q=0.8",
                "Accept-Encoding": "gzip, identity",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read()
                if response.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
                return raw
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as exc:
            last_error = exc
            if attempt < retries:
                time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"GET 失败: {url} ({last_error})")


def decode(raw: bytes) -> str:
    """按常见编码解码，优先 UTF-8。"""
    for encoding in ("utf-8", "utf-8-sig", "gb18030", "latin-1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def strip_tags(fragment: str) -> str:
    """去标签、还原实体、压缩空白。仅用于把 HTML 片段变成纯文本。"""
    text = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", fragment)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</(p|div|li|tr|h[1-6])>", "\n", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    entities = {
        "&nbsp;": " ", "&amp;": "&", "&lt;": "<", "&gt;": ">",
        "&quot;": '"', "&#39;": "'", "&apos;": "'", "&mdash;": "—",
        "&ndash;": "–", "&rsquo;": "’", "&lsquo;": "‘",
        "&ldquo;": "“", "&rdquo;": "”", "&sect;": "§", "&para;": "¶",
    }
    for entity, char in entities.items():
        text = text.replace(entity, char)
    text = re.sub(r"&#(\d+);", lambda m: chr(int(m.group(1))), text)
    text = re.sub(r"[ \t\u00a0]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text)
    return text.strip()


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


# --------------------------------------------------------------------------
# 各法域适配器
#
# 每个适配器返回 list[dict]，一条记录 = 一条法条，字段与 kb.article /
# kb.statute_version 对齐（见《详细设计》§3.9.2 / §3.9.3）。
# --------------------------------------------------------------------------

def record(
    *,
    jurisdiction: str,
    statute_title: str,
    statute_title_original: str,
    statute_no: str,
    version_label: str,
    effective_from: str,
    effective_to: str | None,
    source_url: str,
    article_no: str,
    hierarchy_path: list[str],
    content: str,
    date_precision: str = "DAY",
) -> dict:
    return {
        "jurisdiction_code": jurisdiction,
        "statute_title": statute_title,
        "statute_title_original": statute_title_original,
        "statute_no": statute_no,
        "version_label": version_label,
        "effective_from": effective_from,
        "effective_to": effective_to,
        # 来源站点的日期精度：DAY = 站点给出具体日期；YEAR = 只能确定到年份。
        # 标出来是为了让「引用里写着 1997-01-01 生效」这种精度损失是可解释的，
        # 而不是让读者以为我们真的知道那天生效。
        "date_precision": date_precision,
        "source_url": source_url,
        "source_fetched_at": now_iso(),
        "article_no": article_no,
        "hierarchy_path": hierarchy_path,
        "content": content,
    }


# ---- IE：爱尔兰成文法（irishstatutebook.ie）--------------------------------
# 每个法案一个 "print" 页面即含全部条文，单次请求产出量最大。

IE_ACTS = [
    # 只登记 ELI 路径；**法规名称与日期一律从页面取**。
    # 早期版本这里写死了中文标题，实测发现 1997/act/39 是《税收合并法》而非「金融法」——
    # 凭记忆写法规名一定会错，所以改成从 <title> 读。
    "1997/act/39",
    "1999/act/2",
    "1997/act/22",
    "2003/act/28",
]


def _ie_title(html_text: str) -> str | None:
    """法规名称优先取 <title>。

    实测 <h1> 有时被站点用作导航（取到过 "Page URL:"），而 <title> 一直是法规名。
    """
    match = re.search(r"<title[^>]*>(.*?)</title>", html_text, re.DOTALL | re.IGNORECASE)
    if not match:
        return None
    title = strip_tags(match.group(1)).strip()
    title = re.split(r"\s*[|—]\s*", title)[0].strip()
    if not title or len(title) > 200 or "Page URL" in title:
        return None
    return title


def _ie_year(eli: str) -> str:
    """从 ELI 路径取年份：1997/act/39 → 1997。"""
    return eli.split("/")[0]


_IE_SECTION_ANCHOR = re.compile(r'<a\s+name="sec(\d+[A-Z]?)"')
#: 附表锚点。**必须当成切分边界**，否则最后一条会吞掉它之后的全部附表：
#: 实测《税收合并法》的 `Section 1104` 因此达到 376,126 字符（其中真正属于
#: 该条的只有开头约 8,700 字符，其余是附表 1–10）。这条缺陷不是"某条比较长"，
#: 而是"附表被当成了条文的一部分"——引用它会指向一个并不存在的巨大条款。
_IE_SCHEDULE_ANCHOR = re.compile(r'<a\s+name="sched[^"]*"')


def _ie_sections(html_text: str) -> list[tuple[str, str]]:
    """从爱尔兰成文法 print 页面切出 (条号, 正文)。

    站点是 1990 年代的表格排版，条文以 `<a name="secN">` 锚点起始，
    段级锚点为 `name="sN_pM"`（不是 `secN`，不会误切）。

    **附表（`schedN` 锚点之后）不采集。** 附表是"表"而不是"条"，
    粒度与引用模型不同；本次语料的目标是条文主体。明确丢弃并记录在
    `deploy/seed/corpus/SOURCES.md`，而不是把它们并进最后一条。
    """
    body = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", html_text)
    section_marks = list(_IE_SECTION_ANCHOR.finditer(body))
    # 边界 = 下一条的起点，或附表起点，或全文末尾，三者取最近的一个
    boundaries = sorted(
        [m.start() for m in section_marks] + [m.start() for m in _IE_SCHEDULE_ANCHOR.finditer(body)]
    )

    sections: list[tuple[str, str]] = []
    seen: set[str] = set()
    for mark in section_marks:
        position, article_no = mark.start(), mark.group(1)
        end = next((edge for edge in boundaries if edge > position), len(body))
        text = strip_tags(body[position:end])
        if len(text) < 40 or article_no in seen:
            continue
        seen.add(article_no)
        sections.append((article_no, text))
    return sections


def fetch_ie(limit: int) -> list[dict]:
    out: list[dict] = []
    for eli in IE_ACTS[:limit]:
        url = f"https://www.irishstatutebook.ie/eli/{eli}/enacted/en/print"
        html_text = decode(http_get(url, timeout=180))
        title_en = _ie_title(html_text)
        if not title_en:
            print(f"  IE  {eli}: 未取到法规名称，跳过", file=sys.stderr)
            continue
        sections = _ie_sections(html_text)
        if not sections:
            print(f"  IE  {title_en}: 未解析出条文，跳过", file=sys.stderr)
            continue
        # 站点不提供机器可读的通过日期，因此只登记到年份，并标注精度。
        # 宁可让引用里写着"1997 年（精度：年）"，也不假装知道 1997-06-06。
        year = _ie_year(eli)
        title_zh = f"爱尔兰《{title_en}》"
        for article_no, text in sections:
            out.append(record(
                jurisdiction="IE",
                statute_title=title_zh,
                statute_title_original=title_en,
                statute_no=f"Act No. {eli.split('/')[2]} of {year}",
                version_label="enacted",
                effective_from=f"{year}-01-01",
                effective_to=None,
                source_url=url,
                article_no=f"Section {article_no}",
                hierarchy_path=[title_en, f"Section {article_no}"],
                content=text,
                date_precision="YEAR",
            ))
        print(f"  IE  {title_en}: {len(sections)} 条（生效年 {year}，精度 YEAR）", file=sys.stderr)
    return out


# ---- NL：荷兰 wetten.overheid.nl ------------------------------------------
# 页面为服务端渲染的 XHTML，单篇法律一次请求即含全部条文。

NL_LAWS = [
    # (BWB 编号, 中文标题, 荷兰文标题)
    # 这里的编号只是"候选"：适配器会实际抓取并校验页面是否含条文，
    # 校验失败的候选被丢弃——不做"凭记忆认定某个编号就是某部法律"的事。
    ("BWBR0004770", "荷兰《1990 年税收征收法》", "Invorderingswet 1990"),
    ("BWBR0002320", "荷兰《税收征管总法》", "Algemene wet inzake rijksbelastingen"),
    ("BWBR0011353", "荷兰《2001 年所得税法》", "Wet inkomstenbelasting 2001"),
    ("BWBR0002672", "荷兰《1969 年企业所得税法》", "Wet op de vennootschapsbelasting 1969"),
]


def _nl_date(token: str) -> str:
    """荷兰站点用 DD-MM-YYYY，转成 ISO。"""
    token = token.strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", token):
        return token
    match = re.fullmatch(r"(\d{2})-(\d{2})-(\d{4})", token)
    if not match:
        raise ValueError(f"无法识别的日期: {token}")
    day, month, year = match.groups()
    return f"{year}-{month}-{day}"


def _nl_period(html_text: str) -> tuple[str | None, str | None]:
    """抽取「Geldend van 01-07-2026 t/m heden.」形式的生效区间。"""
    match = re.search(
        r"Geldend van\s+([\d\-]{10})\s*(?:t/m\s+([\d\-]{10}|heden))?", html_text
    )
    if not match:
        return None, None
    start = _nl_date(match.group(1))
    end_token = match.group(2)
    end = None if end_token in (None, "heden") else _nl_date(end_token)
    return start, end


def _nl_articles(html_text: str) -> list[tuple[str, str, list[str]]]:
    """切出 (条号, 正文, 层级路径)。"""
    body = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", html_text)
    marks = [(m.start(), m.group(1)) for m in re.finditer(r'<div class="artikel" id="([^"]+)"', body)]
    articles: list[tuple[str, str, list[str]]] = []
    for index, (position, anchor) in enumerate(marks):
        end = marks[index + 1][0] if index + 1 < len(marks) else len(body)
        chunk = body[position:end]
        heading = re.search(r"<h4[^>]*>\s*Artikel\s+([^<]+?)\s*</h4>", chunk)
        article_no = heading.group(1).strip() if heading else anchor.rsplit("_", 1)[-1]
        # 只取条文正文段落，避开「打印/导出/永久链接」等操作链接
        paragraphs = re.findall(r'<p class="lid labeled">(.*?)</p>', chunk, re.DOTALL)
        if not paragraphs:
            paragraphs = re.findall(r'<p[^>]*>(.*?)</p>', chunk, re.DOTALL)
        text = "\n".join(part for part in (strip_tags(p) for p in paragraphs) if part)
        if len(text) < 20:
            continue
        chapter = anchor.split("_")[0] if "_" in anchor else ""
        path = [chapter.replace("Hoofdstuk", "Hoofdstuk ").strip(), f"Artikel {article_no}"]
        articles.append((article_no, text, [p for p in path if p]))
    return articles


def fetch_nl(limit: int) -> list[dict]:
    out: list[dict] = []
    accepted = 0
    for bwb, title_zh, title_nl in NL_LAWS:
        if accepted >= limit:
            break
        url = f"https://wetten.overheid.nl/{bwb}"
        try:
            html_text = decode(http_get(url, timeout=120))
        except Exception as exc:  # noqa: BLE001 - 单个候选失败不影响其它候选
            print(f"  NL  {bwb}: 抓取失败 {exc}", file=sys.stderr)
            continue
        articles = _nl_articles(html_text)
        if len(articles) < 5:
            print(f"  NL  {bwb}: 未解析出条文（{title_nl} 名实不符或结构变化），跳过", file=sys.stderr)
            continue
        effective_from, effective_to = _nl_period(html_text)
        if not effective_from:
            print(f"  NL  {bwb}: 未取到生效日期，跳过（不猜日期）", file=sys.stderr)
            continue
        accepted += 1
        for article_no, text, path in articles:
            out.append(record(
                jurisdiction="NL",
                statute_title=title_zh,
                statute_title_original=title_nl,
                statute_no=bwb,
                version_label=f"geldend {effective_from}",
                effective_from=effective_from,
                effective_to=effective_to,
                source_url=url,
                article_no=f"Artikel {article_no}",
                hierarchy_path=[title_nl, *path],
                content=text,
            ))
        print(f"  NL  {title_nl}: {len(articles)} 条，生效 {effective_from}", file=sys.stderr)
    return out


# ---- CN：国家法律法规数据库（flk.npc.gov.cn）-----------------------------
# 站点为 SPA，公开检索接口返回法规 id；详情接口返回正文文件路径。

# 端点从站点前端 JS 中实读得到（见 `probe CN` 的 JS[law-search] 输出），不是猜的。
CN_HOST = "https://flk.npc.gov.cn"
CN_SEARCH = "/law-search/search/list"
CN_DETAIL = "/law-search/search/flfgDetails"
CN_FILE_BASE = "https://wb.flk.npc.gov.cn"


CN_AGG = "/law-search/index/aggregateData"


def _cn_get(path: str, params: dict | None = None, timeout: int = 90) -> dict:
    import urllib.parse

    url = CN_HOST + path
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json, text/plain, */*",
            "Origin": CN_HOST,
            "Referer": CN_HOST + "/",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(decode(response.read()))


def _cn_candidates() -> list[dict]:
    """从首页聚合接口取法规清单（含 bbbs 主键）。

    这个接口是首页"最新发布 / 热门检索"的数据源，无需关键字参数即可返回
    真实法规列表——比逆向检索接口的请求体可靠得多。
    """
    data = _cn_get(CN_AGG).get("data") or {}
    seen: set[str] = set()
    out: list[dict] = []
    for key in ("xfsd", "popularSearch", "zdgz", "rmss"):
        for item in data.get(key) or []:
            bbbs = item.get("bbbs")
            if bbbs and bbbs not in seen and item.get("title"):
                seen.add(bbbs)
                out.append(item)
    return out


def _cn_detail(bbbs: str) -> dict:
    return _cn_get(CN_DETAIL, {"bbbs": bbbs}).get("data") or {}


_CN_ARTICLE_RE = re.compile(r"^第[〇零一二三四五六七八九十百千]+条")
_CN_STRUCT_RE = re.compile(r"^第[〇零一二三四五六七八九十百千]+(编|章|节)$")


def _cn_node_text(node: dict) -> str:
    for key in ("content", "text", "body", "articleContent"):
        value = node.get(key)
        if isinstance(value, str) and value.strip():
            return strip_tags(value)
    return ""


def _cn_articles_from_tree(content: Any) -> list[tuple[str, str, list[str]]]:
    """遍历详情接口返回的条款树，产出 (条号, 正文, 层级路径)。

    树的形态：{title, index, children: [...]}。章/节作为路径，条作为记录；
    款/项若挂在条的子节点下，一并并入该条正文（法条的最小可引用单元是"条"）。
    """
    out: list[tuple[str, str, list[str]]] = []

    def collect_descendants(node: dict) -> list[str]:
        parts: list[str] = []
        for child in node.get("children") or []:
            if not isinstance(child, dict):
                continue
            text = _cn_node_text(child)
            if text:
                parts.append(text)
            parts.extend(collect_descendants(child))
        return parts

    def walk(node: Any, ancestors: list[str]) -> None:
        if not isinstance(node, dict):
            return
        title = (node.get("title") or "").strip()
        if _CN_ARTICLE_RE.match(title):
            parts = [part for part in [_cn_node_text(node), *collect_descendants(node)] if part]
            text = "\n".join(parts).strip()
            # 正文缺失时退化为标题本身，但这样太短，交给调用方跳过
            out.append((title, text or title, [*ancestors, title]))
            return
        next_ancestors = [*ancestors, title] if _CN_STRUCT_RE.match(title) else ancestors
        for child in node.get("children") or []:
            walk(child, next_ancestors)

    walk(content, [])
    return [item for item in out if len(item[1]) >= 10]


def _cn_tree_samples(content: Any, limit: int = 25) -> list[str]:
    """解析不出来时打印树的前若干节点，用于确认真实字段名。"""
    samples: list[str] = []

    def walk(node: Any, depth: int) -> None:
        if len(samples) >= limit or not isinstance(node, dict):
            return
        title = (node.get("title") or "").strip()
        keys = ",".join(sorted(k for k in node.keys() if k != "children"))
        samples.append(f"{'  ' * depth}title={title!r} keys=[{keys}]")
        for child in node.get("children") or []:
            walk(child, depth + 1)

    walk(content, 0)
    return samples


def _cn_articles(html_text: str) -> list[tuple[str, str]]:
    """中文法条：以「第X条」为切分锚点。"""
    text = strip_tags(html_text)
    text = re.sub(r"^\s*(目录|目 录).*?(?=第一章|第一条)", "", text, flags=re.DOTALL)
    parts = re.split(r"(?=(第[〇零一二三四五六七八九十百千]+条))", text)
    articles: list[tuple[str, str]] = []
    current_no: str | None = None
    buffer: list[str] = []
    for part in parts:
        match = re.fullmatch(r"第[〇零一二三四五六七八九十百千]+条", part.strip())
        if match:
            if current_no and buffer:
                body = "\n".join(buffer).strip()
                if len(body) >= 20:
                    articles.append((current_no, body))
            current_no = part.strip()
            buffer = []
        else:
            buffer.append(part)
    if current_no and buffer:
        body = "\n".join(buffer).strip()
        if len(body) >= 20:
            articles.append((current_no, body))
    return articles


def fetch_cn(limit: int) -> list[dict]:
    """中国内地：法规清单取自站点首页聚合接口，正文取自详情接口的条款树。

    两个端点都是从站点前端 JS 里实读到的（`probe CN` 的 JS[law-search] 输出），
    接口本身返回的是结构化条款树，因此不需要再解析 HTML。
    """
    candidates = _cn_candidates()
    if not candidates:
        print("  CN  未取到法规清单，跳过", file=sys.stderr)
        return []

    out: list[dict] = []
    accepted = 0
    for item in candidates:
        if accepted >= limit:
            break
        bbbs = item.get("bbbs")
        if not bbbs:
            continue
        try:
            detail = _cn_detail(bbbs)
        except Exception as exc:  # noqa: BLE001 - 单部法规失败不影响其它
            print(f"  CN  {item.get('title')}: 详情抓取失败 {exc}", file=sys.stderr)
            continue

        title = detail.get("title") or item.get("title") or ""
        articles = _cn_articles_from_tree(detail.get("content"))
        if not articles:
            samples = _cn_tree_samples(detail.get("content"), limit=6)
            raise RuntimeError(
                "中国内地法规正文不在条款树里。实测：flfgDetails 的 content 树"
                "只含 {id,index,parentId,title}，正文存放于 ossFile 指向的 OBS，"
                "而该 OBS 的下载链接解析到内网主机（flkoss.obs-bj2-internal.cucloud.cn，"
                "外部不可达）；amazonFile/previewLink 给出的阅读器地址同样指向内网。"
                f"节点样本: {samples[:3]}"
            )
        # 施行日期 sxrq 是"生效"的正确语义；gbrq（公布日）不能替代它
        effective_from = (detail.get("sxrq") or "")[:10]
        if not effective_from:
            print(f"  CN  {title}: 未取到施行日期，跳过（不猜日期）", file=sys.stderr)
            continue
        source_url = f"{CN_HOST}{CN_DETAIL}?bbbs={bbbs}"
        accepted += 1
        for article_no, text, path in articles:
            out.append(record(
                jurisdiction="CN",
                statute_title=f"中国《{title}》",
                statute_title_original=title,
                statute_no=str(detail.get("flxz") or ""),
                version_label=f"施行 {effective_from}",
                effective_from=effective_from,
                effective_to=None,
                source_url=source_url,
                article_no=article_no,
                hierarchy_path=[title, *path],
                content=text,
            ))
        print(f"  CN  {title}: {len(articles)} 条，施行 {effective_from}", file=sys.stderr)
    return out


# ---- 其余法域：结构待探测后实现 -------------------------------------------

def fetch_sg(limit: int) -> list[dict]:  # pragma: no cover - 站点不可达
    raise NotImplementedError(
        "Singapore Statutes Online 由 AWS WAF 保护："
        "GET /Act/ITA1947 返回 2.4KB 的 JavaScript 挑战页"
        "（token.awswaf.com/.../challenge.js），非浏览器会话拿不到正文。"
    )


def fetch_hk(limit: int) -> list[dict]:  # pragma: no cover - 站点不可达
    raise NotImplementedError(
        "香港 e-Legislation 是 JS 单页应用：GET /hk/cap112 与 "
        "/hk/cap112!en.assist.pdf 都只返回 7.5KB 的前端外壳（无正文节点）；"
        "HKLII 同样返回 2.7KB 的壳页。需要一个能执行 JS 的会话。"
    )


def fetch_ky(limit: int) -> list[dict]:  # pragma: no cover - 站点不可达
    raise NotImplementedError(
        "开曼立法站点对普通 HTTP 请求返回 202（前端渲染），"
        "且其法规以 PDF 为主，需先定位稳定的 PDF 直链。"
    )


ADAPTERS = {
    "IE": fetch_ie,
    "CN": fetch_cn,
    "SG": fetch_sg,
    "HK": fetch_hk,
    "NL": fetch_nl,
    "KY": fetch_ky,
}


# --------------------------------------------------------------------------
# probe：结构诊断（写适配器前先看清楚页面长什么样）
# --------------------------------------------------------------------------

PROBE_TARGETS: dict[str, list[str]] = {
    # 1997/act/22（税收合并法）是已知缺陷所在：它的最后一条 `Section 1104` 吞掉了
    # 整段附表，长度达 376K 字符。探测目标里必须包含"出问题的那一页"，
    # 否则探测器只会告诉我们一切正常。
    "IE": ["https://www.irishstatutebook.ie/eli/1997/act/39/enacted/en/html",
           "https://www.irishstatutebook.ie/eli/1997/act/39/enacted/en/print",
           "https://www.irishstatutebook.ie/eli/1997/act/22/enacted/en/print"],
    "CN": ["https://flk.npc.gov.cn/api/?type=&searchType=title%3Bvague&sortTr=f_bbrq_s%3Bdesc&page=1&size=3&keyword=%E7%A8%8E"],
    "SG": ["https://sso.agc.gov.sg/Act/ITA1947?WholeDoc=1"],
    "HK": ["https://www.elegislation.gov.hk/hk/cap112"],
    "NL": ["https://wetten.overheid.nl/BWBR0004770"],
    "KY": ["https://www.legislation.gov.ky/"],
}


def api_call(method: str, path: str, body: str | None) -> None:
    """直接打一个 JSON 接口并打印响应——写适配器时的调试入口。

    用法：python scripts/fetch_corpus.py api POST /law-search/search/list '{"pageNum":1}'
    """
    method = method.upper()
    url = path if path.startswith("http") else CN_HOST + path
    data = body.encode("utf-8") if body else None
    request = urllib.request.Request(
        url, data=data, method=method,
        headers={
            "User-Agent": USER_AGENT,
            "Content-Type": "application/json;charset=UTF-8",
            "Accept": "application/json, text/plain, */*",
            "Origin": CN_HOST,
            "Referer": CN_HOST + "/",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            raw = response.read()
            print(f"HTTP {response.status}  字节 {len(raw)}")
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        print(f"HTTP {exc.code}  字节 {len(raw)}")
    text = decode(raw)
    print(text[:1500])


def probe(jurisdiction: str) -> None:
    for url in PROBE_TARGETS[jurisdiction]:
        print(f"\n=== {jurisdiction}  {url}")
        try:
            raw = http_get(url, timeout=60)
        except Exception as exc:  # noqa: BLE001 - 诊断脚本需要看到失败原因
            print(f"  失败: {exc}")
            continue
        print(f"  字节数: {len(raw)}")
        text = decode(raw)
        print(f"  字符数: {len(text)}")
        if text.lstrip().startswith("{") or text.lstrip().startswith("["):
            print(f"  JSON 顶层键: {list(json.loads(text).keys())[:12]}")
            print(f"  片段: {text[:300]}")
            continue
        classes = Counter(re.findall(r'class="([^"]{1,60})"', text))
        print(f"  高频 class: {classes.most_common(12)}")
        ids = Counter(re.findall(r'id="([^"]{1,40})"', text))
        print(f"  高频 id: {ids.most_common(8)}")
        headings = Counter(re.findall(r"<(h[1-4])[ >]", text))
        print(f"  标题标签: {headings.most_common()}")
        for marker in ("第", "Article", "Section", "§", "collapsable", "collapseable", "prov"):
            print(f"  '{marker}' 出现 {text.count(marker)} 次")
        print(f"  片段: {text[:400]}")

        # 类名全貌：解析器要挂在具体的 class 上，前 40 个够用了
        print(f"  全部 class（前 40）: {classes.most_common(40)}")

        # 生效日期的常见写法：适配器不能靠猜日期，必须先看页面怎么写
        for pattern in (r"\d{4}-\d{2}-\d{2}", r"\d{2}-\d{2}-\d{4}",
                        r"\d{1,2}(?:st|nd|rd|th)?\s+[A-Z][a-z]+,?\s+\d{4}",
                        r"\[\s*\d{1,2}[^\]<]{0,30}\d{4}\s*\]",
                        r"(?:Geldend|Geldende|in werking|in force|effective|commencement|生效|通过|施行)[^<]{0,70}"):
            hits = sorted(set(re.findall(pattern, text)))
            if hits:
                print(f"  日期/生效候选 [{pattern}]: {hits[:8]}")

        # 标题：不靠记忆写法规名，直接从页面取
        title_tag = re.search(r"<title[^>]*>(.*?)</title>", text, re.DOTALL | re.IGNORECASE)
        h1_tag = re.search(r"<h1[^>]*>(.*?)</h1>", text, re.DOTALL | re.IGNORECASE)
        print(f"  <title>: {strip_tags(title_tag.group(1))[:160] if title_tag else '—'}")
        print(f"  <h1>: {strip_tags(h1_tag.group(1))[:160] if h1_tag else '—'}")

        # 元数据：结构化日期通常藏在 meta 里，比正文里的散文日期可靠
        metas = re.findall(r"<meta\s+[^>]*>", text, re.IGNORECASE)[:25]
        for meta in metas:
            if re.search(r"date|eli|dcterms|DC\.", meta, re.IGNORECASE):
                cleaned = re.sub(r"\s+", " ", meta)[:200]
                print(f"  <meta>: {cleaned}")

        # 切片诊断：把"条文锚点"附近的原始 HTML 打出来。
        # 压成单行再打印——多行上下文会被日志按行切碎，反而看不清。
        for marker in ('class="prov"', "prov", "t1", "Section 1", "第1条", "Article 1",
                       'xml:id', 'id="s1"', "div1", "collapseable", "artikel", "lidnr"):
            index = text.find(marker)
            if index >= 0:
                snippet = re.sub(r"\s+", " ", text[max(0, index - 350):index + 350])
                print(f"  --- 首个 '{marker}' 上下文 (±350) ---")
                print(f"  {snippet}")

        # 锚点清单。切分器挂在锚点上，而"最后一条吞掉了正文之后的内容"
        # 这类缺陷，只有看清**非 secN 锚点**才能定位：正文结束后若还有别的锚点，
        # 说明那之后的东西属于另一个结构（通常就是附表），不该并进最后一条。
        anchors = re.findall(r'<a\s+name="([^"]{1,40})"', text)
        print(f"  锚点共 {len(anchors)} 个，前 8：{anchors[:8]}，后 8：{anchors[-8:]}")
        others = [a for a in anchors if not re.fullmatch(r"sec\d+[A-Z]?", a)]
        print(f"  非 secN 锚点 {len(others)} 个：{others[:20]}")
        schedule_marks = re.findall(r"(?i)<h[1-4][^>]*>[^<]{0,60}schedul[^<]{0,40}", text)
        cleaned_marks = [re.sub(r"\s+", " ", mark)[:90] for mark in schedule_marks[:4]]
        print(f"  含 schedule 的标题 {len(schedule_marks)} 个：{cleaned_marks}")
        if others:
            tail = text.find(f'name="{others[0]}"')
            snippet = re.sub(r"\s+", " ", text[max(0, tail - 300):tail + 500])
            print(f"  --- 首个非 secN 锚点上下文 ---")
            print(f"  {snippet}")

        # 尾部结构：最后一条之后到底是什么？
        # 这是"最后一条吞掉了附表"这类缺陷的唯一定位手段——不看尾部，
        # 只能看到"某条特别长"，看不到它长在哪。
        sec_anchors = [(m.start(), m.group(1)) for m in re.finditer(r'<a\s+name="(sec\d+[A-Z]?)"', text)]
        if sec_anchors:
            last_pos, last_name = sec_anchors[-1]
            after = text[last_pos:]
            after_text = strip_tags(after)
            print(f"  最后一条 {last_name}：其后原文 {len(after)} 字符 / 纯文本 {len(after_text)} 字符")
            schedule_hit = re.search(r"(?i)SCHEDULE", after)
            print(f"  其后首个 SCHEDULE 字样：{'位置 ' + str(schedule_hit.start()) if schedule_hit else '未找到'}")
            sched_anchor = re.search(r'<a\s+name="((?:sched|schedul)[^"]*)"', after)
            print(f"  其后首个附表锚点：{sched_anchor.group(1) if sched_anchor else '无'}")
            print(f"  尾部纯文本开头: {re.sub(chr(92) + 's+', ' ', after_text[:300])}")

        # SPA 诊断：这是前端壳子而非数据，去它的 JS 里找接口路径
        scripts = re.findall(r'<script[^>]+src="([^"]+)"', text)
        if scripts:
            print(f"  检测到 SPA，脚本数 {len(scripts)}，尝试从 JS 中提取接口路径")
            import urllib.parse

            for src in scripts[:2]:
                js_url = urllib.parse.urljoin(url, src)
                try:
                    js = decode(http_get(js_url, timeout=90))
                except Exception as exc:  # noqa: BLE001
                    print(f"    取 {js_url} 失败: {exc}")
                    continue
                found = sorted(set(re.findall(r'"(/[a-zA-Z0-9_./\-]{2,60})"', js)))
                api_like = [path for path in found if "api" in path.lower() or "detail" in path.lower()
                            or "search" in path.lower() or "list" in path.lower()]
                print(f"    {js_url} → 候选接口路径: {api_like[:40]}")
                # 接口基址常常写在配置里
                for pattern in (r"https?://[a-zA-Z0-9.\-]+/[a-zA-Z0-9_/\-]*",
                                r"[A-Z_]*BASE[A-Z_]*\s*[:=]\s*[\"'][^\"']+[\"']"):
                    hits = sorted(set(re.findall(pattern, js)))
                    print(f"    基址候选: {hits[:20]}")
                # 接口名往往拼在字符串里，看关键字上下文比看路径更有效
                keywords = ("law-search/search/list", "flfgDetails", "searchType", "sortTr",
                            "pageNum", "requestParam", "advanceSearch")
                for keyword in keywords:
                    for hit in list(re.finditer(re.escape(keyword), js))[:2]:
                        snippet = re.sub(r"\s+", " ", js[max(0, hit.start() - 220):hit.start() + 220])
                        print(f"    JS[{keyword}]: {snippet}")


# --------------------------------------------------------------------------
# 落盘与统计
# --------------------------------------------------------------------------

def write_jsonl(jurisdiction: str, rows: list[dict]) -> Path:
    CORPUS_DIR.mkdir(parents=True, exist_ok=True)
    path = CORPUS_DIR / f"{jurisdiction}.jsonl"
    existing: list[dict] = []
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                existing.append(json.loads(line))
    merged: dict[tuple, dict] = {}
    for row in existing + rows:
        key = (row["statute_no"], row["version_label"], row["article_no"])
        merged[key] = row
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in merged.values():
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    return path


def stats() -> int:
    total = 0
    print(f"{'法域':<6}{'法条数':>8}  {'法规数':>6}")
    for code in JURISDICTIONS:
        path = CORPUS_DIR / f"{code}.jsonl"
        if not path.exists():
            print(f"{code:<6}{0:>8}  {0:>6}   （未采集）")
            continue
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        statutes = {(row["statute_title"], row["version_label"]) for row in rows}
        total += len(rows)
        print(f"{code:<6}{len(rows):>8}  {len(statutes):>6}")
    print(f"{'合计':<6}{total:>8}")
    return total


# =============================================================================
# selftest：解析器的离线自检
# =============================================================================
# 为什么需要它：这些解析器只在四个真实站点上运行，而**"改对了没有"原本需要联网、
# 耗时几分钟**才能看出来。于是每次改解析逻辑都面临一个熟悉的取舍——要么等，
# 要么凭信心提交。自检把关键的结构假设（尤其是曾经出过缺陷的那一处）固化成
# 内嵌样例：一条命令、零依赖、一秒出结果，改坏了立刻知道。
#
# 样例里的 HTML 是**按真实页面的结构手写的**，不是抓取的完整副本：
# 目的是锁住"我们依赖哪些标记"，而不是复现站点全部内容。

#: 爱尔兰：条文锚点 + 段级锚点 + **附表锚点**（附表必须是边界，见 _ie_sections）
_IE_FIXTURE = """<html><body>
<a name="sec1"></a><p>1.—Short title and construction of this Act for testing purposes.</p>
<a name="s1_p0"></a><p>(2) This paragraph anchor must not start a new section at all.</p>
<a name="sec2"></a><p>2.—Interpretation of the terms used throughout this Act for testing.</p>
<a name="sched1"></a><p>SCHEDULE 1 Consequential Amendments to Other Enactments and Provisions.</p>
<p>1. The following provisions are amended in the manner specified in this Schedule.</p>
</body></html>"""

#: 荷兰：`div.artikel` + `h4` + `p.lid.labeled`
_NL_FIXTURE = """<html><body>
<div class="artikel" id="HoofdstukI_Artikel1">
<h4>Artikel 1</h4>
<p class="lid labeled">1 Deze wet geldt bij de invordering van rijksbelastingen.</p>
</div>
<div class="artikel" id="HoofdstukI_Artikel2">
<h4>Artikel 2</h4>
<p class="lid labeled">2 De bepalingen van deze wet gelden in Nederland bij de heffing.</p>
</div>
</body></html>"""

#: 中国内地：详情接口返回的条款树（正文在 content 字段）
_CN_FIXTURE = {
    "title": "第一章 总则",
    "children": [
        {
            "title": "第一条 为了规范税收征收和缴纳行为，保障国家税收收入，制定本法。",
            "content": "为了规范税收征收和缴纳行为，保障国家税收收入，保护纳税人的合法权益，制定本法。",
            "children": [],
        }
    ],
}


def selftest() -> int:
    """对解析器做离线自检。返回进程退出码（0 = 全部通过）。"""
    failures: list[str] = []

    def check(name: str, condition: bool, detail: str = "") -> None:
        if condition:
            print(f"  ✓ {name}")
        else:
            failures.append(name)
            print(f"  ✗ {name}{' — ' + detail if detail else ''}")

    print("IE 条文切分")
    ie_sections = _ie_sections(_IE_FIXTURE)
    check("切出 2 条（段级锚点不误切）", len(ie_sections) == 2, f"实际 {len(ie_sections)}")
    if len(ie_sections) == 2:
        (no1, text1), (no2, text2) = ie_sections
        check("条号为 1 与 2", (no1, no2) == ("1", "2"), f"实际 {(no1, no2)}")
        check("第 1 条含正文", "Short title" in text1)
        check("第 2 条含正文", "Interpretation" in text2)
        # 这条是本模块最重要的一条断言：附表曾经被并进最后一条
        check(
            "附表未并入任何一条",
            "SCHEDULE" not in text1 and "SCHEDULE" not in text2,
            "附表内容出现在条文里，说明 sched 锚点没有当成边界",
        )

    print("NL 条文切分")
    nl_articles = _nl_articles(_NL_FIXTURE)
    check("切出 2 条", len(nl_articles) == 2, f"实际 {len(nl_articles)}")
    if len(nl_articles) == 2:
        check("条号为 1 与 2", (nl_articles[0][0], nl_articles[1][0]) == ("1", "2"))
        check("正文含条文内容", "Deze wet geldt" in nl_articles[0][1])
        check("层级路径含章节", nl_articles[0][2][0].startswith("Hoofdstuk"))

    print("CN 条款树")
    cn_articles = _cn_articles_from_tree(_CN_FIXTURE)
    check("抽到 1 条", len(cn_articles) == 1, f"实际 {len(cn_articles)}")
    if cn_articles:
        check("条号为首条", cn_articles[0][0].startswith("第一条"), cn_articles[0][0][:20])
        check("正文非空", len(cn_articles[0][1]) >= 10)

    print()
    if failures:
        print(f"自检失败 {len(failures)} 项：{'；'.join(failures)}")
        return 1
    print("自检全部通过")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="六法域公开法规语料采集")
    sub = parser.add_subparsers(dest="command", required=True)

    probe_parser = sub.add_parser("probe", help="打印源站结构诊断")
    probe_parser.add_argument("jurisdiction", choices=sorted(PROBE_TARGETS))

    fetch_parser = sub.add_parser("fetch", help="采集并写入 deploy/seed/corpus")
    fetch_parser.add_argument("jurisdiction", help="法域代码或 all")
    fetch_parser.add_argument("--limit", type=int, default=3, help="每个法域处理的法规数上限")

    sub.add_parser("stats", help="统计已采集语料")
    sub.add_parser("selftest", help="离线自检解析器（不联网）")

    api_parser = sub.add_parser("api", help="调试一个 JSON 接口")
    api_parser.add_argument("method")
    api_parser.add_argument("path")
    api_parser.add_argument("body", nargs="?", default=None)

    args = parser.parse_args()

    if args.command == "selftest":
        return selftest()

    if args.command == "api":
        api_call(args.method, args.path, args.body)
        return 0

    if args.command == "probe":
        probe(args.jurisdiction)
        return 0

    if args.command == "stats":
        stats()
        return 0

    targets = sorted(ADAPTERS) if args.jurisdiction == "all" else [args.jurisdiction.upper()]
    for code in targets:
        adapter = ADAPTERS.get(code)
        if adapter is None:
            print(f"未知法域: {code}", file=sys.stderr)
            continue
        print(f"采集 {code} …", file=sys.stderr)
        try:
            rows = adapter(args.limit)
        except NotImplementedError as exc:
            print(f"  {code} 站点不可达，跳过。原因：{exc}", file=sys.stderr)
            continue
        except Exception as exc:  # noqa: BLE001 - 采集失败不应中断其它法域
            print(f"  {code} 采集失败: {exc}", file=sys.stderr)
            continue
        path = write_jsonl(code, rows)
        print(f"  {code} 写入 {len(rows)} 条 → {path.relative_to(REPO_ROOT)}", file=sys.stderr)

    stats()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
