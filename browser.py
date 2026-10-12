"""layer2：真浏览器兜底（默认用系统已装的 Edge，不下载任何浏览器构建）。

取字节的两条路，按顺序试：
  1. **让浏览器直接下载**（profile 里把 PDF 设成「下载」而不是内置阅读器打开 →
     `expect_download` 事件把文件落到磁盘，不用猜阅读器里是什么）。bioRxiv 这种
     「导航 200 application/pdf、但二次请求一律 403」的站点只能靠这条。
  2. 真导航 → 响应头是 application/pdf → 取 302 后的带 token 终址 → 用浏览器上下文的
     HTTP 客户端再取一次（OUP/silverchair 那种：1.71MB %PDF-）。
挑战区分自动与「要人点」：后者窗口是 headful 的，喊一声让人点一下就能救回来。
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import urljoin

# ponytail: 允许用 LITFETCH_PROFILE 换个 profile 目录 —— 上一次跑剩的 Edge 还占着老目录时
# （Windows 上 rename/删除都会 Permission denied），换一个干净的比去 kill 进程省事。
PROFILE_DIR = Path(os.environ.get("LITFETCH_PROFILE") or (Path.home() / ".litfetch_profile"))
CF_WAIT_S = 60
CAND_BUDGET_S = 60          # 单个候选最多花这么久（三招串起来别把整轮拖成几分钟）
NAV_TIMEOUT_MS = 45000

_CHALLENGE = ("just a moment", "請稍候", "请稍候", "cf_chl_opt", "cf-chl", "verify you are human",
              "attention required", "client challenge", "access denied")
_INTERACTIVE = ("challenges.cloudflare.com", "cf-turnstile", "data-sitekey", "verify you are human",
                "請驗證", "请验证", "確認您是真人")
_DOWNLOAD_FIRST_HOSTS = ("biorxiv.org", "medrxiv.org")   # 只有「浏览器下载」那条成过


def _download_first(url: str) -> bool:
    host = (url or "").split("//")[-1].split("/")[0].lower()
    return any(h in host for h in _DOWNLOAD_FIRST_HOSTS)

JS_FETCH = """async (u) => {
  const r = await fetch(u, {credentials: 'include'});
  const b = new Uint8Array(await r.arrayBuffer());
  let s = '';
  for (let i = 0; i < b.length; i += 0x8000) s += String.fromCharCode.apply(null, b.subarray(i, i + 0x8000));
  return {status: r.status, ctype: r.headers.get('content-type') || '', url: r.url, n: b.length, b64: btoa(s)};
}"""


LOG_HOOK = None             # ponytail: RPC 侧接上它，layer2 的行也进插件（插件读不到 stderr）


def _log(msg: str, quiet: bool) -> None:
    if LOG_HOOK:
        with contextlib.suppress(Exception):
            LOG_HOOK(msg)
    if not quiet:
        print(f"[layer2] {msg}", file=sys.stderr)


DOWNLOAD_DIR = PROFILE_DIR / "DownloadedFiles"


def _clear_session(profile_dir: Path) -> None:
    """清掉会话存档。

    ponytail: 我们的浏览器每次都是被 Playwright/进程结束带走的（对 Edge 来说算「非正常退出」），
    于是下次启动它会把这轮跑剩的页签恢复回来 —— 用户看到的就是「一开就多两个空白页」。存档只是
    页签状态，删掉无副作用。
    """
    d = profile_dir / "Default"
    for pat in ("Sessions/*", "EdgeSessions/*", "Current Session", "Current Tabs",
                "Last Session", "Last Tabs"):
        for f in d.glob(pat):
            with contextlib.suppress(Exception):
                if f.is_dir():
                    shutil.rmtree(f, ignore_errors=True)
                else:
                    f.unlink()


def _force_pdf_download(profile_dir: Path) -> None:
    """把 profile 设成「PDF 直接下载」而不是内置阅读器打开。

    ponytail: 这是「导航拿到 200 application/pdf 却取不到字节」的正解 —— 省掉一切猜阅读器的把戏。
    Chromium/Edge 的偏好就在 <profile>/Default/Preferences 这个 JSON 里，启动前写进去即可。
    顺带把「每次询问保存位置」关掉：Edge 弹保存对话框时自动化答不了，target 会直接被关掉。
    """
    d = profile_dir / "Default"
    d.mkdir(parents=True, exist_ok=True)
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    p = d / "Preferences"
    try:
        data = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
        if not isinstance(data, dict):
            data = {}
    except Exception:
        data = {}
    plugins = data.setdefault("plugins", {})
    dl = data.setdefault("download", {})
    savefile = data.setdefault("savefile", {})
    sess = data.setdefault("session", {})
    prof = data.setdefault("profile", {})
    needs = (plugins.get("always_open_pdf_externally") is not True
             or dl.get("prompt_for_download") is not False
             or dl.get("default_directory") != str(DOWNLOAD_DIR)
             or sess.get("restore_on_startup") != 0
             or bool(sess.get("startup_urls"))
             or prof.get("exit_type") != "Normal"
             or prof.get("exited_cleanly") is not True
             or "session_restore_prompt" in data)
    if needs:
        plugins["always_open_pdf_externally"] = True
        dl["prompt_for_download"] = False
        dl["default_directory"] = str(DOWNLOAD_DIR)
        savefile["default_directory"] = str(DOWNLOAD_DIR)
        # 别在启动时恢复上次会话：0=开新标签页（1=继续上次、4=指定页面）
        sess["restore_on_startup"] = 0
        sess["startup_urls"] = ""
        # 上次「非正常退出」是恢复页签的另一个触发点（我们每次都是被结束掉的）
        prof["exit_type"] = "Normal"
        prof["exited_cleanly"] = True
        data.pop("session_restore_prompt", None)
        p.write_text(json.dumps(data), encoding="utf-8")


def _download_via_nav(page, url: str, quiet: bool) -> tuple[bytes | None, str | None]:
    """真导航，让浏览器把 PDF 当下载写盘；我们从盘上取字节（不碰阅读器、不发第二次请求）。

    ponytail: 下载通道整条交给 CDP 显式开（`Browser.setDownloadBehavior`，behavior=allow +
    downloadPath + eventsEnabled），文件由 Edge 自己写进 DOWNLOAD_DIR，我们只负责等它写完。

    为什么不用 Playwright 的 `expect_download` + `save_as`：实测事件会正常触发，但拿到的文件是
    0 字节（事件在传输开始时就发，save_as 拿到的不是最终内容）。让它自己写盘、我们等文件长大，
    只有一个真相来源。

    返回 (body, why)：why="cf_challenge" 表示卡在 Cloudflare 人类验证（调用方据此报 needs_human）。
    """
    t0 = time.time()
    for old in DOWNLOAD_DIR.glob("*.pdf*"):      # 先清干净：免得把上一轮的文件当成这一轮的
        with contextlib.suppress(Exception):
            old.unlink()
    try:
        cdp = page.context.new_cdp_session(page)
        cdp.send("Browser.setDownloadBehavior", {
            "behavior": "allow", "downloadPath": str(DOWNLOAD_DIR), "eventsEnabled": True})
    except Exception as e:
        _log(f"  CDP 下载通道没开成（{type(e).__name__}: {str(e)[:60]}）", quiet)
    with contextlib.suppress(Exception):     # 下载一开始 goto 就会抛，属正常
        page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
    # ponytail: 这条路以前**完全不看挑战** —— bioRxiv 的 CF 一旦给验证页，这里就只是干等 420s 的下载
    # 事件（用户看到的正是「卡很长时间也不通过」，而且没人告诉他要点一下）。所以导航后先判挑战，
    # 把窗口弄到前面、等真人点；点过了再重新导航一次，让下载真正开始。
    if _challenged(page):
        _bring_to_front(page)
        if not _settle(page, quiet, "下载前"):
            return None, "cf_challenge"
        with contextlib.suppress(Exception):
            page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
    body = _wait_fresh_download(t0, quiet)
    if body:
        _log(f"  浏览器下载 → {len(body)}B", quiet)
        return body, None
    _log("  下载目录里没等到 PDF", quiet)
    return None, "cf_challenge" if _challenged(page) else "download_failed"


def _bring_to_front(page) -> None:
    """把窗口提到前面来 —— 挑战要真人点时，光在 stderr 里喊一句是没人看得见的。"""
    with contextlib.suppress(Exception):
        page.bring_to_front()


def _wait_fresh_download(since: float, quiet: bool, timeout: float = 420.0,
                         stall_s: float = 45.0) -> bytes | None:
    """等 CDP 指定的下载目录里出现一个「写完的」PDF，返回它的字节。

    ponytail: 判据不是「总共等多久」而是「还在不在长」。bioRxiv 实测会节流：37MB 先是 40 秒下完，
    被限速后同样一个文件要几分钟（只写到 6.7MB 就卡）。所以只要 `.crdownload` 还在变大就继续等，
    连续 stall_s 秒一点没长才认输；timeout 只是硬上限。
    只认我们自己那条下载通道指定的目录 —— 不去别的目录里瞎捞。
    """
    deadline = time.time() + timeout
    last_size = -1
    last_change = time.time()
    last_told = time.time()
    while time.time() < deadline:
        try:
            done = [p for p in DOWNLOAD_DIR.glob("*.pdf")
                    if p.stat().st_mtime >= since - 1 and p.stat().st_size > 2048]
        except Exception:
            done = []
        if done:
            newest = max(done, key=lambda q: q.stat().st_mtime)
            size = newest.stat().st_size
            if last_size == size and size > 0:
                body = newest.read_bytes()
                if body[:4] == b"%PDF":
                    return body
            last_size = size
            last_change = time.time()
        else:
            try:
                size = max((p.stat().st_size for p in DOWNLOAD_DIR.glob("*.crdownload")),
                           default=0)
            except Exception:
                size = 0
            if size > last_size:
                last_size, last_change = size, time.time()
            elif size and time.time() - last_change > stall_s:
                _log(f"  下载停住了（{size / 1e6:.1f}MB 之后不再增长）", quiet)
                return None
            elif size and time.time() - last_told > 30:
                last_told = time.time()
                _log(f"  还在下… {size / 1e6:.1f}MB", quiet)
        time.sleep(1.0)
    return None


def _challenged(page) -> bool:
    if "__cf_chl_rt_tk" in page.url:
        return True
    try:
        low = page.content()[:200_000].lower()
    except Exception:            # 页面正在导航时会抛，当「还没稳」
        return True
    return any(m in low for m in _CHALLENGE)


def _challenge_kind(page) -> str:
    try:
        low = page.content()[:200_000].lower()
    except Exception:
        return "auto"
    return "interactive" if any(m in low for m in _INTERACTIVE) else "auto"


def _settle(page, quiet: bool, step: str) -> bool:
    started = time.time()
    told = False
    while _challenged(page):
        if not told and time.time() - started > 8 and _challenge_kind(page) == "interactive":
            told = True
            _bring_to_front(page)                 # 光打日志没人看得见，把窗口提到前面来
            _log(f"{step}：挑战需要真人操作 —— 在弹出的浏览器窗口里点一下，我继续等（"
                 f"不点就等它自己过）", quiet)
        if time.time() - started > CF_WAIT_S:
            _log(f"{step}：挑战 {CF_WAIT_S}s 内没过（{page.title()[:40]!r}）—— "
                 f"{'需要真人点' if told else '这是墙，不是论文'}", quiet)
            return False
        time.sleep(2)
    return True


def _first_page(ctx):
    """复用启动时那个空白页（persistent context 启动就带一个 about:blank）。

    ponytail: 以前这里 new_page() + 一个 keep-alive 页，窗口一开就是「两个空白页 + 目标页」。
    现在整轮共用这一个页。
    """
    try:
        pages = [p for p in ctx.pages if not p.is_closed()]
    except Exception:
        pages = []
    if pages:
        return pages[0]
    try:
        return ctx.new_page()
    except Exception:
        return None


class ProfileBusy(RuntimeError):
    """这份 profile 正被另一个 Edge 占着 —— 不能装没看见地把 profile 挪开重开。"""


def _profile_holder() -> str | None:
    """谁在占着这份 profile（Windows：查 msedge 命令行里的 --user-data-dir）。没有就返回 None。

    ponytail: Chromium 在 user-data-dir 被占用时不会起第二个实例，launch 直接抛。以前 _launch 的兜底是
    把 profile 挪开、换个干净目录重开 —— 对普通站点无所谓，但这份 profile 里存着 cf_clearance
    （bioRxiv 这类站点全靠它，实测有效期一年多）。挪开 = 把 cookie 扔掉 = 又变回挑战。所以先查，查到
    就明说"关掉那个窗口再跑"，别偷偷绕开。
    """
    if os.name != "nt":
        return "lockfile 存在" if (PROFILE_DIR / "lockfile").exists() else None
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "(Get-CimInstance Win32_Process -Filter \"name='msedge.exe'\") | "
             "ForEach-Object { $_.ProcessId.ToString() + ' ' + $_.CommandLine }"],
            capture_output=True, text=True, timeout=20)
    except Exception:
        return None                       # 查不动就别拦着，让 launch 自己去撞
    for line in (r.stdout or "").splitlines():
        if f"--user-data-dir={PROFILE_DIR}" in line:
            pid = line.split(maxsplit=1)[0]
            return f"pid={pid} --user-data-dir={PROFILE_DIR}"
    return None


def _launch_at(pw, kwargs: dict, profile: Path):
    profile.mkdir(parents=True, exist_ok=True)
    _force_pdf_download(profile)
    _clear_session(profile)              # 别把上轮的页签恢复回来
    return pw.chromium.launch_persistent_context(
        user_data_dir=str(profile), headless=False, viewport={"width": 1280, "height": 900},
        accept_downloads=True, **kwargs)


def _launch(pw, kwargs: dict):
    """开浏览器；profile 起不来就挪开它、用干净的重开一次。

    ponytail: 实测过一次「profile 一启动就让 Edge 退出」（exit 21）—— 那种状态下它会一直坏着，
    每次都得人工插手。自动挪开 + 重开，工具才敢在无人看着的地方跑（比如将来被 Zotero 插件调）。
    但「被别的 Edge 占着」不算坏：那会丢掉 profile 里的 cf_clearance，要明着报错。
    """
    if (holder := _profile_holder()):
        raise ProfileBusy(
            f"这份 profile（{PROFILE_DIR.name}）正被另一个 Edge 占着（{holder}）—— "
            f"先关掉那个 Edge 窗口再跑；如果它是上一轮留下的人类验证页，先在窗口里点过验证再关。"
            f"直接开会被 Chromium 拒掉，绕开它又会丢掉里面的 cf_clearance")
    try:
        return _launch_at(pw, kwargs, PROFILE_DIR)
    except Exception as e:
        _log(f"  profile {PROFILE_DIR.name} 起不来（{type(e).__name__}）→ 挪开它、用干净的重开"
             f"（注意：新目录里没有 cf_clearance，CF 站点会重新给挑战）", False)
        fresh = PROFILE_DIR
        with contextlib.suppress(Exception):
            broken = PROFILE_DIR.with_name(PROFILE_DIR.name + ".broken")
            if broken.exists():
                shutil.rmtree(broken, ignore_errors=True)
            PROFILE_DIR.rename(broken)
        if PROFILE_DIR.exists():        # 挪不动（老目录还被占着）→ 换个目录名，别在这儿卡死
            fresh = PROFILE_DIR.with_name(PROFILE_DIR.name + ".fresh")
        return _launch_at(pw, kwargs, fresh)


def _route_grab(page, url: str, quiet: bool) -> tuple[bytes | None, str | None]:
    """首选：用 Playwright 的 route.fetch() 在渲染器拿到 PDF 之前把响应体截下来。

    ponytail: 这是三条路里最干净的一条 —— 不改 Edge 的 PDF 偏好、不二次请求、不碰内置阅读器，
    页面也不会因为「把 PDF 交给下载流程」而整个挂掉（实测 dummy.pdf 13KB %PDF-，页面存活）。
    返回 (body, why)；why 形如 "http_403"，让调用方知道「这个候选是被直接拒绝的，别再折腾」。
    """
    box: dict = {"err": None}

    def handler(route):
        try:
            resp = route.fetch()
            body = resp.body()
            if body[:4] == b"%PDF" and len(body) > 2048:
                box["body"] = body
            else:
                box["err"] = f"http_{resp.status}" if resp.status >= 400 else f"not_pdf_{resp.status}"
            route.fulfill(status=204, body=b"")
        except Exception as e:
            box["err"] = f"{type(e).__name__}: {str(e)[:60]}"
            with contextlib.suppress(Exception):
                route.abort()

    page.route(lambda u: u == url, handler)
    try:
        page.goto(url, wait_until="commit", timeout=NAV_TIMEOUT_MS)
    except Exception:
        pass                       # 路由里 fulfill 掉之后 goto 必然抛，属正常
    finally:
        with contextlib.suppress(Exception):
            page.unroute(lambda u: u == url)
    if box.get("body"):
        _log(f"  route.fetch → {len(box['body'])}B", quiet)
        return box["body"], None
    _log(f"  route.fetch 没拿到 PDF（{box.get('err') or '路由没触发'}）", quiet)
    return None, box.get("err")


def _landing_grab(page, url: str, quiet: bool) -> tuple[bytes | None, str | None]:
    """落地页候选专用：导航 → 等它跳完（meta refresh / JS）→ 读 citation_pdf_url → 页面内 fetch。

    ponytail: 落地页走 ①route.fetch 是纯粹的浪费 —— ① 会 `fulfill(204)` 把这次导航吞掉，页面停在
    about:blank，linkinghub.elsevier.com 那种「JS/meta 跳转壳」于是永远跳不动（用户看到的就是空白页，
    一直不导航）；接着 ②下载那条再干等 420s 等一个落地页根本不会产生的下载事件，等回来时候选预算
    （60s）早爆了，于是 ③「读 citation_pdf_url」这条真正有用的分支一次都轮不到。落地页没有 PDF 响应
    可截，唯一值得做的就是 ③。返回 (body, why)。
    """
    why = None
    try:
        resp = page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
        why = f"http_{resp.status}" if resp and resp.status >= 400 else None
    except Exception as e:
        _log(f"  导航失败 {type(e).__name__}", quiet)
        return None, "nav_failed"
    if _challenged(page):
        if not _settle(page, quiet, "导航"):
            return None, "cf_challenge"
        with contextlib.suppress(Exception):
            resp = page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
            why = f"http_{resp.status}" if resp and resp.status >= 400 else why
    # 跳转壳要等它跳完才有正文页：linkinghub 的 meta refresh 是 2s。最多等 15s，跳不动就是跳不动。
    meta = None
    for _ in range(15):
        with contextlib.suppress(Exception):
            m = re.search(r'citation_pdf_url"\s+content="([^"]+)"', page.content(), re.I)
            if m:
                meta = m.group(1)
                break
        page.wait_for_timeout(1000)
    if not meta:
        # cell.com 那种新版落地页不写 citation_pdf_url，PDF 链接就摆在页面里（/action/showPdf?pii=、
        # /pdfft?、xxx.pdf）。顺手把找到的链接打进日志 —— 一个都没有就说明这一页压根没有下载入口。
        try:
            html = page.content()
        except Exception:
            html = ""
        hrefs = re.findall(r'href="([^"]*(?:showpdf|/pdfft|\.pdf)[^"]*)"', html, re.I)
        _log(f"  落地页没有 citation_pdf_url（停在 {page.url[:80]}）；页面里的 PDF 链接 "
             f"{len(hrefs)} 个: {[urljoin(page.url, h)[:90] for h in hrefs[:3]]}", quiet)
        if not hrefs:
            return None, why or "no_meta"
        meta = urljoin(page.url, hrefs[0])
    _log(f"  落地页 meta: {meta[:90]}", quiet)
    try:
        body = _b64(page.evaluate(JS_FETCH, meta))
    except Exception as e:
        _log(f"  页面内 fetch 失败: {str(e)[:60]}", quiet)
        return None, why or "meta_fetch_failed"
    if body:
        _log(f"  页面内 fetch → {len(body)}B", quiet)
        return body, None
    return None, why or "meta_not_pdf"


def fetch_with_browser(blocked: list[dict], out: Path | None, title: str = "",
                       engine: str = "edge", quiet: bool = False) -> dict:
    """blocked: lit_fetch 传进来的 [{'kind','url'}]。返回 ok/url/bytes/sha256/path/verdict/..."""
    try:
        from patchright.sync_api import sync_playwright
    except ImportError:
        return {"ok": False, "error": "patchright 未安装（uv pip install patchright）"}
    import lit_fetch as lf

    urls = [b["url"] for b in blocked if b.get("url")]
    if not urls:
        return {"ok": False, "error": "没有被墙候选可试"}

    kwargs = {} if engine == "chromium" else {"channel": "msedge" if engine == "edge" else "chrome"}
    result: dict = {"ok": False}
    rejected: dict | None = None            # 拿到了但标题不对 → 别让它被后面的「被墙」盖掉
    with sync_playwright() as pw:
        try:
            ctx = _launch(pw, kwargs)       # 一律带「PDF 直接下载」偏好（预印本就吃这条）
        except ProfileBusy as e:
            _log(f"  {e}", quiet)
            return {"ok": False, "error": str(e), "blocked_kind": "profile_busy", "needs_human": True}
        # ponytail: 整轮只用启动时那一个空白页，不再额外留 keep-alive 页、也不再每个候选开新标签页
        # ——用户看到的就是「多开两个空白页」。它被下载流程关掉了才补一个，补不上就收工（重开浏览器
        # 等于又弹一次窗口，那正是之前「反复开关」的来源）。
        page = _first_page(ctx)
        try:
            for url in urls[:4]:
                if page is None or page.is_closed():
                    try:
                        page = ctx.new_page()
                    except Exception as e:
                        _log(f"  浏览器上下文没了（{type(e).__name__}）—— 不再重开，就此收工", quiet)
                        break
                try:
                    _log(f"试 {url[:100]}", quiet)
                    t_cand = time.time()
                    if _download_first(url):
                        # 特例：bioRxiv / medRxiv —— 只走「浏览器直接下载」。实测它俩对任何**二次**
                        # 请求都 403（裸 HTTP、代理、route.fetch、带导航头重取），导航本身也只是
                        # 阅读器占位文档；唯一成过的一条（36,880,144B）就是让浏览器把 PDF 当下载交
                        # 出来 —— 下载不产生第二次请求。所以别的路一概不试。
                        body, why = _download_via_nav(page, url, quiet)
                        if body:
                            result = _finish(body, url, out, title, lf)
                            if result.get("ok"):
                                break
                            if result.get("verdict") == "title_absent":
                                rejected = result
                        else:
                            # 卡在人类验证就如实报：窗口留着（needs_human），插件里显示「需要真人点」
                            result = {"ok": False, "url": url,
                                      "blocked_kind": why or "download_failed",
                                      "needs_human": why == "cf_challenge"}
                        continue
                    if not lf._looks_like_pdf(url):
                        # 落地页候选（layer1 空手时喂进来的）：①② 都会白等（① 甚至把导航吞成
                        # 204 空白页），只有 ③ 读 citation_pdf_url 对它有指望。理由见 _landing_grab。
                        body, why = _landing_grab(page, url, quiet)
                        if body:
                            result = _finish(body, page.url, out, title, lf)
                            if result.get("ok"):
                                break
                            if result.get("verdict") == "title_absent":
                                rejected = result
                        else:
                            result = {"ok": False, "url": page.url,
                                      "blocked_kind": why or "no_pdf",
                                      "needs_human": why == "cf_challenge"}
                        continue
                    else:
                        # ① route.fetch()：最干净的一条（不改偏好、不二次请求、页面不死）
                        body, why = _route_grab(page, url, quiet)
                        if body:
                            result = _finish(body, url, out, title, lf)
                            if result.get("ok"):
                                break
                            if result.get("verdict") == "title_absent":
                                rejected = result
                            continue
                        if why in ("http_403", "http_429"):
                            # ponytail: 浏览器侧 ① 被拒**不代表** ② 也没戏 —— bioRxiv 那次 37MB 就是
                            # ② 下载拿到的，而 ① 是 403。所以这里不早退，只记下原因，靠单候选时
                            # 间预算兜住总时长。
                            _log(f"  route.fetch 被拒（{why}），继续试下载那条", quiet)
                            result = {"ok": False, "url": url, "blocked_kind": why,
                                      "needs_human": False}
                        # ② 让浏览器把 PDF 当下载交出来（profile 已设 always_open_pdf_externally）
                        if time.time() - t_cand < CAND_BUDGET_S:
                            body, why = _download_via_nav(page, url, quiet)
                            if body:
                                result = _finish(body, url, out, title, lf)
                                if result.get("ok"):
                                    break
                                if result.get("verdict") == "title_absent":
                                    rejected = result
                                continue
                            if why == "cf_challenge":
                                result = {"ok": False, "url": url, "blocked_kind": why,
                                          "needs_human": True}
                                continue
                        else:
                            _log(f"  候选预算 {CAND_BUDGET_S}s 用完 → 换下一个", quiet)
                    # ③ 没有下载事件 → 当页面处理：先看是不是挑战，再看响应头
                    if time.time() - t_cand >= CAND_BUDGET_S:
                        _log(f"  候选预算 {CAND_BUDGET_S}s 用完 → 换下一个", quiet)
                        continue
                    try:
                        resp = page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
                    except Exception as e:
                        _log(f"  导航失败 {type(e).__name__}", quiet)
                        continue
                    if _challenged(page):
                        if not _settle(page, quiet, "导航"):
                            result = {"ok": False, "blocked_kind": "cf_challenge", "url": url,
                                      "needs_human": True}
                            continue
                        body, why = _download_via_nav(page, url, quiet)  # 挑战过了再试下载
                        if body:
                            result = _finish(body, url, out, title, lf)
                            if result.get("ok"):
                                break
                            if result.get("verdict") == "title_absent":
                                rejected = result
                            continue
                        if why == "cf_challenge":
                            result = {"ok": False, "url": url, "blocked_kind": why,
                                      "needs_human": True}
                            continue
                        try:
                            resp = page.goto(url, wait_until="domcontentloaded",
                                             timeout=NAV_TIMEOUT_MS)
                        except Exception:
                            continue
                    status = resp.status if resp else 0
                    if status in (403, 429):
                        # ponytail: 站点直截了当地拒绝（页面上没有可解的挑战）。这类没有
                        # 「点一下就好了」的可能，只有等或者换网络 —— 说清楚，别让人以为没试。
                        _log(f"  站点直接拒绝（HTTP {status}），页面上没有可解的挑战 —— 这是"
                             f"来源/频率层面的挡，不是浏览器能绕的", quiet)
                        result = {"ok": False, "url": url, "blocked_kind": f"http_{status}",
                                  "needs_human": False}
                        continue
                    ctype = (resp.headers.get("content-type") or "") if resp else ""
                    if "pdf" in ctype.lower():
                        # 先试「这次导航响应自己的本体」。bioRxiv 实测：导航能拿 PDF，但任何**二次**
                        # 请求（裸 HTTP / 代理 / route.fetch / 带导航头重取）一律 403，所以能用的只有
                        # 导航这一次的字节。Chromium 里 PDF 交给阅读器时 resp.body() 常常取不到，
                        # 取不到再退回「带 token 终址」。
                        body = None
                        try:
                            raw = resp.body()
                            if raw[:4] == b"%PDF" and len(raw) > 2048:
                                _log(f"  导航响应本体 → {len(raw)}B", quiet)
                                body = raw
                            else:
                                _log(f"  导航响应本体不是 PDF（{len(raw)}B）→ 退回终址", quiet)
                        except Exception as e:
                            _log(f"  导航响应本体取不到（{type(e).__name__}）→ 退回终址", quiet)
                        body = body or _bytes_from_nav(page, resp.url, url, quiet)
                        if body:
                            result = _finish(body, resp.url, out, title, lf)
                            if result.get("ok"):
                                break
                        time.sleep(5)       # 二次请求可能撞 429 → 歇一下原址再试
                        body = _bytes_from_nav(page, url, url, quiet)
                        if body:
                            result = _finish(body, url, out, title, lf)
                            if result.get("ok"):
                                break
                    else:
                        # 落地页：找 citation_pdf_url，再用页面内 fetch 取
                        import re
                        m = re.search(r'citation_pdf_url"\s+content="([^"]+)"',
                                      page.content(), re.I)
                        if m:
                            _log(f"  落地页 meta: {m.group(1)[:90]}", quiet)
                            try:
                                js = page.evaluate(JS_FETCH, m.group(1))
                                body = _b64(js)
                                if body:
                                    result = _finish(body, m.group(1), out, title, lf)
                                    if result.get("ok"):
                                        break
                            except Exception as e:
                                _log(f"  页面内 fetch 失败: {str(e)[:60]}", quiet)
                        result = {"ok": False, "url": page.url,
                                  "blocked_kind": f"http_{resp.status}" if resp else "unknown"}
                finally:
                    # ponytail: 不再关这个标签页 —— 整轮共用启动时那一个；关掉它浏览器可能整只退出，
                    # 而且「一开一关」本身就是多余的动静。
                    pass
        finally:
            # ponytail: 一轮结束就把浏览器关掉（以前不关，靠"脚本退出顺带带走"—— 父进程被 kill 或异常
            # 退出时 Edge 会残留，那份 profile 就被占住，下一次 launch 直接被 Chromium 拒掉/假死）。
            # 唯一的例外是「挑战需要真人点」：窗口留着，点完 cf_clearance 会落进 profile，下次跑就能用。
            if not result.get("needs_human"):
                with contextlib.suppress(Exception):
                    ctx.close()
    return result if result.get("ok") else (rejected or result)


def _b64(js: dict) -> bytes | None:
    import base64
    if js.get("status") != 200:
        return None
    body = base64.b64decode(js.get("b64") or "")
    return body if body[:4] == b"%PDF" and len(body) > 2048 else None


def _bytes_from_nav(page, final_url: str, orig_url: str, quiet: bool) -> bytes | None:
    """导航进了 Chromium 阅读器：resp.body() 只有阅读器文档，但带 token 的终址能再取一次。

    ponytail: 二次请求要装成「导航」—— 只带 Referer 时 Cloudflare/akamai 那类规则会按「非导航
    请求」直接 403（实测 bioRxiv 的终址就是这样，同一个 URL 导航能拿 PDF，带头重取就 403）。
    Sec-Fetch-* 三个头就是浏览器导航时自己会发的。
    """
    nav_headers = {
        "Referer": orig_url,
        "Accept": "application/pdf,text/html;q=0.9,*/*;q=0.8",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "same-origin",
        "Sec-Fetch-User": "?1",
        "Upgrade-Insecure-Requests": "1",
    }
    for src in dict.fromkeys([final_url, orig_url]):
        try:
            r = page.context.request.get(src, timeout=NAV_TIMEOUT_MS, headers=nav_headers)
        except Exception as e:
            _log(f"  终址取字节失败 {type(e).__name__}", quiet)
            continue
        body = r.body()
        if r.status == 200 and body[:4] == b"%PDF":
            _log(f"  终址取字节 → HTTP 200 {len(body)}B", quiet)
            return body
        _log(f"  终址取字节 → HTTP {r.status} {len(body)}B（不是 PDF）", quiet)
    return None


def _finish(body: bytes, url: str, out: Path | None, title: str, lf) -> dict:
    verdict = detail = None
    if title:
        verdict, detail = lf.title_verdict(body, title)
        lf.say(f"  [verify] {verdict}: {detail}")     # 跟 layer1 一个格式，也一并进插件
        if verdict == "title_absent":
            return {"ok": False, "url": url, "verdict": verdict, "verify_detail": detail,
                    "blocked_kind": "title_absent"}
    res = {"ok": True, "url": url, "bytes": len(body), "sha256": hashlib.sha256(body).hexdigest(),
           "verdict": verdict, "verify_detail": detail, "path": None}
    if out:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(body)
        res["path"] = str(out)
    return res
