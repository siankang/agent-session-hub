"""本地索引库：只写项目目录内的 data/index.db，绝不碰任何源数据。"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;

CREATE TABLE IF NOT EXISTS sources (
  id TEXT PRIMARY KEY, name TEXT NOT NULL, vendor TEXT, kind TEXT,
  level TEXT, root TEXT, color TEXT, note TEXT,
  enabled INTEGER DEFAULT 1, detected INTEGER DEFAULT 0,
  file_count INTEGER DEFAULT 0, total_bytes INTEGER DEFAULT 0,
  session_count INTEGER DEFAULT 0, message_count INTEGER DEFAULT 0,
  last_scan_at INTEGER, error TEXT
);

CREATE TABLE IF NOT EXISTS sessions (
  id TEXT PRIMARY KEY, source_id TEXT NOT NULL, native_id TEXT,
  title TEXT, project TEXT, cwd TEXT,
  created_at INTEGER DEFAULT 0, updated_at INTEGER DEFAULT 0,
  message_count INTEGER DEFAULT 0, size_bytes INTEGER DEFAULT 0,
  file_path TEXT, level TEXT, preview TEXT, meta TEXT
);
CREATE INDEX IF NOT EXISTS idx_sess_updated ON sessions(updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_sess_source ON sessions(source_id);
CREATE INDEX IF NOT EXISTS idx_sess_title ON sessions(title);

CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY, session_id TEXT NOT NULL, seq INTEGER,
  role TEXT, ts INTEGER, kind TEXT, text TEXT
);
CREATE INDEX IF NOT EXISTS idx_msg_session ON messages(session_id, seq);

CREATE TABLE IF NOT EXISTS assets (
  id TEXT PRIMARY KEY, source_id TEXT, path TEXT, files INTEGER,
  bytes INTEGER, latest_mtime INTEGER, note TEXT
);

CREATE TABLE IF NOT EXISTS scan_state (k TEXT PRIMARY KEY, v TEXT);
"""

FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
  text, content='messages', content_rowid='id', tokenize='unicode61'
);
CREATE TRIGGER IF NOT EXISTS msg_ai AFTER INSERT ON messages BEGIN
  INSERT INTO messages_fts(rowid, text) VALUES (new.id, new.text);
END;
CREATE TRIGGER IF NOT EXISTS msg_ad AFTER DELETE ON messages BEGIN
  INSERT INTO messages_fts(messages_fts, rowid, text) VALUES('delete', old.id, old.text);
