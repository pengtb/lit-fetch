# lit-fetch

两层文献 PDF 抓取器：**layer1 纯 HTTP** 走开放获取路线，**layer2 真浏览器** 只兜底「有源但被 bot 墙挡住」
的情况。只做开放获取——不碰付费墙、不做机构代理、不含 Sci-Hub。参考实现是 `paper-fetch`（MIT），
抄了它的路线思路与校验方法，砍掉了 TDM/机构/ClinicalKey 那一整套。

## 装

    uv venv --python 3.11 .venv
    uv pip install --python .venv/Scripts/python.exe requests pymupdf patchright
    # layer2 用系统已装的 Edge（channel=msedge），不需要下载任何浏览器构建

## 用

    python lit_fetch.py 2610.04475 out/a.pdf
    python lit_fetch.py arXiv:2610.04475 out/a.pdf --title "文章标题" --email you@x.edu
    python lit_fetch.py 10.1038/s41586-020-2649-2 out/b.pdf --json
    python lit_fetch.py 10.1101/2025.05.31.656838 out/c.pdf --no-browser   # 只走 layer1
    python lit_fetch.py rpc            # stdio JSON-RPC 2.0（给 Zotero 插件用）
    python lit_fetch.py --selfcheck    # 离线自检

引擎：`--engine edge|chrome|chromium`（默认 edge）。代理：默认不挂，需要时那一次命令前
`export https_proxy=... http_proxy=...`（requests 继承环境变量）。

## 配置（.env）

本机配置放项目根目录的 `.env`（`.gitignore` 里有它，不会进仓库；照 `.env.example` 抄一份即可）。
**环境变量优先于 `.env`**，想临时覆盖就 export，不用改文件。值只认字面量（不展开 `%USERPROFILE%`/`$HOME`）。

    LITFETCH_EMAIL=you@example.com          # Unpaywall/Crossref 要求带真邮箱，不填就跳过那条路线
    LITFETCH_PROFILE=C:\Users\you\.litfetch_profile   # 浏览器 profile 目录（默认 ~/.litfetch_profile）
    LITFETCH_FIXTURES=out\fixtures.json     # realcheck.py 的标题夹具（可选）

`LITFETCH_PROFILE` 里存着各站点的 `cf_clearance`，换目录等于把已过的 Cloudflare 挑战丢掉 —— 见下面那节。

## 契约

JSON envelope（`--json` 时 stdout 只有这一行，诊断全在 stderr）：

    schema · input · kind · value · ok · route · url · tried[] · blocked_by[{kind,url}] ·
    verdict(match|title_absent|no_text|unreadable) · verify_detail · bytes · sha256 · path ·
    free_at_publisher · elapsed_s

退出码：`0` 拿到 PDF · `1` 用法错 · `2` 所有路线空手 · `7` 被 bot 墙挡住（不是论文的问题）。

    # 只有 2 是关于论文的证据；7 意思是「等一会再试，或换条路」

RPC（一行一个 JSON，stdout 只出协议帧）：

    → {"jsonrpc":"2.0","id":1,"method":"fetch","params":{"identifier":"10.1038/x","out":"C:/a.pdf","title":"..."}}
    ← {"jsonrpc":"2.0","method":"progress","params":{"stage":"layer1","identifier":"10.1038/x"}}
    ← {"jsonrpc":"2.0","method":"progress","params":{"stage":"done","ok":true,"route":"unpaywall","exit_code":0}}
    ← {"jsonrpc":"2.0","id":1,"result":{...envelope..., "exit_code":0}}

方法：`ping` · `fetch` · `shutdown`。

## 今天实测（2026-10-10，校园网直连，无代理）

| 输入 | 结果 |
|---|---|
| `2610.04475`（arXiv） | exit 0 · route=arxiv · 1.96MB · **0.9s** |
| `10.1371/journal.pcbi.1011787`（PLOS） | exit 0 · route=landing_meta · 3.16MB · verdict=match |
| `10.1186/s12984-023-01168-x`（BMC） | exit 0 · route=unpaywall · 880KB · match · 18s |
| `10.3390/ijms25179306`（MDPI） | exit 0 · route=mdpi_cdn · 3.69MB · match · 19s |
| `10.1038/s41586-020-2649-2`（Nature） | exit 0 · route=unpaywall · 1.22MB · match · 17s |
| `10.1101/2024.01.30.24301983`（medRxiv） | exit 0 · route=unpaywall · 2.46MB · 14s |
| `10.1101/2025.05.31.656838`（bioRxiv） | exit 7 · 两种 PDF 形态都 403（见下） |

RPC 冒烟：ping/fetch/未知方法/shutdown 四帧全对，stdout 纯协议帧，arXiv 那篇 1.96MB 落盘。

## layer2 取字节的三条路（按顺序试）

0. **bioRxiv / medRxiv 特例：只有「浏览器直接下载」这一条**。这两个站点对任何**二次**请求
   （裸 HTTP、经代理、`route.fetch()`、带 `Sec-Fetch-*` 导航头重取）都 403，导航本身拿到的也只是
   Chromium 阅读器的占位文档（348B）；唯一成过的一次（36,880,144B）是让浏览器把 PDF 当下载交出来
   ——下载不产生第二次请求。名单外还有：Europe PMC 里这条预印本 `hasPDF=N`，它给的唯一 PDF 链接
   就是同一个 bioRxiv early URL（没有替代源）。
   `lit_fetch.py` 那边只有 `biorxiv.org` 会被标成 browser_first（medRxiv 在 layer1 有能成的路，
   不抢它的机会）；只要 layer2 拿到 medRxiv 的 URL，走的也是下载这条。

