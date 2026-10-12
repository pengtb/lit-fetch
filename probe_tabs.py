"""探针：用我们那个 Edge profile 启动一次，看**启动后有几个页签**（会话恢复有没有把旧页签带回来）。
用法: python probe_tabs.py [--fix]    --fix 先按 browser._force_pdf_download + 清会话写一遍再启
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import browser  # noqa: E402

FIX = "--fix" in sys.argv

if FIX:
    browser._force_pdf_download(browser.PROFILE_DIR)
    browser._clear_session(browser.PROFILE_DIR)
    print("已写 pref + 清掉会话残留")

from patchright.sync_api import sync_playwright  # noqa: E402

with sync_playwright() as pw:
    ctx = pw.chromium.launch_persistent_context(
        user_data_dir=str(browser.PROFILE_DIR), headless=True, channel="msedge",
        args=["--no-first-run", "--no-default-browser-check"])
    pages = ctx.pages
    print(f"启动后页签数：{len(pages)}")
    for i, p in enumerate(pages, 1):
        print(f"  {i}. {p.url}")
    ctx.close()
sys.exit(0 if len(pages) == 1 else 1)
