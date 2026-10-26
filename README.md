# lit-fetch

给 **DOI / arXiv / PMID** 拿全文 PDF 的命令行工具，也是 Zotero 插件
[zotero-litfetch](https://github.com/pengtb/zotero-litfetch) 的后端。

两层设计：先走纯 HTTP 的开放获取路线（便宜、快），拿不到再开真浏览器兜底（Cloudflare 挑战、
跳转壳、只认导航的 PDF 端点）。**不绕付费墙**：不做机构代理、不含 Sci-Hub；只有在有机构 IP
（校网/VPN）时，才对「Unpaywall 说 closed」的文献用出版商自己的正文页试一次。

## 功能

- **layer1（纯 HTTP）**：arXiv、Unpaywall 的全部 OA 位置、Semantic Scholar、Europe PMC、
  PubMed free 标记、出版商直链、落地页 `citation_pdf_url`、MDPI CDN、bioRxiv/medRxiv 直链。
- **layer2（真浏览器）**：用系统已装的 Edge（`channel=msedge`，不下载浏览器构建），处理
  Cloudflare 挑战、跳转壳、带 token 的终址、只认导航的 PDF 端点、以及「让浏览器把 PDF 当下载交出来」。
- **内容校验**：`%PDF` magic + 体积下限；给了 `--title` 再算标题命中率（前 6 页），文件名对不上就拒收。
- **代理**：`http://` / `https://` / `socks5://`。默认只让「付费刊那次探测」走，`--proxy-all` 才整条链路走。
- **稳定契约**：`--json` 输出一行 envelope，诊断全在 stderr；退出码 `0/1/2/7`；另有 stdio
  JSON-RPC 供插件调用。

## 安装

需要 Python 3.11+。

```bash
uv venv --python 3.11 .venv
uv pip install --python .venv/Scripts/python.exe -r requirements.txt
```

- `requirements.txt`：`requests[socks]`（带 PySocks，socks5 两层可用）、`pymupdf`、`patchright`。
- layer2 用系统已装的 Edge（或 Chrome / Chromium），不需要下载任何浏览器构建。
- 想用代理/邮箱而不改命令行参数，就把项目根目录的 `.env.example` 抄成 `.env` 再改。

## 用法

```bash
python lit_fetch.py 2610.04475 out/a.pdf                    # arXiv
python lit_fetch.py 10.1038/s41586-020-2649-2 out/b.pdf     # DOI
python lit_fetch.py 10.1101/2025.05.31.656838 out/c.pdf --no-browser   # 只走 layer1
python lit_fetch.py arXiv:2610.04475 out/a.pdf --title "文章标题" --email you@x.edu
python lit_fetch.py 10.1186/s12984-023-01168-x out/d.pdf --proxy http://127.0.0.1:7890
python lit_fetch.py rpc            # stdio JSON-RPC 2.0（给 Zotero 插件用）
python lit_fetch.py --selfcheck    # 离线自检
```

支持：DOI、`arXiv:xxxx` / 裸 arXiv id、bioRxiv/medRxiv DOI、PubMed ID。
引擎：`--engine edge|chrome|chromium`（默认 edge）。

### 三个开关

| 开关 | CLI | `.env` | RPC | 默认 | 作用 |
|---|---|---|---|---|---|
| layer1 | `--no-layer1` | `LITFETCH_LAYER1=0` | `layer1` | on | 关掉＝不跑任何 HTTP 阶梯，只把 `doi.org` 入口留给浏览器 |
| layer2 | `--no-layer2`（别名 `--no-browser`） | `LITFETCH_LAYER2=0` | `layer2` | on | 关掉＝不开浏览器 |
| paywall | `--no-paywall` | `LITFETCH_PAYWALL=0` | `paywall` | on | 关掉＝付费刊那一次出版商正文页也不试，只做开放获取 |

三个开关和当前代理都会写进 envelope（`layer1` / `layer2` / `paywall` / `proxy` / `proxy_all`）。

### 代理

```bash
python lit_fetch.py <id> out.pdf --proxy http://127.0.0.1:7890
python lit_fetch.py <id> out.pdf --proxy socks5://127.0.0.1:1080
python lit_fetch.py <id> out.pdf --proxy http://127.0.0.1:7890 --proxy-all
```

- 默认**只让「付费刊那次探测」走代理**：Unpaywall 说 closed、或**拿不到判定**（没填邮箱 /
  接口失败）时，出版商的直链和落地页（含浏览器）才带代理；确定是 OA 的路线、元数据 API、预印本全直连。
- `--proxy-all` / `LITFETCH_PROXY_ALL=1`：整条链路都走。
- 不写 scheme 按 `http://` 算；端口必须写。`http://u:p@host:port` 支持账号密码。

## 配置（`.env`）

复制 `.env.example` 为 `.env`；环境变量优先于文件。值只认字面量（不展开 `%USERPROFILE%`）。

| 变量 | 说明 |
|---|---|
| `LITFETCH_EMAIL` | Unpaywall/Crossref 要求的联系邮箱；不填会跳过 Unpaywall 那条路线 |
| `LITFETCH_PROFILE` | 浏览器 profile 目录（默认 `~/.litfetch_profile`，存着各站点的 `cf_clearance`） |
| `LITFETCH_FIXTURES` | `realcheck.py` 的标题夹具（可选） |
| `LITFETCH_PROXY` | 代理地址，见上节 |
| `LITFETCH_PROXY_ALL` | `1` = 整条链路都走代理（默认只付费刊探测走） |
| `LITFETCH_LAYER1` / `LITFETCH_LAYER2` / `LITFETCH_PAYWALL` | 三个开关，`0` 关 |

## 输出契约

`--json` 时 stdout 只有一行 envelope（诊断全在 stderr）：

```
schema · input · kind · value · ok · route · url · tried[] · blocked_by[{kind,url}] ·
verdict(match|title_absent|no_text|unreadable) · verify_detail · bytes · sha256 · path ·
free_at_publisher · is_oa · oa_status · paywall · paywalled · proxy · proxy_all ·
layer1 · layer2 · elapsed_s
```

退出码：`0` 拿到 PDF · `1` 用法错 · `2` 所有路线空手 · `7` 被 bot 墙挡住（不是论文的问题）。

RPC（一行一个 JSON，stdout 只出协议帧）：

```json
→ {"jsonrpc":"2.0","id":1,"method":"fetch","params":{"identifier":"10.1038/x","out":"C:/a.pdf"}}
← {"jsonrpc":"2.0","method":"progress","params":{"stage":"layer1","identifier":"10.1038/x"}}
← {"jsonrpc":"2.0","id":1,"result":{...envelope..., "exit_code":0}}
```

方法：`ping` · `fetch` · `shutdown`。`fetch` 可选参数：`identifier` · `out` · `title` · `email` ·
`engine` · `layer1` · `layer2`（老名字 `allow_browser`）· `paywall` · `proxy` · `proxy_all`。

## 致谢

- [paper-fetch](https://github.com/drpwchen/paper-fetch)（MIT，© drpwchen）—— 路线阶梯的思路、
  `%PDF`/标题校验方法、MDPI CDN、「导航 + 带 token 终址」这些取字节套路都参考了它。本项目只保留了
  开放获取 + bot 墙兜底，砍掉了它的 TDM API / 机构代理 / ClinicalKey 那一整套。
- [zotero-pdf2zh](https://github.com/guaguastandup/zotero-pdf2zh) —— 「Zotero 插件 + 本地 Python
  后端」的形态参考；本项目的后端用 stdio JSON-RPC，没有常驻 HTTP 服务。
- [Zotero](https://www.zotero.org/) —— 插件宿主与相关文档。

## 许可证

[MIT](LICENSE)，© 2026 Peng Tianbo。

配套插件：[zotero-litfetch](https://github.com/pengtb/zotero-litfetch)。
工程细节（路线实现、实测样本、踩过的坑）在 [AGENTS.md](AGENTS.md)。