1. **`route.fetch()`**（首选）：用 Playwright 的路由在渲染器拿到 PDF 前把响应体截下来，
   再 `fulfill(204)`。不改 Edge 偏好、不二次请求、不碰内置阅读器，页面也不会死。
   实测：控制用的 dummy.pdf → 13,264B `%PDF-`，页面存活（`selfcheck_layer2.py` 就是这一条的验收）。
2. **浏览器直接下载**：profile 里写 `plugins.always_open_pdf_externally=true`，让 Edge 把 PDF 当下载交出来。
   这条正是 bioRxiv 那次成功拿到的 **36,880,144B**（29 页，标题 "The mGluR5 Agonist, CHPG, …"）——
   代价是它有时会让整个 Edge 上下文直接退出（控制 PDF 也能复现），所以现在只当备用。
3. **真导航 + 带 token 终址**：导航后响应头是 `application/pdf` 时，取 302 后的终址再用浏览器上下文
   取一次（OUP/silverchair 那条：1.71MB）。会撞 429 的站点（bioRxiv）在这条上必然失败。

配套的加固：每个候选一个新页面；上下文整个挂了就重启一次浏览器再继续；浏览器侧也吃到
`403/429` 就跳过该候选（否则单候选三种方法串起来要两分钟）。

## Cloudflare 挑战：第一次遇到某个站点时（实测流程）

症状：自动化那个窗口弹出「人类验证」，等满 60s 也过不去；同一个站在你日常浏览器里一切正常。

原因（2026-10-12 实测，不是猜的）：**不是指纹问题**。用本项目的启动参数开 Edge 跑 bot.sannysoft.com，
`navigator.webdriver=false`、UA 是真 Edge（`Edg/155`）、plugins/chrome/WebGL 全过 —— 干净。真正缺的是
这份 profile 里**没有该站的 `cf_clearance`**。这个 cookie 绑 IP + User-Agent，一旦拿到能在 profile 里留很久
（实测 bioRxiv 那条有效期到 2027-10-12；本机日常 Edge profile 里的 wiley/linux.do 等也都是 2027 年到期）。

解法：用**同一份** profile、**不带任何自动化参数**手动开一次，把挑战过掉就行：

    "%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe" --user-data-dir="%USERPROFILE%\.litfetch_profile" <文章 URL>

过完关掉窗口，之后 `lit_fetch.py` 跑同一站就不会再挑战了。

三个坑：

- 那一次必须**不带** `--remote-debugging-pipe` / `--disable-blink-features=AutomationControlled` 之类自动化参数，
  否则 CF 照旧给挑战（patchright 自己加的那套参数也算）。
- 手动那次和自动化必须同一个 `--engine`（都是 msedge）：`cf_clearance` 绑 UA，换成 chrome 立刻失效。
- **别在 profile 被占用时跑自动化**：Chromium 不接受同一个 `user-data-dir` 起第二个实例。
  `browser.py` 现在启动前会查占用（`_profile_holder()`），查到就直接报
  `blocked_kind=profile_busy` + `needs_human`，而不是把 profile 挪开偷偷换一个干净目录
  （那样等于把 clearance 扔掉、又变回挑战）。跑完也会显式关掉浏览器，不留残骸；
  只有「需要真人点挑战」那次例外，窗口会留着让你点。

实测结果：喂完 clearance 后跑 `10.1101/2025.05.31.656838`（bioRxiv，README 里原来唯一过不去的那篇）→
exit 0、`route=browser:edge`、**36,880,144B**（与 2025 年那次成功的大小一致）、标题命中 100%、87.6s，
走的就是「浏览器直接下载」那条。

## 已知的坑（都是实测踩出来的）

- **bioRxiv/medRxiv 现在会对本机 403/429**，而且是四种方法一起挡（裸 HTTP、经代理、浏览器下载、
  `route.fetch`）。这是限速/名声，不是方法问题 —— 15:15 那次成功就是证据。别连点：同一站点连打三次，
  本来能过的站点就变成过不去。（2026-10-12 复核：主因其实是「这份 profile 没有 cf_clearance」，
  见上一节 —— 喂过之后同一篇 36.9MB 一次过。）
- **bioRxiv 的两种 PDF 形态**：`/content/<doi>v1.full.pdf` 一律 403；能用的只有
  `/content/<server>/early/<YYYY>/<MM>/<DD>/<id>.full.pdf`（medRxiv 实测 200 2.46MB %PDF-）。
  early 形式靠 `api.biorxiv.org/details/<server>/<doi>` 给的那一版的日期拼出来，所以俩都要试。
- **doi.org 很慢**（实测 10–27s），所以它只做「解析 302 拿落地页」，且排在所有直链之后、用短超时。
  PLOS 全靠这条 meta 路救回来。
- 落地页与直链要分开处理：把 `https://doi.org/...` 当 PDF 直接抓，白等 27s 拿回一坨 HTML（改前 147s、
  改后 67s 的就是这里）。
- `--title` 才做内容校验（前 6 页最长连续命中率 ≥0.85 判 match）；不给标题就只看 `%PDF` + 体积下限。

## 文件

    lit_fetch.py    标识符归一化 · 路线阶梯 · 校验 · CLI
    browser.py      layer2：Edge + 挑战判定（自动/需真人）+ 导航终址取字节
    rpc_server.py   stdio JSON-RPC 2.0
    selfcheck.py    离线断言（标识符 11 例 / MDPI 2 例 / EPMC 3 例 / 退出码 4 例 / 直链判定 9 例）
    realcheck.py    联网跑那 6 个已知用例
    PLAN.zh.md      设计计划与调研结论

下一步：Zotero 插件（菜单项 + 设置面板 + 起 RPC 子进程 + `Zotero.Attachments.importFromFile` 挂附件）。
