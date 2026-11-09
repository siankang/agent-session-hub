"""零依赖 HTTP 服务：REST API + 静态单页界面。"""
from __future__ import annotations

import io
import json
import mimetypes
import os
import time
import urllib.parse
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .adapters import SOURCES
from .scanner import Scanner
from .store import Store


def fmt_ms(ms):
    if not ms:
        return "—"
    try:
        return datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return "—"


def export_markdown(store: Store, rows: list[dict], include_messages=True):
    buf = io.StringIO()
    buf.write("# Agent 会话导出\n\n")
    buf.write(f"> 由会话管理工具导出 · {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} · 共 {len(rows)} 个会话\n\n")
    for s in rows:
        buf.write(f"\n---\n\n## {s['title']}\n\n")
        buf.write(f"- 来源：{s['source_id']}\n- 项目：{s.get('project') or '—'}\n")
        buf.write(f"- 创建：{fmt_ms(s.get('created_at'))} · 更新：{fmt_ms(s.get('updated_at'))}\n")
        buf.write(f"- 消息数：{s.get('message_count', 0)} · 文件：`{s.get('file_path') or '—'}`\n\n")
        if include_messages:
            msgs = store.query("SELECT seq,role,ts,kind,text FROM messages "
                               "WHERE session_id=? ORDER BY seq", (s["id"],))
            for m in msgs:
                label = {"user": "👤 用户", "assistant": "🤖 助手"}.get(m["role"], m["role"])
                buf.write(f"### {label} · {fmt_ms(m['ts'])}\n\n{m['text']}\n\n")
    return buf.getvalue().encode("utf-8")


class API:
    def __init__(self, store: Store, scanner: Scanner, cleanup=None):
        self.store = store
        self.scanner = scanner
        self.cleanup = cleanup

    def scan_status(self):
        """统一识别状态：会话索引扫描 + 清理候选只读分析，用户只看得到一次识别。"""
        st = dict(self.scanner.status())
        sess_running = bool(st.get("running"))
        cs = self.cleanup.scan_status() if self.cleanup else {}
        c_running = bool(cs.get("running"))
        st["sessions_running"] = sess_running
        st["cleanup_running"] = c_running
        st["cleanup_items"] = cs.get("total_items", 0)
        st["running"] = sess_running or c_running
        if c_running and not sess_running:
            st["current"] = "清理数据分析"
            st["done"], st["total"] = 0, 0
            st["phase"] = "cleanup"
        elif c_running and sess_running:
            st["current"] = (st.get("current") or "会话") + " + 清理数据"
        return st

    def dispatch(self, method: str, path: str, qs: dict, body: dict):
        p = path.rstrip("/") or "/"
        g = lambda k, d="": (qs.get(k) or [d])[0]

        if p == "/api/health":
            return {"ok": True, "ts": int(time.time() * 1000)}

        if p == "/api/bootstrap":
            return {"overview": self.store.overview(),
                    "sources": self.store.sources(),
                    "scan": self.scan_status(),
                    "registry_count": len(SOURCES),
                    "cleanup_allowed": bool(self.cleanup and self.cleanup.allow_delete),
                    "cleanup_summary": self.cleanup.summary() if self.cleanup else None}

        if p == "/api/overview":
            return self.store.overview()

        if p == "/api/sources":
            return {"items": self.store.sources()}

        if p == "/api/sessions":
            try:
                page, size = max(1, int(g("page", "1"))), min(200, max(1, int(g("size", "30"))))
                days = int(g("days", "0") or 0)
            except ValueError:
                page, size, days = 1, 30, 0
            return self.store.sessions(source=g("source") or None, q=g("q") or None,
                                       page=page, size=size, sort=g("sort", "updated"),
                                       days=days)

        if p == "/api/session":
            sid = g("id")
            try:
                offset, limit = int(g("offset", "0")), min(500, max(1, int(g("limit", "120"))))
            except ValueError:
                offset, limit = 0, 120
            r = self.store.session(sid, offset, limit)
            return r or {"error": "not found"}

        if p == "/api/search":
            q = g("q")
            return {"items": self.store.search(q, min(200, int(g("limit", "60") or 60))),
                    "has_fts": self.store.has_fts}

        if p == "/api/timeline":
            try:
                days = int(g("days", "30"))
            except ValueError:
                days = 30
            return {"items": self.store.timeline(days), "days": days}

        if p == "/api/storage":
            return self.store.storage()

        if p == "/api/scan/status":
            return self.scan_status()

        if p == "/api/scan" and method == "POST":
            ids = body.get("sources") or None
            started = self.scanner.start(ids)
            cleanup_started = bool(self.cleanup.start_scan().get("started")) \
                if self.cleanup else False
            return {"started": started, "cleanup_started": cleanup_started,
                    "status": self.scan_status()}

        if p == "/api/export":
            fmt = g("format", "md")
            scope = g("scope", "session")
            sid = g("id")
            if scope == "session":
                rows = [r for r in [self.store.one("SELECT * FROM sessions WHERE id=?", (sid,))] if r]
            elif scope == "source":
                rows = self.store.query("SELECT * FROM sessions WHERE source_id=? "
                                        "ORDER BY updated_at DESC", (sid,))
            elif scope == "filtered":
                try:
                    days = int(g("days", "0") or 0)
                except ValueError:
                    days = 0
                rows = self.store.sessions(source=g("source") or None, q=g("q") or None,
                                           page=1, size=1000,
                                           sort=g("sort", "updated"), days=days)["items"]
            else:
                rows = self.store.query("SELECT * FROM sessions ORDER BY updated_at DESC")
            if fmt == "json":
                payload = []
                for s in rows:
                    d = dict(s)
                    if scope == "session":
                        d["messages"] = self.store.query(
                            "SELECT seq,role,ts,kind,text FROM messages "
                            "WHERE session_id=? ORDER BY seq", (s["id"],))
                    payload.append(d)
                data = json.dumps({"exported_at": datetime.now().isoformat(),
                                  "count": len(payload), "sessions": payload},
                                 ensure_ascii=False, indent=2).encode("utf-8")
                return {"__download__": ("sessions.json", "application/json; charset=utf-8", data)}
            data = export_markdown(self.store, rows, include_messages=(scope == "session"))
            return {"__download__": ("sessions.md", "text/markdown; charset=utf-8", data)}

        # ---------------- 清理（扫描只读；执行只需界面输入确认词） ----------------
        if p.startswith("/api/cleanup/"):
            if self.cleanup is None:
                return {"error": "清理模块未启用"}
            c = self.cleanup
            if p == "/api/cleanup/summary":
                return c.summary()
            if p == "/api/cleanup/scan" and method == "POST":
                return c.start_scan()          # 后台执行，绝不阻塞 UI 线程
            if p == "/api/cleanup/scan/status":
                return c.scan_status()
            if p == "/api/cleanup/items":
                try:
                    page, size = int(g("page", "1")), min(200, max(1, int(g("size", "50"))))
                except ValueError:
                    page, size = 1, 50
                return c.list_items(category=g("category"), source=g("source"),
                                    risk=g("risk"), q=g("q"),
                                    only_suggested=g("suggested") == "1",
                                    page=page, size=size)
            if p == "/api/cleanup/plan" and method == "POST":
                return c.make_plan(body.get("ids") or [])
            if p == "/api/cleanup/execute" and method == "POST":
                res = c.execute(body.get("plan_id") or "", body.get("confirm") or "")
                if res.get("ok") and res.get("done", 0):
                    self.scanner.start()   # 源数据变了，后台重建会话索引
                return res
            if p == "/api/cleanup/history":
                return c.history()
            if p == "/api/cleanup/restore" and method == "POST":
                res = c.restore(body.get("batch_id") or "")
                if res.get("ok"):
                    self.scanner.start()
                return res

        if p == "/api/help":
            return {
                "readonly": True,
                "index_db": str(self.store.path),
                "levels": {
                    "full": "已解析逐条对话，可查看与全文搜索",
                    "meta": "已识别会话清单，对话正文未解析",
                    "aggregate": "仅识别数据目录与体量",
                },
                "adapters": len(SOURCES),
            }
        return {"error": "unknown endpoint", "path": p}


