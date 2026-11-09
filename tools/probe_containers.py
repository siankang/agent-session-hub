"""只读容器侦察：WorkBuddy 会话目录、vscdb / sqlite 容器、会话索引 JSON 的结构。"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
H = Path.home()


def brief(p: Path, n=30):
    if not p.is_dir():
        return [f"<missing {p}>"]
    out = []
    for c in sorted(p.iterdir(), key=lambda x: -x.stat().st_mtime)[:n]:
        if c.is_dir():
            try:
                cnt = sum(1 for _ in c.rglob("*") if _.is_file())
                out.append(f"  DIR  {c.name}/  files~{cnt}")
            except OSError:
                out.append(f"  DIR  {c.name}/")
        else:
            out.append(f"  FILE {c.name}  {c.stat().st_size/1024:.0f}KB")
    return out


def sqlite_info(p: Path):
    try:
        con = sqlite3.connect(f"file:{p.as_posix()}?mode=ro", uri=True, timeout=1)
    except Exception as e:
        return f"<open-error {e}>"
    try:
        out = []
        for (t,) in con.execute("SELECT name FROM sqlite_master WHERE type='table' LIMIT 25"):
            try:
                n = con.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
                cols = [r[1] for r in con.execute(f'PRAGMA table_info("{t}")')]
            except Exception:
                continue
            row = f"    {t} rows={n} cols={cols[:10]}"
            if "key" in cols and 0 < n < 200:
                keys = [str(r[0])[:60] for r in con.execute(f'SELECT key FROM "{t}" LIMIT 25')]
                row += f"\n      keys={keys}"
            out.append(row)
        return "\n".join(out) or "    <no tables>"
    finally:
        con.close()


def json_shape(p: Path, depth=2):
    try:
        d = json.loads(p.read_text(encoding="utf-8", errors="replace"))
    except Exception as e:
        return f"<err {type(e).__name__}>"

    def sh(v, lv=0):
        if lv >= depth:
            return "..."
        if isinstance(v, dict):
            return {k: sh(x, lv + 1) for k, x in list(v.items())[:12]}
        if isinstance(v, list):
            return [sh(x, lv + 1) for x in v[:2]] + ([f"...x{len(v)}"] if len(v) > 2 else [])
        return type(v).__name__
    return json.dumps(sh(d), ensure_ascii=False)[:900]


print("=== .workbuddy 顶层 ===")
print("\n".join(brief(H / ".workbuddy", 40)))
print("\n=== .workbuddy/workspace/sessions 前 5 个会话目录 ===")
base = H / ".workbuddy/workspace/sessions"
if base.is_dir():
    for d in sorted([x for x in base.iterdir() if x.is_dir()],
                    key=lambda x: -x.stat().st_mtime)[:3]:
        print(f"\n-- {d.name}")
        print("\n".join(brief(d, 22)))

print("\n=== VSCode 系 User/globalStorage & History 中的 vscdb ===")
for rel in ["AppData/Roaming/CodeBuddy CN", "AppData/Roaming/CodeBuddy",
            "AppData/Roaming/TRAE SOLO CN", "AppData/Roaming/Devin",
            "AppData/Roaming/Trae CN", "AppData/Roaming/QoderCN"]:
    root = H / rel
    if not root.is_dir():
        continue
    for p in list(root.rglob("*.vscdb"))[:4]:
        print(f"\n-- {p.relative_to(H).as_posix()}  {p.stat().st_size/1024:.0f}KB")
        print(sqlite_info(p))

print("\n=== 独立 sqlite 容器 ===")
for rel in ["AppData/Roaming/Devin/cli/sessions.db",
            "AppData/Roaming/Xiaomi MiMo/db/rolechat.db",
            "AppData/Roaming/Xiaomi MiMo/db/session-review.db",
            "AppData/Roaming/Devin/User/acp-messages/71c13f8a-ab1a-470c-8fa2-95da033a215e.db",
            "AppData/Roaming/ai.opencode.desktop", ".codex/thread_history_1.sqlite"]:
    p = H / rel
    if p.is_dir():
        for q in list(p.rglob("*.db"))[:3]:
            print(f"\n-- {q.relative_to(H).as_posix()}")
            print(sqlite_info(q))
    elif p.is_file():
        print(f"\n-- {rel}")
        print(sqlite_info(p))

print("\n=== 会话索引 JSON ===")
for rel in [".openclaw-autoclaw/agents/main/sessions/sessions.json",
            ".codex/session_index.jsonl"]:
    p = H / rel
    if p.exists():
        print(f"\n-- {rel}: {json_shape(p)}")

# 动态取一份会话 jsonl 作为结构示例，不硬编码任何本机路径
for root_rel in (".catpaw/projects", ".claude/projects"):
    root = H / root_rel
    if not root.is_dir():
        continue
    samples = list(root.rglob("*.jsonl"))[:1]
    if samples:
        q = samples[0]
        print(f"\n-- {q.relative_to(H).as_posix()}: {json_shape(q)}")
        break

print("\n=== Claude-3p 会话目录直接子项 ===")
p = H / "AppData/Local/Claude-3p/local-agent-mode-sessions"
if p.is_dir():
    subs = sorted((d for d in p.iterdir() if d.is_dir()),
                  key=lambda x: x.stat().st_mtime, reverse=True)
    if subs:
        print("\n".join(brief(subs[0], 30)))
