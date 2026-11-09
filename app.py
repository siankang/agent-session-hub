"""Agent 会话管理工具 —— 本地多端会话聚合台（同时支持源码运行与 PyInstaller 单文件 exe）。

源码运行：
    python app.py                     # 默认仅本机 127.0.0.1:8765
    python app.py --port 9000
    python app.py --lan               # 允许手机/平板等局域网设备访问（有隐私风险）
    python app.py --scan              # 启动时强制重新扫描

exe 运行（双击或命令行）：
    AgentSessionHub.exe
    AgentSessionHub.exe --port 9000 --no-browser
    AgentSessionHub.exe --lan

全程只读源数据；索引库默认写到 exe / 源码同目录的 data/index.db（不可写时回退到 %LOCALAPPDATA%）。
清理执行无需额外开关：逐项勾选 + 核对完整路径 + 输入「确认清理」即可，执行前完整备份、走回收站且可按批次还原。
"""
from __future__ import annotations

import argparse
import os
import socket
import sys
import tempfile
import threading
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass  # 无控制台时 stdout 可能为 None

BASE = Path(__file__).resolve().parent
FROZEN = getattr(sys, "frozen", False)
# frozen 模式下 __file__ 指向临时解包目录，资源在那里；用户数据必须放 exe 所在目录
RES = Path(getattr(sys, "_MEIPASS", str(BASE)))
APP_DIR = Path(sys.executable).resolve().parent if FROZEN else BASE
sys.path.insert(0, str(RES))

from core.cleanup import CleanupManager  # noqa: E402
from core.scanner import Scanner          # noqa: E402
from core.server import serve             # noqa: E402
from core.store import Store              # noqa: E402


def local_ips():
    out = []
    try:
        host = socket.gethostname()
        for ip in socket.gethostbyname_ex(host)[2]:
            if not ip.startswith("127."):
                out.append(ip)
    except Exception:
        pass
    return out


def default_db_path() -> Path:
    """优先复用 exe/源码同目录的索引；目录不可写（如 Program Files）则回退用户目录。"""
    primary = APP_DIR / "data" / "index.db"
    try:
        primary.parent.mkdir(parents=True, exist_ok=True)
        probe = primary.parent / ".write_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return primary
    except Exception:
        pass
    fallback = (Path(os.environ.get("LOCALAPPDATA") or tempfile.gettempdir())
                / "AgentSessionHub" / "data" / "index.db")
    fallback.parent.mkdir(parents=True, exist_ok=True)
    return fallback


def port_busy(host: str, port: int) -> bool:
    """主动探测是否已有服务在监听（Windows 下 bind 探测不可靠，用 connect 更准）。"""
    target = "127.0.0.1" if host in ("0.0.0.0", "") else host
    s = socket.socket()
    s.settimeout(0.3)
    try:
        return s.connect_ex((target, port)) == 0
    except OSError:
        return False
    finally:
        s.close()


def bind_server(store, scanner, web_dir, host, start_port, cleanup, tries=20):
    """端口被占用时自动顺延，避免双击后因旧实例残留而启动失败。"""
    last = None
    for port in range(start_port, start_port + tries):
        if port_busy(host, port):
            last = "已有服务监听"
            continue
        try:
            return serve(store, scanner, web_dir, host, port, cleanup), port
        except OSError as e:
            last = e
    raise RuntimeError(f"{start_port}~{start_port + tries - 1} 端口均不可用: {last}")


def main():
    ap = argparse.ArgumentParser(description="本机 Agent 会话聚合管理工具（只读）")
    ap.add_argument("--host", default="127.0.0.1", help="监听地址，默认 127.0.0.1")
    ap.add_argument("--port", type=int, default=8765, help="端口，默认 8765")
    ap.add_argument("--lan", action="store_true", help="监听 0.0.0.0，允许局域网设备访问")
    ap.add_argument("--scan", action="store_true", help="启动时强制重新扫描")
    ap.add_argument("--data", default="", help="自定义索引库路径")
    ap.add_argument("--no-browser", action="store_true", help="启动后不自动打开浏览器")
    ap.add_argument("--allow-cleanup", action="store_true",
                    help="已废弃（保留兼容）：清理执行现在只需在界面输入确认词")
    args = ap.parse_args()

    db = Path(args.data).expanduser().resolve() if args.data else default_db_path()
    web_dir = RES / "web"
    if not (web_dir / "index.html").is_file():
        print(f"[错误] 找不到界面文件: {web_dir / 'index.html'}")
        return 1

    store = Store(db)
    scanner = Scanner(store)
    cleanup = CleanupManager(APP_DIR)
    cleanup.start_scan()   # 内存态，每次启动重建；与会话索引组成一次统一识别
    n = store.one("SELECT COUNT(*) n FROM sources")["n"]
    if args.scan or n == 0:
        scanner.start()
        print("[识别] 已后台启动会话索引 + 清理候选分析，界面上可看到进度。")

    host = "0.0.0.0" if args.lan else args.host
    try:
        httpd, port = bind_server(store, scanner, web_dir, host, args.port, cleanup)
    except RuntimeError as e:
        print(f"[错误] {e}")
        return 1
    url = f"http://127.0.0.1:{port}"

    print("=" * 68)
    print("  Agent 会话管理台已启动（只读模式）")
    print(f"  本机访问:  {url}")
    if args.lan:
        print("  ⚠ 局域网模式：同网段设备可访问，对话内容会暴露给该网络")
        for ip in local_ips():
            print(f"  其他设备:  http://{ip}:{port}")
    else:
        print("  手机/平板访问: 加 --lan 参数后用本机局域网 IP 打开")
    print(f"  索引库:    {db}")
    print(f"  界面资源:  {web_dir}")
    print("  清理模式:  扫描只读；执行需逐项勾选并在界面输入「确认清理」")
    print("  关闭本窗口 / 按 Ctrl+C 即可停止")
    print("=" * 68, flush=True)

    if not args.no_browser:
        import webbrowser
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