def make_handler(store: Store, scanner: Scanner, web_dir: Path, host: str, cleanup=None):
    api = API(store, scanner, cleanup)

    class Handler(BaseHTTPRequestHandler):
        server_version = "AgentSessionHub/1.0"

        def log_message(self, fmt, *args):
            pass  # 静默，避免刷屏

        def _json(self, obj, status=200):
            data = json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _bytes(self, data, ctype, filename=None, status=200):
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            if filename:
                safe = filename.replace('"', "")
                self.send_header("Content-Disposition", f'attachment; filename="{safe}"')
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            self._handle("GET")

        def do_POST(self):
            self._handle("POST")

        def _handle(self, method):
            u = urllib.parse.urlparse(self.path)
            qs = urllib.parse.parse_qs(u.query)
            body = {}
            if method == "POST":
                try:
                    n = int(self.headers.get("Content-Length") or 0)
                    if n > 0 and n < 1_000_000:
                        body = json.loads(self.rfile.read(n).decode("utf-8", "replace"))
                except Exception:
                    body = {}
            if u.path.startswith("/api/"):
                try:
                    res = api.dispatch(method, u.path, qs, body)
                except Exception as e:
                    return self._json({"error": f"{type(e).__name__}: {e}"}, 500)
                if isinstance(res, dict) and "__download__" in res:
                    fn, ct, data = res["__download__"]
                    return self._bytes(data, ct, fn)
                return self._json(res)
            # 静态文件（严格限制在 web_dir 内）
            rel = "index.html" if u.path in ("/", "") else u.path.lstrip("/")
            target = (web_dir / rel).resolve()
            if web_dir.resolve() not in target.parents and target != web_dir.resolve() / "index.html":
                return self._bytes(b"forbidden", "text/plain", status=403)
            if not target.is_file():
                target = web_dir / "index.html"
            if not target.is_file():
                return self._bytes(b"index.html missing", "text/plain", status=404)
            ctype = mimetypes.guess_type(target.name)[0] or "text/html; charset=utf-8"
            if target.suffix == ".html":
                ctype = "text/html; charset=utf-8"
            return self._bytes(target.read_bytes(), ctype)

    return Handler


class LocalServer(ThreadingHTTPServer):
    # Windows 的 SO_REUSEADDR 允许多个进程绑定同一端口，会让「重复双击」的两个实例
    # 抢同一个端口且请求随机分配；在 Windows 上必须关掉，让第二次绑定失败并触发端口顺延。
    allow_reuse_address = (os.name != "nt")
    daemon_threads = True


def serve(store: Store, scanner: Scanner, web_dir: Path, host="127.0.0.1",
          port=8765, cleanup=None):
    return LocalServer((host, port),
                       make_handler(store, scanner, web_dir, host, cleanup))
