"""layer2 的可跑自检：拿一个确定能下的 PDF（W3C 的 dummy.pdf）当「被墙候选」喂给 layer2，
验证 route.fetch 那条路能把字节取回来、文件落地、页面不死。需要 Edge + 联网。

用法: python selfcheck_layer2.py
"""
import sys
from pathlib import Path

import browser

DUMMY = "https://www.w3.org/WAI/ER/tests/xhtml/testfiles/resources/pdf/dummy.pdf"
OUT = Path("out/layer2-selfcheck.pdf")

if OUT.exists():
    OUT.unlink()
res = browser.fetch_with_browser([{"kind": "cf_challenge", "url": DUMMY}], OUT, title="", engine="edge",
                                 quiet=False)
print("layer2 返回:", {k: v for k, v in res.items() if k != "sha256"})
body = OUT.read_bytes() if OUT.exists() else b""
ok = bool(res.get("ok")) and body[:4] == b"%PDF" and len(body) > 2048
print(f"文件: {OUT} {len(body)}B magic={body[:5]!r}")
print("layer2 selfcheck:", "OK" if ok else "失败")
sys.exit(0 if ok else 1)
