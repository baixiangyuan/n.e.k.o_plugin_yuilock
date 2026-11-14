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
LOCK_PATH = Path(tempfile.gettempdir()) / "yuilock_pc_lock.lock"


class _FileLock:
    """文件锁：包住「检查是否已锁 → 启动 → 写状态」全程，防并发双开。

    Windows 用 msvcrt，其它平台用 fcntl。CI（Linux）会 import 本模块，
    所以不能无条件 import msvcrt。"""
    _IS_WIN = sys.platform == "win32"

    def __enter__(self):
        self._fp = open(LOCK_PATH, "a+b")
        try:
            if self._IS_WIN:
                import msvcrt
                msvcrt.locking(self._fp.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl
                fcntl.flock(self._fp.fileno(), fcntl.LOCK_EX)
        except (OSError, ImportError):
            pass  # 拿不到锁也继续（尽力互斥）
        return self

    def __exit__(self, *exc):
        try:
            self._fp.seek(0)
            if self._IS_WIN:
                import msvcrt
                msvcrt.locking(self._fp.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._fp.fileno(), fcntl.LOCK_UN)
        except (OSError, ImportError):
            pass
        self._fp.close()
        return False


def _read_state() -> dict | None:
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


class _UNICODE_STRING(ctypes.Structure):
    _fields_ = [("Length", ctypes.c_ushort),
                ("MaximumLength", ctypes.c_ushort),
                ("Buffer", ctypes.c_void_p)]


class _PROCESS_BASIC_INFORMATION(ctypes.Structure):
    _fields_ = [("Reserved1", ctypes.c_void_p),
                ("PebBaseAddress", ctypes.c_void_p),
                ("Reserved2", ctypes.c_void_p * 2),
                ("UniqueProcessId", ctypes.c_void_p),
                ("Reserved3", ctypes.c_void_p)]


def _read_ptr(handle, address: int) -> int | None:
    k32 = ctypes.WinDLL("kernel32")
    buf = ctypes.c_void_p()
    got = ctypes.c_size_t()
    ok = k32.ReadProcessMemory(handle, ctypes.c_void_p(address),
                               ctypes.byref(buf), ctypes.sizeof(buf), ctypes.byref(got))
    return buf.value if ok else None


def _win_process_cmdline_ctypes(pid: int) -> str:
    """直接读 PEB 里的命令行（不依赖 PowerShell，不受其超时/输出编码影响）。
    读不到返回空串，由调用方按「未知」处理，绝不据此下结论。"""
    if sys.platform != "win32":
        return ""
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    PROCESS_VM_READ = 0x0010
    k32 = ctypes.WinDLL("kernel32")
    handle = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_VM_READ, False, int(pid))
    if not handle:
        return ""
    try:
        ntdll = ctypes.WinDLL("ntdll")
        pbi = _PROCESS_BASIC_INFORMATION()
        ret = ctypes.c_ulong()
        status = ntdll.NtQueryInformationProcess(
            handle, 0, ctypes.byref(pbi), ctypes.sizeof(pbi), ctypes.byref(ret))
        if status != 0 or not pbi.PebBaseAddress:
            return ""
        is64 = ctypes.sizeof(ctypes.c_void_p) == 8
        pp_offset = 0x20 if is64 else 0x10       # PEB.ProcessParameters
        cmd_offset = 0x70 if is64 else 0x40      # RTL_USER_PROCESS_PARAMETERS.CommandLine
        pp = _read_ptr(handle, pbi.PebBaseAddress + pp_offset)
        if not pp:
            return ""
        us = _UNICODE_STRING()
        got = ctypes.c_size_t()
        if not k32.ReadProcessMemory(handle, ctypes.c_void_p(pp + cmd_offset),
                                     ctypes.byref(us), ctypes.sizeof(us), ctypes.byref(got)):
            return ""
        if not us.Length or not us.Buffer:
            return ""
        buf = ctypes.create_unicode_buffer(us.Length // 2 + 1)
        if not k32.ReadProcessMemory(handle, ctypes.c_void_p(us.Buffer), buf,
                                     us.Length, ctypes.byref(got)):
            return ""
        return buf.value
    except Exception:
        return ""
    finally:
        k32.CloseHandle(handle)


def _win_process_cmdline(pid: int) -> str:
    """兜底：ctypes 读不到时再问 WMI。超时/失败一律返回空串。"""
    if sys.platform != "win32":
        return ""
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             f"(Get-CimInstance Win32_Process -Filter 'ProcessId={int(pid)}').CommandLine"],
            capture_output=True, text=True, timeout=15,
            creationflags=CREATE_NO_WINDOW)
        if out.returncode != 0:
            return ""
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


