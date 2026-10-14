"""stdio JSON-RPC 2.0 服务（给 Zotero 插件用，进程常驻 → 浏览器/驱动都是热的）。

协议：一行一个 JSON 对象（换行分隔），stdout 只出协议帧，日志一律走 stderr。
    → {"jsonrpc":"2.0","id":1,"method":"fetch","params":{"identifier":"10.1038/xxx","out":"C:/a.pdf","title":"..."}}
    ← {"jsonrpc":"2.0","method":"progress","params":{"stage":"layer1","identifier":"..."}}   （通知，无 id）
    ← {"jsonrpc":"2.0","method":"progress","params":{"stage":"note","identifier":"...","message":"  → 试 unpaywall: ..."}}
    ← {"jsonrpc":"2.0","id":1,"result":{...lit_fetch 的 envelope...}}

方法：ping · fetch · shutdown
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

VERSION = "0.1.2"
_LOADED_MTIME: dict[str, float] = {}


def _fresh(name: str):
    """按 mtime 重载工具自己的模块（lit_fetch / browser）。

    ponytail: 插件那个子进程是常驻的，import 一次之后就一直用内存里那份——改了代码还得重启 Zotero
    才生效（实测踩到三次）。按 __file__ 的 mtime 重载，把这个坑一次填掉。
    """
    import importlib
    mod = sys.modules.get(name)
    if mod is None:
        mod = importlib.import_module(name)
        try:
            _LOADED_MTIME[name] = os.path.getmtime(mod.__file__)
        except OSError:
            pass
        return mod
    try:
        mtime = os.path.getmtime(mod.__file__)
    except OSError:
        return mod
    if mtime > _LOADED_MTIME.get(name, 0.0):
        mod = importlib.reload(mod)
        _LOADED_MTIME[name] = mtime
        print(f"[rpc] {name}.py 变过 → 已重新加载", file=sys.stderr)
    return mod


def _send(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _note(stage: str, **kw) -> None:
    _send({"jsonrpc": "2.0", "method": "progress", "params": {"stage": stage, **kw}})


def _progress_hook(ident: str):
    """把 lit-fetch 的一行行诊断（fetch 的 note + browser 的 _log）包成 progress 帧。

    ponytail: 复用现有 progress 通道，不另开协议 —— 插件按 stage 分流就够了
    （start 启动 / layer1 起步 / note 正在试哪条路 / done 收尾）。
    """
    def hook(message: str) -> None:
        _note("note", identifier=ident, message=str(message)[:300])
    return hook


def _handle(req: dict) -> dict | None:
    rid, method = req.get("id"), req.get("method")
    params = req.get("params") or {}
    if method == "ping":
        return {"jsonrpc": "2.0", "id": rid, "result": {"ok": True, "version": VERSION}}
    if method in ("shutdown", "exit"):
        _send({"jsonrpc": "2.0", "id": rid, "result": {"ok": True}})
        raise SystemExit(0)
    if method != "fetch":
        return {"jsonrpc": "2.0", "id": rid,
                "error": {"code": -32601, "message": f"unknown method: {method}"}}
    ident = params.get("identifier") or params.get("id") or params.get("doi") or params.get("arxiv")
    if not ident:
        return {"jsonrpc": "2.0", "id": rid,
                "error": {"code": -32602, "message": "需要 identifier"}}
    lf = _fresh("lit_fetch")
    _fresh("browser")        # 也一并热起来，fetch 里 import browser 时拿到的是重载后的
    _note("start", identifier=ident)          # 启动（插件据此就能弹「启动中」）
    _note("layer1", identifier=ident)
    hook = _progress_hook(ident)
    env = lf.fetch(str(ident), Path(params["out"]) if params.get("out") else None,
                   title=params.get("title", ""), email=params.get("email", lf.CONTACT),
                   engine=params.get("engine", "edge"),
                   # layer2 是新的名字；allow_browser 是插件老版本发过来的名字，留着兼容
                   allow_browser=bool(params.get("layer2",
                                                 params.get("allow_browser", lf.LAYER2_DEFAULT))),
                   layer1=bool(params.get("layer1", lf.LAYER1_DEFAULT)),
                   quiet=True,
                   paywall=bool(params.get("paywall", lf.TRY_PAYWALL_DEFAULT)),
                   on_progress=hook)
    env["exit_code"] = lf.exit_code(env)
    _note("done", identifier=ident, ok=env["ok"], route=env.get("route"), exit_code=env["exit_code"])
    return {"jsonrpc": "2.0", "id": rid, "result": env}


def serve() -> int:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError as e:
            _send({"jsonrpc": "2.0", "id": None,
                   "error": {"code": -32700, "message": f"parse error: {e}"}})
            continue
        try:
            resp = _handle(req)
        except SystemExit:
            return 0
        except Exception as e:            # 任何未预期异常都回成 JSON-RPC error，别让插件看到裸 traceback
            resp = {"jsonrpc": "2.0", "id": req.get("id"),
                    "error": {"code": -32603, "message": f"{type(e).__name__}: {e}"}}
        if resp is not None and req.get("id") is not None:
            _send(resp)
    return 0


if __name__ == "__main__":
    sys.exit(serve())
