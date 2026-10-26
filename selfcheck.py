"""离线自检：纯逻辑分支的断言（不联网、不需要浏览器）。`python lit_fetch.py --selfcheck`"""
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

import browser
import lit_fetch as lf


def run() -> int:
    # 标识符归一化
    cases = {
        "10.1038/s41586-020-2649-2": ("doi", "10.1038/s41586-020-2649-2"),
        "doi:10.1371/journal.pcbi.1011787": ("doi", "10.1371/journal.pcbi.1011787"),
        "https://doi.org/10.3390/ijms25179306": ("doi", "10.3390/ijms25179306"),
        "arXiv:2610.04475": ("arxiv", "2610.04475"),
        "2601.12345v3": ("arxiv", "2601.12345"),
        "https://arxiv.org/abs/2401.00001": ("arxiv", "2401.00001"),
        "https://arxiv.org/pdf/cs.CL/0501001v2": ("arxiv", "cs.CL/0501001"),
        "10.1101/2025.05.31.656838": ("preprint", "10.1101/2025.05.31.656838"),
        "10.64898/2026.08.26.26361384": ("preprint", "10.64898/2026.08.26.26361384"),
        "2025.05.31.656838": ("preprint", "2025.05.31.656838"),
        "PMID: 32165432": ("pmid", "32165432"),
    }
    for raw, (kind, value) in cases.items():
        got = lf.classify(raw)
        assert got["kind"] == kind and got["value"] == value, (raw, got)

    for bad in ("hello world", "", "10."):
        try:
            lf.classify(bad)
        except ValueError:
            pass
        else:
            raise AssertionError(f"应当拒绝: {bad!r}")

    # MDPI CDN 直链拼装（把 Crossref 查询打桩，保证离线确定性）
    lf.crossref_volume = lambda doi, email="": (None, None)          # 走「两位期号+四位文章号」回退
    assert lf.mdpi_cdn("10.3390/ijms25179306") == [
        "https://mdpi-res.com/d_attachment/ijms/ijms-25-09306/article_deploy/ijms-25-09306.pdf"]
    lf.crossref_volume = lambda doi, email="": ("25", "International Journal of Molecular Sciences")
    urls = lf.mdpi_cdn("10.3390/ijms25179306")                        # 有全名时多试一个 slug
    assert urls[0] == "https://mdpi-res.com/d_attachment/ijms/ijms-25-09306/article_deploy/ijms-25-09306.pdf"
    assert urls[1].startswith("https://mdpi-res.com/d_attachment/internationaljournalofmolecularsciences/")
    lf.crossref_volume = lambda doi, email="": (None, None)
    assert lf.mdpi_cdn("10.1038/s41586-020-2649-2") == []

    # PMC → Europe PMC render
    assert lf.epmc_render("https://pmc.ncbi.nlm.nih.gov/articles/PMC8728283/") == \
        "https://europepmc.org/articles/PMC8728283?pdf=render"
    assert lf.epmc_render("https://europepmc.org/articles/PMC6063992?pdf=render") == \
        "https://europepmc.org/articles/PMC6063992?pdf=render"
    assert lf.epmc_render("https://example.org/nothing") is None

    # 退出码语义
    assert lf.exit_code({"ok": True}) == 0
    assert lf.exit_code({"ok": False, "error": "认不出"}) == 1
    assert lf.exit_code({"ok": False, "blocked_by": []}) == 2
    assert lf.exit_code({"ok": False, "blocked_by": [{"kind": "cf_challenge"}]}) == 7

    # 标题归一化
    assert lf._norm("Ensembl 2022!") == ["ensembl", "2022"]
    assert lf._norm(None) == []

    # arXiv 的 DataCite DOI（Zotero 存的就是这个形态）
    for raw, want, ver in (("10.48550/arXiv.2610.04475", "2610.04475", ""),
                           ("10.48550/arXiv.2610.04475v2", "2610.04475", "v2"),
                           ("10.48550/arxiv.cs.CL/0501001", "cs.CL/0501001", ""),
                           ("doi:10.48550/arXiv.0706.0044", "0706.0044", "")):
        got = lf.classify(raw)
        assert got["kind"] == "arxiv" and got["value"] == want and got["version"] == ver, (raw, got)
    got = lf.classify("10.1038/s44320-025-00131-3")
    assert got["kind"] == "doi" and got["value"] == "10.1038/s44320-025-00131-3", got

    # 直链 / 落地页分辨（决定要不要直接当 PDF 抓）
    for u in ("https://x.org/a.pdf", "https://x.org/a.pdf?download=1",
              "https://onlinelibrary.wiley.com/doi/pdfdirect/10.1/x?download=true",
              "https://academic.oup.com/nar/article-pdf/50/D1/D988/1/x.pdf",
              "https://europepmc.org/articles/PMC1?pdf=render",
              "https://www.cell.com/action/showPdf?pii=S0092-8674(26)01119-0"):
        assert lf._looks_like_pdf(u), u
    for u in ("https://doi.org/10.1/x", "https://journals.plos.org/ploscompbiol/article?id=10.1/x",
              "https://pmc.ncbi.nlm.nih.gov/articles/PMC1/", ""):
        assert not lf._looks_like_pdf(u), u

    # bioRxiv 特例：只有它走「直接交给浏览器下载」
    assert lf._browser_first("https://www.biorxiv.org/content/biorxiv/early/2025/06/01/x.full.pdf")
    for u in ("https://www.medrxiv.org/content/10.1101/xv1.full.pdf",
              "https://arxiv.org/pdf/2610.04475", "https://link.springer.com/content/pdf/10.1/x.pdf"):
        assert not lf._browser_first(u), u

    # 出版商直链：10.1038 两条都拼（Nature 本家 + Springer 转移刊），别的出版社不给
    urls = [u for u, _ in lf.publisher_pdf_candidates("10.1038/s44320-025-00131-3")]
    assert urls == ["https://www.nature.com/articles/s44320-025-00131-3.pdf",
                    "https://link.springer.com/content/pdf/10.1038/s44320-025-00131-3.pdf"], urls
    assert [r for _, r in lf.publisher_pdf_candidates("10.1007/s00239-024-10186-9")] == ["springer_pdf"]
    for doi in ("10.3390/ijms25179306", "10.1101/2025.05.31.656838", "10.1371/journal.pcbi.1011787"):
        assert lf.publisher_pdf_candidates(doi) == [], doi

    # Unpaywall location 排序：出版商的 publishedVersion 最先，补充材料和预印本仓库最后
    rank = lf._loc_rank
    assert rank({"host_type": "publisher", "version": "publishedVersion", "url": "https://a/x.pdf"}) == 0
    assert rank({"host_type": "publisher", "version": "acceptedVersion", "url": "https://a/x.pdf"}) == 1
    assert rank({"host_type": "repository", "url": "https://pmc.ncbi.nlm.nih.gov/articles/PMC1"}) == 2
    assert rank({"url": "https://www.biorxiv.org/content/x.full.pdf"}) == 3
    assert rank({"host_type": "publisher", "version": "publishedVersion",
                 "url_for_pdf": "https://www.embopress.org/action/downloadSupplement?file=x_MOESM2_ESM.pdf"}) == 4
    assert rank({"url": "https://x.org/suppl/data.pdf"}) == 4

    # Cell Press：PII 两种写法互转（DOI 里没有 PII，Crossref 的 alternative-id 才是那串紧凑形式）
    assert lf._hyphen_pii("S0092867426011190") == "S0092-8674(26)01119-0"
    assert lf._hyphen_pii("S009286742601119") is None         # 少一位
    assert lf._hyphen_pii("") is None
    assert lf._hyphen_pii(None) is None
    assert lf.cell_press_pdf_candidates("10.1038/x") == []    # 非 Elsevier 不碰网络
    _real_json, lf._get_json = lf._get_json, lambda *a, **k: {"message": {"alternative-id": ["S0092867426011190"]}}
    try:
        got = lf.cell_press_pdf_candidates("10.1016/j.cell.2026.09.017")
    finally:
        lf._get_json = _real_json
    assert got == [("https://www.cell.com/action/showPdf?pii=S0092-8674(26)01119-0",
                    "cell_press_pdf")], got

    # layer2 的门槛：被墙的候选优先；空手但落地页还在 → 用落地页顶；确认非 OA 且 PubMed 也不 free 时
    # **只**为出版商页（rank 0 / doi_resolver）开窗（机构 IP 下这条路实测能成），仓库页不开。
    B = [{"kind": "cf_challenge", "url": "https://x/y.pdf"}]
    L = [("https://doi.org/10.1016/j.cell.2026.08.042", "unpaywall", 0)]
    assert lf._l2_candidates(B, L, None, False) == B
    assert lf._l2_candidates([], L, True, False) == [{"kind": "landing", "url": L[0][0]}]
    assert lf._l2_candidates([], L, None, False) == [{"kind": "landing", "url": L[0][0]}]
    assert lf._l2_candidates([], L, False, True) == [{"kind": "landing", "url": L[0][0]}]
    assert lf._l2_candidates([], L, False, False) == [{"kind": "landing", "url": L[0][0]}]
    assert lf._l2_candidates([], [], True, False) == []
    # closed + 落地页里只有 doi_resolver（rank 4）也要开 —— 它就是出版商页（10.1016/j.cell.2026.09.017）
    R = [("https://pmc.ncbi.nlm.nih.gov/articles/PMC1/", "unpaywall", 2),
         ("https://doi.org/10.1016/j.cell.2026.09.017", "doi_resolver", 4)]
    assert lf._l2_candidates([], R, False, False) == [
        {"kind": "landing", "url": "https://doi.org/10.1016/j.cell.2026.09.017"}]
    # closed + 只有仓库/预印本落地页 → 还是不开窗
    assert lf._l2_candidates([], [("https://pmc.ncbi.nlm.nih.gov/articles/PMC1/", "unpaywall", 2)],
                             False, False) == []

    # 付费刊那条的开窗门槛：paywall=False（插件设置 / --no-paywall / LITFETCH_PAYWALL=0）时整条关掉
    assert lf._l2_candidates([], L, False, False, paywall=False) == []
    assert lf._l2_candidates([], L, False, False, paywall=True) == [{"kind": "landing", "url": L[0][0]}]
    assert lf._l2_candidates(B, L, False, False, paywall=False) == B      # 被墙的候选不受这个开关影响
    # `LITFETCH_PAYWALL` 的取值解析（默认试）
    assert lf._env_flag("SELF_CHECK_NOT_SET_XYZ", True) is True
    for v in ("0", "false", "NO", " off "):
        os.environ["SELF_CHECK_FLAG"] = v
        assert lf._env_flag("SELF_CHECK_FLAG", True) is False, v
    for v in ("1", "true", "yes", ""):
        os.environ["SELF_CHECK_FLAG"] = v
        assert lf._env_flag("SELF_CHECK_FLAG", True) is True, v
    del os.environ["SELF_CHECK_FLAG"]

    # 两层开关：layer1 关掉 + layer2 关掉 = 一个请求都不发、直接空手回来（arXiv 没有 doi.org 入口可兜）
    env = lf.fetch("arXiv:2610.04475", layer1=False, allow_browser=False, quiet=True)
    assert env["ok"] is False and env["tried"] == [], env
    assert env["layer1"] is False and env["layer2"] is False, env
    assert (lf.LAYER1_DEFAULT, lf.LAYER2_DEFAULT, lf.TRY_PAYWALL_DEFAULT) == (True, True, True)

    # 出版商优先只对期刊 DOI 成立（预印本自己就是"出版商"，走 browser_first 最快）
    P0 = [("https://publisher.example/x.pdf", "pub_pdf", 0)]
    L0 = [("https://doi.org/10.1/x", "unpaywall", 0)]
    assert lf._prefer_publisher({"kind": "doi"}, P0, L0, True) is True
    assert lf._prefer_publisher({"kind": "preprint"}, P0, L0, True) is False
    assert lf._prefer_publisher({"kind": "doi"}, P0, L0, False) is False
    assert lf._prefer_publisher({"kind": "doi"}, [], [("https://a/b", "unpaywall", 2)], True) is False

    # 版本分层：出版商那版有 OA 候选（rank 0）时，预印本/仓库的直链和落地页都不抢在它前面
    P = [("https://publisher.example/x.pdf", "pub_pdf", 0),
         ("https://arxiv.org/pdf/2409.11654", "unpaywall", 2),
         ("https://doi.org/10.48550/arxiv.2409.11654", "unpaywall", 2)]
    now, late = lf._defer_low_versions(P, True)
    assert now == [P[0]], now
    assert late == P[1:], late
    now, late = lf._defer_low_versions(P, False)
    assert now == P and late == [], (now, late)

    # .env 解析：引号里的 # 不是注释、引号 + 行内注释要一起处理、export 前缀要认、已有环境变量优先
    import tempfile
    envfile = Path(tempfile.mkdtemp()) / ".env"
    envfile.write_text('LITFETCH_EMAIL="a#b@x.edu"  # 行内注释\n'
                       "LITFETCH_PROFILE='C:\\Users\\x\\.p'   # 单引号也一样\n"
                       "# 整行注释\n"
                       'export LITFETCH_FIXTURES=out\\fixtures.json  # 没引号就按 # 切\n'
                       'export JUNK\n'
                       'NOT_A_PAIR\n', encoding="utf-8")
    for k in ("LITFETCH_EMAIL", "LITFETCH_FIXTURES", "LITFETCH_PROFILE"):
        os.environ.pop(k, None)
    lf.load_dotenv(envfile)
    assert os.environ["LITFETCH_EMAIL"] == "a#b@x.edu", os.environ["LITFETCH_EMAIL"]
    assert os.environ["LITFETCH_PROFILE"] == "C:\\Users\\x\\.p", os.environ["LITFETCH_PROFILE"]
    assert os.environ["LITFETCH_FIXTURES"] == "out\\fixtures.json", os.environ["LITFETCH_FIXTURES"]
    assert "JUNK" not in os.environ and "NOT_A_PAIR" not in os.environ
    os.environ["LITFETCH_EMAIL"] = "keep@me"
    lf.load_dotenv(envfile)
    assert os.environ["LITFETCH_EMAIL"] == "keep@me"      # 已有的环境变量优先，不被 .env 覆盖
    for k in ("LITFETCH_EMAIL", "LITFETCH_FIXTURES", "LITFETCH_PROFILE"):
        os.environ.pop(k, None)

    # profile 占用检测：只有真在命令行里查到 --user-data-dir=<profile> 才算「被占用」，不会误报
    if os.name == "nt":
        h = browser._profile_holder()
        assert h is None or "--user-data-dir=" in h, h

    # 下载已经落盘、但页面读不出内容时（page.content() 抛 → _challenged 的兜底会判"还在挑战"），
    # _download_via_nav 必须先看磁盘把它认下来 —— 实测 10.64898/2026.08.04.742916 就是在这儿丢的：
    # 文件 03:46:40 写完，代码却按 cf_challenge 收尾、没人读它。
    class _FakeCDP:
        def send(self, *a, **k):
            pass

    class _FakeCtx:
        def new_cdp_session(self, page):
            return _FakeCDP()

    browser.DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    old_dl = browser.DOWNLOAD_DIR
    browser.DOWNLOAD_DIR = Path(tempfile.mkdtemp())     # 别动真实下载目录（_download_via_nav 会清空它）
    fakedl = browser.DOWNLOAD_DIR / "selfcheck-dl2.pdf"

    class _FakePage:
        """模拟真实时序：导航那一刻下载落盘，而页面正在下载 → content() 取不到。"""

        def __init__(self):
            self.context = _FakeCtx()
            self.url = "https://www.biorxiv.org/content/x/y.full.pdf"

        def goto(self, *a, **k):
            fakedl.write_bytes(b"%PDF-1.4\n" + b"x" * 4096)
            raise RuntimeError("下载一开始 goto 必然抛")

        def content(self):
            raise RuntimeError("页面正在下载 → 取不到内容")

        def title(self):
            return "请稍候…"

        def wait_for_timeout(self, ms):
            time.sleep(ms / 1000)

        def bring_to_front(self):
            pass

    old_wait = browser.CF_WAIT_S
    browser.CF_WAIT_S = 0          # 万一回归到"先等挑战"，也别让自检干等 60s
    try:
        got, why = browser._download_via_nav(_FakePage(), "https://www.biorxiv.org/x.full.pdf",
                                             quiet=True)
        assert got and got[:4] == b"%PDF" and why is None, (got and len(got), why)
    finally:
        browser.CF_WAIT_S = old_wait
        browser.DOWNLOAD_DIR = old_dl
        shutil.rmtree(fakedl.parent, ignore_errors=True)

    # _route_grab 的 route/unroute 必须用「同一个谓词 + 同一个 handler」对象：实测用新 lambda 时
    # Playwright 匹配不上，路由会一直留着，之后同一个 URL 的所有导航都被吞掉（空白页 + 静默 420s）。
    class _RoutePage:
        def __init__(self):
            self.registered, self.unrouted = [], []

        def route(self, pred, handler):
            self.registered.append((pred, handler))

        def unroute(self, pred=None, handler=None):
            self.unrouted.append((pred, handler))

        def goto(self, *a, **k):
            raise RuntimeError("fulfill 掉之后 goto 必然抛")

    rp = _RoutePage()
    body, _why = browser._route_grab(rp, "https://x/y.pdf", quiet=True)
    assert body is None, body
    assert rp.registered and rp.unrouted, (rp.registered, rp.unrouted)
    assert rp.registered[0][0] is rp.unrouted[0][0], "unroute 的谓词必须是同一个对象"
    assert rp.registered[0][1] is rp.unrouted[0][1], "unroute 的 handler 必须是同一个对象"

    # CDP 下载的取件逻辑：大小稳定的 PDF 才算；旧文件/未来时间点/非 PDF 都不认
    import time as _t
    browser.DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    probe = browser.DOWNLOAD_DIR / "selfcheck-dl.pdf"
    probe.write_bytes(b"%PDF-1.4\n" + b"x" * 4096)
    try:
        assert browser._wait_fresh_download(_t.time() - 5, quiet=True, timeout=8) is not None
        assert browser._wait_fresh_download(_t.time() + 3600, quiet=True, timeout=2) is None
        probe.write_bytes(b"<html>not a pdf</html>")
        assert browser._wait_fresh_download(_t.time() - 5, quiet=True, timeout=3) is None
    finally:
        probe.unlink()

    # 代理：写法规范化 / 脱敏 / requests 与浏览器两边的参数形状
    assert lf._proxy_url("") == ""
    assert lf._proxy_url("127.0.0.1:7890") == "http://127.0.0.1:7890"
    assert lf._proxy_url("socks5://127.0.0.1:1080") == "socks5://127.0.0.1:1080"
    try:
        lf._proxy_url("ftp://x:1")
    except ValueError:
        pass
    else:
        raise AssertionError("ftp 代理应当被拒")
    try:
        lf._proxy_url("http://127.0.0.1")                 # 没端口 → 入口就拒，别等到浏览器那层
    except ValueError:
        pass
    else:
        raise AssertionError("缺端口的代理应当被拒")
    old_state = (lf._ACTIVE_PROXY, lf._ACTIVE_PROXY_ALL, lf._PROXY_FORCE)
    try:
        lf._ACTIVE_PROXY, lf._ACTIVE_PROXY_ALL, lf._PROXY_FORCE = "http://127.0.0.1:7890", False, False
        assert lf._proxies() == {}, "默认（只付费刊）在普通段不能走代理"
        lf._PROXY_FORCE = True
        assert lf._proxies() == {"http": "http://127.0.0.1:7890", "https": "http://127.0.0.1:7890"}
        lf._PROXY_FORCE = False
        lf._ACTIVE_PROXY_ALL = True
        assert lf._proxies() == {"http": "http://127.0.0.1:7890", "https": "http://127.0.0.1:7890"}
        lf._ACTIVE_PROXY = ""
        assert lf._proxies() == {}, "没配代理时必须返回空 dict（保持 requests 默认）"
    finally:
        lf._ACTIVE_PROXY, lf._ACTIVE_PROXY_ALL, lf._PROXY_FORCE = old_state

    # 默认代理范围：非 OA（closed 或判断不了）的出版商探测才开
    assert lf._is_paywall_probe(False, False, True) is True
    assert lf._is_paywall_probe(False, True, True) is False      # PubMed 标了 free
    assert lf._is_paywall_probe(True, False, True) is False      # 本来就是 OA
    assert lf._is_paywall_probe(None, False, True) is True       # 判断不了 → 按付费刊探测处理
    assert lf._is_paywall_probe(False, False, False) is False    # paywall 关了

    assert lf._proxy_display("http://u:p@127.0.0.1:7890") == "http://***@127.0.0.1:7890"
    assert lf._socks_needs_pysocks("http://127.0.0.1:7890") is False
    assert browser._proxy_settings("") is None
    assert browser._proxy_settings("127.0.0.1:7890") == {"server": "http://127.0.0.1:7890"}
    assert browser._proxy_settings("socks5://u:p@127.0.0.1:1080") == {
        "server": "socks5://127.0.0.1:1080", "username": "u", "password": "p"}
    try:
        browser._proxy_settings("http://127.0.0.1")      # 没端口 → Chromium 不会替我们猜
    except ValueError:
        pass
    else:
        raise AssertionError("缺端口的代理应当被拒")

    print("selfcheck OK：标识符 11 例 / 坏输入 3 例 / MDPI 2 例 / EPMC 3 例 / 退出码 4 例 / "
          "标题归一 2 例 / 直链判定 10 例 / bioRxiv 特例 4 例 / 下载取件 3 例 / "
          "出版商直链 5 例 / location 排序 6 例 / layer2 门槛 8 例 / Cell Press PII 6 例 / paywall 开关 11 例 / 两层开关 4 例 / 版本分层 3 例 / 代理 20 例")
    return 0


if __name__ == "__main__":
    sys.exit(run())
