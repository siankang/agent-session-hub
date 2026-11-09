"""一键构建单文件桌面 exe：python build_exe.py

产物直接输出到项目根目录（而不是 dist/），这样 exe 同目录就是已有的 data/index.db，
双击即可复用现成索引，不必重新扫描。

桌面版使用 pywebview + 系统 WebView2 呈现原生窗口：
不启动 HTTP 服务、不打开浏览器、不显示网址、无控制台黑窗口。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent


def main():
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print("缺少打包工具，请先执行: python -m pip install pyinstaller pywebview pythonnet")
        return 1
    try:
        import webview  # noqa: F401
    except ImportError:
        print("缺少桌面窗口依赖，请先执行: python -m pip install pywebview pythonnet")
        return 1

    sys.path.insert(0, str(BASE / "tools"))
    import make_icon
    icon = BASE / "assets" / "icon.ico"
    if not icon.is_file():
        make_icon.build()

    sep = os.pathsep
    args = [
        "--onefile", "--noconsole", "--clean", "--noconfirm",
        "--name", "AgentSessionHub",
        "--icon", str(icon),
        "--add-data", f"web{sep}web",
        "--add-data", f"assets{sep}assets",
        # WebView2 / pythonnet 的 .NET 程序集需要显式收进单文件包
        "--collect-all", "webview",
        "--collect-all", "pythonnet",
        "--collect-all", "clr_loader",
        "--hidden-import", "clr",
        "--distpath", str(BASE),
        "--workpath", str(BASE / "build_desktop"),
        "--specpath", str(BASE),
        str(BASE / "desktop.py"),
    ]
    import PyInstaller.__main__ as pyi
    print("开始构建桌面版 exe（首次约 1-3 分钟）…", flush=True)
    pyi.run(args)
    exe = BASE / "AgentSessionHub.exe"
    if not exe.is_file():
        print("[失败] 未生成 exe")
        return 1
    print(f"\n构建完成: {exe}  ({exe.stat().st_size / 1048576:.1f} MiB)")
    print("双击即可打开独立软件窗口；无需浏览器、无需网址、无需本地服务。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
