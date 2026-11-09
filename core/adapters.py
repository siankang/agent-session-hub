"""来源适配器注册表。

设计原则：基于本机真实探测结果配置驱动；解析失败只降级、不中断；
能解析对话的标 full，只有会话清单的标 meta，只有目录体量的标 aggregate。
全程只读源文件。
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

SKIP_DIRS = {
    "node_modules", ".git", "cache", "caches", "gpucache", "code cache",
    "blob_storage", "crashpad", "service worker", "shadercache", "dawn-cache",
    "cacheddata", "cachedextensions", "cachedconfigurations", "clp",
    "bin", "lib", "locales", "resources", "swiftshader", ".venv", "venv",
    "site-packages", "__pycache__", "cef", "webview", "gpu", "shader",
}
MAX_WALK_FILES = 20000
MAX_SESSION_BYTES = 80 * 1024 * 1024
MAX_MESSAGES_PER_SESSION = 6000
MAX_TEXT_CHARS = 20000


@dataclass
class Source:
    id: str
    name: str
    vendor: str
    kind: str                       # CLI / IDE / 桌面 / 云
    roots: list[str]                # 相对用户主目录
    color: str
    parser: str = "aggregate"       # jsonl | sqlite | vscdb | jsonmeta | aggregate
    level: str = "aggregate"        # full / meta / aggregate
    note: str = ""
    glob: str = "**/*.jsonl"        # jsonl 解析器用
    flavor: str = "claude"          # claude | workbuddy | codex | qoder | catpaw | auto
    exclude: tuple[str, ...] = field(default_factory=tuple)
    db: str = ""                    # sqlite/vscdb 解析器用（相对 root）
    key_pattern: str = ""           # vscdb 解析器用
    meta_hint: str = ""             # 附加说明


def S(*a, **kw) -> Source:
    return Source(*a, **kw)


# ---------------------------------------------------------------- 来源注册表
SOURCES: list[Source] = [
    S("workbuddy", "WorkBuddy", "腾讯", "桌面", [".workbuddy"], "#4f8cff",
      parser="workbuddy", level="full",
      note="主库 workbuddy.db 存会话清单，projects/*/*.jsonl 存逐条消息"),
    S("claude-code", "Claude Code", "Anthropic", "CLI", [".claude/projects"], "#d97757",
      parser="jsonl", level="full", glob="**/*.jsonl", flavor="claude",
      exclude=("subagents", "plugins", "skills"),
      note="projects/<项目>/*.jsonl，每行一条 user/assistant 消息"),
    S("codex-cli", "Codex CLI", "OpenAI", "CLI", [".codex"], "#10a37f",
      parser="codex", level="full", glob="**/*.jsonl", flavor="codex",
      exclude=("memories", "process_manager"),
      note="sessions/YYYY/MM/DD/rollout-*.jsonl + session_index.jsonl + thread_history_1.sqlite"),
    S("qoder-cn", "Qoder CN (CLI)", "阿里", "CLI", [".qoder-cn/projects"], "#7c5cff",
      parser="jsonl", level="full", glob="**/*.jsonl", flavor="qoder",
      exclude=("memories",), note="Claude 系 JSONL 变体，带 humanInput.text"),
    S("catpaw", "CatPaw / 美团", "美团", "CLI", [".catpaw/projects"], "#ff8f3c",
      parser="jsonl", level="full", glob="**/*.jsonl", flavor="catpaw",
      note="message.content 为数组，conversationId 标识会话"),
    S("autoclaw", "OpenClaw / AutoClaw", "社区", "CLI",
      [".openclaw-autoclaw"], "#22b8a6", parser="jsonl", level="full",
      glob="**/*.jsonl", flavor="auto",
      exclude=("skills", "workspace", "trajectory"),
      note="agents/main/sessions/*.jsonl 与 chat-history/*.json"),
    S("claude-desktop", "Claude Desktop 本地 Agent", "Anthropic", "桌面",
      ["AppData/Local/Claude-3p"], "#d97757", parser="jsonmeta", level="meta",
      glob="**/local_*.json", note="local_*.json 含 title/cwd/createdAt 等会话元数据"),
    S("codebuddy-cn", "CodeBuddy CN", "腾讯", "IDE",
      ["AppData/Roaming/CodeBuddy CN"], "#2f7cf6", parser="vscdb", level="meta",
      db="codebuddy-sessions.vscdb", key_pattern="session:",
      note="codebuddy-sessions.vscdb 的 ItemTable 存 session:* 清单（含 title/cwd）"),
    S("codebuddy-old", "CodeBuddy（旧版）", "腾讯", "IDE",
      [".codebuddy", "AppData/Roaming/CodeBuddy"], "#5b8def"),
    S("qoder-ide", "QoderCN IDE", "阿里", "IDE",
      ["AppData/Roaming/QoderCN", ".qoder"], "#8b5cf6", parser="vscdb_meta", level="meta",
      key_pattern="aicoding-chat-",
      note="state.vscdb 中 aicoding-chat-*.state 为会话状态条目"),
    S("qoder-cli", "Qoder CLI", "阿里", "CLI", [".qoder-cli"], "#6d5dfc",
      note="ai-stats 为 AI 编辑统计，不是对话记录"),
    S("trae-solo", "TRAE SOLO CN", "字节", "IDE",
      ["AppData/Roaming/TRAE SOLO CN"], "#3b82f6", parser="vscdb_meta", level="meta",
      key_pattern="chat.ChatSessionStore.index"),
    S("trae-cn", "Trae CN", "字节", "IDE",
      ["AppData/Roaming/Trae CN"], "#60a5fa", parser="vscdb_meta", level="meta",
      key_pattern="chat.ChatSessionStore.index"),
    S("devin", "Devin / Windsurf 桌面", "Cognition", "IDE",
      ["AppData/Roaming/Devin", ".devin", ".devin-shared"], "#a78bfa",
      parser="devin", level="meta",
      note="cli/sessions.db 有 sessions/message_nodes 表；acp-messages/*.db 存消息"),
    S("xiami-mimo", "Xiaomi MiMo", "小米", "桌面",
      ["AppData/Roaming/Xiaomi MiMo"], "#ff6900", parser="sqlite_meta", level="meta",
      db="db/rolechat.db", note="rolechat.db 的 role_chat_record 表"),
    S("dsh", "DSH NEXT", "—", "桌面", [".dsh"], "#0ea5e9",
      parser="jsonmeta", level="meta", glob="**/session-*.json",
      note="storages/session_projcache/sessions/ 下的会话缓存"),
    S("qwenworkcn", "千问办公", "阿里", "桌面",
      ["AppData/Roaming/QwenWorkCN", ".qwenworkcn"], "#f59e0b",
      parser="jsonmeta", level="meta", glob="**/session-*.json",
      note="session-drafts.json 存会话草稿"),
    S("opencode", "OpenCode Desktop", "社区", "IDE",
      ["AppData/Roaming/ai.opencode.desktop"], "#22c55e",
      note="LevelDB / SQLite 混合存储，对话内容未解析"),
    S("zcode", "ZCode / Pixmind", "—", "桌面",
      ["AppData/Roaming/ZCode", ".pixmind", "AppData/Roaming/pixmind-builder"], "#f472b6",
      note="主要为 Electron LevelDB 与运行日志，对话内容未解析"),
    S("grok-desktop", "Grok Desktop", "xAI", "桌面",
      ["AppData/Roaming/Grok-Desktop", ".grok"], "#94a3b8",
      note="LevelDB 存储，对话内容未解析"),
    S("qwenpaw", "QwenPaw Desktop", "阿里", "桌面", [".qwenpaw", ".qwen-agent"], "#fbbf24"),
    S("codexpp", "Codex++", "社区", "桌面",
      ["AppData/Roaming/Codex++", "AppData/Local/Codex"], "#34d399"),
    S("cc-switch", "CC Switch", "社区", "工具", [".cc-switch"], "#64748b",
      note="Claude Code 配置切换器，本身不存对话"),
    S("coze", "Coze", "字节", "桌面",
      ["AppData/Roaming/Coze", ".coze"], "#4f46e5"),
    S("doubao", "豆包", "字节", "桌面",
      ["AppData/Roaming/Doubao", "AppData/Local/Doubao"], "#38bdf8",
      note="对话主要保存在云端，本地仅缓存"),
    S("dreamina", "即梦 AI", "字节", "桌面",
      ["AppData/Roaming/dreamina-desktop"], "#ec4899",
      note="创作记录主要保存在云端"),
    S("deepseek", "DeepSeek", "深度求索", "桌面", [".deepseek"], "#4d6bfe"),
    S("hermes", "Hermes", "Nous Research", "桌面",
      ["AppData/Local/hermes"], "#a3e635"),
    S("aiwork", "AI 工作助手", "—", "桌面",
      ["AppData/Roaming/AIWorkAssistant", "AppData/Local/com.aiwork.assistant"], "#14b8a6"),
    S("wbswitch", "WorkBuddy switch", "—", "工具",
      ["AppData/Local/com.wbswitch.app"], "#3b82f6"),
    S("kimi", "Kimi 桌面 / WebBridge", "月之暗面", "桌面",
      ["AppData/Roaming/kimi-desktop", ".kimi-work", ".kimi-webbridge"], "#0ea5e9"),
    S("codebuddycn-home", "CodeBuddy CN 用户目录", "腾讯", "IDE",
      [".codebuddycn"], "#3b82f6"),
]


# ---------------------------------------------------------------- 通用工具
def parse_ts(v) -> int:
    """把各种时间表示归一化成 unix 毫秒。"""
    if v is None or v == "":
        return 0
    if isinstance(v, bool):
        return 0
    if isinstance(v, (int, float)):
        n = int(v)
        return n * 1000 if n < 10_000_000_000 else (n if n < 10_000_000_000_000 else n // 1000)
    if isinstance(v, str):
        s = v.strip().replace("Z", "+00:00")
        if s.isdigit():
            return parse_ts(int(s))
        s = re.sub(r"(\.\d{6})\d+", r"\1", s)  # Python 只接受 6 位小数
        try:
            dt = datetime.fromisoformat(s)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return int(dt.timestamp() * 1000)
        except Exception:
            return 0
    return 0


def flatten(v) -> str:
    """把各家 content（str / list[block] / dict）压成纯文本。"""
    if v is None:
        return ""
    if isinstance(v, str):
        return v
    if isinstance(v, (int, float, bool)):
        return str(v)
    if isinstance(v, list):
        parts = []
        for x in v:
            t = flatten(x)
            if t:
                parts.append(t)
        return "\n".join(parts)
    if isinstance(v, dict):
        typ = str(v.get("type", ""))
        if typ in ("tool_use", "tool_result", "thinking", "redacted_thinking", "image"):
            return ""
        for key in ("text", "input_text", "output_text", "content", "value"):
            if key in v:
                return flatten(v[key])
        return ""
    return ""


def safe_str(v, limit=MAX_TEXT_CHARS) -> str:
    s = v if isinstance(v, str) else ""
    return s[:limit]


def iter_jsonl(path: Path):
    try:
        f = path.open("r", encoding="utf-8", errors="replace")
    except OSError:
        return
    with f:
        for line in f:
            line = line.strip()
            if not line or len(line) > 4_000_000:
                continue
            try:
                d = json.loads(line)
            except Exception:
                continue
            if isinstance(d, dict):
                yield d


def walk(root: Path, max_depth=5, limit=MAX_WALK_FILES):
    if not root.is_dir():
        return
    n = 0
    for cur, dirs, files in os.walk(root, topdown=True):
        dirs[:] = [d for d in dirs
                   if d.lower() not in SKIP_DIRS and not d.startswith(".")]
        if len(Path(cur).relative_to(root).parts) >= max_depth:
            dirs[:] = []
        for name in files:
            n += 1
            if n > limit:
                return
            p = Path(cur) / name
            try:
                yield p, p.stat()
            except OSError:
                continue


def dir_size(root: Path):
    files, size, latest = 0, 0, 0
    for _, st in walk(root, max_depth=6):
        files += 1
        size += st.st_size
        latest = max(latest, int(st.st_mtime * 1000))
    return files, size, latest


def make_title(text: str, fallback: str) -> str:
    t = " ".join((text or "").split())[:70].strip()
    return t or fallback


def is_system_text(text: str) -> bool:
    """过滤掉被塞进首条「用户消息」的系统注入，避免它变成会话标题。"""
    t = (text or "").lstrip()[:120].lower()
    return (t.startswith("<environment_context") or t.startswith("<system-reminder")
            or t.startswith("<command-name>") or t.startswith("<local-command")
            or t.startswith("# valid channels") or "<system-reminder>" in t)


# ---------------------------------------------------------------- 消息提取
def extract_line(d: dict, flavor: str):
    """返回 (role, ts_ms, kind, text, cwd, extra_title)。非消息行返回 None。"""
    typ = str(d.get("type", ""))
    cwd = d.get("cwd") or ""

    if flavor == "workbuddy":
        if typ == "ai-title":
            return ("_title", 0, "meta", flatten(d.get("content") or d.get("title")), "", "")
        if typ == "session-meta":
            return ("_meta", parse_ts(d.get("timestamp")), "meta", "", str(cwd), "")
        if typ != "message":
            return None
        role = str(d.get("role") or "assistant")
        text = flatten(d.get("content"))
        if not text:
            return None
        return (role, parse_ts(d.get("timestamp")), "message", safe_str(text), str(cwd), "")

    if flavor == "codex":
        if typ == "session_meta":
            p = d.get("payload") or {}
            return ("_meta", parse_ts(d.get("timestamp")), "meta", "",
                    str(p.get("cwd") or ""), "")
        if typ != "response_item":
            return None
        p = d.get("payload") or {}
        if str(p.get("type", "")) != "message":
            return None
        role = str(p.get("role") or "assistant")
        text = flatten(p.get("content"))
        if not text:
            return None
        return (role, parse_ts(d.get("timestamp")), "message", safe_str(text), "", "")

    # Claude / Qoder / CatPaw / 通用
    msg = d.get("message") if isinstance(d.get("message"), dict) else None
    role = ""
    content = None
    if typ in ("user", "assistant"):
        role = typ
        content = (msg or {}).get("content")
    elif msg and str(msg.get("type", "")) in ("user", "assistant"):
        role = str(msg.get("role") or msg.get("type"))
        content = msg.get("content")
    elif "role" in d and ("content" in d or "text" in d):
        role = str(d.get("role"))
        content = d.get("content", d.get("text"))
    else:
        return None
    text = flatten(content)
    if not text and isinstance(d.get("humanInput"), dict):
        text = flatten(d["humanInput"].get("text"))
    if not text and isinstance(d.get("text"), str):
        text = d["text"]
    if not text:
        return None
    ts = parse_ts(d.get("timestamp") or d.get("ts") or d.get("createdAt"))
    return (role, ts, "message", safe_str(text), str(cwd), "")


def parse_session_file(path: Path, flavor: str, source_id: str, native_id: str,
                       title_map: dict | None = None, project: str = "", fallback_title="",
                       session_key: str = ""):
    """一次流式读取，产出会话 stub 与消息元组。"""
    try:
        st = path.stat()
    except OSError:
        return None, []
    key = session_key or f"{source_id}:{native_id}"
    if st.st_size > MAX_SESSION_BYTES:
        # 超限不静默丢弃：降级成「仅清单」条目，让用户知道它存在但没解析
        return ({
            "id": key, "source_id": source_id, "native_id": native_id,
            "title": make_title(str(fallback_title or (title_map or {}).get(native_id) or ""), native_id),
            "project": project, "cwd": "",
            "created_at": int(st.st_ctime * 1000), "updated_at": int(st.st_mtime * 1000),
            "message_count": 0, "size_bytes": st.st_size, "file_path": str(path),
            "level": "meta",
            "preview": f"文件超过 {MAX_SESSION_BYTES // 1048576} MB 上限，未解析对话正文",
            "meta": {"truncated": True},
        }, [])
    title_map = title_map or {}
    msgs, roles_seen = [], 0
    first_user = ""
    ai_title = ""
    cwd = ""
    first_ts, last_ts = 0, 0
    for d in iter_jsonl(path):
        r = extract_line(d, flavor)
        if not r:
            continue
        role, ts, kind, text, c, _ = r
        if c and not cwd:
            cwd = c
        if role == "_meta":
            if ts and not first_ts:
                first_ts = ts
            continue
        if role == "_title":
            ai_title = make_title(text, "")
            continue
        if ts:
            first_ts = first_ts or ts
            last_ts = max(last_ts, ts)
        roles_seen += 1
        if role == "user" and not first_user and not is_system_text(text):
            first_user = make_title(text, "")
        if len(msgs) < MAX_MESSAGES_PER_SESSION:
            msgs.append((key, len(msgs), role, ts, kind, text))
    if not msgs and not title_map.get(native_id):
        return None, []
    title = title_map.get(native_id) or ai_title or first_user or fallback_title or native_id
    created = first_ts or int(st.st_ctime * 1000)
    updated = last_ts or int(st.st_mtime * 1000)
    preview = ""
    for m in msgs:
        if m[2] == "user":
            preview = m[5][:180]
            break
    if not preview and msgs:
        preview = msgs[0][5][:180]
    stub = {
        "id": key, "source_id": source_id, "native_id": native_id,
        "title": make_title(str(title), native_id), "project": project, "cwd": cwd,
        "created_at": created, "updated_at": updated, "message_count": len(msgs),
        "size_bytes": st.st_size, "file_path": str(path), "level": "full",
        "preview": preview, "meta": {},
    }
    return stub, msgs


# ---------------------------------------------------------------- 元数据读取
def sqlite_rows(path: Path, table: str = "", key_pattern: str = ""):
    """只读打开 sqlite/vscdb，返回 (表名, 列名, 行列表)。"""
    if not path.is_file():
        return []
    try:
        con = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=2)
    except Exception:
        return []
    try:
        names = [r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' LIMIT 40")]
        out = []
        for t in names:
            if table and t != table:
                continue
            try:
                cols = [r[1] for r in con.execute(f'PRAGMA table_info("{t}")')]
                if key_pattern and "key" in cols:
                    rows = con.execute(
                        f'SELECT * FROM "{t}" WHERE CAST(key AS TEXT) LIKE ? LIMIT 500',
                        (f"%{key_pattern}%",)).fetchall()
                else:
                    rows = con.execute(f'SELECT * FROM "{t}" LIMIT 500').fetchall()
                out.append((t, cols, rows))
            except Exception:
                continue
        return out
    finally:
        con.close()


def workbuddy_title_map(home: Path):
    out = {}
    p = home / ".workbuddy/workbuddy.db"
    for t, cols, rows in sqlite_rows(p, table="sessions"):
        for r in rows:
            d = dict(zip(cols, r))
            sid = str(d.get("id") or "")
            if sid:
                out[sid] = {
                    "title": d.get("custom_title") or d.get("title") or "",
                    "cwd": d.get("cwd") or "",
                    "created_at": parse_ts(d.get("created_at")),
                    "updated_at": parse_ts(d.get("updated_at") or d.get("last_activity_at")),
                }
    return out


def codex_title_map(home: Path):
    out = {}
    p = home / ".codex/session_index.jsonl"
    for d in iter_jsonl(p):
        sid = str(d.get("id") or "")
        if sid:
            out[sid] = {"title": d.get("thread_name") or "",
                        "updated_at": parse_ts(d.get("updated_at"))}
    return out


def jsonmeta_sessions(root: Path, source: Source):
    """从 local_*.json / session-*.json 之类的元数据文件提取会话清单。"""
    out = []
    for p in sorted(root.glob(source.glob)):
        try:
            if p.stat().st_size > 4_000_000:
                continue
            d = json.loads(p.read_text(encoding="utf-8", errors="replace"))
        except Exception:
            continue
        if not isinstance(d, dict):
            continue
        sid = (d.get("sessionId") or d.get("id") or d.get("conversationId")
               or p.stem)
        title = (d.get("title") or d.get("name") or d.get("custom_title")
                 or d.get("purpose") or "")
        created = parse_ts(d.get("createdAt") or d.get("created_at")
                           or d.get("sessionStartedAt"))
        updated = parse_ts(d.get("lastActivityAt") or d.get("updatedAt")
                           or d.get("updated_at") or d.get("last_opened_at"))
        out.append({
            "id": f"{source.id}:{sid}", "source_id": source.id, "native_id": str(sid),
            "title": make_title(str(title), p.stem), "project": "",
            "cwd": str(d.get("cwd") or d.get("workspace") or ""),
            "created_at": created, "updated_at": updated or int(p.stat().st_mtime * 1000),
            "message_count": 0, "size_bytes": p.stat().st_size,
            "file_path": str(p), "level": "meta",
            "preview": make_title(str(d.get("initialMessage") or d.get("task") or ""), ""),
            "meta": {"model": d.get("model"), "status": d.get("status")},
        })
    return out


def vscdb_sessions(root: Path, source: Source):
    """从 codebuddy-sessions.vscdb 的 session:* 提取会话清单。"""
    out = []
    p = root / source.db if source.db else None
    candidates = [p] if p and p.is_file() else []
    if source.parser == "vscdb_meta":
        candidates = list(root.glob("User/globalStorage/state.vscdb")) + \
                     list(root.glob("User/workspaceStorage/*/state.vscdb"))
    for db in candidates[:40]:
        for t, cols, rows in sqlite_rows(db, key_pattern=source.key_pattern):
            for r in rows:
                d = dict(zip(cols, r))
                key = str(d.get("key") or "")
                raw = d.get("value")
                if isinstance(raw, bytes):
                    try:
                        raw = raw.decode("utf-8")
                    except Exception:
                        continue
                try:
                    v = json.loads(raw) if isinstance(raw, str) else None
                except Exception:
                    v = None
                if isinstance(v, dict) and ("title" in v or "conversationId" in v
                                            or "createdAt" in v or "updatedAt" in v):
                    sid = str(v.get("conversationId") or v.get("id") or key.split(":")[-1])
                    out.append({
                        "id": f"{source.id}:{sid}", "source_id": source.id, "native_id": sid,
                        "title": make_title(str(v.get("title") or v.get("name") or ""), sid[:12]),
                        "project": "", "cwd": str(v.get("cwd") or ""),
                        "created_at": parse_ts(v.get("createdAt")),
                        "updated_at": parse_ts(v.get("updatedAt")) or int(db.stat().st_mtime * 1000),
                        "message_count": 0, "size_bytes": db.stat().st_size,
                        "file_path": str(db), "level": "meta", "preview": "",
                        "meta": {"key": key, "status": v.get("status")},
                    })
                elif isinstance(v, dict) and "entries" in v and isinstance(v["entries"], dict):
                    for sid, sv in list(v["entries"].items())[:500]:
                        if not isinstance(sv, dict):
                            continue
                        out.append({
                            "id": f"{source.id}:{sid}", "source_id": source.id,
                            "native_id": str(sid),
                            "title": make_title(str(sv.get("title") or sv.get("name") or ""), sid[:12]),
                            "project": "", "cwd": str(sv.get("cwd") or ""),
                            "created_at": parse_ts(sv.get("createdAt")),
                            "updated_at": parse_ts(sv.get("updatedAt")) or int(db.stat().st_mtime * 1000),
                            "message_count": 0, "size_bytes": 0, "file_path": str(db),
                            "level": "meta", "preview": "", "meta": {"key": key},
                        })
    # 去重
    seen, uniq = set(), []
    for s in out:
        if s["id"] not in seen:
            seen.add(s["id"])
            uniq.append(s)
    return uniq


def sqlite_meta_sessions(root: Path, source: Source):
    out = []
    p = root / source.db if source.db else None
    if not p or not p.is_file():
        return out
    for t, cols, rows in sqlite_rows(p):
        if not any(c in cols for c in ("session_id", "id", "title")):
            continue
        if t in ("schema_migrations", "_sqlx_migrations", "sqlite_sequence",
                 "refinery_schema_history"):
            continue
        for r in rows:
            d = dict(zip(cols, r))
            sid = str(d.get("session_id") or d.get("id") or "")
            if not sid:
                continue
            out.append({
                "id": f"{source.id}:{sid}", "source_id": source.id, "native_id": sid,
                "title": make_title(str(d.get("title") or d.get("name") or d.get("purpose") or ""), sid[:12]),
                "project": "", "cwd": str(d.get("working_directory") or d.get("workspace") or ""),
                "created_at": parse_ts(d.get("created_at")),
                "updated_at": parse_ts(d.get("last_activity_at") or d.get("updated_at")),
                "message_count": 0, "size_bytes": 0, "file_path": str(p),
                "level": "meta", "preview": make_title(str(d.get("task") or ""), ""),
                "meta": {"table": t},
            })
    return out


def devin_sessions(root: Path, source: Source):
    p = root / "cli/sessions.db"
    out = []
    for t, cols, rows in sqlite_rows(p, table="sessions"):
        for r in rows:
            d = dict(zip(cols, r))
            sid = str(d.get("id") or "")
            if not sid:
                continue
            out.append({
                "id": f"{source.id}:{sid}", "source_id": source.id, "native_id": sid,
                "title": make_title(str(d.get("title") or ""), sid[:12]),
                "project": "", "cwd": str(d.get("working_directory") or ""),
                "created_at": parse_ts(d.get("created_at")),
                "updated_at": parse_ts(d.get("last_activity_at")),
                "message_count": 0, "size_bytes": 0, "file_path": str(p),
                "level": "meta", "preview": "", "meta": {"model": d.get("model")},
            })
    return out


# ---------------------------------------------------------------- 资产聚合
def assets_for(root: Path, source_id: str, limit=12):
    """把根目录下的主要子目录当作数据资产，供占用分析使用。"""
    if not root.is_dir():
        return []
    buckets: dict[str, list] = {}
    for p, st in walk(root, max_depth=3):
        rel = p.relative_to(root).as_posix()
        parts = rel.split("/")
        key = "/".join(parts[:2]) if len(parts) > 1 else (parts[0] if len(parts) == 1 else ".")
        b = buckets.setdefault(key, [0, 0, 0])
        b[0] += 1
        b[1] += st.st_size
        b[2] = max(b[2], int(st.st_mtime * 1000))
    out = []
    for k, (n, size, latest) in sorted(buckets.items(), key=lambda kv: -kv[1][1])[:limit]:
        out.append({"id": f"{source_id}:{k}", "path": k, "files": n,
                    "bytes": size, "latest_mtime": latest,
                    "note": str(root / k)})
    return out
