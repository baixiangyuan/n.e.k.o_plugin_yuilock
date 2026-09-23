"""电脑锁机进程管理：N.E.K.O. 插件与 web_panel.py 共用。

防 PID 复用误杀：锁机进程启动时带随机 --instance 标识并把 {pid, instance}
写入状态文件；结束前用 PowerShell 校验该 PID 的命令行确实包含本次 instance
才执行 taskkill，否则视为进程已消失（崩溃/重启），只清理状态。
所有 OpenProcess 句柄均 CloseHandle 释放。
"""
from __future__ import annotations

import ctypes
import json
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

CREATE_NO_WINDOW = 0x08000000
STATE_PATH = Path(tempfile.gettempdir()) / "yuilock_pc_lock.json"


def _read_state() -> dict | None:
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _win_process_cmdline(pid: int) -> str:
    if sys.platform != "win32":
        return ""
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             f"(Get-CimInstance Win32_Process -Filter 'ProcessId={int(pid)}').CommandLine"],
            capture_output=True, text=True, timeout=10,
            creationflags=CREATE_NO_WINDOW)
        return (out.stdout or "").strip()
    except Exception:
        return ""


def _win_process_path(pid: int) -> str:
    """返回进程可执行文件完整路径；句柄确保释放。"""
    if sys.platform != "win32":
        return ""
    try:
        k32 = ctypes.WinDLL("kernel32")
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not h:
            return ""
        try:
            buf = ctypes.create_unicode_buffer(1024)
            size = ctypes.c_ulong(1024)
            if k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                return buf.value
            return ""
        finally:
            k32.CloseHandle(h)
    except Exception:
        return ""


def is_locker_alive() -> bool:
    st = _read_state()
    if not st:
        return False
    pid = st.get("pid")
    instance = str(st.get("instance", ""))
    if not pid or not instance:
        return False
    return instance in _win_process_cmdline(int(pid))


def spawn_locker(script: Path, allow_exes: list[str] | None = None) -> dict:
    if sys.platform != "win32":
        return {"ok": False, "error": "锁定电脑仅支持 Windows"}
    instance = uuid.uuid4().hex
    python = Path(sys.executable)
    pythonw = python.with_name("pythonw.exe")
    exe = str(pythonw if pythonw.exists() else python)
    args = [exe, str(script), "--instance", instance, "--state", str(STATE_PATH)]
    for p in (allow_exes or []):
        args += ["--allow", str(p)]
    try:
        proc = subprocess.Popen(args, creationflags=CREATE_NO_WINDOW,
                                cwd=str(script.parent))
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    STATE_PATH.write_text(json.dumps({"pid": proc.pid, "instance": instance}),
                          encoding="utf-8")
    return {"ok": True, "pid": proc.pid, "instance": instance}


def kill_locker() -> tuple[bool, str]:
    """结束锁机进程。返回 (是否确实结束过, 给用户看的消息)。"""
    st = _read_state()
    if not st:
        return False, "电脑当前没有锁定"
    pid = st.get("pid")
    instance = str(st.get("instance", ""))
    killed = False
    if pid and instance and instance in _win_process_cmdline(int(pid)):
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/F", "/PID", str(int(pid))],
                           capture_output=True, creationflags=CREATE_NO_WINDOW)
        killed = True
    try:
        STATE_PATH.unlink()
    except Exception:
        pass
    if killed:
        return True, "电脑已解锁，恢复自由"
    return False, "锁机进程已不存在（可能崩溃或重启过），残留状态已清理"
