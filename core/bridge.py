"""桌面窗口的 Python <-> JS 桥接。

桌面模式不启动 HTTP 服务、不监听端口：前端把原本的 /api/* 请求
直接交给 Bridge.call()，由其调用同一个 API.dispatch（与网页模式共用全部业务逻辑）。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlparse


class Bridge:
    def __init__(self, api, cleanup, base: Path):
        self.api = api
        self.cleanup = cleanup
        self.base = base
        self.window = None   # 必须保持 None：不要持有 pywebview Window 对象
        self.export_dir = base / "exports"
        self.reports = {}   # 供桌面自检脚本回报前端状态（不依赖 evaluate_js）

    # ------------------------------------------------------------ 通用 API 转发
    def call(self, method="GET", path="/api/health", body=None):
        try:
            u = urlparse(path)
            qs = parse_qs(u.query, keep_blank_values=True)
            res = self.api.dispatch((method or "GET").upper(), u.path, qs,
                                    body if isinstance(body, dict) else {})
            if isinstance(res, dict) and "__download__" in res:
                return self._save_file(*res["__download__"])
            # 保证返回值一定可被 JSON 序列化
            return json.loads(json.dumps(res, ensure_ascii=False, default=str))
        except Exception as e:
            return {"error": f"{type(e).__name__}: {e}"}

    def _save_file(self, filename, content_type, data):
        """直接写入 exports/ 目录。

        刻意不持有 pywebview 的 Window 引用、也不弹原生「另存为」：
        WinForms 后端遍历 Window 对象会触发 pythonnet 递归/跨线程 COM 异常，
        把 UI 线程拖成「未响应」。导出后界面会提示完整路径。
        """
        try:
            self.export_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            p = self.export_dir / f"{stamp}-{filename}"
            p.write_bytes(data)
            return {"__saved__": str(p), "fallback": True}
        except Exception as e:
            return {"__error__": f"保存失败: {type(e).__name__}: {e}"}

    # ------------------------------------------------------------ 桌面专用能力
    def report(self, key, value=None):
        self.reports[str(key)] = value
        return {"ok": True}

    def quit(self):
        """界面「退出软件」按钮：立即结束进程，不依赖窗口关闭消息循环。"""
        os._exit(0)

    def app_info(self):
        return {
            "mode": "desktop",
            "base": str(self.base),
            "export_dir": str(self.export_dir),
            "cleanup_allowed": bool(self.cleanup.allow_delete),
        }

    def open_folder(self, path=""):
        """在资源管理器中打开目录（仅允许本工具输出目录）。"""
        try:
            p = Path(path).resolve() if path else self.export_dir
            allowed = (self.base / "exports").resolve(), (self.base / "backup").resolve()
            if not any(str(p).startswith(str(a)) for a in allowed):
                p = self.export_dir
            p.mkdir(parents=True, exist_ok=True)
            if sys.platform == "win32":
                os.startfile(str(p))  # noqa: S606
            elif sys.platform == "darwin":
                subprocess.run(["open", str(p)], check=False)
            else:
                subprocess.run(["xdg-open", str(p)], check=False)
            return {"ok": True, "path": str(p)}
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}
