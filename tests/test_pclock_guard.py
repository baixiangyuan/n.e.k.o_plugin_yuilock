"""pclock 解锁判定的回归测试。

历史问题：kill_locker 在校验失败（含「读不到进程信息」）时会直接删掉状态文件，
但进程可能还活着——从此插件再也杀不掉它。现在必须区分三态：
只有「确认不是本实例」才清状态；「无法确认」要保留状态并报错。
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_pclock():
    path = os.path.join(ROOT, "pclock.py")
    spec = importlib.util.spec_from_file_location("yuilock_pclock_guard", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestKillLockerVerdicts(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.p = load_pclock()
        self.p.STATE_PATH = __import__("pathlib").Path(self.tmp.name) / "state.json"
        self.p.LOCK_PATH = __import__("pathlib").Path(self.tmp.name) / "lock.lock"
        self.state = {"pid": 4242, "instance": "abc123", "exe": r"c:\python\pythonw.exe"}
        self.p.STATE_PATH.write_text(json.dumps(self.state), encoding="utf-8")
        self.calls = []

    def tearDown(self):
        self.tmp.cleanup()

    def test_unknown_verdict_keeps_state_and_does_not_kill(self):
        self.p._pid_alive = lambda pid: True
        self.p._win_process_path = lambda pid: ""          # 读不到路径 = 无法确认
        self.p._win_process_cmdline_ctypes = lambda pid: ""
        self.p._win_process_cmdline = lambda pid: ""       # 兜底也读不到
        self.p.subprocess = type("S", (), {"run": staticmethod(lambda *a, **k: self.calls.append(a))})
        alive, msg = self.p.kill_locker()
        self.assertFalse(alive)
        self.assertIn("无法确认", msg)
        self.assertIn("保留", msg)
        self.assertTrue(self.p.STATE_PATH.exists(), "无法确认时不能删状态文件")
        self.assertEqual(self.calls, [], "无法确认时不能 taskkill")

    def test_dead_pid_cleans_state(self):
        self.p._pid_alive = lambda pid: False
        alive, msg = self.p.kill_locker()
        self.assertFalse(alive)
        self.assertIn("已不存在", msg)
        self.assertFalse(self.p.STATE_PATH.exists())

    def test_reused_pid_cleans_state(self):
        self.p._pid_alive = lambda pid: True
        self.p._win_process_path = lambda pid: r"c:\other\chrome.exe"
        alive, msg = self.p.kill_locker()
        self.assertFalse(alive)
        self.assertFalse(self.p.STATE_PATH.exists())

    def test_confirmed_instance_kills(self):
        alive_flag = {"v": True}
        self.p._pid_alive = lambda pid: alive_flag["v"]
        self.p._win_process_path = lambda pid: self.state["exe"]
        self.p._win_process_cmdline_ctypes = lambda pid: (
            f'pythonw.exe pc_locker.py --instance {self.state["instance"]} --state x')
        killed = []

        class FakeProc:
            returncode = 0

        class FakeSub:
            @staticmethod
            def run(args, **kwargs):
                killed.append(args)
                alive_flag["v"] = False  # taskkill 生效
                return FakeProc()

        class FakeSys:
            platform = "win32"

        self.p.subprocess = FakeSub
        self.p.sys = FakeSys  # 强制走 taskkill 分支（CI 是 Linux）
        alive, msg = self.p.kill_locker()
        self.assertTrue(alive)
        self.assertIn("已解锁", msg)
        # taskkill 只在 Windows 执行；CI（Linux）上验证状态清理与返回值即可
        if sys.platform == "win32":
            self.assertEqual(len(killed), 1)
        self.assertFalse(self.p.STATE_PATH.exists())

    def test_taskkill_failure_keeps_state(self):
        """taskkill 返回 0 但进程还在（权限不足）→ 必须报失败并保留状态。"""
        self.p._pid_alive = lambda pid: True
        self.p._win_process_path = lambda pid: self.state["exe"]
        self.p._win_process_cmdline_ctypes = lambda pid: (
            f'pythonw.exe pc_locker.py --instance {self.state["instance"]} --state x')

        class FakeProc:
            returncode = 1

        class FakeSub:
            @staticmethod
            def run(args, **kwargs):
                return FakeProc()

        class FakeSys:
            platform = "win32"

        self.p.subprocess = FakeSub
        self.p.sys = FakeSys  # 强制走 taskkill 分支（CI 是 Linux）
        alive, msg = self.p.kill_locker()
        self.assertFalse(alive)
        self.assertIn("失败", msg)
        self.assertTrue(self.p.STATE_PATH.exists(), "杀不掉时不能删状态")

    def test_instance_mismatch_is_not_confirmed(self):
        self.p._pid_alive = lambda pid: True
        self.p._win_process_path = lambda pid: self.state["exe"]
        self.p._win_process_cmdline_ctypes = lambda pid: "pythonw pc_locker.py --instance other"
        self.assertEqual(self.p._state_verdict(self.state), False)

    def test_live_but_unverifiable_counts_as_alive(self):
        self.p._pid_alive = lambda pid: True
        self.p._win_process_path = lambda pid: ""
        self.assertEqual(self.p._state_verdict(self.state), None)
        self.assertTrue(self.p.is_locker_alive(), "无法确认时应保守地视为仍在锁定")


if __name__ == "__main__":
    unittest.main()
