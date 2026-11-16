"""解除文件占用：查出占用进程 -> 直接强制结束。

用 Windows Restart Manager 只做「谁占用了这个文件」的查询；结束动作自己控制，
因为 RmShutdown 会阻塞调用线程并可能弹系统对话框，而这里跑在 UI 桥接线程上。

一律不结束：Windows 服务 / 关键进程 / explorer 等系统级进程、本工具自身及其宿主进程
（DeskGrid 会把本窗口 SetParent 挂进它的功能区，杀掉宿主等于杀掉自己）。
"""
from __future__ import annotations

import ctypes
import errno
import os
import time
from ctypes import wintypes
from pathlib import Path

EXIT_WAIT = 3.0            # 强制结束后等进程真正消失的时间
MAX_ROUNDS = 3             # 多进程客户端逐个释放，最多处理几轮
LOCK_WINERRORS = ("WinError 32", "WinError 33")   # 共享冲突 / 区域锁冲突

SYSTEM_NAMES = {
    "system", "registry", "smss.exe", "csrss.exe", "wininit.exe", "winlogon.exe",
    "services.exe", "lsass.exe", "lsaiso.exe", "svchost.exe", "dwm.exe",
    "fontdrvhost.exe", "explorer.exe", "shellexperiencehost.exe",
    "startmenuexperiencehost.exe", "searchhost.exe", "runtimebroker.exe",
    "taskhostw.exe", "sihost.exe", "ctfmon.exe", "spoolsv.exe", "wmiprvse.exe",
    "conhost.exe", "msmpeng.exe", "securityhealthservice.exe", "winlogon.exe",
}
RM_SERVICE, RM_EXPLORER, RM_CRITICAL = 3, 4, 1000

PROCESS_TERMINATE = 0x0001
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
PROCESS_SYNCHRONIZE = 0x00100000
STILL_ACTIVE = 259
WAIT_TIMEOUT = 0x00000102
ERROR_MORE_DATA = 234


class RM_UNIQUE_PROCESS(ctypes.Structure):
    _fields_ = [("dwProcessId", wintypes.DWORD),
                ("ProcessStartTime", wintypes.FILETIME)]


class RM_PROCESS_INFO(ctypes.Structure):
    _fields_ = [("Process", RM_UNIQUE_PROCESS),
                ("strAppName", wintypes.WCHAR * 256),
                ("strServiceShortName", wintypes.WCHAR * 64),
                ("ApplicationType", ctypes.c_int),
                ("AppStatus", wintypes.ULONG),
                ("TSSessionId", wintypes.DWORD),
                ("bRestartable", wintypes.BOOL)]


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [("dwSize", wintypes.DWORD),
                ("cntUsage", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.c_size_t),
                ("th32ModuleID", wintypes.DWORD),
                ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD),
                ("pcPriClassBase", ctypes.c_long),
                ("dwFlags", wintypes.DWORD),
                ("szExeFile", wintypes.WCHAR * 260)]


def _parent_map() -> dict[int, int]:
    """pid -> 父 pid。用于保住本工具所在的整条进程链。"""
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    snap = k32.CreateToolhelp32Snapshot(0x00000002, 0)
    if snap == ctypes.c_void_p(-1).value or snap is None:
        return {}
    out = {}
    try:
        e = PROCESSENTRY32W()
        e.dwSize = ctypes.sizeof(e)
        if not k32.Process32FirstW(snap, ctypes.byref(e)):
            return {}
        while True:
            out[int(e.th32ProcessID)] = int(e.th32ParentProcessID)
            if not k32.Process32NextW(snap, ctypes.byref(e)):
                break
    finally:
        k32.CloseHandle(snap)
    return out


def protected_pids() -> set[int]:
    """本工具自身 + 全部祖先进程（宿主 / 启动器 / 看门狗链）。"""
    parents = _parent_map()
    chain, pid = set(), os.getpid()
    for _ in range(32):
        if pid in chain or pid <= 0:
            break
        chain.add(pid)
        pid = parents.get(pid, 0)
    return chain


def exe_name(pid: int) -> str:
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(32768)
        size = wintypes.DWORD(len(buf))
        if k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return Path(buf.value).name
    finally:
        k32.CloseHandle(h)
    return ""


