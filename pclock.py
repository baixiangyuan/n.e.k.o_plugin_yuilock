"""电脑锁机进程管理：N.E.K.O. 插件与 web_panel.py 共用。

防 PID 复用误杀：锁机进程启动时带随机 --instance 标识并把 {pid, instance}
写入状态文件；结束前用 PowerShell 校验该 PID 的命令行确实包含本次 instance
才执行 taskkill，否则视为进程已消失（崩溃/重启），只清理状态。
所有 OpenProcess 句柄均 CloseHandle 释放。
"""
from __future__ import annotations

import ctypes
import json
import os
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

CREATE_NO_WINDOW = 0x08000000


def _state_dir() -> Path:
    """状态放用户数据目录（%LOCALAPPDATA%\\yuilock），不放在全员可写的 %TEMP%。"""
    base = os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()
    d = Path(base) / "yuilock"
    try:
        d.mkdir(parents=True, exist_ok=True)
        return d
    except Exception:
        return Path(tempfile.gettempdir())


STATE_PATH = _state_dir() / "pc_lock.json"
LOCK_PATH = _state_dir() / "pc_lock.lock"


class _FileLock:
    """文件锁：包住「检查是否已锁 → 启动 → 写状态」全程，防并发双开。

    Windows 用 msvcrt，其它平台用 fcntl（CI 在 Linux 上 import 本模块）。
    拿不到锁不再静默继续：重试到超时后抛 TimeoutError，让调用方明确报错。"""
    _IS_WIN = sys.platform == "win32"
    TIMEOUT_SEC = 5.0

    def __enter__(self):
        self._fp = open(LOCK_PATH, "a+b")
        self._locked = False
        deadline = time.monotonic() + self.TIMEOUT_SEC
        while True:
            try:
                self._fp.seek(0)
                if self._IS_WIN:
                    import msvcrt
                    msvcrt.locking(self._fp.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self._fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                self._locked = True
                return self
            except ImportError as exc:
                self._fp.close()
                raise TimeoutError("当前平台没有可用的文件锁实现") from exc
            except OSError as exc:
                if time.monotonic() >= deadline:
                    self._fp.close()
                    raise TimeoutError("另一个锁机操作正在进行，请稍后重试") from exc
                time.sleep(0.05)

    def __exit__(self, *exc):
        try:
            if self._locked:
                self._fp.seek(0)
                if self._IS_WIN:
                    import msvcrt
                    msvcrt.locking(self._fp.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self._fp.fileno(), fcntl.LOCK_UN)
        except Exception:
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


def _ancestor_exes(pid: int | None = None) -> list[str]:
    """沿父进程链取可执行文件路径：放行宿主（N.E.K.O. 前端/后端），避免锁机时误关。"""
    if sys.platform != "win32":
        return []
    try:
        start = int(pid or os.getpid())
        script = (
            f"$p={start}; $r=@(); "
            "for($i=0;$i -lt 8;$i++){"
            "$x=Get-CimInstance Win32_Process -Filter \"ProcessId=$p\"; "
            "if(-not $x){break}; "
            "if($x.ExecutablePath){$r+=$x.ExecutablePath}; "
            "$p=$x.ParentProcessId; "
            "if($p -le 4){break}}; "
            "$r -join \"`n\""
        )
        out = subprocess.run(["powershell", "-NoProfile", "-Command", script],
                             capture_output=True, text=True, timeout=12,
                             creationflags=CREATE_NO_WINDOW)
        return [line.strip() for line in (out.stdout or "").splitlines() if line.strip()]
    except Exception:
        return []


def spawn_locker(script: Path, allow_exes: list[str] | None = None,
                 ttl_sec: int = 0, force_kill: bool = False) -> dict:
    """启动锁机进程。ttl_sec>0 时锁机进程到点自行退出（插件崩溃也能解除）。"""
    if sys.platform != "win32":
        return {"ok": False, "error": "锁定电脑仅支持 Windows"}
    # 先取祖先进程（可能跑一次 PowerShell），不要占着文件锁
    ancestors = _ancestor_exes()
    try:
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
            if force_kill:
                args += ["--force-kill"]
            allow_list = [str(p) for p in (allow_exes or [])]
            for p in ancestors:
                if p not in allow_list:
                    allow_list.append(p)
            for p in allow_list:
                args += ["--allow", p]
            try:
                proc = subprocess.Popen(args, creationflags=CREATE_NO_WINDOW,
                                        cwd=str(script.parent))
            except Exception as exc:
                return {"ok": False, "error": str(exc)}
            try:
                STATE_PATH.write_text(json.dumps(
                    {"pid": proc.pid, "instance": instance, "exe": exe.lower()}),
                    encoding="utf-8")
            except Exception as exc:
                # 状态写不进去就不要让锁机进程继续跑：否则插件再也找不到它
                try:
                    proc.kill()
                except Exception:
                    pass
                return {"ok": False, "error": f"写入锁机状态失败，已结束刚启动的进程：{exc}"}
            return {"ok": True, "pid": proc.pid, "instance": instance}
    except TimeoutError as exc:
        return {"ok": False, "error": str(exc)}


# 解锁结果状态（调用方不要再靠中文消息前缀判断成败）
UNLOCK_KILLED = "killed"          # 确认结束了锁机进程
UNLOCK_NOT_LOCKED = "not_locked"  # 本来就没锁
UNLOCK_GONE = "gone"              # 进程已不在（崩溃/重启），残留状态已清理
UNLOCK_FAILED = "failed"          # 失败，状态已保留

UNLOCK_OK_STATES = frozenset({UNLOCK_KILLED, UNLOCK_NOT_LOCKED, UNLOCK_GONE})


def kill_locker() -> tuple[str, str]:
    """结束锁机进程。返回 (状态码, 给用户看的消息)，状态码见上方常量。

    关键：只有「确认不是本实例」才清状态；「无法确认」必须保留状态并报错。
    否则一次 PowerShell/权限抖动就会把状态删掉，而锁还在跑——从此插件再也找不到它。"""
    try:
        with _FileLock():
            st = _read_state()
            if not st:
                return UNLOCK_NOT_LOCKED, "电脑当前没有锁定"
            verdict = _state_verdict(st)
            if verdict is None:
                return UNLOCK_FAILED, ("无法确认锁机进程身份（读取进程信息失败），状态已保留、未结束任何进程；"
                                       "请重试，或用任务管理器结束 pythonw.exe")
            if verdict is False:
                try:
                    STATE_PATH.unlink()
                except Exception:
                    pass
                return UNLOCK_GONE, "锁机进程已不存在（可能崩溃或重启过），残留状态已清理"
            pid = int(st["pid"])
            if sys.platform == "win32":
                try:
                    # /T：venv 的 pythonw.exe 只是转发启动器，真正的锁机进程是它的子进程
                    subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                                   capture_output=True, timeout=15,
                                   creationflags=CREATE_NO_WINDOW)
                except Exception as exc:
                    return UNLOCK_FAILED, f"调用 taskkill 失败（{exc}），状态已保留"
            # 不能凭 taskkill 的返回码下结论：以「进程是否真的消失」为准（最多等 1 秒）
            for _ in range(20):
                if not _pid_alive(pid):
                    break
                time.sleep(0.05)
            if _pid_alive(pid):
                return UNLOCK_FAILED, ("结束锁机进程失败（可能权限不足），状态已保留；"
                                       "请用任务管理器结束 pythonw.exe")
            try:
                STATE_PATH.unlink()
            except Exception:
                pass
            return UNLOCK_KILLED, "电脑已解锁，恢复自由"
    except TimeoutError as exc:
        return UNLOCK_FAILED, str(exc)
