# AGENTS.md — lit-fetch 工程说明

面向改代码的 agent/人。用户文档看 [README.md](README.md)；这里只留实现、实测和踩过的坑。

## 定位与红线

- 两层：layer1 纯 HTTP（开放获取路线阶梯），layer2 真浏览器（只兜底 bot 墙/跳转壳/只认导航的端点）。
- **不绕付费墙**：不做机构代理、不接 TDM API、不含 Sci-Hub。机构 IP（校网/VPN）在时，只对
  「非 OA 或判断不了 OA」的文献，用出版商自己的正文页试一次（`paywall` 开关）。
- 代理默认只作用于「付费刊那次探测」（含**判断不了 OA** 的情况），确定 OA 的路线保持直连；
  `--proxy-all` 才是整条链路全走。
- 拿不到就报「付费墙」（exit 2）或「被墙」（exit 7），不要用「通常/应该」糊过去。

## 文件

    lit_fetch.py    标识符归一化 · 路线阶梯 · 校验 · CLI
    browser.py      layer2：Edge + 挑战判定（自动/需真人）+ 三条取字节路
    rpc_server.py   stdio JSON-RPC 2.0（插件用；按 mtime 热重载 lit_fetch/browser）
    selfcheck.py    离线断言（含代理 20 例）
    selfcheck_layer2.py  layer2 的 route.fetch 验收（控制用 dummy.pdf）
    realcheck.py    联网跑已知用例（夹具见 LITFETCH_FIXTURES）
    probe_tabs.py   probe_rpc/浏览器调试用的小脚本
    requirements.txt  依赖（requests[socks] / pymupdf / patchright）
    PLAN.zh.md      设计计划与调研结论（历史文档，结论已并进本文件）

## 装与跑

    uv venv --python 3.11 .venv
    uv pip install --python .venv/Scripts/python.exe -r requirements.txt
    python lit_fetch.py --selfcheck
    python realcheck.py                 # 联网
    python selfcheck_layer2.py          # 会开一次浏览器

layer2 用系统已装的 Edge（`channel=msedge`），不下载浏览器构建。

## layer1 路线与排序

1. arXiv 直链；预印本（bioRxiv/medRxiv）自己的直链。
2. 期刊 DOI 走 OA 阶梯：出版商直链 → Cell Press `showPdf`（`paywall` 开着才问 Crossref）→
   Unpaywall 全部 location → Semantic Scholar → Europe PMC → MDPI CDN。
3. `doi.org` 只当落地页入口（解析 302 拿终址），排在直链之后、用短超时（实测 10–27s）。
4. 候选带版本 rank：0=出版商 publishedVersion，2=仓库，3=预印本。`_prefer_publisher()` 在
   出版商那版是 OA 时把低版本推后；`_defer_low_versions()` 分出 `late_p/late_l`，等出版商那路
   （含浏览器）失败后才回退。
5. 预印本 DOI **不走** unpaywall/S2/Europe PMC 阶梯：bioRxiv 反正只有浏览器下载那条成，
   多问只会多撞 CF；medRxiv 自己的两条直链在 layer1 就能 200。

## layer2 取字节的三条路（按顺序）

0. **bioRxiv / medRxiv 特例**：只有「浏览器直接下载」这一条。这两个站对任何二次请求
   （裸 HTTP、经代理、`route.fetch()`、带 `Sec-Fetch-*` 重取）都 403；导航拿到的只是 Chromium
   阅读器占位文档（348B）。唯一成过的一次是让浏览器把 PDF 当下载交出来（36,880,144B / 29 页）——
   下载不产生第二次请求。`lit_fetch.py` 只把 `biorxiv.org` 标成 browser_first。
1. **`route.fetch()`（首选）**：在渲染器拿到 PDF 前截响应体，拿到 `%PDF` 就 `fulfill(204)`。
   两个坑：
   - **不是 PDF 时要把真实响应还回页面**（`route.fulfill(response=resp)`），不能一律 204 ——
     否则 CF 挑战页没机会渲染、挑战永远不解，用户看到白屏 + 静默等满 420s。
   - 收尾 `page.unroute(pred, handler)` 必须传**同一个谓词对象和同一个 handler**；新写个 lambda
     Playwright 匹配不上，路由会一直留着，吞掉同 URL 的所有后续导航。selfcheck 有恒等断言。
2. **浏览器直接下载**：profile 里写 `plugins.always_open_pdf_externally=true`，让 Edge 把 PDF
   当下载交出来。bioRxiv 那次成功靠它；代价是偶尔会让整个 Edge 上下文退出，所以只当备用。
3. **真导航 + 带 token 终址**：响应头是 `application/pdf` 时，取 302 后的终址再用浏览器上下文
   取一次（OUP/silverchair 那条：1.71MB）。会撞 429 的站点（bioRxiv）在这条上必然失败。

**落地页候选**不走上面三条，走 `_landing_grab()`：导航 → 等跳完（meta refresh / JS）→ 读
`citation_pdf_url` → 页内 fetch。页内 fetch 被拒（`TypeError: Failed to fetch`）不等于拿不到：
出版商的 PDF 端点常常只认导航，所以失败后退到 ②「让浏览器当下载交出来」。

