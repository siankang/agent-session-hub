"""只读侦察：探测本机各 Agent 客户端的会话数据落地位置与文件结构。

安全约束：
- 全程只读；不创建、不修改、不删除任何源目录中的文件。
- 只输出文件元数据与 JSON/SQLite 的键名与类型，绝不输出对话正文内容。
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from collections import Counter
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
HOME = Path.home()
OUT = Path(__file__).with_name("probe_report.json")

# 候选数据根目录（相对用户主目录）
CANDIDATES = [
    ".claude", ".codex", ".codebuddy", ".codebuddycn", ".workbuddy",
    ".cursor", ".continue", ".qoder", ".qoder-cn", ".qoder-cli", ".qodersec",
    ".trae", ".cc-switch", ".devin", ".devin-shared", ".grok",
    ".qwenpaw", ".qwen-agent", ".qwenworkcn", ".dsh", ".dws", ".pixmind",
    ".catpaw", ".catpawai", ".aipyapp", ".coze", ".deepseek",
    ".neural-memory", ".neuralmemory", ".openclaw-autoclaw", ".agents",
    ".agent-browser", ".agentscope-runtime", ".kimi-work", ".kimi-webbridge",
    ".hermes", ".pi", ".config", ".local", ".catdesk", ".apodex",
    ".marvis", ".qmind", ".cliguard", ".codeium", ".ai_completion",
    "AppData/Roaming/CodeBuddy", "AppData/Roaming/CodeBuddy CN",
    "AppData/Roaming/Codex", "AppData/Roaming/Codex++",
    "AppData/Roaming/Coze", "AppData/Roaming/Devin", "AppData/Roaming/Doubao",
    "AppData/Roaming/Grok-Desktop", "AppData/Roaming/QoderCN",
    "AppData/Roaming/QwenWorkCN", "AppData/Roaming/TRAE SOLO CN",
    "AppData/Roaming/Trae CN", "AppData/Roaming/WorkBuddy",
    "AppData/Roaming/WorkBuddy AI", "AppData/Roaming/Xiaomi MiMo",
    "AppData/Roaming/ZCode", "AppData/Roaming/ai.opencode.desktop",
    "AppData/Roaming/pixmind-builder", "AppData/Roaming/com.ccswitch.desktop",
    "AppData/Roaming/AIWorkAssistant", "AppData/Roaming/@genie",
    "AppData/Roaming/Code", "AppData/Roaming/Cursor", "AppData/Roaming/Windsurf",
    "AppData/Roaming/Tencent", "AppData/Roaming/Qoder", "AppData/Roaming/Qoder CN",
    "AppData/Local/Codex", "AppData/Local/WorkBuddy", "AppData/Local/devin",
    "AppData/Local/hermes", "AppData/Local/Qoder CN", "AppData/Local/OpenAI",
    "AppData/Local/com.aiwork.assistant", "AppData/Local/Claude-3p",
    "AppData/Local/CodeBuddyExtension", "AppData/Local/CodeBuddy",
]

SKIP_DIRS = {
    "node_modules", ".git", "cache", "caches", "gpucache", "code cache",
    "blob_storage", "crashpad", "service worker", "shadercache", "dawn-cache",
    "cacheddata", "cachedextensions", "cachedconfigurations", "clp",
    "bin", "lib", "locales", "resources", "swiftshader", ".venv", "venv",
    "site-packages", "__pycache__", "logs", "crashdumps", "backups",
    "webview", "cef", "temp", "tmp", "update", "updates", "packages",
}
SESSION_HINT = ("session", "chat", "conversation", "history", "message",
                "thread", "dialog", "对话", "会话", "记录", "transcript")
DATA_EXT = {".json", ".jsonl", ".ndjson", ".db", ".sqlite", ".sqlite3",
            ".vscdb", ".md", ".txt", ".yaml", ".yml", ".log"}
MAX_FILES = 12000
MAX_DEPTH = 4
MAX_SAMPLE_BYTES = 120_000


def shape(value, depth=0):
    """只返回结构（键名 + 类型 + 长度），绝不返回字符串内容。"""
    if depth > 2:
        return "<deep>"
    if isinstance(value, dict):
        return {k: shape(v, depth + 1) for k, v in list(value.items())[:24]}
    if isinstance(value, list):
        head = shape(value[0], depth + 1) if value else None
        return {"__list__": len(value), "item": head}
    if isinstance(value, str):
        return f"<str:{len(value)}>"
    if isinstance(value, bool):
        return "<bool>"
    if isinstance(value, (int, float)):
        return "<num>"
    return "<null>"


def inspect_file(path: Path):
    ext = path.suffix.lower()
    info = {"ext": ext, "size": path.stat().st_size}
    try:
        if ext in {".db", ".sqlite", ".sqlite3", ".vscdb"}:
            uri = f"file:{path.as_posix()}?mode=ro"
            con = sqlite3.connect(uri, uri=True, timeout=1)
            try:
                rows = con.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' LIMIT 30"
                ).fetchall()
                tables = []
                for (name,) in rows:
                    try:
                        cols = [r[1] for r in con.execute(f'PRAGMA table_info("{name}")')]
                        cnt = con.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
                    except Exception:
                        cols, cnt = [], -1
                    tables.append({"name": name, "columns": cols[:20], "rows": cnt})
                info["sqlite_tables"] = tables
            finally:
                con.close()
        elif ext in {".jsonl", ".ndjson"}:
            with path.open("r", encoding="utf-8", errors="replace") as f:
                lines = [f.readline() for _ in range(3)]
            objs = []
            for ln in lines:
                ln = ln.strip()
                if ln:
                    try:
                        objs.append(shape(json.loads(ln)))
                    except Exception as e:
                        objs.append(f"<parse-error:{type(e).__name__}>")
            info["jsonl_lines"] = objs
        elif ext == ".json":
            if info["size"] <= MAX_SAMPLE_BYTES:
                data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
                info["json_shape"] = shape(data)
            else:
                info["json_shape"] = "<too-large>"
        else:
            with path.open("r", encoding="utf-8", errors="replace") as f:
                head = f.read(240)
            info["text_head_shape"] = f"<len={len(head)} ascii_ratio={sum(c.isascii() for c in head) / max(len(head), 1):.2f}>"
    except Exception as e:
        info["error"] = f"{type(e).__name__}: {str(e)[:100]}"
    return info


def probe_root(root: Path):
    result = {
        "root": str(root), "exists": root.exists(),
        "files": 0, "bytes": 0, "ext": Counter(), "candidates": [],
        "sample_paths": [], "truncated": False,
    }
    if not root.is_dir():
        return result
    seen = 0
    for cur, dirs, files in os.walk(root, topdown=True):
        rel = Path(cur).relative_to(root)
        depth = len(rel.parts)
        dirs[:] = [d for d in dirs if d.lower() not in SKIP_DIRS and not d.startswith(".")]
        if depth >= MAX_DEPTH:
            dirs[:] = []
        for name in files:
            seen += 1
            if seen > MAX_FILES:
                result["truncated"] = True
                break
            p = Path(cur) / name
            try:
                st = p.stat()
            except OSError:
                continue
            result["files"] += 1
            result["bytes"] += st.st_size
            ext = p.suffix.lower()
            result["ext"][ext or "<none>"] += 1
            low = (str(p.relative_to(root))).lower()
            if ext in DATA_EXT and (ext == ".jsonl" or any(h in low for h in SESSION_HINT)):
                if len(result["candidates"]) < 400:
                    result["candidates"].append({
                        "path": str(p), "size": st.st_size,
                        "mtime": time.strftime("%Y-%m-%d %H:%M", time.localtime(st.st_mtime)),
                    })
        if result["truncated"]:
            break
    result["ext"] = dict(result["ext"].most_common(10))
    # 抽样：优先挑最近的候选文件
    cands = sorted(result["candidates"], key=lambda c: c["mtime"], reverse=True)
    for c in cands[:3]:
        p = Path(c["path"])
        if p.stat().st_size > 2_000_000:
            continue
        result["sample_paths"].append({"path": str(p), **inspect_file(p)})
    result["candidate_count"] = len(result["candidates"])
    result.pop("candidates", None)
    return result


def main():
    reports = []
    for rel in CANDIDATES:
        try:
            reports.append(probe_root(HOME / rel))
        except Exception as e:
            reports.append({"root": rel, "error": f"{type(e).__name__}: {e}"})
    OUT.write_text(json.dumps(reports, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"{'root':<52} {'files':>7} {'MB':>9} {'cand':>6}  ext")
    print("-" * 110)
    for r in reports:
        if not r.get("files"):
            continue
        c = r.get("candidate_count", 0)
        exts = ",".join(f"{k}:{v}" for k, v in list(r.get("ext", {}).items())[:4])
        print(f"{os.path.basename(r['root']):<52} {r['files']:>7} {r['bytes']/1048576:>9.1f} {c:>6}  {exts}")
    print(f"\n详细结构报告: {OUT}")


if __name__ == "__main__":
    main()