def locked_by(path: Path) -> list[dict]:
    """Restart Manager 查询占用该文件的进程。查询不到 / 失败都返回空列表。"""
    if os.name != "nt" or not path.is_file():
        return []
    try:
        rm = ctypes.WinDLL("rstrtmgr.dll")
    except OSError:
        return []
    session = wintypes.DWORD()
    key = ctypes.create_unicode_buffer(33)
    if rm.RmStartSession(ctypes.byref(session), 0, key) != 0:
        return []
    try:
        names = (wintypes.LPCWSTR * 1)(str(path))
        if rm.RmRegisterResources(session, 1, names, 0, None, 0, None) != 0:
            return []
        needed, count = wintypes.UINT(0), wintypes.UINT(0)
        reasons = wintypes.DWORD()
        rc = rm.RmGetList(session, ctypes.byref(needed), ctypes.byref(count),
                          None, ctypes.byref(reasons))
        if rc != ERROR_MORE_DATA or needed.value == 0:
            return []
        count = wintypes.UINT(needed.value)
        infos = (RM_PROCESS_INFO * count.value)()
        if rm.RmGetList(session, ctypes.byref(needed), ctypes.byref(count),
                        infos, ctypes.byref(reasons)) != 0:
            return []
        return [{"pid": int(i.Process.dwProcessId),
                 "app": i.strAppName,
                 "type": int(i.ApplicationType)} for i in infos[:count.value]]
    finally:
        rm.RmEndSession(session)


def kill_refusal(pid: int, name: str, app_type: int, guarded: set[int]) -> str:
    """返回拒绝结束的理由；空字符串表示可以结束。"""
    if pid <= 4:
        return "系统进程"
    if pid in guarded:
        return "本工具自身或其宿主进程"
    if app_type in (RM_SERVICE, RM_EXPLORER, RM_CRITICAL):
        return "Windows 服务 / 系统组件"
    if name.lower() in SYSTEM_NAMES:
        return "Windows 系统级进程"
    return ""


def _alive(pid: int) -> bool:
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    h = k32.OpenProcess(PROCESS_SYNCHRONIZE, False, pid)
    if h:
        try:
            return k32.WaitForSingleObject(h, 0) == WAIT_TIMEOUT
        finally:
            k32.CloseHandle(h)
    h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return False        # 打不开且已无同步句柄：视为已退出
    try:
        code = wintypes.DWORD()
        if k32.GetExitCodeProcess(h, ctypes.byref(code)):
            return code.value == STILL_ACTIVE
    finally:
        k32.CloseHandle(h)
    return False


def _terminate(pid: int) -> tuple[bool, str]:
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    h = k32.OpenProcess(PROCESS_TERMINATE, False, pid)
    if not h:
        return False, f"OpenProcess 失败（错误 {ctypes.get_last_error()}，可能需要管理员权限）"
    try:
        if not k32.TerminateProcess(h, 1):
            return False, f"TerminateProcess 失败（错误 {ctypes.get_last_error()}）"
    finally:
        k32.CloseHandle(h)
    deadline = time.monotonic() + EXIT_WAIT
    while time.monotonic() < deadline:
        if not _alive(pid):
            return True, ""
        time.sleep(0.2)
    return True, ""


def stop_process(pid: int) -> str:
    """直接强制结束占用进程，不再请求正常退出。返回可读结果。"""
    ok, why = _terminate(pid)
    return "已强制结束" if ok else f"结束失败：{why}"


def release_file(path: Path) -> dict:
    """结束占用该文件的进程。占用是否真的解除由调用方重试备份来判定。"""
    actions: list[dict] = []
    guarded = protected_pids()
    for _ in range(MAX_ROUNDS):
        holders = locked_by(path)
        if not holders:
            break
        acted = False
        for h in holders:
            pid = h["pid"]
            name = exe_name(pid) or h["app"] or f"PID {pid}"
            why = kill_refusal(pid, name, h["type"], guarded)
            if why:
                actions.append({"pid": pid, "name": name, "path": str(path),
                                "result": "已跳过", "why": why, "stopped": False})
                continue
            result = stop_process(pid)
            actions.append({"pid": pid, "name": name, "path": str(path),
                            "result": result, "why": "",
                            "stopped": result == "已强制结束"})
            acted = True
        if not acted:
            break
    return {"actions": actions,
            "stopped": any(a["stopped"] for a in actions)}


def _looks_locked(why) -> bool:
    """是否可能是占用导致的失败。

    CRT 的 open() 会把共享冲突压成 EACCES 并丢掉 winerror（3.11 如此，3.13 才报
    WinError 32），所以这里放宽到「权限类失败」，真正是不是占用交给 Restart
    Manager 判定：查不到占用者就一个进程都不动。
    """
    text = str(why)
    return (any(w in text for w in LOCK_WINERRORS)
            or "Permission denied" in text or "Errno 13" in text)


def locked_targets(exc: Exception) -> list[str]:
    """从 copy2 / copytree 的异常里挑出疑似被占用的源文件路径。"""
    import shutil

    out: list[str] = []
    if isinstance(exc, shutil.Error):
        entries = exc.args[0] if exc.args else []
        for src, _dst, why in entries:
            if _looks_locked(why):
                out.append(str(src))
    elif isinstance(exc, OSError):
        if (getattr(exc, "winerror", None) in (32, 33)
                or exc.errno in (errno.EACCES, errno.EPERM)) and getattr(exc, "filename", None):
            out.append(str(exc.filename))
    seen, uniq = set(), []
    for p in out:
        if p not in seen:
            seen.add(p)
            uniq.append(p)
    return uniq