#!/usr/bin/env python
"""lit-fetch — 两层文献 PDF 抓取器（只走开放获取，不碰付费墙）。

layer1  HTTP：标识符归一化 → OA 路线阶梯（arXiv / Unpaywall / S2 / PMC / PubMed free /
        落地页 citation_pdf_url / 预印本直链）→ %PDF 校验 → 可选标题校验
layer2  浏览器（browser.py，按需 import，默认系统 Edge）：只处理 layer1 报的 bot 墙

用法:
    python lit_fetch.py <id> [out.pdf] [--json] [--title "文章标题"] [--email you@x.edu]
                        [--engine edge|chrome|chromium] [--no-browser]
    python lit_fetch.py rpc          # stdio JSON-RPC 2.0（给 Zotero 插件用）
    python lit_fetch.py --selfcheck  # 离线自检

退出码（照 paper-fetch 的语义，少一档）:
    0 拿到 PDF · 1 用法错误 · 2 所有自动路线空手 · 7 被 bot 墙挡住（不是论文的问题）
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Callable

import requests

SCHEMA = 1
TIMEOUT = 25
RETRY_5XX_WAIT_S = 3


def load_dotenv(path: Path | None = None) -> None:
    """把项目根目录的 .env 读进 os.environ（已存在的环境变量优先，不覆盖）。

    ponytail: 不引 python-dotenv —— 就这点语法，自己解析十来行够了。支持 `export K=V`、单双引号
    包住的值、整行注释；**行内注释只在没加引号时切**（`K="a#b"` 里的 # 是值的一部分，别切错）。
    """
    p = path or Path(__file__).with_name(".env")
    try:
        lines = p.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for line in lines:
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        if s.lower().startswith("export "):
            s = s[7:].lstrip()
        key, sep, val = s.partition("=")
        key, val = key.strip(), val.strip()
        if not sep or not key:
            continue
        if val[:1] in ("\"", "'"):           # 引号包住的值：只取引号里的，后面的行内注释丢掉
            end = val.find(val[0], 1)
            val = val[1:end] if end > 0 else val[1:]
        elif " #" in val:                     # 没引号才按行内注释切（`a #b` 会切错，这种写法就别用）
            val = val.split(" #", 1)[0].strip()
        os.environ.setdefault(key, val)


load_dotenv()
CONTACT = os.environ.get("LITFETCH_EMAIL", "")

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36")


def hdrs(email: str = "") -> dict:
    ua = _UA + (f" lit-fetch/0.1 (mailto:{email})" if email else " lit-fetch/0.1")
    return {"User-Agent": ua}


# ── 标识符归一化 ──────────────────────────────────────────────────────────────
ARXIV_NEW = re.compile(r"^(?P<id>\d{4}\.\d{4,5})(?P<ver>v\d+)?$")
ARXIV_OLD = re.compile(r"^(?P<id>[a-z-]+(?:\.[A-Z]{2})?/\d{7})(?P<ver>v\d+)?$")
PREPRINT_ID = re.compile(r"^(?P<id>\d{4}\.\d{2}\.\d{2}\.\d{5,})(?P<ver>v\d+)?$")
DOI_RE = re.compile(r"10\.\d{4,9}/[^\s\"<>]+")
# ponytail: 只认这三类前缀——付费、Sci-Hub 那一套按计划不做
PREPRINT_PREFIXES = {"10.1101": "biorxiv", "10.64898": "biorxiv"}   # 10.64898 是 2026 起的新前缀


def classify(raw: str) -> dict:
    """把用户/插件给的东西归一成 {kind, value, ...}。kind: arxiv | doi | preprint | pmid。"""
    s = (raw or "").strip()
    s = re.sub(r"^doi:\s*", "", s, flags=re.I)
    s = re.sub(r"^https?://(dx\.)?doi\.org/", "", s, flags=re.I)

    if re.match(r"^pmid:?\s*\d+$", s, flags=re.I):
        return {"kind": "pmid", "value": re.sub(r"\D", "", s)}

    m = re.search(r"arxiv\.org/(?:abs|pdf)/(?P<id>[^\s?#]+)", s, flags=re.I)
    if m:
        s = m.group("id")
    # arXiv 的 DataCite DOI：Zotero 给 arXiv 条目存的就是这个（10.48550/arXiv.2610.04475）。
    # 不剥掉前缀的话它会被当成普通 DOI 走 Unpaywall/S2/EPMC——三者都不认 arXiv 的 DOI，白跑到 exit 2。
    m = re.match(r"^10\.48550/arxiv\.(?P<id>.+)$", s, flags=re.I)
    if m:
        s = m.group("id").rstrip(".")
    s = re.sub(r"^arxiv:\s*", "", s, flags=re.I)
    s = re.sub(r"\.pdf$", "", s, flags=re.I)
    for rx in (ARXIV_NEW, ARXIV_OLD):
        m = rx.match(s)
        if m:
            return {"kind": "arxiv", "value": m.group("id"), "version": m.group("ver") or ""}

    d = DOI_RE.search(s)
    if d:
        doi = d.group(0).rstrip(".")
        server = PREPRINT_PREFIXES.get(doi.split("/")[0])
        return {"kind": "preprint" if server else "doi", "value": doi, "server": server}

    m = PREPRINT_ID.match(s)          # 裸的 2025.05.31.656838 —— bioRxiv/medRxiv 的形态
    if m:
        return {"kind": "preprint", "value": m.group("id"), "version": m.group("ver") or "",
                "server": None}
    raise ValueError(f"认不出这是什么标识符: {raw!r}")


# ── HTTP 抓取与校验 ──────────────────────────────────────────────────────────
BLOCK_MARKERS = ("just a moment", "cf_chl_opt", "cf-chl", "attention required",
                 "verify you are human", "请稍候", "請稍候", "access denied",
                 "<title>client challenge")


class Blocked(Exception):
    def __init__(self, kind: str, url: str):
        super().__init__(kind)
        self.kind, self.url = kind, url


# ponytail: 直链 vs 落地页的分辨只看 URL 形态——够用，且不花一次请求。
_PDFISH = re.compile(r"\.pdf($|[?#])|[?&]pdf=|pdfdirect|/pdf/|article-pdf|full\.pdf|"
                     r"type=printable|getfreepdf|counter/pdf", re.I)


def _looks_like_pdf(url: str) -> bool:
    u = url or ""
    if "doi.org/" in u or "dx.doi.org/" in u:
        return False
    return bool(_PDFISH.search(u))


# ponytail: bioRxiv 是特例。实测：HTTP 层怎么都是 403，浏览器侧 route.fetch/导航也都是 403，
# 唯一成过的一条是「让浏览器直接下载」（36,880,144B 那次）。所以它的候选不再浪费时间试别的，
# 直接交给 layer2 的下载那条。
BROWSER_FIRST_HOSTS = ("biorxiv.org",)


def _browser_first(url: str) -> bool:
    host = re.sub(r"^https?://", "", url or "").split("/")[0].lower()
    return any(host.endswith(h) or f".{h}" in host for h in BROWSER_FIRST_HOSTS)


# ponytail: 模块级进度钩子。CLI 用不到（stderr 本来就给终端看）；RPC 侧把 fetch 的 on_progress 接到
# 这里，好让 grab()/Unpaywall 那些「这条试得怎么样」的单行诊断也进插件 —— Zotero 读不到 stderr。
PROGRESS_HOOK: Callable[[str], None] | None = None


def say(msg: str, quiet: bool = False) -> None:
    """一行诊断：CLI 走 stderr，插件走 PROGRESS_HOOK。"""
    if not quiet:
        print(msg, file=sys.stderr)
    if PROGRESS_HOOK:
        with contextlib.suppress(Exception):
            PROGRESS_HOOK(msg.strip())


def grab(url: str, referer: str = "", email: str = "") -> bytes | None:
    """GET → 校验 %PDF + 体积下限。被墙时抛 Blocked（不让调用方误判成"没资源"）。"""
    h = hdrs(email)
    if referer:
        h["Referer"] = referer
    t0 = time.time()
    for attempt in (1, 2):
        try:
            r = requests.get(url, headers=h, timeout=TIMEOUT, allow_redirects=True)
        except requests.RequestException as e:
            if attempt == 2:
                say(f"  ✗ {url[:90]} → {type(e).__name__} ({time.time()-t0:.1f}s)")
                return None
            time.sleep(1)
            continue
        if r.status_code >= 500 and attempt == 1:
            time.sleep(RETRY_5XX_WAIT_S)
            continue
        body = r.content or b""
        if body[:4] == b"%PDF" and len(body) > 2048:
            say(f"  ✓ {url[:90]} → %PDF {len(body)}B ({time.time()-t0:.1f}s)")
            return body
        head = body[:4000].decode("utf-8", "ignore").lower()
        if r.status_code in (403, 429) or any(m in head for m in BLOCK_MARKERS):
            kind = "cf_challenge" if "cf_chl" in head or "just a moment" in head else f"http_{r.status_code}"
            say(f"  ✗ {url[:90]} → HTTP {r.status_code}（{kind}, {time.time()-t0:.1f}s）")
            raise Blocked(kind, url)
        say(f"  ✗ {url[:90]} → HTTP {r.status_code} {r.headers.get('Content-Type','')[:24]} "
            f"{len(body)}B ({time.time()-t0:.1f}s)")
        return None
    return None


def _get_json(url: str, email: str = "") -> dict | None:
    try:
        r = requests.get(url, headers=hdrs(email), timeout=TIMEOUT)
        if r.status_code == 200:
            return r.json()
        say(f"  · {url[:80]} → HTTP {r.status_code}")
    except Exception as e:
        say(f"  · {url[:80]} → {type(e).__name__}")
    return None


# ── 路线：产生候选 URL ───────────────────────────────────────────────────────
def crossref_volume(doi: str, email: str = "") -> tuple[str | None, str | None]:
    j = _get_json(f"https://api.crossref.org/works/{doi}", email) or {}
    msg = j.get("message") or {}
    return msg.get("volume"), (msg.get("container-title") or [None])[0]


def mdpi_cdn(doi: str, email: str = "") -> list[str]:
    """MDPI 主站对脚本 403，但正文同时放在 mdpi-res.com 的 CDN 直链上（paper-fetch 实测 17 选 16）。

    DOI 尾串 = 期刊代码 + 卷号 + 两位期号 + 文章号，所以要先知道卷号长度（Crossref 给卷号，
    拿不到就假设「两位期号 + 四位文章号」）。CDN 路径里的 `{j}` 可能是 DOI 代码，也可能是
    期刊全名去空白 —— 两个都试。
    """
    m = re.match(r"^10\.3390/([a-z]+)(\d+)$", doi.lower())
    if not m:
        return []
    code, digits = m.group(1), m.group(2)
    vol, container = crossref_volume(doi, email)
    if vol and digits.startswith(vol) and len(digits) > len(vol) + 2:
        vol, art = vol, digits[len(vol) + 2:]
    else:
        art, vol = digits[-4:], digits[:-6] or None
    if not vol:
        return []
    art = art.zfill(5)
    slugs = [code]
    if container:
        slug = re.sub(r"[^a-z0-9]", "", container.lower())
        if slug and slug != code:
            slugs.append(slug)
    return [f"https://mdpi-res.com/d_attachment/{j}/{j}-{vol}-{art}/article_deploy/"
            f"{j}-{vol}-{art}.pdf" for j in slugs]


def _loc_rank(loc: dict) -> int:
    """候选排序：越小越先试。

    ponytail: 实测 10.1038/s44320-025-00131-3（Molecular Systems Biology）时，Unpaywall 的
    best_oa_location 是 **补充材料**（`action/downloadSupplement?...MOESM2_ESM.pdf`），真正的正文
    PDF 得从出版商那边拿；而第二个 location 是 biorxiv 的 acceptedVersion——顺着试就会「拿补充材料
    当正文」或者干脆开浏览器去 biorxiv。所以：出版商的 publishedVersion 最先，supplement 和预印本
    仓库放最后。
    """
    url = (loc.get("url_for_pdf") or loc.get("url") or "").lower()
    if any(k in url for k in ("downloadsupplement", "moesm", "supplement", "/suppl", "_si_", "esm")):
        return 4
    if "biorxiv.org" in url or "medrxiv.org" in url:
        return 3                      # 预印本仓库：正文在出版商那儿时它是下策
    if loc.get("host_type") == "publisher":
        return 0 if loc.get("version") == "publishedVersion" else 1
    return 2


def unpaywall_candidates(doi: str, email: str) -> tuple[list[tuple[str, str, int]], bool | None, str | None]:
    """(候选, is_oa, oa_status)。候选形如 [(url, "unpaywall", rank)]，rank 见 _loc_rank。"""
    out: list[tuple[str, str, int]] = []
    if not email:
        say("  · Unpaywall: 没给邮箱（--email / LITFETCH_EMAIL）→ 跳过")
        # ponytail: 没邮箱 = 「不知道」，不是「不是 OA」。标 False 会让下游把这篇当付费墙
        # （paywalled 判定 + layer2 的门槛都看它），白白少兜一层。
        return out, None, None
    j = _get_json(f"https://api.unpaywall.org/v2/{doi}?email={email}", email) or {}
    is_oa = bool(j.get("is_oa"))
    oa_status = j.get("oa_status")
    locs = ([j["best_oa_location"]] if j.get("best_oa_location") else []) + (j.get("oa_locations") or [])
    locs = [l for l in locs if l]
    locs.sort(key=_loc_rank)
    for loc in locs:
        rk = _loc_rank(loc)
        for key in ("url_for_pdf", "url"):
            u = loc.get(key)
            if not u:
                continue
            epmc = epmc_render(u)
            for cand in (u, epmc):
                if cand and all(c[0] != cand for c in out):
                    out.append((cand, "unpaywall", rk))
    if not locs:
        say(f"  · Unpaywall: is_oa={j.get('is_oa')} oa_status={oa_status} 无 location")
    return out, is_oa, oa_status


def publisher_pdf_candidates(doi: str) -> list[tuple[str, str]]:
    """出版商自己的直链 PDF（不用 Unpaywall 指路）。

    ponytail: 实测 10.1038/s44320-025-00131-3（Molecular Systems Biology，EMBO 刊现在挂在
    Springer 下）→ `https://link.springer.com/content/pdf/<doi>.pdf` 直接 200 application/pdf
    7.94MB；同一个 DOI 的 `nature.com/articles/<suffix>.pdf` 是 404。所以 10.1038 两条都拼
    （Nature 本家的是 nature.com，转移刊的是 Springer），10.1007/10.1186/10.15252 只拼 Springer。
    """
    if not re.match(r"^10\.(1007|1186|1038|15252)/", doi or ""):
        return []
    out: list[tuple[str, str]] = []
    if doi.startswith("10.1038/"):
        out.append((f"https://www.nature.com/articles/{doi.split('/', 1)[1]}.pdf", "nature_pdf"))
    out.append((f"https://link.springer.com/content/pdf/{doi}.pdf", "springer_pdf"))
    return out


def epmc_render(url: str) -> str | None:
    """PMC 落地页/PDF 链接 → Europe PMC render 端点（PMC 自己上了 reCAPTCHA）。"""
    m = re.search(r"(?:pmc\.ncbi\.nlm\.nih\.gov/articles/|/articles/)?(PMC\d{5,})", url or "")
    if not m:
        return None
    return f"https://europepmc.org/articles/{m.group(1)}?pdf=render"


def pmcid_candidates(doi: str, email: str) -> list[tuple[str, str]]:
    j = _get_json("https://www.ebi.ac.uk/europepmc/webservices/rest/search"
                  f"?query=DOI:%22{doi}%22&format=json&resultType=core", email)
    res = ((j or {}).get("resultList") or {}).get("result") or []
    for r in res:
        pmcid = r.get("pmcid")
        if pmcid:
            return [(f"https://europepmc.org/articles/{pmcid}?pdf=render", "europepmc")]
    return []


def s2_candidates(doi: str, email: str) -> list[tuple[str, str]]:
    j = _get_json(f"https://api.semanticscholar.org/graph/v1/paper/DOI:{doi}?fields=openAccessPdf",
                  email) or {}
    oa = (j.get("openAccessPdf") or {}).get("url")
    return [(oa, "s2")] if oa else []


def resolve_landing(url: str, email: str = "", timeout: int = TIMEOUT) -> str:
    """跟掉 doi.org 的 302，只拿最终 URL，不读 body（解析器慢，但别为它下两份 HTML）。"""
    try:
        r = requests.get(url, headers=hdrs(email), timeout=timeout, stream=True)
        final = r.url
        r.close()
        return final or url
    except Exception:
        return url


def landing_meta_candidates(url: str, email: str = "", timeout: int = 12) -> list[tuple[str, str]]:
    """落地页里的 <meta name="citation_pdf_url"> —— 很多仓库/出版商都留了这个 tag。

    ponytail: 落地页只值一次快请求；doi.org 那种解析器 302 到真正的落地页（常已在本列表里），
    单独抓它只会白等 10–27s，所以调用方会跳过它。
    """
    try:
        r = requests.get(url, headers=hdrs(email), timeout=timeout)
    except Exception:
        return []
    if r.status_code != 200 or "html" not in (r.headers.get("Content-Type") or "").lower():
        return []
    m = re.search(r'citation_pdf_url"\s+content="([^"]+)"', r.text, re.I)
    return [(m.group(1), "landing_meta")] if m else []


def pubmed_free(doi: str, email: str) -> bool:
    j = _get_json("https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
                  f"?db=pubmed&retmode=json&term={doi}[DOI] AND free full text[sb]", email)
    return int(((j or {}).get("esearchresult") or {}).get("count", "0") or 0) > 0


def arxiv_pdf_urls(ident: dict) -> list[tuple[str, str]]:
    return [(f"https://arxiv.org/pdf/{ident['value']}{ident.get('version','')}", "arxiv")]


def preprint_candidates(ident: dict, email: str) -> list[tuple[str, str]]:
    """bioRxiv/medRxiv：先经 API 拿版本与日期，再拼两种 PDF 路径。

    ponytail: **只有 early 形式在 HTTP 层拿得到**（实测 medRxiv：`/content/<doi>v1.full.pdf` 403，
    而 `/content/<server>/early/2024/02/09/<id>.full.pdf` → 200 2.46MB %PDF-）。所以 early 排前面。
    """
    doi, server = ident.get("value"), ident.get("server")
    if not re.match(r"^10\.", doi):                      # 裸的日期型 id：两个前缀、两个 server 都试
        pairs = [(s, f"{p}/{doi}") for p in ("10.1101", "10.64898")
                 for s in ("biorxiv", "medrxiv")]
    else:
        pairs = [(server or "biorxiv", doi), ("biorxiv" if server == "medrxiv" else "medrxiv", doi)]
    out: list[tuple[str, str]] = []
    for srv, d in pairs:
        j = _get_json(f"https://api.biorxiv.org/details/{srv}/{d}", email)
        suffix = d.split("/", 1)[1] if "/" in d else d
        for item in (j or {}).get("collection") or []:
            ver = item.get("version") or "1"
            date = (item.get("date") or "").replace("-", "/")
            urls = []
            if re.match(r"^\d{4}/\d{2}/\d{2}$", date):
                urls.append(f"https://www.{srv}.org/content/{srv}/early/{date}/{suffix}.full.pdf")
            urls.append(f"https://www.{srv}.org/content/{d}v{ver}.full.pdf")
            for u in urls:
                if (u, "preprint") not in out:
                    out.append((u, "preprint"))
        if out:
            break
    return out


# ── 内容校验（可选，--title 才做）───────────────────────────────────────────
def _norm(s: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", (s or "").lower())


def title_verdict(pdf: bytes, title: str) -> tuple[str, str]:
    """返回 (verdict, detail)。verdict: match | title_absent | no_text | unreadable。"""
    try:
        import fitz  # PyMuPDF
    except ImportError:
        return "unreadable", "pymupdf 未安装，跳过内容校验"
    try:
        doc = fitz.open(stream=pdf, filetype="pdf")
    except Exception as e:
        return "unreadable", f"打不开: {e}"
    want = _norm(title)
    if not want:
        return "no_text", "标题为空"
    best = 0.0
    for page in doc[:6]:
        got = set(_norm(page.get_text()))
        if not got:
            continue
        hit = sum(1 for w in want if w in got) / len(want)
        best = max(best, hit)
    doc.close()
    if best >= 0.85:
        return "match", f"标题命中 {best:.0%}"
    if best == 0:
        return "title_absent", "前 6 页找不到标题 —— 可能是别的文章或整卷"
    return "title_absent", f"标题只命中 {best:.0%}（低于 0.85）"


# ── 主流程 ───────────────────────────────────────────────────────────────────
def _l2_candidates(blocked: list[dict], landings: list[tuple[str, str, int]],
                   is_oa: bool | None, free_at_publisher: bool) -> list[dict]:
    """layer2 该拿哪些 URL 去开浏览器。

    ① 有被墙的候选 → 原样给（老行为）。② 一个候选都没试出来、落地页链还在 → 用落地页顶上：
    Elsevier 那种 doi.org → linkinghub 跳转壳 → CF 墙，跳转和墙都只有浏览器跟得动，而落地页上
    的 citation_pdf_url 正是 browser.py 里现成的分支（只是以前没被喂过）。
    ③ Unpaywall 明说不是 OA 且 PubMed 也没标 free → 不开窗，那是付费样本，白开。
    """
    if blocked:
        return blocked
    if is_oa is False and not free_at_publisher:
        return []
    return [{"kind": "landing", "url": u} for u, _, _ in landings]


def _prefer_publisher(ident: dict, pdfish: list[tuple[str, str, int]],
                      landings: list[tuple[str, str, int]], allow_browser: bool) -> bool:
    """要不要走「出版商那版优先」（把预印本/仓库的候选推后）。

    只对**期刊 DOI** 成立：预印本（bioRxiv/medRxiv）自己就是"出版商"，没有版本优劣可言，它的直链
    本来就该直接交给 layer2 的浏览器下载（BROWSER_FIRST_HOSTS 那套）；arXiv 更没有这套候选。
    ponytail: 不这么分的话，biorxiv 的 DOI 会被 unpaywall 标成 host_type=publisher，于是"出版商优先"
    被误触发，它的直链被当成低版本推后、先去试 www.biorxiv.org/lookup/doi 那种落地页 = 白白多撞一次 CF。
    """
    if not allow_browser or ident.get("kind") != "doi":
        return False
    return (any(rk == 0 for _, _, rk in landings)
            or any(rk == 0 for _, _, rk in pdfish))


def _defer_low_versions(cands: list[tuple[str, str, int]], prefer_pub: bool
                        ) -> tuple[list[tuple[str, str, int]], list[tuple[str, str, int]]]:
    """(先试的, 留到后面的)，保留 rank 好让调用方继续分层。

    prefer_pub 时把 rank>0 的候选（预印本/仓库那版，直链和落地页都算）留到出版商那路试完之后 ——
    否则 arXiv 的 submittedVersion 会先被拿到：实测 10.1016/j.cell.2024.11.015 里先被 grab 的是
    arxiv.org/pdf/2409.11654，而 Cell 那版本身是 OA 的。rank 0（出版商自己的）不受影响。
    """
    if not prefer_pub:
        return list(cands), []
    return [c for c in cands if c[2] == 0], [c for c in cands if c[2] != 0]


def fetch(raw_id: str, out: Path | None = None, title: str = "", email: str = "",
          engine: str = "edge", allow_browser: bool = True, quiet: bool = False,
          on_progress: Callable[[str], None] | None = None) -> dict:
    started = time.time()
    tried: list[str] = []
    blocked: list[dict] = []

    def note(*a) -> None:
        """一行诊断：CLI 看 stderr，插件走 PROGRESS_HOOK（Zotero 读不到 stderr）。

        ponytail: 只挂在 note() 这一处 —— fetch 里所有「现在在试什么」都从这里出，插件因此免费拿到
        路线级进度，不用在每个循环里再喊一遍。
        """
        say(" ".join(str(x) for x in a), quiet=quiet)

    global PROGRESS_HOOK
    PROGRESS_HOOK = on_progress          # grab()/_get_json() 那些单行诊断也一并送去插件

    try:
        ident = classify(raw_id)
    except ValueError as e:
        return {"schema": SCHEMA, "input": raw_id, "ok": False, "error": str(e),
                "tried": [], "elapsed_s": 0}

    # 候选带版本层 rank（沿用 _loc_rank 的数）：0 = 出版商的 publishedVersion，2 = 仓库，
    # 3 = 预印本仓库。layer1 的候选顺序以前是「先所有直链、再所有落地页」—— 形状压过了版本，
    # 于是 arXiv 的 submittedVersion 会抢在出版商落地页前面（实测 10.1016/j.cell.2024.11.015
    # 拿的就是 arXiv 那一版，而 Cell 自己那版是 OA 的）。rank 留着给下面的「优先出版商」用。
    cands: list[tuple[str, str, int]] = []
    free_at_publisher = False
    is_oa: bool | None = None
    oa_status: str | None = None
    if ident["kind"] == "arxiv":
        cands = [(u, r, 3) for u, r in arxiv_pdf_urls(ident)]
    elif ident["kind"] == "preprint":
        cands = [(u, r, 3) for u, r in preprint_candidates(ident, email)]   # 预印本自己的直链先试
    elif ident["kind"] == "pmid":
        pmcid = _get_json("https://www.ncbi.nlm.nih.gov/pmc/utils/idconv/v1.0/"
                          f"?ids={ident['value']}&format=json", email)
        recs = (pmcid or {}).get("records") or []
        if recs and recs[0].get("pmcid"):
            cands = [(f"https://europepmc.org/articles/{recs[0]['pmcid']}?pdf=render",
                      "europepmc", 2)]

    if ident["kind"] in ("doi", "preprint") and re.match(r"^10\.", ident["value"]):
        doi = ident["value"]
        cands += [(u, r, 0) for u, r in publisher_pdf_candidates(doi)]   # 出版商直链：便宜，且是正文
        for name, fn in (("unpaywall", lambda: unpaywall_candidates(doi, email)),
                         ("s2", lambda: s2_candidates(doi, email)),
                         ("europepmc", lambda: pmcid_candidates(doi, email))):
            if name not in tried:
                tried.append(name)
            res = fn()
            if name == "unpaywall":
                res, is_oa, oa_status = res          # 自带 rank
                free_at_publisher = not is_oa and pubmed_free(doi, email)
                cands += res
            else:
                cands += [(u, r, 2) for u, r in res]
        cands += [(u, "mdpi_cdn", 0) for u in mdpi_cdn(doi, email)]

    # 按 URL 去重（同 URL 取更小的 rank，也就是更好的那一版），再拆成「直链」和「落地页」——
    # ponytail: 以前把 doi.org/落地页也当 PDF 直接抓，白等 27s 拿回一坨 HTML。落地页只用来找
    # citation_pdf_url。
    uniq: dict[str, tuple[str, int]] = {}
    for u, r, rk in cands:
        if u and (u not in uniq or rk < uniq[u][1]):
            uniq[u] = (r, rk)
    pdfish = [(u, r, rk) for u, (r, rk) in uniq.items() if _looks_like_pdf(u)]
    landings = [(u, r, rk) for u, (r, rk) in uniq.items() if not _looks_like_pdf(u)]
    resolver = f"https://doi.org/{ident['value']}" if re.match(r"^10\.", ident["value"]) else ""
    # ponytail: 元数据 API 一条直链都给不出时（Elsevier 这类），doi.org 是唯一入口 —— 它以前只当
    # Referer 用，从不被访问。它的 rank 跟 unpaywall 说的出版商版本一致（人家说出版商就是 OA 时
    # 它是 rank 0，否则排最后）。
    if resolver and resolver not in uniq:
        pub_rank = uniq.get(f"https://doi.org/{ident['value']}", ("", 4))[1]
        landings.append((resolver, "doi_resolver", pub_rank))

    note(f"[layer1] {ident['kind']} {ident['value']} → {len(pdfish)} 个直链 + "
         f"{len(landings)} 个落地页候选")
    body: bytes | None = None
    used: dict = {}

    def try_grab(url: str, route: str) -> bool:
        nonlocal body, used
        if route not in tried:
            tried.append(route)
        note(f"  → 试 {route}: {url[:100]}")
        try:
            got = grab(url, referer=resolver, email=email)
        except Blocked as b:
            blocked.append({"kind": b.kind, "url": b.url})
            return False
        if got:
            body, used = got, {"route": route, "url": url}
            return True
        return False

    # 出版商优先：unpaywall 说出版商自己就是 OA（有 rank 0 的候选）时，预印本/仓库那版先放着 ——
    # 不然 arXiv 的 submittedVersion 会先被拿到，出版商的 publishedVersion 就轮不到了。
    # 直链和落地页都要分层：arXiv 的 DOI 落地页（rank 2）上的 citation_pdf_url 就是 arXiv PDF，
    # 只挡直链的话它照样会从落地页那条绕回来。预印本（biorxiv/medrxiv）不参与，见 _prefer_publisher。
    prefer_pub = _prefer_publisher(ident, pdfish, landings, allow_browser)
    now_p, late_p = _defer_low_versions(pdfish, prefer_pub)
    # ponytail: doi.org 是解析器（302 到真正的落地页），可能慢（实测 10–27s），所以同层里排最后。
    ordered_l = sorted(landings, key=lambda t: (t[2], "doi.org/" in t[0]))
    now_l, late_l = _defer_low_versions(ordered_l, prefer_pub)
    if late_p or late_l:
        note(f"  ⇢ 先放着 {len(late_p)} 个低版本直链 + {len(late_l)} 个低版本落地页，"
             f"先试出版商的 OA 那版")

    def try_grab(url: str, route: str) -> bool:
        nonlocal body, used
        if route not in tried:
            tried.append(route)
        note(f"  → 试 {route}: {url[:100]}")
        try:
            got = grab(url, referer=resolver, email=email)
        except Blocked as b:
            blocked.append({"kind": b.kind, "url": b.url})
            return False
        if got:
            body, used = got, {"route": route, "url": url}
            return True
        return False

    def try_landing(url: str, route: str) -> bool:
        """落地页那一路：跟掉 doi.org 的 302 → 读 citation_pdf_url → 当直链抓。"""
        # 预印本站的落地页必定是 CF 墙（BROWSER_FIRST_HOSTS 特例就是这么来的）。除了直接命中，
        # 还要挡 doi.org 解析器那条：预印本的 doi.org 一定落到 biorxiv/medrxiv，白等它 10–27s 再撞 CF。
        if _browser_first(url) or (ident["kind"] == "preprint" and "doi.org/" in url):
            note(f"  ⇢ 跳过预印本站落地页（CF 墙，直链已交给 layer2）: {url[:80]}")
            return False
        target = resolve_landing(url, email) if "doi.org/" in url else url
        if _browser_first(target):
            note(f"  ⇢ 落地页落到预印本站（CF 墙，直链已交给 layer2）: {target[:80]}")
            return False
        note(f"  → 试落地页: {target[:100]}")
        for meta_url, _ in landing_meta_candidates(target, email):
            if try_grab(meta_url, "landing_meta"):
                return True
        return False

    for url, route, _ in now_p:
        if _browser_first(url):
            note(f"  ⇢ {url[:90]} —— 预印本特例，直接交给 layer2 的浏览器下载")
            if "browser_first" not in tried:
                tried.append("browser_first")
            blocked.append({"kind": "browser_first", "url": url})
            continue
        if try_grab(url, route):
            break
    if not body:
        for url, route, _ in now_l:
            if try_landing(url, route):
                break

    # layer2：门槛见 _l2_candidates（被墙的优先，空手时用落地页顶）。放在校验/写盘之前 ——
    # 「退到低版本」那句得排在它后面，而正文写盘只该有一处。
    l2: dict | None = None
    l2_cands = _l2_candidates(blocked, now_l, is_oa, free_at_publisher)
    if not body and allow_browser and l2_cands:
        try:
            import browser
            browser.LOG_HOOK = PROGRESS_HOOK      # layer2 的诊断也送去插件（它自己照样打 stderr）
            note(f"[layer2] 引擎={engine}，用真浏览器再试"
                 f"（{len(l2_cands)} 个{'被墙' if blocked else '落地页'}候选）")
            l2 = browser.fetch_with_browser(l2_cands, out, title=title, engine=engine,
                                            quiet=False)   # layer2 的诊断始终打到 stderr：--json 只约束 stdout
        except ImportError:
            note("[layer2] browser.py 不在（或缺少 patchright）→ 只报 layer1 结果")

    # 出版商那一路（含浏览器）都空手了，才退到预印本/仓库那版
    if not body and not (l2 or {}).get("ok"):
        if late_p:
            note(f"[layer1] 出版商那版没拿到 → 退到 {len(late_p)} 个低版本直链")
            for url, route, _ in late_p:
                if try_grab(url, route):
                    break
        if not body and late_l:
            note(f"[layer1] 再退到 {len(late_l)} 个低版本落地页")
            for url, route, _ in late_l:
                if try_landing(url, route):
                    break

    verdict = detail = None
    if body and title:
        verdict, detail = title_verdict(body, title)
        note(f"  [verify] {verdict}: {detail}")
        if verdict == "title_absent":
            note("  ✗ 是 PDF 但不是这篇 → 不算成功")
            body = None

    env = {"schema": SCHEMA, "input": raw_id, "kind": ident["kind"], "value": ident["value"],
           "ok": bool(body), "route": used.get("route"), "url": used.get("url"),
           "tried": tried + ([f"browser:{engine}"] if l2 is not None else []),
           "blocked_by": blocked, "verdict": verdict, "verify_detail": detail,
           "bytes": len(body) if body else 0,
           "sha256": hashlib.sha256(body).hexdigest() if body else None,
           "path": None, "free_at_publisher": free_at_publisher,
           "is_oa": is_oa, "oa_status": oa_status,
           "elapsed_s": round(time.time() - started, 1)}

    if body and out:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(body)
        env["path"] = str(out)

    # 浏览器那份（出版商的正文）覆盖 layer1 的结果
    if l2:
        if l2.get("ok"):
            env.update(ok=True, route=f"browser:{engine}", url=l2.get("url"),
                       bytes=l2.get("bytes", 0), sha256=l2.get("sha256"),
                       path=l2.get("path"), verdict=l2.get("verdict"),
                       verify_detail=l2.get("verify_detail"))
        if l2.get("blocked_kind"):
            env["blocked_by"] = blocked + [{"kind": l2["blocked_kind"],
                                            "url": l2.get("url") or ""}]
            if not l2.get("ok"):
                env["needs_human"] = bool(l2.get("needs_human"))

    # 付费墙：Unpaywall 明说不是 OA、PubMed 也没标 free、最后两手都没拿到 → 这不是「被墙」，
    # 是没有可下载副本（付费样本只能靠机构订阅）。插件/脚本据此给一句不同的提示。
    env["paywalled"] = bool(is_oa is False and not free_at_publisher and not env.get("ok"))
    if env["paywalled"]:
        note(f"  · 付费墙（Unpaywall oa_status={oa_status or 'closed'}）：没有 OA 副本 —— "
             f"这是付费样本，不是被墙")

    env["elapsed_s"] = round(time.time() - started, 1)
    return env


def exit_code(env: dict) -> int:
    if env.get("ok"):
        return 0
    if env.get("error"):
        return 1
    return 7 if env.get("blocked_by") else 2


# ── CLI / RPC ────────────────────────────────────────────────────────────────
def main(argv: list[str]) -> int:
    if argv and argv[0] == "rpc":
        import rpc_server
        return rpc_server.serve()
    if "--selfcheck" in argv:
        import selfcheck
        return selfcheck.run()

    ap = argparse.ArgumentParser(prog="lit-fetch", add_help=True)
    ap.add_argument("identifier")
    ap.add_argument("out", nargs="?")
    ap.add_argument("--title", default="")
    ap.add_argument("--email", default=CONTACT)
    ap.add_argument("--engine", default="edge", choices=["edge", "chrome", "chromium"])
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)

    env = fetch(a.identifier, Path(a.out) if a.out else None, title=a.title, email=a.email,
                engine=a.engine, allow_browser=not a.no_browser, quiet=a.json)
    if a.json:
        print(json.dumps(env, ensure_ascii=False))
    return exit_code(env)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
