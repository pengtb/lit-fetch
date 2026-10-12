# lit-fetch 计划（暂定名 / 自研两层抓取器 + Zotero 插件）

编写：2026-10-10　状态：调研完成，等拍板后开工
参考实现：`project/paper-fetch`（MIT，可抄代码，需保留 LICENSE 与出处）
全部"实测"结论都在本机、校园网直连（除注明代理）跑过；"自述"结论来自官方文档/README，已标注。

## 0. 调研结论（决定设计的事实）

### 0.1 目标源的端点与实测

| 源 | 元数据 | PDF 端点 | 实测 |
|---|---|---|---|
| arXiv | `export.arxiv.org/api/query?id_list=<id>`（Atom，含 title/published/期刊 DOI） | `arxiv.org/pdf/<id>` | 元数据 200；PDF **直连 200 `%PDF-`（271ms），不需要代理、任何 UA**。旧式 id（`cs.CL/0501001`）与带版本（`vN`）都要支持 |
| bioRxiv / medRxiv | `api.biorxiv.org/details/<server>/<from>/<to>`（JSON，含 doi/version/date） | `www.<server>.org/content/<doi>v<N>.full.pdf` | 元数据 200；**PDF 全线被挡**：HTTP 直连 403、经代理 403（13:0x 曾成功一次 200 application/pdf，14:1x 就变 403 —— 限速/Cloudflare）；浏览器导航 → medRxiv 200 application/pdf、bioRxiv 直接 cf 挑战；**字节捕获失败**：Playwright `resp.body()` 与 CDP `Network.getResponseBody` 都只给 348B 阅读器文档；导航后再 HTTP 取一次会 429 |
| DOI 泛用 | Crossref / Unpaywall / S2 / Europe PMC / NCBI | 见路线表 | 直连全通（paper-fetch 测试里 4 条成功路线用的就是这些） |

**DOI 前缀两个都要认**：bioRxiv/medRxiv 旧的是 `10.1101`，2026 年起 API 里真实出现 `10.64898`。

### 0.2 Firefox 能不能用（你问的第 3 条）

三选一，都有代价：

1. **camoufox**（推荐）：Firefox 内核的 anti-detect 浏览器，12,436★、最后提交 2026-10-08、MPL-2.0；
   Playwright 兼容 API，`persistent_context=True` 时直接走 `firefox.launch_persistent_context`；
   自带**打过补丁的 Firefox 构建**（`camoufox fetch` 下载，包与浏览器版本成对锁定）——自述。
2. Playwright 自带的 Firefox：官方文档写明不吃系统 Firefox（"relies on patches"），且没有 stealth 补丁，最弱。
3. Selenium + geckodriver + **复制你的真 Firefox profile**：能用你的 cookie（Firefox 的 `cookies.sqlite` 明文可读），
   但 `navigator.webdriver` 这类自动化特征明显，是被墙站点的常见拦截点。

结论：**系统那个 Firefox 谁也用不了**；要 Firefox 内核就用 camoufox。建议 layer2 引擎做成可换
（`--engine camoufox|edge|chromium`），默认 camoufox，Edge 作为今天已验证可用的备选。

### 0.3 不做付费那一套（你的第 4 条）

直接删掉：出版商 TDM API（要 key）、机构代理/EZProxy/NetScaler、ClinicalKey、LWW/Ovid 签名 URL、馆藏权限表。
layer1 只走 OA，layer2 只处理 bot 墙。红线写进 README：不碰付费墙、不加 Sci-Hub、不绕订阅。

### 0.4 从 paper-fetch 抄什么（MIT）

抄：`%PDF` magic 校验、`--json` envelope + 退出码语义（0/1/2/7）、落地页 `citation_pdf_url` 路线、
MDPI 的 CDN 绕行、Unpaywall 全 location 遍历 + PMC→Europe PMC render、内容校验（标题命中率 + 整卷切页）、
真导航取字节的 `nav+终址` 套路。
不抄：机构/TDM/ClinicalKey/LWW/Ovid/馆藏表。
（今天实测证明有价值的两处：MDPI 的 CDN 让裸 requests 403 的站点拿到 3.6MB 正文；`nav+终址` 让 cf 墙后的 OUP
文章拿到 1.71MB 正文。）

### 0.5 Zotero 侧接口（从本机 Zotero 9.0.6 源码核实，不是记忆）

- 起进程：`Zotero.Utilities.Internal.subprocess(command, args)`（`utilities_internal.js:615`）
  —— **只读 stdout 并返回字符串，stderr 不读**。所以我们的 CLI：JSON 全在 stdout、stderr 保持小；
  要更细控制（读 stderr/杀进程）就在插件里直接 `ChromeUtils.importESModule("resource://gre/modules/Subprocess.sys.mjs")`。
- 挂附件：`Zotero.Attachments.importFromFile({file, parentItemID, title, contentType, ...})`（`attachments.js:58`）。
- 从条目取标识符：`Zotero.Utilities.extractIdentifiers(text)`（`utilities.js:383`，官方推荐用它，
  `Zotero.Utilities.Internal.extractIdentifiers` 已标 deprecated）。
