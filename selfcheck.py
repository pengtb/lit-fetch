"""离线自检：纯逻辑分支的断言（不联网、不需要浏览器）。`python lit_fetch.py --selfcheck`"""
import os
import sys

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
              "https://europepmc.org/articles/PMC1?pdf=render"):
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

    # layer2 的门槛：被墙的候选优先；空手但落地页还在 → 用落地页顶；确认非 OA 且 PubMed 也不 free → 不开
    B = [{"kind": "cf_challenge", "url": "https://x/y.pdf"}]
    L = [("https://doi.org/10.1016/j.cell.2026.08.042", "unpaywall", 0)]
    assert lf._l2_candidates(B, L, None, False) == B
    assert lf._l2_candidates([], L, True, False) == [{"kind": "landing", "url": L[0][0]}]
    assert lf._l2_candidates([], L, None, False) == [{"kind": "landing", "url": L[0][0]}]
    assert lf._l2_candidates([], L, False, True) == [{"kind": "landing", "url": L[0][0]}]
    assert lf._l2_candidates([], L, False, False) == []
    assert lf._l2_candidates([], [], True, False) == []

    # 版本分层：出版商那版有 OA 候选（rank 0）时，预印本/仓库的直链和落地页都不抢在它前面
    P = [("https://publisher.example/x.pdf", "pub_pdf", 0),
         ("https://arxiv.org/pdf/2409.11654", "unpaywall", 2),
         ("https://doi.org/10.48550/arxiv.2409.11654", "unpaywall", 2)]
    now, late = lf._defer_low_versions(P, True)
    assert now == [P[0]], now
    assert late == P[1:], late
    now, late = lf._defer_low_versions(P, False)
    assert now == P and late == [], (now, late)

    # profile 占用检测：只有真在命令行里查到 --user-data-dir=<profile> 才算「被占用」，不会误报
    if os.name == "nt":
        h = browser._profile_holder()
        assert h is None or "--user-data-dir=" in h, h

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

    print("selfcheck OK：标识符 11 例 / 坏输入 3 例 / MDPI 2 例 / EPMC 3 例 / 退出码 4 例 / "
          "标题归一 2 例 / 直链判定 9 例 / bioRxiv 特例 4 例 / 下载取件 3 例 / "
          "出版商直链 5 例 / location 排序 6 例 / layer2 门槛 6 例 / 版本分层 3 例")
    return 0


if __name__ == "__main__":
    sys.exit(run())
