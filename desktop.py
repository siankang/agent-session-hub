"""Agent 会话管理台 —— 原生桌面窗口入口（exe 双击运行）。

与 app.py（本地服务 / 浏览器模式）共用同一套业务内核，但：
- 不启动 HTTP 服务、不监听任何端口、不打开浏览器、不显示任何 URL；
- 界面由系统 WebView2 渲染在独立原生窗口里，看起来与操作起来都是桌面软件；
- 前端请求通过 core/bridge.py 的 JS 桥接直接调用 API.dispatch。

用法（一般直接双击 exe，无需参数）：
    AgentSessionHub.exe
    AgentSessionHub.exe --data D:\\path\\index.db   # 自定义索引库
    AgentSessionHub.exe --scan                      # 启动时强制重扫
    AgentSessionHub.exe --debug                     # 打开 WebView2 调试模式

清理执行无需额外开关：逐项勾选 -> 核对完整路径 -> 输入「确认清理」即可。
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent
FROZEN = getattr(sys, "frozen", False)
RES = Path(getattr(sys, "_MEIPASS", str(BASE)))
APP_DIR = Path(sys.executable).resolve().parent if FROZEN else BASE
sys.path.insert(0, str(RES))

_STD_FILE = None


def _ensure_std_streams():
    """--noconsole 下 stdout/stderr 为 None，重定向到日志，避免第三方库写入时出错。"""
    global _STD_FILE
    try:
        p = APP_DIR / "data" / "desktop_stdout.log"
        p.parent.mkdir(parents=True, exist_ok=True)
        _STD_FILE = p.open("a", encoding="utf-8", buffering=1)
        if sys.stdout is None:
            sys.stdout = _STD_FILE
        if sys.stderr is None:
            sys.stderr = _STD_FILE
    except Exception:
        pass
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass


_ensure_std_streams()


def log(msg: str):
    try:
        p = APP_DIR / "data" / "desktop.log"
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            f.write(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")
    except Exception:
        pass


def default_db_path() -> Path:
    primary = APP_DIR / "data" / "index.db"
    try:
        primary.parent.mkdir(parents=True, exist_ok=True)
        probe = primary.parent / ".write_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return primary
    except Exception:
        pass
    fb = (Path(os.environ.get("LOCALAPPDATA") or tempfile.gettempdir())
          / "AgentSessionHub" / "data" / "index.db")
    fb.parent.mkdir(parents=True, exist_ok=True)
    return fb


def storage_path() -> Path:
    """WebView2 用户数据目录：固定英文路径 + 非私有模式，避免临时目录清理导致的 E_ABORT。"""
    p = (Path(os.environ.get("LOCALAPPDATA") or tempfile.gettempdir())
         / "AgentSessionHub" / "webview")
    p.mkdir(parents=True, exist_ok=True)
    return p


# 窗口图标由 exe 自身图标提供。
# 曾用 FindWindowW + SendMessageW(WM_SETICON) 设置：该同步消息发给正在初始化
# WebView2 的 UI 线程会死锁，导致窗口卡在初始化阶段。已移除，不再使用。


_SINGLE_MUTEX = None


def acquire_single_instance(title: str) -> bool:
    """命名 Mutex 保证只开一个窗口；已有实例则把它置前并返回 False。

    多个实例共用同一个 WebView2 用户数据目录会互相阻塞，出现黑屏/灰屏/未响应。
    """
    global _SINGLE_MUTEX
    try:
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
        k32.CreateMutexW.restype = wintypes.HANDLE
        _SINGLE_MUTEX = k32.CreateMutexW(None, True,
                                         "Local\\AgentSessionHub_SingleInstance")
        if ctypes.get_last_error() == 183:      # ERROR_ALREADY_EXISTS
            u = ctypes.windll.user32
            hwnd = u.FindWindowW(None, title)
            if hwnd:
                u.ShowWindow(hwnd, 9)            # SW_RESTORE
                u.SetForegroundWindow(hwnd)
            return False
        return True
    except Exception:
        return True   # 锁机制不可用时放行，避免完全无法启动


def _run_watchdog(main_pid: int, title: str):
    """独立进程：窗口销毁后确保主进程退出（不依赖主进程的 GIL）。

    pythonnet/WinForms 关闭窗口后可能既不退出消息循环、也不释放 GIL，
    进程内的 Python 看门狗线程同样会饿死，因此必须用独立进程。
    """
    try:
        import ctypes
        from ctypes import wintypes
        u = ctypes.windll.user32
        u.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
        u.FindWindowW.restype = wintypes.HWND
        u.IsWindow.argtypes = [wintypes.HWND]
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k32.OpenProcess.restype = wintypes.HANDLE
        k32.TerminateProcess.argtypes = [wintypes.HANDLE, ctypes.c_uint]
        k32.CloseHandle.argtypes = [wintypes.HANDLE]

        hwnd = 0
        for _ in range(80):                      # 等主窗口出现，最多 40 秒
            hwnd = u.FindWindowW(None, title)
            if hwnd:
                break
            time.sleep(0.5)
        if not hwnd:
            return
        while u.IsWindow(hwnd):
            time.sleep(0.5)
        h = k32.OpenProcess(0x0001, False, main_pid)   # PROCESS_TERMINATE
        if h:
            k32.TerminateProcess(h, 0)
            k32.CloseHandle(h)
    except Exception:
        pass


def _exit_watchdog(title: str):
    """用户点 X 关闭窗口后，WinForms/.NET 残留线程可能让进程不退出。

    检测到窗口消失即直接结束进程；SQLite WAL 的数据在每次 commit 时已落盘。
    """
    try:
        import ctypes
        u = ctypes.windll.user32
        for _ in range(60):                 # 先等窗口真正出现，避免启动期误判
            if u.FindWindowW(None, title):
                break
            time.sleep(0.5)
        else:
            return
        while True:
            if not u.FindWindowW(None, title):
                log("窗口已关闭（watchdog），结束进程")
                os._exit(0)
            time.sleep(0.5)
    except Exception:
        pass


SELFTEST_JS = """
<script>
window.addEventListener('pywebviewready', async () => {
  try {
    const b=await window.pywebview.api.call('GET','/api/bootstrap');
    const s=await window.pywebview.api.call('GET','/api/sessions?size=3');
    const q=await window.pywebview.api.call('GET','/api/search?q=%E4%BC%9A%E8%AF%9D');
    // 验证 POST + body 通道（对应「重新扫描」这类按钮），不真正触发扫描
    const pl=await window.pywebview.api.call('POST','/api/cleanup/plan',{ids:[]});
    const cs0=await window.pywebview.api.call('GET','/api/cleanup/scan/status');
    const ss0=await window.pywebview.api.call('GET','/api/scan/status');
    // 验证扫描转圈的显示/隐藏切换
    setScanBusy(true,{running:true,done:3,total:32,current:'WorkBuddy'});
    const spinOn=document.querySelector('#scanSpin').style.display;
    const badgeOn=document.querySelector('#scanBadge').textContent.trim();
    setScanBusy(false,{phase:'done'});
    const spinOff=document.querySelector('#scanSpin').style.display;
    await new Promise(r=>setTimeout(r,900));
    // 进入清理页应自动跟随统一识别（按钮转圈 + 分析中占位）
    switchView('cleanup');
    await new Promise(r=>setTimeout(r,1600));
    const cspin=document.querySelector('#cleanupSpin');
    const autoSpin=cspin?cspin.style.display:'';
    const autoRow=!!document.querySelector('.scanning-row');
    await window.pywebview.api.report('__selftest__', {
      sessions:b.overview.sessions, sources:b.sources.length,
      rows:s.items.length, hits:q.items.length,
      post_ok:!!(pl&&pl.plan&&Array.isArray(pl.plan.items)),
      unified:{status_ok:!!(cs0&&typeof cs0.running==='boolean'),
               merged_ok:!!(ss0&&'sessions_running' in ss0&&'cleanup_running' in ss0&&'running' in ss0),
               auto_spin:autoSpin, auto_row:autoRow},
      spin:{on:spinOn,badge:badgeOn,off:spinOff},
      early:window.__earlyResult||'(none)',
      jsErrors:(window.__jsErrors||[]).slice(0,5),
      nav:document.querySelectorAll('#nav button').length,
      k2:(document.querySelector('#k2')||{}).textContent,
      desktop:!!window.__DESKTOP__, started:!!window.__appStarted
    });
  }catch(e){ await window.pywebview.api.report('__selftest_error__',String(e)); }
});
</script>
"""

# 刻意在主脚本刚执行、桥接可能尚未注入时发起一次请求，验证等待逻辑
EARLY_PROBE_JS = """
<script>
window.__earlyResult='pending';
try{
  api('/api/health').then(r=>{
    window.__earlyResult=(r&&r.ok)?'ok':JSON.stringify(r);
  }).catch(e=>{ window.__earlyResult='ERR '+String(e); });
}catch(e){ window.__earlyResult='THROW '+String(e); }
</script>
"""


def build_html(selftest: bool = False) -> str:
    p = RES / "web" / "index.html"
    html = p.read_text(encoding="utf-8")
    # 告诉前端当前是桌面模式：走 JS 桥接而不是 fetch，且隐藏网页相关提示
    html = html.replace("</head>",
                        "<script>window.__DESKTOP__=true;</script></head>", 1)
    if selftest:
        html = html.replace("</body>", SELFTEST_JS + EARLY_PROBE_JS + "</body>")
    return html


def main():
    ap = argparse.ArgumentParser(description="Agent 会话管理台（桌面窗口）")
    ap.add_argument("--data", default="", help="自定义索引库路径")
    ap.add_argument("--scan", action="store_true", help="启动时强制重新扫描")
    ap.add_argument("--allow-cleanup", action="store_true",
                    help="已废弃（保留兼容）：清理执行现在只需在界面输入确认词")
    ap.add_argument("--debug", action="store_true", help="WebView2 调试模式")
    ap.add_argument("--selftest", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--watchdog", type=int, default=0, help=argparse.SUPPRESS)
    args = ap.parse_args()

    title = "Agent 会话管理台"
    if args.watchdog:
        _run_watchdog(args.watchdog, title)
        return 0
    if not args.selftest and not acquire_single_instance(title):
        return 0       # 已有实例：置前旧窗口后直接退出，不再开新窗口

    from core.bridge import Bridge
    from core.cleanup import CleanupManager
    from core.scanner import Scanner
    from core.server import API
    from core.store import Store
    import webview

    db = Path(args.data).expanduser().resolve() if args.data else default_db_path()
    html = build_html(args.selftest)
    if "window.__DESKTOP__" not in html:
        raise RuntimeError("界面文件注入失败")

    store = Store(db)
    scanner = Scanner(store)
    cleanup = CleanupManager(APP_DIR)
    api = API(store, scanner, cleanup)
    bridge = Bridge(api, cleanup, APP_DIR)
    # 清理候选分析是内存态，每次启动都要重建；与会话索引一起构成一次「统一识别」
    if not args.selftest:
        cleanup.start_scan()
        if store.one("SELECT COUNT(*) n FROM sources")["n"] == 0 or args.scan:
            scanner.start()
        log("统一识别已启动（会话索引 + 清理候选分析）")

    if not args.selftest:
        try:
            cmd = ([sys.executable, "--watchdog", str(os.getpid())] if FROZEN else
                   [sys.executable, str(Path(__file__).resolve()),
                    "--watchdog", str(os.getpid())])
            subprocess.Popen(cmd, creationflags=0x08000000,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception as e:
            log(f"看门狗启动失败: {type(e).__name__}: {e}")

    window = webview.create_window(
        title, html=html, js_api=bridge,
        width=1360, height=880, min_size=(980, 640),
        background_color="#0D1016", resizable=True,
        text_select=True, confirm_close=False,
    )
    # 刻意不把 Window 传给 Bridge（见 core/bridge.py 注释），避免 UI 线程卡死

    # 关闭事件在窗口销毁「之前」触发，此时 GIL 仍可用；
    # 直接结束进程，绕开 pythonnet/WinForms 关闭后不释放 GIL 导致的进程残留。
    def _on_closing():
        log("closing 事件 -> 结束进程")
        try:
            store.db.close()
        except Exception:
            pass
        os._exit(0)
    try:
        window.events.closing += _on_closing
    except Exception as e:
        log(f"closing 事件不可用: {type(e).__name__}: {e}")

    threading.Thread(target=_exit_watchdog, args=(title,), daemon=True).start()
    log(f"启动桌面窗口 | db={db} | cleanup={cleanup.allow_delete} | selftest={args.selftest}")
    if args.selftest:
        def _selftest():
            for _ in range(60):
                if bridge.reports.get("__selftest__") or bridge.reports.get("__selftest_error__"):
                    break
                time.sleep(0.5)
            result = {"ok": bool(bridge.reports.get("__selftest__")),
                      "data": bridge.reports.get("__selftest__"),
                      "error": bridge.reports.get("__selftest_error__")}
            try:
                p = APP_DIR / "data" / "selftest.json"
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(json.dumps(result, ensure_ascii=False, indent=2),
                             encoding="utf-8")
            except Exception:
                pass
            try:
                window.destroy()
            except Exception:
                pass
        threading.Thread(target=_selftest, daemon=True).start()

    # 不使用 start(func=...)：该回调会从非 UI 线程触碰 WebView2 控件，
    # 在部分环境下触发 COM 线程异常；所有辅助逻辑一律用独立线程调度。
    webview.start(gui="edgechromium", private_mode=False,
                  storage_path=str(storage_path()), debug=args.debug)
    log("窗口已关闭")
    try:
        store.db.close()
    except Exception:
        pass
    os._exit(0)   # 绕开 .NET 残留线程，保证关闭窗口后进程立即退出


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        log("启动失败:\n" + traceback.format_exc())
        raise
