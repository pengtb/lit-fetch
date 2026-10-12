"""真跑一遍：layer1 对 6 个标识符的结果（联网）。用法: python realcheck.py [--no-browser]"""
import json
import os
import sys
from pathlib import Path

import lit_fetch as lf

EMAIL = os.environ.get("LITFETCH_EMAIL", "")          # Unpaywall 要一个真邮箱，别写进仓库
OUT = Path("out")
# 有跑过 paper-fetch 的话就用它的标题夹具来校验内容；没有就只验 %PDF
FIXTURES = Path(os.environ.get("LITFETCH_FIXTURES", "out/fixtures.json"))
titles = json.loads(FIXTURES.read_text(encoding="utf-8")) if FIXTURES.exists() else {}

CASES = [
    ("arXiv 新式 id", "2610.04475"),
    ("PLOS OA", "10.1371/journal.pcbi.1011787"),
    ("BMC OA", "10.1186/s12984-023-01168-x"),
    ("MDPI OA（CDN 绕行）", "10.3390/ijms25179306"),
    ("Nature（出版商自己给）", "10.1038/s41586-020-2649-2"),
    ("bioRxiv（预期被墙）", "10.1101/2025.05.31.656838"),
]

allow_browser = "--no-browser" not in sys.argv
OUT.mkdir(exist_ok=True)
rows = []
for label, ident in CASES:
    title = titles.get(ident, "")
    out = OUT / (ident.replace("/", "_").replace(".", "-") + ".pdf")
    env = lf.fetch(ident, out, title=title, email=EMAIL, allow_browser=allow_browser,
                   engine="edge", quiet=True)
    env["_label"] = label
    rows.append(env)
    print(f"{label:22} exit={lf.exit_code(env)} ok={env['ok']} route={str(env['route']):18} "
          f"bytes={env['bytes']:>9} verdict={env['verdict']} "
          f"blocked={[b['kind'] for b in env['blocked_by']]} {env['elapsed_s']}s")
    Path("out/last-run.json").write_text(json.dumps(rows, ensure_ascii=False, indent=1),
                                         encoding="utf-8")
ok = sum(1 for r in rows if r["ok"])
print(f"\nlayer1 成功 {ok}/{len(rows)}；明细见 out/last-run.json")