END;
"""


class Store:
    def __init__(self, db_path: Path):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.path = db_path
        self.lock = threading.RLock()
        self.db = sqlite3.connect(str(db_path), check_same_thread=False, timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.has_fts = True
        try:
            self.db.executescript(FTS_SCHEMA)
        except sqlite3.Error:
            self.has_fts = False
        self.db.commit()

    # ---- 基础 ----
    def execute(self, sql, args=()):
        with self.lock:
            return self.db.execute(sql, args)

    def executemany(self, sql, rows):
        with self.lock:
            self.db.executemany(sql, rows)

    def commit(self):
        with self.lock:
            self.db.commit()

    def query(self, sql, args=()):
        with self.lock:
            return [dict(r) for r in self.db.execute(sql, args).fetchall()]

    def one(self, sql, args=()):
        with self.lock:
            r = self.db.execute(sql, args).fetchone()
            return dict(r) if r else None

    def set_state(self, key, value):
        with self.lock:
            self.db.execute(
                "INSERT INTO scan_state(k,v) VALUES(?,?) "
                "ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                (key, json.dumps(value, ensure_ascii=False)),
            )
            self.db.commit()

    def get_state(self, key, default=None):
        r = self.one("SELECT v FROM scan_state WHERE k=?", (key,))
        if not r:
            return default
        try:
            return json.loads(r["v"])
        except Exception:
            return default

    # ---- 扫描写入 ----
    def reset_source(self, source_id: str):
        """清掉某来源的旧索引（只清本工具的索引库）。

        FTS 是外部内容表，msg_ad 触发器会在 DELETE 时自动同步；
        绝不能再手动发一次 'delete' 命令，否则同一 rowid 被删两次会损坏索引。
        """
        with self.lock:
            self.db.execute("DELETE FROM messages WHERE session_id IN "
                            "(SELECT id FROM sessions WHERE source_id=?)", (source_id,))
            self.db.execute("DELETE FROM sessions WHERE source_id=?", (source_id,))
            self.db.execute("DELETE FROM assets WHERE source_id=?", (source_id,))
            self.db.commit()

    def upsert_source(self, s: dict):
        cols = ("id", "name", "vendor", "kind", "level", "root", "color", "note",
                "enabled", "detected", "file_count", "total_bytes",
                "session_count", "message_count", "last_scan_at", "error")
        vals = tuple(s.get(c) for c in cols)
        ph = ",".join("?" for _ in cols)
        upd = ",".join(f"{c}=excluded.{c}" for c in cols if c != "id")
        with self.lock:
            self.db.execute(f"INSERT INTO sources({','.join(cols)}) VALUES({ph}) "
                            f"ON CONFLICT(id) DO UPDATE SET {upd}", vals)

    def save_sessions(self, source_id: str, sessions: list[dict], messages: list[tuple]):
        """整批替换某来源的索引，使用单事务保证一致性。"""
        with self.lock:
            self.db.execute("BEGIN")
            try:
                # msg_ad 触发器自动同步 FTS 删除，不可重复手动删除
                self.db.execute("DELETE FROM messages WHERE session_id IN "
                                "(SELECT id FROM sessions WHERE source_id=?)", (source_id,))
                self.db.execute("DELETE FROM sessions WHERE source_id=?", (source_id,))
                self.db.execute("DELETE FROM assets WHERE source_id=?", (source_id,))
                self.db.executemany(
                    "INSERT OR REPLACE INTO sessions"
                    "(id,source_id,native_id,title,project,cwd,created_at,updated_at,"
                    " message_count,size_bytes,file_path,level,preview,meta) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    [(s["id"], s["source_id"], s.get("native_id"), s.get("title"),
                      s.get("project"), s.get("cwd"), s.get("created_at", 0),
                      s.get("updated_at", 0), s.get("message_count", 0),
                      s.get("size_bytes", 0), s.get("file_path"), s.get("level"),
                      s.get("preview"), json.dumps(s.get("meta") or {}, ensure_ascii=False))
                     for s in sessions],
                )
                self.db.executemany(
                    "INSERT INTO messages(session_id,seq,role,ts,kind,text) VALUES(?,?,?,?,?,?)",
                    messages,
                )
                self.db.execute("UPDATE sources SET session_count=?, message_count=?, "
                                "last_scan_at=?, error=NULL WHERE id=?",
                                (len(sessions), len(messages), int(time.time() * 1000), source_id))
                self.db.commit()
            except Exception:
                self.db.rollback()
                raise

    def save_assets(self, source_id: str, assets: list[dict]):
        with self.lock:
            self.db.executemany(
                "INSERT OR REPLACE INTO assets(id,source_id,path,files,bytes,latest_mtime,note) "
                "VALUES(?,?,?,?,?,?,?)",
                [(a["id"], source_id, a["path"], a["files"], a["bytes"],
                  a["latest_mtime"], a.get("note")) for a in assets],
            )
            self.db.commit()

    # ---- 查询 ----
    def overview(self):
        s = self.one("SELECT COUNT(*) n, COALESCE(SUM(message_count),0) m, "
                     "COALESCE(SUM(size_bytes),0) b, MIN(NULLIF(created_at,0)) mn, "
                     "MAX(updated_at) mx FROM sessions")
        src = self.one("SELECT COUNT(*) n, COALESCE(SUM(total_bytes),0) b, "
                       "COALESCE(SUM(file_count),0) f FROM sources WHERE detected=1")
        msg = self.one("SELECT COUNT(*) n FROM messages")
        return {
            "sources": src["n"] if src else 0,
            "data_bytes": src["b"] if src else 0,
            "data_files": src["f"] if src else 0,
            "sessions": s["n"] if s else 0,
            "messages_indexed": msg["n"] if msg else 0,
            "messages_total": s["m"] if s else 0,
            "session_bytes": s["b"] if s else 0,
            "first_at": (s or {}).get("mn") or 0,
            "last_at": (s or {}).get("mx") or 0,
            "has_fts": self.has_fts,
            "last_scan": self.get_state("last_scan"),
        }

    def sources(self):
        return self.query(
            "SELECT s.*, COUNT(DISTINCT a.id) asset_count FROM sources s "
            "LEFT JOIN assets a ON a.source_id=s.id GROUP BY s.id "
            "ORDER BY s.detected DESC, s.session_count DESC, s.total_bytes DESC, s.name"
        )

    def sessions(self, *, source=None, q=None, page=1, size=30, sort="updated", days=0):
        where, args = ["1=1"], []
        if source:
            where.append("source_id=?")
            args.append(source)
        if q:
            where.append("(title LIKE ? OR project LIKE ? OR preview LIKE ? OR cwd LIKE ?)")
            like = f"%{q}%"
            args += [like, like, like, like]
        if days and days > 0:
            import time as _t
            where.append("updated_at>=?")
            args.append(int(_t.time() * 1000) - days * 86400000)
        w = " AND ".join(where)
        order = {"updated": "updated_at DESC", "created": "created_at DESC",
                 "size": "size_bytes DESC", "messages": "message_count DESC",
                 "title": "title ASC"}.get(sort, "updated_at DESC")
        total = self.one(f"SELECT COUNT(*) n FROM sessions WHERE {w}", args)["n"]
        rows = self.query(
            f"SELECT * FROM sessions WHERE {w} ORDER BY {order} LIMIT ? OFFSET ?",
            args + [size, (page - 1) * size],
        )
        return {"items": rows, "total": total, "page": page, "size": size}

    def session(self, sid, offset=0, limit=120):
        s = self.one("SELECT * FROM sessions WHERE id=?", (sid,))
        if not s:
            return None
        total = self.one("SELECT COUNT(*) n FROM messages WHERE session_id=?", (sid,))["n"]
        msgs = self.query("SELECT seq,role,ts,kind,text FROM messages "
                          "WHERE session_id=? ORDER BY seq LIMIT ? OFFSET ?",
                          (sid, limit, offset))
        return {"session": s, "messages": msgs, "total": total, "offset": offset}

    def search(self, q, limit=60):
        """FTS5（英文/短语）+ LIKE（中文子串）双路合并。

        FTS5 默认 unicode61 会把连续中文切成一整个 token，中文子串检索必须靠 LIKE 兜底。
        返回的 snippet 已做 HTML 转义，仅由本方法插入 <mark> 标签，前端可安全内联。
        """
        import html as _html
        q = (q or "").strip()
        if not q:
            return []
        rows = []
        if self.has_fts:
            try:
                rows += self.query(
                    "SELECT m.session_id, s.title, s.source_id, s.updated_at, m.role, m.ts, "
                    "snippet(messages_fts, 0, char(1), char(2), '…', 18) snippet, m.id mid "
                    "FROM messages_fts f JOIN messages m ON m.id=f.rowid "
                    "JOIN sessions s ON s.id=m.session_id "
                    "WHERE messages_fts MATCH ? ORDER BY s.updated_at DESC LIMIT ?",
                    (q, limit),
                )
            except sqlite3.Error:
                pass
        like = f"%{q}%"
        try:
            rows += self.query(
                "SELECT m.session_id, s.title, s.source_id, s.updated_at, m.role, m.ts, "
                "substr(m.text, max(1, instr(m.text, ?)-40), 160) snippet, m.id mid "
                "FROM messages m JOIN sessions s ON s.id=m.session_id "
                "WHERE m.text LIKE ? ORDER BY s.updated_at DESC LIMIT ?",
                (q, like, limit),
            )
        except sqlite3.Error:
            pass
        seen, out = set(), []
        for r in rows:
            if r["mid"] in seen:
                continue
            seen.add(r["mid"])
            sn = _html.escape(str(r.get("snippet") or ""))
            sn = sn.replace(chr(1), "<mark>").replace(chr(2), "</mark>")
            if "<mark>" not in sn:
                eq = _html.escape(q)
                if eq:
                    sn = sn.replace(eq, f"<mark>{eq}</mark>")
            r["snippet"] = sn
            out.append(r)
            if len(out) >= limit:
                break
        return out

    def timeline(self, days=30):
        import time as _t
        since = int(_t.time() * 1000) - days * 86400000
        rows = self.query(
            "SELECT date(updated_at/1000,'unixepoch','localtime') d, source_id, COUNT(*) n "
            "FROM sessions WHERE updated_at>=? GROUP BY d, source_id ORDER BY d", (since,))
        return rows

    def storage(self):
        by_source = self.query(
            "SELECT source_id, COUNT(*) n, SUM(size_bytes) b, MIN(updated_at) oldest "
            "FROM sessions GROUP BY source_id ORDER BY b DESC")
        by_project = self.query(
            "SELECT COALESCE(NULLIF(project,''),'(未标记项目)') project, COUNT(*) n, "
            "SUM(size_bytes) b FROM sessions GROUP BY project ORDER BY b DESC LIMIT 20")
        by_month = self.query(
            "SELECT strftime('%Y-%m', updated_at/1000,'unixepoch','localtime') m, "
            "COUNT(*) n, SUM(size_bytes) b FROM sessions WHERE updated_at>0 "
            "GROUP BY m ORDER BY m DESC LIMIT 24")
        oldest = self.query(
            "SELECT id,title,source_id,project,size_bytes,updated_at FROM sessions "
            "WHERE updated_at>0 ORDER BY updated_at ASC LIMIT 15")
        assets = self.query(
            "SELECT a.*, s.name source_name FROM assets a "
            "JOIN sources s ON s.id=a.source_id ORDER BY a.bytes DESC LIMIT 25")
        return {"by_source": by_source, "by_project": by_project,
                "by_month": by_month, "oldest": oldest, "assets": assets}