## Cloudflare 挑战：第一次遇到某站点

症状：自动化窗口弹「人类验证」，等满 60s 也过不去；同一个站在日常浏览器里正常。

原因（实测）：**不是指纹问题**（`navigator.webdriver=false`、真 Edge UA、plugins/WebGL 全过），
缺的是这份 profile 里没有该站的 `cf_clearance`。这个 cookie 绑 IP + UA，拿到后能留很久。

解法：用**同一份** profile、**不带任何自动化参数**手动开一次过挑战：

    "%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe" --user-data-dir="%USERPROFILE%\.litfetch_profile" <URL>

三个坑：手动那次不能带 `--remote-debugging-pipe` / `--disable-blink-features=AutomationControlled`
之类参数（patchright 自己加的也算）；手动与自动化必须同一个 `--engine`（UA 要一致）；profile 被
占用时不要跑自动化（`_profile_holder()` 会查，报 `blocked_kind=profile_busy` + `needs_human`，
而不是偷偷换 profile 把 clearance 丢掉）。

## 代理实现

- `PROXY`（`.env`/环境变量默认）、`_ACTIVE_PROXY`（本次生效）、`_ACTIVE_PROXY_ALL`（是否全走）、
  `_PROXY_FORCE`（fetch 内部临时开：付费刊探测段）。
- `_proxies()`：`_ACTIVE_PROXY_ALL or _PROXY_FORCE` 时返回 `{"http": p, "https": p}`，否则空 dict
  （保持 requests 默认：环境变量 / Windows 系统代理）。
- `_is_paywall_probe()`：`is_oa is not True and not free_at_publisher and paywall` —— Unpaywall 说
  closed，或**拿不到判定**（没邮箱 / 接口失败 → `is_oa=None`）都算；只有确定 OA 才全程直连。
  layer2 只在 `_ACTIVE_PROXY_ALL or paywall_case` 时把 proxy 传给 Playwright。
- socks：requests 需要 PySocks（`requirements.txt` 已带）；没装时默认范围下 layer1 探测降级直连、
  `--proxy-all` 直接报用法错误。浏览器那层 Chromium 原生支持 socks5。
- client 侧不要把密码写进日志：envelope 里的 `proxy` 走 `_proxy_display()` 脱敏成 `***@host`。

## 校验与契约

- `%PDF` magic + 体积下限；`--title` 时算前 6 页最长连续命中率，≥0.85 判 match，`title_absent` 拒收。
- envelope 字段与退出码见 README；RPC 的 `fetch` 参数也见 README。RPC 进程常驻，按 `__file__`
  的 mtime 热重载 `lit_fetch` / `browser`（改代码不用重启 Zotero）。
- 退出码：`0` 成功 · `1` 用法错 · `2` 全路线空手 · `7` 被 bot 墙挡住。

## 实测样本（供回归对照）

| 输入 | 结果 |
|---|---|
| `2610.04475`（arXiv） | exit 0 · route=arxiv · 1.96MB · 0.9s |
| `10.1371/journal.pcbi.1011787`（PLOS） | exit 0 · landing_meta · 3.16MB · match |
| `10.1186/s12984-023-01168-x`（BMC） | exit 0 · unpaywall · 880KB · 18s |
| `10.3390/ijms25179306`（MDPI） | exit 0 · mdpi_cdn · 3.69MB · 19s |
| `10.1038/s41586-020-2649-2`（Nature） | exit 0 · unpaywall · 1.22MB · 17s |
| `10.1016/j.cell.2026.09.017`（Cell，closed，校网 VPN） | exit 0 · browser:edge · 11,110,651B / 36 页 · 标题 100% |
| `10.1093/bib/bbag439`（OUP） | exit 0 · browser:edge · 2,690,419B / 25 页 · 标题 100% |
| `10.1101/2025.05.31.656838`（bioRxiv，喂过 clearance 后） | exit 0 · browser:edge · 36,880,144B · 87.6s |

Cell Press 那条：`https://www.cell.com/action/showPdf?pii=<PII>` 是免 token 的正文入口；
PII 不在 DOI 里，用 Crossref 的 `alternative-id`（`S0092867426011190` → `S0092-8674(26)01119-0`）。
必须判成直链（`_looks_like_pdf` 认 `showpdf`），喂给 `_landing_grab` 只会得到一个 `goto` 异常。

## 环境坑

- **profile 占用**：Chromium 不接受同一 `user-data-dir` 起第二个实例；`browser.py` 启动前查占用，
  查到就报 `profile_busy`，不自动换目录（换目录＝丢 `cf_clearance`）。
- **doi.org 很慢**（10–27s），只做解析 302，排在直链之后、短超时。
- **bioRxiv 会限速**：同一站连打三次，本来能过的站点会变成过不去；按 host 冷却 + 串行。
- **Windows 低完整性环境**：从带 Low 完整性标签的目录里跑 `python.exe`，进程会是 Low，写不了
  家目录/Zotero 数据目录（`PermissionError`）。修复：`icacls <path> /setintegritylevel M`。
- **`--title` 才做内容校验**；不给标题只看 `%PDF` + 体积下限。