def _pid_alive(pid: int) -> bool:
    if sys.platform != "win32":
        return False
    try:
        k32 = ctypes.WinDLL("kernel32")
        h = k32.OpenProcess(0x1000, False, int(pid))  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        try:
            code = ctypes.c_ulong()
            if k32.GetExitCodeProcess(h, ctypes.byref(code)):
                return code.value == 259  # STILL_ACTIVE
            return True
        finally:
            k32.CloseHandle(h)
    except Exception:
        return False


def _state_verdict(st: dict) -> bool | None:
    """三重校验，返回三态而不是布尔：
    True  = 确认是本次锁机进程；
    False = 确认不是（进程已退出 / PID 被复用 / 状态文件无效）；
    None  = 无法判断（进程信息读不到，例如权限或瞬时失败）。
    三态的意义：无法判断时绝不能当成「已退出」去清状态，否则会留下一个杀不掉的锁。"""
    try:
        pid = int(st.get("pid"))
    except (TypeError, ValueError):
        return False
    instance = str(st.get("instance", ""))
    exe = str(st.get("exe", "")).strip().lower()
    if not instance or not exe:
        return False
    if not _pid_alive(pid):
        return False
    path = _win_process_path(pid).lower()
    if not path:
        return None
    if path != exe:
        return False
    args = _win_process_cmdline_ctypes(pid)
    if not args:
        args = _win_process_cmdline(pid)
    if not args:
        return None
    parts = args.split()
    for i, a in enumerate(parts):
        if a == "--instance" and i + 1 < len(parts) and parts[i + 1].strip() == instance:
            return True
    return False


def _state_matches_process(st: dict) -> bool:
    return _state_verdict(st) is True


def is_locker_alive() -> bool:
    st = _read_state()
    if not st:
        return False
    # 无法确认时按「还活着」处理：宁可拒绝二次锁定，也不双开锁机进程
    return _state_verdict(st) is not False


def spawn_locker(script: Path, allow_exes: list[str] | None = None,
                 ttl_sec: int = 0) -> dict:
    """启动锁机进程。ttl_sec>0 时锁机进程到点自行退出（插件崩溃也能解除）。"""
    if sys.platform != "win32":
        return {"ok": False, "error": "锁定电脑仅支持 Windows"}
    with _FileLock():
        if is_locker_alive():
            return {"ok": False, "error": "已有锁机进程在运行，不要重复锁定"}
        instance = uuid.uuid4().hex
        python = Path(sys.executable)
        pythonw = python.with_name("pythonw.exe")
        exe = str(pythonw if pythonw.exists() else python)
        args = [exe, str(script), "--instance", instance, "--state", str(STATE_PATH)]
        if ttl_sec > 0:
            args += ["--ttl", str(int(ttl_sec))]
        for p in (allow_exes or []):
            args += ["--allow", str(p)]
        try:
            proc = subprocess.Popen(args, creationflags=CREATE_NO_WINDOW,
                                    cwd=str(script.parent))
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        STATE_PATH.write_text(json.dumps(
            {"pid": proc.pid, "instance": instance, "exe": exe.lower()}),
            encoding="utf-8")
        return {"ok": True, "pid": proc.pid, "instance": instance}


def kill_locker() -> tuple[bool, str]:
    """结束锁机进程。返回 (是否确实结束过, 给用户看的消息)。

    关键：只有「确认不是本实例」才清状态；「无法确认」必须保留状态并报错。
    否则一次 PowerShell/权限抖动就会把状态删掉，而锁还在跑——从此插件再也找不到它。"""
    with _FileLock():
        st = _read_state()
        if not st:
            return False, "电脑当前没有锁定"
        verdict = _state_verdict(st)
        if verdict is None:
            return False, ("无法确认锁机进程身份（读取进程信息失败），状态已保留、未结束任何进程；"
                           "请重试，或用任务管理器结束 pythonw.exe")
        if verdict is False:
            try:
                STATE_PATH.unlink()
            except Exception:
                pass
            return False, "锁机进程已不存在（可能崩溃或重启过），残留状态已清理"
        pid = int(st["pid"])
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                           capture_output=True, creationflags=CREATE_NO_WINDOW)
        try:
            STATE_PATH.unlink()
        except Exception:
            pass
        return True, "电脑已解锁，恢复自由"