- 参考形态：zotero-pdf2zh 是「插件 + 常驻 Python server（HTTP）」；我们不需要常驻服务，一次调用一次子进程就够。

## 1. 目标 / 非目标

目标：给一个标识符（arXiv id / bioRxiv-medRxiv DOI / 普通 DOI / PMID），拿到 PDF；两层结构；输出 JSON；
能被 Zotero 插件直接调用。
非目标：付费墙与订阅内容、Sci-Hub、批量镜像全库、元数据管理（Zotero 自己做）、常驻服务。

## 2. 架构

- 单包 CLI：`python lit_fetch.py <id> <out.pdf> [--json] [--title "..."] [--engine edge|camoufox] [--no-browser]`
- **layer1（HTTP，无浏览器）**：标识符归一化 → 路线阶梯 → 校验 → 落盘
  - 路线顺序：arXiv 直链 → Unpaywall 全部 `oa_locations` → S2 `openAccessPdf` → PMCID→Europe PMC render →
    PubMed free 旗标 → 落地页 `citation_pdf_url` → bioRxiv/medRxiv `.full.pdf`
  - 校验：magic bytes + 体积下限 +（给了 title 时）标题命中率
- **layer2（浏览器，引擎可换）**：仅当 layer1 报 `blocked_by`（bot 墙/403）或 `free_at_publisher` 时启动
  - 挑战检测沿用已验证的判定（自动 vs 需要真人点，后者的窗口是 headful、能喊人点）
  - 取字节：优先 `nav+终址`（已验证），bioRxiv 那条另议（见实验 E1）
- 统一产物：JSON envelope（`schema/ok/route/tried/bytes/sha256/path/blocked_by/verdict/elapsed_s`）
  + 退出码（`0` 成功 · `1` 用法 · `2` 全路线空手 · `7` 被墙）
- 缓存/限速：**按 host 冷却**（命中 cf/429 就退避，写进本地状态文件）+ 串行 + 已下过的按 sha256 跳过。
  今天吃过教训：同一站点连打三次就把可过的站点打成不可过。

## 3. 分阶段（每阶段留一个可跑的验收）

| 阶段 | 交付 | 验收（可跑） |
|---|---|---|
| P0 脚手架 | 仓库、CLI 骨架、envelope、退出码、`--json` | 无参 exit 1；`--selfcheck` 通过 |
| P1 layer1 | arXiv + OA 阶梯（四形态已实测有效） | 4 条已知 DOI 全 exit 0 且标题 match；一个 arXiv id 直下成功 |
| P2 预印本 | 元数据 API + bioRxiv/medRxiv PDF（含 E1 结论） | 真实 bioRxiv、medRxiv 各一篇拿到 PDF 且标题命中 |
| P3 layer2 | 引擎可换 + 挑战检测 + 取字节 | 对 walled 的 OA 页面拿到 PDF；被墙时退出 7 且原因可读 |
| P4 质量控制 | `--title` 内容校验 + 命名（citekey/DOI 安全化）+ 重复检测 | 标题命中率输出；错文件被拒 |
| P5 Zotero 插件 | 菜单项 + 设置面板（python 路径/引擎/超时）+ 子进程调用 + 挂附件 + 进度通知 | 选中条目 → 菜单 → 附件出现；失败给出 envelope 里的原因 |

## 4. 开工前要跑的 3 个实验（会改设计，所以先做）

- **E1 bioRxiv 取字节**（三选一，各跑一次）：① CDP `Fetch` domain 在 Response 阶段拦 `Fetch.getResponseBody`；
  ② 导航后等 5–10s 再用 `ctx.request.get(终址)`（避 429）；③ 不做自动化，只回链接让人自己点。
- **E2 camoufox 实测**：在 OUP 那种 cf 站跑一遍，和 Edge 对比通过率/稳定性/启动耗时；顺便验证持久 profile。
- **E3 冷却画像**：bioRxiv 与 OUP 的冷却到底多长（今天 13:0x 代理 200、14:1x 就 403/429），决定退避参数。

## 5. 待你拍板（都给了默认值）

1. 名字/位置：默认 `C:\Users\ptbia\project\lit-fetch`（与 lit-alert、paper-fetch 同级）。
2. layer2 默认引擎：默认 **camoufox**（要下它自带的 Firefox 构建，百 MB 级）；备选 Edge（今天已验证）。
3. 代理：默认不挂，需要时那一次命令前 `export`（沿用今天的方案）。
4. 要不要 PMID/PMC 那条线：默认要（便宜且已验证）。
5. Zotero 插件形态：默认「插件起 CLI 子进程 + 读 stdout JSON」，不做常驻 server。
6. 要不要把「人工点击验证」也算合法路径：默认算（headful 窗口 + 明确提示），但只在交互式挑战时喊人。

## 6. 证据分级

- 实测：0.1 表格里的状态码/时延/magic bytes、0.2 里 patchright+Edge 的表现、0.4 的两处数字、0.5 的 Zotero API 行号。
- 自述（未实机验证）：camoufox 的 stealth 效果与 `camoufox fetch` 的版本成对锁定、Playwright 文档里关于 branded Firefox 的说明、zotero-pdf2zh 的 server 形态。
