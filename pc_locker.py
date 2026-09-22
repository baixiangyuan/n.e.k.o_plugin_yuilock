"""Yui PC 锁机程序（由 N.E.K.O. 插件调起，也可手动运行）。

锁定期间：
- 打开的任何程序窗口都会被立即关闭（弹回桌面），N.E.K.O. 与系统外壳不受影响；
- Win 键、Alt+Tab 被拦截；
- 屏幕右上角显示一个小的「已锁定」角标（不提示任何解锁方式）。

解锁方式（对用户不可见）：
- 秘密按键组合（↑ ↑ ↓ ↓ ← → ← → B A）；
- N.E.K.O. 插件结束本进程（unlock_pc 工具）；
- 重启电脑即自动解锁（本程序不写开机自启）。

用法：
    pythonw pc_locker.py [--allow 可放行的程序路径 ...]
"""
from __future__ import annotations

import argparse
import ctypes
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
WAIT_TIMEOUT = 0x00000102
CREATE_NO_WINDOW = 0x08000000

state = {"unlocked": False}

# 永远放行的系统外壳 / 输入法 / 搜索等（不含可用来绕锁的资源管理器窗口会被保留，但点开的程序仍会被关）
WHITELIST_BASE = {
    "explorer", "system", "searchhost", "startmenuexperiencehost",
    "shellexperiencehost", "textinputhost", "lockapp", "runtimebroker",
}


def norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


ARGS = argparse.ArgumentParser()
ARGS.add_argument("--allow", action="append", default=[],
                  help="额外放行的程序完整路径（如 N.E.K.O. 的 python.exe）")
OPTS = ARGS.parse_args()

ALLOW_PATHS = {norm(os.path.abspath(p)) for p in OPTS.allow}
ALLOW_NAMES = {norm(os.path.basename(p)) for p in OPTS.allow}
SELF_NAME = norm(os.path.basename(sys.executable))


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


def allowed_pid(pid: int) -> bool:
    if pid in (0, 4):
        return True
    path = exe_path(pid)
    name = norm(os.path.basename(path))
    if not name:
        return True
    if name in WHITELIST_BASE:
        return True
    npath = norm(path)
    # N.E.K.O. 本体：可执行文件名或路径里带 neko
    if "neko" in npath or "neko" in name:
        return True
    if npath in ALLOW_PATHS or name in ALLOW_NAMES or name == SELF_NAME:
        return True
    return False


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
    minimize_all()
    threading.Thread(target=hook_thread, daemon=True).start()
    threading.Thread(target=watchdog, daemon=True).start()
    threading.Thread(target=banner, daemon=True).start()
    try:
        while not state["unlocked"]:
            time.sleep(0.25)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
