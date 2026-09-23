"""Yui PC 锁机程序（由 N.E.K.O. 插件或 web_panel.py 调起）。

锁定期间：
- 打开的任何程序窗口都会被立即关闭（弹回桌面）；
- N.E.K.O. 只按「启动时扫描到的完整可执行路径」放行（不做名字模糊匹配）；
- 系统外壳（explorer/搜索/输入法等）按进程名放行；
- Win 键、Alt+Tab 被拦截；
- 屏幕右上角显示一个小的「已锁定」角标（不提示任何解锁方式）。

解锁方式（对用户不可见）：秘密按键组合（↑↑↓↓←→←→BA）、插件/面板结束本进程、重启电脑。

用法：
    pythonw pc_locker.py --instance <uuid> --state <状态文件> [--allow 可放行程序路径 ...]
"""
from __future__ import annotations

import argparse
import ctypes
import json
import os
import re
import subprocess
import sys
import threading
import time
from ctypes import wintypes

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

WM_CLOSE = 0x0010
WH_KEYBOARD_LL = 13
HC_ACTION = 0
WM_KEYDOWN, WM_SYSKEYDOWN = 0x0100, 0x0104
VK_TAB, VK_ALT, VK_LWIN, VK_RWIN = 0x09, 0x12, 0x5B, 0x5C
VK_BY_NAME = {"up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27, "b": 0x42, "a": 0x41}
NAME_BY_VK = {v: k for k, v in VK_BY_NAME.items()}
KONAMI = ["up", "up", "down", "down", "left", "right", "left", "right", "b", "a"]

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
TH32CS_SNAPPROCESS = 0x2
CREATE_NO_WINDOW = 0x08000000

state = {"unlocked": False}

# 系统外壳 / 搜索 / 输入法（有窗口但不是"应用"）
WHITELIST_BASE = {
    "explorer", "system", "searchhost", "startmenuexperiencehost",
    "shellexperiencehost", "textinputhost", "lockapp", "runtimebroker",
}


def norm_name(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


ARGS = argparse.ArgumentParser()
ARGS.add_argument("--instance", required=True, help="本次锁定的唯一标识（写入命令行，供 unlock 校验）")
ARGS.add_argument("--state", required=True, help="状态文件路径")
ARGS.add_argument("--allow", action="append", default=[],
                  help="额外放行的程序完整路径（如 N.E.K.O. 的 python.exe）")
OPTS = ARGS.parse_args()

SELF_PATH = os.path.abspath(sys.executable).lower()
ALLOW_PATHS = {os.path.abspath(p).lower() for p in OPTS.allow}


def exe_path(pid: int) -> str:
    h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(1024)
        if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return buf.value
        return ""
    finally:
        kernel32.CloseHandle(h)


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", ctypes.c_wchar * 260),
    ]


def scan_process_paths() -> list[tuple[int, str]]:
    """枚举当前所有进程，返回 [(pid, 完整路径)]。"""
    out = []
    snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snap == -1 or not snap:
        return out
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        ok = kernel32.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            pid = entry.th32ProcessID
            path = exe_path(pid)
            if path:
                out.append((pid, path))
            ok = kernel32.Process32NextW(snap, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snap)
    return out


def build_trusted_paths() -> set[str]:
    """启动时确定可信可执行文件集合（完整路径精确匹配）：
    - 进程名含 neko 的（N.E.K.O. 本体及其组件）；
    - 命令行 --allow 传入的路径。"""
    trusted = set(ALLOW_PATHS)
    for pid, path in scan_process_paths():
        if "neko" in norm_name(os.path.basename(path)):
            trusted.add(path.lower())
    return trusted


TRUSTED_PATHS = build_trusted_paths()


def allowed_pid(pid: int) -> bool:
    if pid in (0, 4):
        return True
    path = exe_path(pid)
    if not path:
        return True
    name = norm_name(os.path.basename(path))
    if name in WHITELIST_BASE:
        return True
    lp = path.lower()
    if lp in TRUSTED_PATHS or lp == SELF_PATH:
        return True
    return False


def write_state() -> None:
    try:
        with open(OPTS.state, "w", encoding="utf-8") as f:
            json.dump({"pid": os.getpid(), "instance": OPTS.instance}, f)
    except Exception:
        pass


def clear_state() -> None:
    try:
        os.remove(OPTS.state)
    except Exception:
        pass


def minimize_all() -> None:
    # Win+D：锁定开始时先回到桌面
    user32.keybd_event(VK_LWIN, 0, 0, 0)
    user32.keybd_event(0x44, 0, 0, 0)  # D
    user32.keybd_event(0x44, 0, 2, 0)
    user32.keybd_event(VK_LWIN, 0, 2, 0)


def watchdog() -> None:
    hits: dict[int, int] = {}
    while not state["unlocked"]:
        try:
            hwnd = user32.GetForegroundWindow()
            if hwnd:
                pid = wintypes.DWORD(0)
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                p = pid.value
                if p and not allowed_pid(p):
                    hits[p] = hits.get(p, 0) + 1
                    user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
                    if hits.get(p, 0) >= 4:
                        # 不肯自己关的，强杀
                        subprocess.run(["taskkill", "/F", "/PID", str(p)],
                                       capture_output=True, creationflags=CREATE_NO_WINDOW)
            if len(hits) > 64:
                hits = {k: v for k, v in hits.items() if v < 4}
            time.sleep(0.2)
        except Exception:
            time.sleep(0.5)


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("vkCode", wintypes.DWORD),
        ("scanCode", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


HOOKPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)


def hook_thread() -> None:
    idx = 0

    def proc(ncode, wparam, lparam):
        nonlocal idx
        if ncode == HC_ACTION and wparam in (WM_KEYDOWN, WM_SYSKEYDOWN):
            vk = ctypes.cast(lparam, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents.vkCode
            if vk in (VK_LWIN, VK_RWIN):
                return 1
            if vk == VK_TAB and (user32.GetAsyncKeyState(VK_ALT) & 0x8000):
                return 1
            name = NAME_BY_VK.get(vk)
            if name:
                if name == KONAMI[idx]:
                    idx += 1
                elif name == KONAMI[0]:
                    idx = 1
                else:
                    idx = 0
                if idx >= len(KONAMI):
                    idx = 0
                    state["unlocked"] = True
        return user32.CallNextHookEx(None, ncode, wparam, lparam)

    ptr = HOOKPROC(proc)
    hook = user32.SetWindowsHookExW(WH_KEYBOARD_LL, ptr, None, 0)
    msg = wintypes.MSG()
    while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
        pass
    if hook:
        user32.UnhookWindowsHookEx(hook)


def banner() -> None:
    try:
        import tkinter as tk
    except Exception:
        return
    try:
        root = tk.Tk()
        root.overrideredirect(True)
        root.attributes("-topmost", True)
        root.configure(bg="#E8536B")
        w, h = 240, 52
        sw = root.winfo_screenwidth()
        root.geometry(f"{w}x{h}+{max(sw - w - 20, 0)}+20")
        tk.Label(root, text="🔒 已锁定", font=("Microsoft YaHei UI", 16, "bold"),
                 fg="white", bg="#E8536B").pack(expand=True, fill="both")
        root.mainloop()
    except Exception:
        pass


def main() -> None:
    write_state()
    minimize_all()
    threading.Thread(target=hook_thread, daemon=True).start()
    threading.Thread(target=watchdog, daemon=True).start()
    threading.Thread(target=banner, daemon=True).start()
    try:
        while not state["unlocked"]:
            time.sleep(0.25)
    except KeyboardInterrupt:
        pass
    clear_state()


if __name__ == "__main__":
    main()
