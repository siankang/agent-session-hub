"""扫描编排：按注册表逐个来源扫描，错误隔离，进度可查。"""
from __future__ import annotations

import fnmatch
import json
import threading
import time
import traceback
from pathlib import Path

from . import adapters as A
from .store import Store


class Scanner:
    def __init__(self, store: Store, home: Path | None = None):
        self.store = store
        self.home = home or Path.home()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self.state = {
            "running": False, "phase": "idle", "done": 0, "total": 0,
            "current": "", "started_at": 0, "finished_at": 0,
            "message": "", "errors": [], "elapsed_ms": 0,
        }

    # ------------------------------------------------------------ 对外接口
    def status(self):
        with self._lock:
            s = dict(self.state)
        s["errors"] = list(s["errors"])
        return s

    def start(self, source_ids: list[str] | None = None) -> bool:
        with self._lock:
            if self.state["running"]:
                return False
            self.state = {
                "running": True, "phase": "scanning", "done": 0, "total": 0,
                "current": "", "started_at": int(time.time() * 1000),
                "finished_at": 0, "message": "准备扫描", "errors": [],
                "elapsed_ms": 0,
            }
        self._thread = threading.Thread(target=self._run, args=(source_ids,), daemon=True)
        self._thread.start()
        return True

    def _set(self, **kw):
        with self._lock:
            self.state.update(kw)

    # ------------------------------------------------------------ 扫描主体
    def _run(self, source_ids):
        t0 = time.time()
        targets = [s for s in A.SOURCES
                   if not source_ids or s.id in source_ids]
        self._set(total=len(targets), message="正在扫描本机 Agent 数据目录")
        try:
            for i, src in enumerate(targets):
                self._set(done=i, current=src.name,
                          message=f"[{i + 1}/{len(targets)}] 正在识别 {src.name}")
                try:
                    self._scan_one(src)
                except Exception as e:
                    self._add_error(src, f"{type(e).__name__}: {e}")
                    traceback.print_exc()
            self.store.set_state("last_scan", {
                "at": int(time.time() * 1000),
                "sources": len(targets),
                "elapsed_ms": int((time.time() - t0) * 1000),
            })
            self._set(phase="done", done=len(targets), current="",
                      message="扫描完成",
                      elapsed_ms=int((time.time() - t0) * 1000))
        except Exception as e:
            self._set(phase="error", message=f"扫描中断: {e}",
                      elapsed_ms=int((time.time() - t0) * 1000))
        finally:
            self._set(running=False, finished_at=int(time.time() * 1000))

    def _add_error(self, src: A.Source, msg: str):
        with self._lock:
            errs = list(self.state["errors"])
            errs.append({"source": src.name, "error": msg[:300]})
            self.state["errors"] = errs[-40:]
        self.store.execute("UPDATE sources SET error=? WHERE id=?", (msg[:300], src.id))
        self.store.commit()

    def _scan_one(self, src: A.Source):
        roots = [(self.home / r) for r in src.roots]
        existing = [r for r in roots if r.exists()]
        record = {
            "id": src.id, "name": src.name, "vendor": src.vendor, "kind": src.kind,
            "level": src.level, "root": " | ".join(str(r) for r in existing) or
                     " | ".join(str(r) for r in roots),
            "color": src.color, "note": src.note, "enabled": 1,
            "detected": 1 if existing else 0,
            "file_count": 0, "total_bytes": 0, "session_count": 0,
            "message_count": 0, "last_scan_at": int(time.time() * 1000), "error": None,
        }
        self.store.upsert_source(record)
        if not existing:
            self.store.commit()
            return

        # 一次遍历同时得到：体量统计、资产分组、候选文件
        all_entries: list[tuple[Path, object, str]] = []
        for root in existing:
            for p, st in A.walk(root, max_depth=6, limit=A.MAX_WALK_FILES):
                try:
                    rel = p.relative_to(root).as_posix()
                except ValueError:
                    continue
                all_entries.append((p, st, rel))
        record["file_count"] = len(all_entries)
        record["total_bytes"] = sum(int(st.st_size) for _, st, _ in all_entries)
        self.store.upsert_source(record)
        self.store.commit()

        sessions = []
        messages: list[tuple] = []
        try:
            sessions, messages = self._parse(src, existing, all_entries)
        except Exception as e:
            self._add_error(src, f"解析降级: {type(e).__name__}: {e}")
        self.store.save_sessions(src.id, sessions, messages)

        assets = []
        for root in existing:
            assets += self._assets_from_entries(root, src.id, all_entries)
        self.store.save_assets(src.id, assets[:20])
        self.store.upsert_source({**record,
                                  "session_count": len(sessions),
                                  "message_count": len(messages)})
        self.store.commit()

    def _assets_from_entries(self, root: Path, source_id: str, entries, limit=12):
        buckets: dict[str, list] = {}
        for p, st, rel in entries:
            try:
                p.relative_to(root)
            except ValueError:
                continue
            parts = rel.split("/")
            key = "/".join(parts[:2]) if len(parts) > 1 else parts[0]
            b = buckets.setdefault(key, [0, 0, 0])
            b[0] += 1
            b[1] += int(st.st_size)
            b[2] = max(b[2], int(st.st_mtime * 1000))
        out = []
        for k, (n, size, latest) in sorted(buckets.items(),
                                           key=lambda kv: -kv[1][1])[:limit]:
            out.append({"id": f"{source_id}:{root.name}:{k}", "path": f"{root.name}/{k}",
                        "files": n, "bytes": size, "latest_mtime": latest,
                        "note": str(root / k)})
        return out

    # ------------------------------------------------------------ 解析器分派
    def _parse(self, src: A.Source, roots: list[Path], entries):
        sessions: list[dict] = []
        messages: list[tuple] = []
        if src.parser == "aggregate":
            return sessions, messages

        if src.parser == "workbuddy":
            title_map = A.workbuddy_title_map(self.home)
            root = roots[0]
            for p, st, rel in entries:
                if not p.name.endswith(".jsonl") or "projects" not in rel:
                    continue
                if "modify_backup" in rel or "file-rollback" in rel:
                    continue
                sid = p.stem
                project = p.parent.name
                stub, msgs = A.parse_session_file(p, "workbuddy", src.id, sid,
                                                  {sid: (title_map.get(sid) or {}).get("title", "")},
                                                  project=project)
                if not stub:
                    continue
                meta = title_map.get(sid) or {}
                if meta.get("cwd"):
                    stub["cwd"] = meta["cwd"]
                if meta.get("created_at"):
                    stub["created_at"] = meta["created_at"]
                if meta.get("updated_at"):
                    stub["updated_at"] = meta["updated_at"]
                sessions.append(stub)
                messages += msgs
            # 主库中未找到消息文件的会话也列出来
            known = {s["native_id"] for s in sessions}
            for sid, m in title_map.items():
                if sid in known:
                    continue
                sessions.append({
                    "id": f"{src.id}:{sid}", "source_id": src.id, "native_id": sid,
                    "title": A.make_title(str(m.get("title") or ""), sid[:12]),
                    "project": "", "cwd": str(m.get("cwd") or ""),
                    "created_at": m.get("created_at", 0), "updated_at": m.get("updated_at", 0),
                    "message_count": 0, "size_bytes": 0,
                    "file_path": str(self.home / ".workbuddy/workbuddy.db"),
                    "level": "meta", "preview": "", "meta": {},
                })
            return sessions, messages

        if src.parser == "codex":
            title_map = A.codex_title_map(self.home)
            for p, st, rel in entries:
                if not p.name.endswith(".jsonl"):
                    continue
                if "memories" in rel or "process_manager" in rel or p.name == "session_index.jsonl":
                    continue
                # rollout 首行的 payload.session_id 才是与 session_index 对应的 ID
                sid, meta_sid = p.stem, ""
                try:
                    with p.open("r", encoding="utf-8", errors="replace") as f:
                        head = json.loads(f.readline())
                    meta_sid = str((head.get("payload") or {}).get("session_id") or "")
                except Exception:
                    pass
                sid = meta_sid or sid.replace("rollout-", "")
                tm = {sid: title_map[sid]["title"]} if sid in title_map else {}
                stub, msgs = A.parse_session_file(p, "codex", src.id, sid, tm,
                                                  project=p.parent.name,
                                                  fallback_title=p.name[:40],
                                                  session_key=f"{src.id}:{rel}")
                if stub:
                    if sid in title_map and title_map[sid].get("updated_at"):
                        stub["updated_at"] = title_map[sid]["updated_at"]
                    sessions.append(stub)
                    messages += msgs
            return self._dedup(sessions, messages)

        if src.parser == "jsonl":
            pat = src.glob.split("/")[-1]
            for p, st, rel in entries:
                if not fnmatch.fnmatch(p.name, pat):
                    continue
                if any(x and f"/{x}/" in f"/{rel}" for x in src.exclude):
                    continue
                sid = p.stem
                project = p.parent.name if "projects" in rel else ""
                stub, msgs = A.parse_session_file(
                    p, src.flavor, src.id, sid, project=project,
                    fallback_title=p.name[:40],
                    session_key=f"{src.id}:{rel}")
                if stub:
                    sessions.append(stub)
                    messages += msgs
            return self._dedup(sessions, messages)

        if src.parser in ("jsonmeta",):
            for root in roots:
                sessions += A.jsonmeta_sessions(root, src)
            return self._dedup(sessions, messages)

        if src.parser in ("vscdb", "vscdb_meta"):
            for root in roots:
                sessions += A.vscdb_sessions(root, src)
            return self._dedup(sessions, messages)

        if src.parser == "sqlite_meta":
            for root in roots:
                sessions += A.sqlite_meta_sessions(root, src)
            return self._dedup(sessions, messages)

        if src.parser == "devin":
            for root in roots:
                sessions += A.devin_sessions(root, src)
            return self._dedup(sessions, messages)

        return sessions, messages

    @staticmethod
    def _dedup(sessions, messages):
        """会话 id 去重；消息按 session_id 保留仍在索引中的那些。"""
        seen, uniq = set(), []
        for s in sessions:
            if s["id"] in seen:
                continue
            seen.add(s["id"])
            uniq.append(s)
        msgs = [m for m in messages if m[0] in seen]
        return uniq, msgs
