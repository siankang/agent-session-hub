"""只读深挖：定位各客户端「真正的会话文件」路径模式与容器结构。

只输出路径、键名、类型、条数；绝不输出任何对话正文。
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
HOME = Path.home()
SKIP = {"node_modules", "cache", "caches", "gpucache", "code cache",
        "blob_storage", "crashpad", "service worker", "shadercache",
        "cacheddata", "resources", "locales", "logs", "backups", "temp",
        ".git", "site-packages", "__pycache__", "cef", "webview"}
HINT = ("session", "chat", "conversation", "history", "message", "thread",
        "dialog", "transcript", "对话", "会话", "record", "rollout")
EXT = {".json", ".jsonl", ".ndjson", ".db", ".sqlite", ".sqlite3", ".vscdb", ".md"}

TARGETS = [
    ".workbuddy", ".claude", ".codex", ".codebuddy", ".codebuddycn",
    ".qoder-cn", ".qoder-cli", ".catpaw", ".openclaw-autoclaw", ".qwenpaw",
    ".qwenworkcn", ".dsh", ".pixmind", ".devin", ".grok", ".agents",
    "AppData/Roaming/CodeBuddy CN", "AppData/Roaming/CodeBuddy",
    "AppData/Roaming/QoderCN", "AppData/Roaming/TRAE SOLO CN",
    "AppData/Roaming/Trae CN", "AppData/Roaming/Devin",
    "AppData/Roaming/Xiaomi MiMo", "AppData/Roaming/ZCode",
    "AppData/Roaming/Grok-Desktop", "AppData/Roaming/ai.opencode.desktop",
    "AppData/Roaming/QwenWorkCN", "AppData/Roaming/Codex",
    "AppData/Roaming/WorkBuddy", "AppData/Local/Claude-3p",
]


def collect(root: Path):
    groups = defaultdict(list)
    total = 0
    for cur, dirs, files in os.walk(root, topdown=True):
        dirs[:] = [d for d in dirs if d.lower() not in SKIP and not d.startswith(".")]
        if len(Path(cur).relative_to(root).parts) >= 6:
            dirs[:] = []
        for name in files:
            p = Path(cur) / name
            ext = p.suffix.lower()
            rel = p.relative_to(root).as_posix()
            if ext not in EXT:
                continue
            if ext != ".jsonl" and not any(h in rel.lower() for h in HINT):
                continue
            try:
                sz = p.stat().st_size
            except OSError:
                continue
            if sz < 40:  # 空文件/占位符忽略
                continue
            total += 1
            groups[p.parent.relative_to(root).as_posix()].append((rel, sz))
    return total, groups


def inspect_container(p: Path):
    """vscdb / sqlite：只输出表名、列名、行数、key 名，不输出 value 正文。"""
    try:
        con = sqlite3.connect(f"file:{p.as_posix()}?mode=ro", uri=True, timeout=1)
    except Exception as e:
        return f"<open-error {type(e).__name__}>"
    try:
        out = []
        for (t,) in con.execute("SELECT name FROM sqlite_master WHERE type='table' LIMIT 20"):
            try:
                n = con.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
                cols = [r[1] for r in con.execute(f'PRAGMA table_info("{t}")')]
            except Exception:
                continue
            item = {"table": t, "rows": n, "cols": cols[:12]}
            if "key" in cols and n and n < 500:
                keys = [r[0] for r in con.execute(f'SELECT key FROM "{t}" LIMIT 40')]
                item["keys"] = [str(k)[:70] for k in keys]
            out.append(item)
        return out
    finally:
        con.close()


def inspect_json(p: Path):
    try:
        if p.stat().st_size > 400_000:
            return f"<large {p.stat().st_size}B>"
        d = json.loads(p.read_text(encoding="utf-8", errors="replace"))
        if isinstance(d, dict):
            return {k: (f"list[{len(v)}]" if isinstance(v, list) else
                        f"dict[{len(v)}]" if isinstance(v, dict) else type(v).__name__)
                    for k, v in list(d.items())[:22]}
        if isinstance(d, list):
            return f"list[{len(d)}] first_keys=" + str(
                list(d[0].keys())[:18] if d and isinstance(d[0], dict) else type(d[0]).__name__ if d else "")
        return type(d).__name__
    except Exception as e:
        return f"<err {type(e).__name__}>"


def main():
    for rel in TARGETS:
        root = HOME / rel
        if not root.is_dir():
            continue
        total, groups = collect(root)
        if not total:
            continue
        print(f"\n### {rel}   candidate_files={total}")
        for g, items in sorted(groups.items(), key=lambda kv: -len(kv[1]))[:8]:
            exts = Counter(Path(i[0]).suffix for i in items)
            print(f"  {g or '.'}/  n={len(items):<5} {dict(exts)}")
            for r, sz in sorted(items, key=lambda x: -x[1])[:3]:
                print(f"      e.g. {r[:110]}  ({sz/1024:.0f}KB)")
        # 抽 1-2 个代表性文件看容器结构
        picks = []
        for g, items in sorted(groups.items(), key=lambda kv: -len(kv[1]))[:4]:
            for r, sz in sorted(items, key=lambda x: -x[1])[:1]:
                picks.append(root / r)
        for p in picks[:2]:
            if p.suffix.lower() in {".db", ".sqlite", ".sqlite3", ".vscdb"}:
                print(f"  [sqlite] {p.name}: {json.dumps(inspect_container(p), ensure_ascii=False)[:800]}")
            elif p.suffix.lower() == ".json" and p.stat().st_size < 400_000:
                print(f"  [json]   {p.name}: {json.dumps(inspect_json(p), ensure_ascii=False)[:600]}")


if __name__ == "__main__":
    main()
