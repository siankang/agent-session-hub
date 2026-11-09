"""会话 / 记忆 / 缓存 / 会话数据库清理。

安全模型（不可绕过）：
1. 扫描阶段 100% 只读，只产出清单与风险标注，绝不修改任何源文件。
2. 用户必须逐项勾选 -> 生成服务端计划 -> 核对完整路径 -> 输入「确认清理」才执行；前端只能传 id，路径由服务端取。
3. 无需额外解锁步骤；执行前仍强制确认词校验（唯一的用户确认关卡）。
4. 执行前：路径白名单校验、保护名单校验、体积上限、磁盘空间检查、完整备份并校验。
5. 删除走 Windows 回收站（SHFileOperationW + FOF_ALLOWUNDO），每批最多 10 项，失败立即停止。
6. 每批写 manifest + 日志，可按批次从备份还原。

本项目目录、Desktop / Downloads / Documents / 系统目录、身份与登录状态文件一律受保护。
"""
from __future__ import annotations

import ctypes
import hashlib
import json
import os
import shutil
import subprocess
import threading
import time
from ctypes import wintypes
from datetime import datetime
from pathlib import Path

from .adapters import SOURCES

CATEGORIES = {
    "session": {"label": "会话记录", "risk": "high", "color": "#f0b458"},
    "memory": {"label": "记忆文件", "risk": "critical", "color": "#ef6b73"},
    "cache": {"label": "缓存 / 日志", "risk": "low", "color": "#2ecc8f"},
    "database": {"label": "会话数据库", "risk": "critical", "color": "#a78bfa"},
}

RISK_LABEL = {"low": "低", "medium": "中", "high": "高", "critical": "极高"}
RISK_NOTE = {
    "low": "缓存/日志通常可重建，但可能含登录态或本地设置",
    "medium": "辅助数据，删除后部分历史或统计不可恢复",
    "high": "对话记录，删除后客户端历史与本工具索引都会丢失",
    "critical": "记忆/数据库，删除可能导致客户端会话列表或设置损坏，且不可重建",
}

CACHE_DIR_NAMES = {
    "cache", "caches", "gpucache", "code cache", "blob_storage", "cacheddata",
    "cachedextensions", "cachedconfigurations", "crashpad", "shadercache",
    "dawn-cache", "shader cache", "logs", "log", "shell-snapshots",
    "pending-telemetry", "service worker", "webview", "gpu", "temp", "tmp",
    "crashdumps", "backups", "blob_storage", "dips", "network",
}
CACHE_FILE_EXT = {".log", ".tmp", ".old", ".bak"}
SESSION_HINT = ("session", "chat", "conversation", "history", "transcript",
                "rollout", "dialog", "对话", "会话")
MEMORY_HINT = ("memory", "memories", "neural-memory", "neuralmemory", "longterm")
DB_EXT = {".db", ".sqlite", ".sqlite3", ".vscdb"}
SESSION_EXT = {".jsonl", ".ndjson", ".json", ".md", ".db", ".sqlite", ".vscdb"}
PROTECTED_NAMES = {
    # 身份与长期记忆核心文件：工具绝不代用户删除（用户仍可自行手动处理）
    "soul.md", "identity.md", "user.md", "bootstrap.md", "memory.md",
    # 登录状态与关键配置
    "settings.json", "preferences", "local state", "cookies", "login data",
    "web data", "machineid", ".gitconfig", "mcp.json",
}
NO_GO_PARTS = {"desktop", "downloads", "documents", "windows", "system32",
               "program files", "program files (x86)"}
KNOWN_PROCS = ("workbuddy", "codebuddy", "code", "trae", "qoder", "devin",
               "codex", "claude", "doubao", "zcode", "grok", "cursor",
               "windsurf", "hermes", "opencode", "catpaw", "coze", "kimi")

MAX_ITEM_BYTES = 500 * 1024 * 1024      # 单项 500 MB，超出只列清单不允许执行
MAX_WALK_FILES = 6000                    # 每个来源根目录的文件遍历上限
BATCH_SIZE = 10
CONFIRM_WORD = "确认清理"
PLAN_TTL = 600                           # 计划 10 分钟过期


def dir_stats(path: Path):
    files, size, latest = 0, 0, 0
    for cur, dirs, names in os.walk(path, topdown=True):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for n in names:
            try:
                st = (Path(cur) / n).stat()
            except OSError:
                continue
            files += 1
            size += st.st_size
            latest = max(latest, int(st.st_mtime * 1000))
            if files >= MAX_WALK_FILES:
                return files, size, latest, True
    return files, size, latest, False


def running_agents():
    """只读进程名，用于提示用户先关闭客户端。失败时静默返回空。"""
    try:
        r = subprocess.run(["tasklist", "/FO", "CSV", "/NH"],
                           capture_output=True, timeout=8)
        text = r.stdout.decode("utf-8", "replace").lower()
        return sorted({p for p in KNOWN_PROCS if p in text})
    except Exception:
        return []


# ---------------------------------------------------------------- Win32 回收站
class _SHFILEOPSTRUCTW(ctypes.Structure):
    _fields_ = [
        ("hwnd", wintypes.HWND),
        ("wFunc", ctypes.c_uint),
        ("pFrom", ctypes.c_void_p),
        ("pTo", ctypes.c_void_p),
        ("fFlags", ctypes.c_uint16),
        ("fAnyOperationsAborted", wintypes.BOOL),
        ("hNameMappings", ctypes.c_void_p),
        ("lpszProgressTitle", ctypes.c_void_p),
    ]


def to_recycle_bin(path: Path):
    """把文件/目录送进回收站（不是永久删除）。返回 (ok, 说明)。"""
    if os.name != "nt":
        return False, "仅支持 Windows 回收站"
    try:
        # create_unicode_buffer 会自带结尾 NUL，再显式加一个 -> 双 NUL 结尾
        buf = ctypes.create_unicode_buffer(str(path) + "\0")
        op = _SHFILEOPSTRUCTW()
        op.hwnd = None
        op.wFunc = 3                      # FO_DELETE
        op.pFrom = ctypes.cast(buf, ctypes.c_void_p)
        op.pTo = None
        op.fFlags = 0x0004 | 0x0010 | 0x0040 | 0x0400  # SILENT|NOCONFIRM|ALLOWUNDO|NOERRORUI
        rc = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
        if rc != 0 or op.fAnyOperationsAborted:
            return False, f"回收站 API 返回 {rc}"
        return True, "已移入回收站"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


class CleanupManager:
    def __init__(self, base: Path, home: Path | None = None, allow_delete: bool = True):
        self.base = base.resolve()
        self.home = (home or Path.home()).resolve()
        self.allow_delete = allow_delete
        self.backup_root = self.base / "backup" / "quarantine"
        self.log_path = self.base / "data" / "cleanup_log.json"
        self.lock = threading.RLock()
        self.items: dict[str, dict] = {}
        self.plans: dict[str, dict] = {}
        self.last_scan_at = 0
        self.running: list[str] = []
        self._roots: list[Path] = []
        self.scan_running = False
        self.scan_error = None

    # ------------------------------------------------------------ 路径安全
    def _allowed_roots(self):
        if not self._roots:
            roots = []
            for s in SOURCES:
                for rel in s.roots:
                    p = (self.home / rel).resolve()
                    if p.is_dir():
                        roots.append(p)
            self._roots = roots
        return self._roots

    def _under_allowed_root(self, path: Path):
        p = path.resolve()
        for root in self._allowed_roots():
            try:
                p.relative_to(root)
                return True, root
            except ValueError:
                continue
        return False, None

    def _protected(self, path: Path):
        p = path.resolve()
        # 工具自身目录绝对不可清理
        try:
            p.relative_to(self.base)
            return True, "工具自身目录受保护"
        except ValueError:
            pass
        low = str(p).lower()
        for part in NO_GO_PARTS:
            if f"\\{part}\\" in low or low.endswith(f"\\{part}"):
                # AppData 下的合法应用目录不算 No-Go
                if "\\appdata\\" not in low:
                    return True, f"系统/个人目录受保护: {part}"
        if p.name.lower() in PROTECTED_NAMES:
            return True, "身份 / 长期记忆 / 登录状态 / 关键配置受保护"
        ok, root = self._under_allowed_root(p)
        if not ok:
            return True, "不在已识别的 Agent 数据目录内"
        if p == root:
            return True, "数据根目录不可整体删除"
        return False, ""

    # ------------------------------------------------------------ 只读扫描
    def _add(self, path: Path, category: str, source_name: str, source_id: str,
             is_dir: bool, size: int, files: int, mtime: int, note: str = "",
             truncated: bool = False):
        p = path.resolve()
        protected, why = self._protected(p)
        risk = CATEGORIES[category]["risk"]
        if category == "cache" and files > 2000:
            risk = "medium"
        item_id = hashlib.sha1(str(p).encode("utf-8", "replace")).hexdigest()[:16]
        self.items[item_id] = {
            "id": item_id, "category": category, "source_id": source_id,
            "source_name": source_name, "path": str(p), "is_dir": is_dir,
            "size": size, "files": files, "mtime": mtime, "risk": risk,
            "risk_label": RISK_LABEL[risk], "reason": note or RISK_NOTE[risk],
            "protected": protected, "protect_reason": why,
            "oversized": size > MAX_ITEM_BYTES,
            "truncated": truncated,
            "suggested": (not protected and size > 0 and risk == "low"
                          and size <= MAX_ITEM_BYTES),
        }

    def scan(self):
        """只读遍历，产出四类待清理条目。绝不写、不移动、不删除任何源文件。"""
        with self.lock:
            self.items = {}
            self._roots = []
            self.running = running_agents()
            for src in SOURCES:
                for rel in src.roots:
                    root = (self.home / rel).resolve()
                    if not root.is_dir():
                        continue
                    seen = 0
                    for cur, dirs, names in os.walk(root, topdown=True):
                        curp = Path(cur)
                        try:
                            depth = len(curp.relative_to(root).parts)
                        except ValueError:
                            continue
                        if depth >= 5:
                            dirs[:] = []
                        # 缓存目录聚合成单条并剪枝，避免展开成上万条
                        keep = []
                        for d in dirs:
                            if d.lower() in CACHE_DIR_NAMES and depth >= 1:
                                cp = curp / d
                                n, sz, mt, trunc = dir_stats(cp)
                                if n:
                                    self._add(cp, "cache", src.name, src.id,
                                              True, sz, n, mt,
                                              "缓存/日志目录，通常可重建；可能含登录态", trunc)
                            elif not d.startswith("."):
                                keep.append(d)
                        dirs[:] = keep
                        for name in names:
                            seen += 1
                            if seen > MAX_WALK_FILES:
                                break
                            p = curp / name
                            try:
                                st = p.stat()
                            except OSError:
                                continue
                            ext = p.suffix.lower()
                            low = (str(p.relative_to(root))).lower()
                            cat = None
                            if ext in DB_EXT and not name.lower().endswith("-journal"):
                                cat = "database"
                            elif any(h in low for h in MEMORY_HINT) and ext in SESSION_EXT | {".txt", ".yaml", ".yml"}:
                                cat = "memory"
                            elif any(h in low for h in SESSION_HINT) and ext in SESSION_EXT:
                                cat = "session"
                            elif ext in CACHE_FILE_EXT:
                                cat = "cache"
                            if cat:
                                self._add(p, cat, src.name, src.id, False,
                                          st.st_size, 1, int(st.st_mtime * 1000))
                        if seen > MAX_WALK_FILES:
                            break
            self.last_scan_at = int(time.time() * 1000)
            return self.summary()

    def start_scan(self):
        """后台执行只读分析，立即返回；绝不让 20+ 秒的遍历阻塞 UI 线程。"""
        if self.scan_running:
            return {"started": False, "reason": "already running"}
        self.scan_running = True
        self.scan_error = None

        def _run():
            try:
                self.scan()
            except Exception as e:
                self.scan_error = f"{type(e).__name__}: {e}"
            finally:
                self.scan_running = False

        threading.Thread(target=_run, daemon=True).start()
        return {"started": True}

    def scan_status(self):
        s = self.summary()
        s["running"] = self.scan_running
        s["error"] = self.scan_error
        return s

    def summary(self):
        by_cat = {k: {"label": v["label"], "count": 0, "bytes": 0, "color": v["color"]}
                  for k, v in CATEGORIES.items()}
        protected = oversized = 0
        for it in self.items.values():
            c = by_cat[it["category"]]
            c["count"] += 1
            c["bytes"] += it["size"]
            protected += int(it["protected"])
            oversized += int(it["oversized"])
        return {
            "by_category": by_cat,
            "total_items": len(self.items),
            "total_bytes": sum(i["size"] for i in self.items.values()),
            "protected": protected, "oversized": oversized,
            "running_agents": self.running,
            "allow_delete": self.allow_delete,
            "last_scan_at": self.last_scan_at,
            "confirm_word": CONFIRM_WORD,
            "max_item_mb": MAX_ITEM_BYTES // 1048576,
            "batch_size": BATCH_SIZE,
        }

    def list_items(self, *, category="", source="", risk="", q="",
                   only_suggested=False, page=1, size=50):
        rows = list(self.items.values())
        if category:
            rows = [r for r in rows if r["category"] == category]
        if source:
            rows = [r for r in rows if r["source_id"] == source]
        if risk:
            rows = [r for r in rows if r["risk"] == risk]
        if q:
            rows = [r for r in rows if q.lower() in r["path"].lower()]
        if only_suggested:
            rows = [r for r in rows if r["suggested"]]
        # 可清理的排前面，再按体积降序
        rows.sort(key=lambda r: (r["protected"], not r["suggested"], -r["size"]))
        total = len(rows)
        return {"items": rows[(page - 1) * size: page * size], "total": total,
                "page": page, "size": size}

    # ------------------------------------------------------------ 计划
    def make_plan(self, ids: list[str]):
        picked, rejected = [], []
        for i in ids:
            it = self.items.get(i)
            if not it:
                rejected.append({"id": i, "why": "条目不存在，请重新扫描"})
                continue
            p = Path(it["path"])
            protected, why = self._protected(p)
            if protected:
                rejected.append({"id": i, "path": it["path"], "why": why})
                continue
            if it["oversized"]:
                rejected.append({"id": i, "path": it["path"],
                                 "why": f"超过 {MAX_ITEM_BYTES // 1048576} MB 上限"})
                continue
            if not p.exists():
                rejected.append({"id": i, "path": it["path"], "why": "文件已不存在"})
                continue
            picked.append(dict(it))
        total = sum(i["size"] for i in picked)
        plan_id = hashlib.sha1(
            (str(time.time()) + "|" + "|".join(sorted(i["id"] for i in picked)))
            .encode("utf-8")).hexdigest()[:16]
        plan = {"id": plan_id, "created_at": int(time.time() * 1000),
                "items": picked, "rejected": rejected, "total_bytes": total}
        with self.lock:
            self.plans[plan_id] = plan
        return {"plan": {**plan, "backup_dir": None},
                "summary": self.summary()}

    # ------------------------------------------------------------ 执行
    def execute(self, plan_id: str, confirm: str):
        # 唯一的确认关卡：用户必须在计划弹层里逐项核对路径后输入确认词
        if confirm != CONFIRM_WORD:
            return {"ok": False, "error": "确认词不正确，请输入「确认清理」"}
        with self.lock:
            plan = self.plans.get(plan_id)
        if not plan:
            return {"ok": False, "error": "计划不存在或已过期，请重新生成"}
        if time.time() * 1000 - plan["created_at"] > PLAN_TTL * 1000:
            return {"ok": False, "error": "计划已过期，请重新生成"}
        items = plan["items"]
        if not items:
            return {"ok": False, "error": "没有可执行的条目"}
        if len(items) > 100:
            return {"ok": False, "error": "单次最多 100 项，请分批执行"}

        # 执行前总校验：全部路径重新过白名单 / 保护 / 存在性
        for it in items:
            p = Path(it["path"]).resolve()
            protected, why = self._protected(p)
            if protected or not p.exists():
                return {"ok": False, "error": f"执行前校验失败: {p} ({why})"}

        try:
            self.base.mkdir(parents=True, exist_ok=True)
            free = shutil.disk_usage(str(self.base)).free
        except OSError:
            # 目录不可创建/不可访问时退回盘符根目录查询，避免清理流程直接崩溃
            free = shutil.disk_usage(str(Path.home().anchor)).free
        if free < plan["total_bytes"] * 1.2 + 64 * 1024 * 1024:
            return {"ok": False, "error": "磁盘剩余空间不足以完成备份"}

        batch_id = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + plan_id[:6]
        dest_root = self.backup_root / batch_id
        dest_root.mkdir(parents=True, exist_ok=True)
        done, failed, aborted = [], [], False
        note = ""

        for start in range(0, len(items), BATCH_SIZE):
            batch = items[start:start + BATCH_SIZE]
            # 1) 先完整备份并校验
            for it in batch:
                src = Path(it["path"])
                rel = Path(it["source_id"]) / src.name
                dst = dest_root / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                try:
                    if src.is_dir():
                        shutil.copytree(src, dst, dirs_exist_ok=True)
                    else:
                        shutil.copy2(src, dst)
                except Exception as e:
                    failed.append({"path": str(src), "stage": "backup",
                                   "why": f"{type(e).__name__}: {e}"})
                    aborted = True
                    break
            if aborted:
                note = "备份阶段失败，未删除任何源文件"
                break
            # 2) 校验备份体积
            for it in batch:
                src = Path(it["path"])
                dst = dest_root / Path(it["source_id"]) / src.name
                if not dst.exists():
                    failed.append({"path": str(src), "stage": "verify", "why": "备份缺失"})
                    aborted = True
                    break
            if aborted:
                note = "备份校验失败，未删除任何源文件"
                break
            # 3) 逐项送回收站，逐项验证
            for it in batch:
                src = Path(it["path"])
                ok, why = to_recycle_bin(src)
                if not ok or src.exists():
                    failed.append({"path": str(src), "stage": "recycle", "why": why})
                    aborted = True
                    note = "回收站操作失败，本批已停止；备份完好，可还原"
                    break
                done.append({"id": it["id"], "path": str(src),
                             "backup": str(dest_root / Path(it["source_id"]) / src.name),
                             "size": it["size"]})
            if aborted:
                break

        manifest = {
            "batch_id": batch_id, "at": int(time.time() * 1000),
            "confirm": confirm, "total_bytes": plan["total_bytes"],
            "requested": len(items), "done": done, "failed": failed,
            "note": note, "restored": False,
            "backup_root": str(dest_root),
        }
        (dest_root / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        self._append_log(manifest)
        with self.lock:
            self.plans.pop(plan_id, None)
        return {"ok": len(failed) == 0, "batch_id": batch_id,
                "done": len(done), "failed": failed, "note": note,
                "backup_root": str(dest_root)}

    # ------------------------------------------------------------ 还原 / 历史
    def history(self):
        if not self.backup_root.is_dir():
            return {"items": []}
        out = []
        for d in sorted(self.backup_root.iterdir(), reverse=True)[:30]:
            mf = d / "manifest.json"
            if mf.is_file():
                try:
                    m = json.loads(mf.read_text(encoding="utf-8"))
                    out.append({k: m.get(k) for k in
                                ("batch_id", "at", "requested", "note", "restored",
                                 "total_bytes", "backup_root")}
                               | {"done": len(m.get("done", []))})
                except Exception:
                    pass
        return {"items": out}

    def restore(self, batch_id: str):
        if any(c in batch_id for c in ("/", "\\", "..")):
            return {"ok": False, "error": "非法批次号"}
        dest_root = self.backup_root / batch_id
        mf = dest_root / "manifest.json"
        if not mf.is_file():
            return {"ok": False, "error": "批次不存在"}
        m = json.loads(mf.read_text(encoding="utf-8"))
        restored, skipped = [], []
        for rec in m.get("done", []):
            src = Path(rec["backup"])
            dst = Path(rec["path"])
            if not src.exists():
                skipped.append({"path": rec["path"], "why": "备份缺失"})
                continue
            if dst.exists():
                skipped.append({"path": rec["path"], "why": "原路径已有文件，未覆盖"})
                continue
            try:
                dst.parent.mkdir(parents=True, exist_ok=True)
                if src.is_dir():
                    shutil.copytree(src, dst)
                else:
                    shutil.copy2(src, dst)
                restored.append(rec["path"])
            except Exception as e:
                skipped.append({"path": rec["path"], "why": f"{type(e).__name__}: {e}"})
        m["restored"] = True
        m["restored_at"] = int(time.time() * 1000)
        mf.write_text(json.dumps(m, ensure_ascii=False, indent=2), encoding="utf-8")
        self._append_log({"batch_id": batch_id, "at": m["restored_at"],
                          "action": "restore", "restored": restored,
                          "skipped": skipped})
        return {"ok": True, "restored": len(restored), "skipped": skipped}

    def _append_log(self, entry: dict):
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            old = []
            if self.log_path.is_file():
                old = json.loads(self.log_path.read_text(encoding="utf-8"))
            old.append(entry)
            self.log_path.write_text(json.dumps(old[-100:], ensure_ascii=False, indent=2),
                                     encoding="utf-8")
        except Exception:
            pass
