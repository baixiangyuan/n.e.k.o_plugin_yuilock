"""guard.py 风控逻辑测试：冷却、每日上限、到期记录。

不依赖 plugin.sdk，直接按文件加载。
"""
from __future__ import annotations

import importlib.util
import os
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_guard():
    path = os.path.join(ROOT, "guard.py")
    spec = importlib.util.spec_from_file_location("yuilock_guard", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


guard_mod = load_guard()


class FakeClock:
    def __init__(self, now: float = 1_700_000_000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, sec: float) -> None:
        self.now += sec


class TestCooldown(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.clock = FakeClock()
        self.g = guard_mod.LockGuard(os.path.join(self.tmp.name, "g.json"),
                                     clock=self.clock, mono_clock=self.clock)

    def tearDown(self):
        self.tmp.cleanup()

    def test_first_lock_allowed(self):
        self.assertIsNone(self.g.check("phone", 180, 5))

    def test_second_lock_on_same_kind_blocked(self):
        self.g.record("phone")
        msg = self.g.check("phone", 180, 5)
        self.assertIsNotNone(msg)
        self.assertIn("冷却", msg)

    def test_other_kind_not_blocked_by_cooldown(self):
        self.g.record("phone")
        self.assertIsNone(self.g.check("pc", 180, 5))

    def test_cooldown_expires(self):
        self.g.record("phone")
        self.clock.advance(181)
        self.assertIsNone(self.g.check("phone", 180, 5))

    def test_cross_midnight_cooldown_still_applies(self):
        import time as _time
        self.clock.now = _time.mktime((2026, 1, 1, 23, 59, 50, 0, 0, -1))
        self.g.record("phone")
        self.clock.advance(20)  # 次日 00:00:10，日期变了
        msg = self.g.check("phone", 180, 5)
        self.assertIsNotNone(msg, "跨零点不能绕过冷却")

    def test_zero_cooldown_disables_rule(self):
        self.g.record("phone")
        self.assertIsNone(self.g.check("phone", 0, 5))


class TestDailyLimit(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.clock = FakeClock()
        self.g = guard_mod.LockGuard(os.path.join(self.tmp.name, "g.json"),
                                     clock=self.clock, mono_clock=self.clock)

    def tearDown(self):
        self.tmp.cleanup()

    def test_limit_counts_both_kinds(self):
        for kind in ("phone", "pc", "phone"):
            self.assertIsNone(self.g.check(kind, 0, 3))
            self.g.record(kind)
        msg = self.g.check("pc", 0, 3)
        self.assertIsNotNone(msg)
        self.assertIn("上限", msg)

    def test_new_day_resets(self):
        for _ in range(3):
            self.g.record("phone")
        self.clock.advance(86400 + 1)
        self.assertIsNone(self.g.check("phone", 0, 3))

    def test_zero_limit_disables_rule(self):
        for _ in range(50):
            self.g.record("phone")
        self.assertIsNone(self.g.check("phone", 0, 0))


class TestExpiry(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.g = guard_mod.LockGuard(os.path.join(self.tmp.name, "g.json"))

    def tearDown(self):
        self.tmp.cleanup()

    def test_note_and_read(self):
        self.g.note_expiry("phone", 123456.0)
        self.assertEqual(self.g.pending_expiry(), ("phone", 123456.0))

    def test_clear(self):
        self.g.note_expiry("phone", 123456.0)
        self.g.clear_expiry()
        self.assertIsNone(self.g.pending_expiry())

    def test_pending_none_when_absent(self):
        self.assertIsNone(self.g.pending_expiry())

    def test_no_tmp_leftovers(self):
        self.g.record("phone")
        self.g.note_expiry("phone", 1.0)
        leftovers = list(__import__("pathlib").Path(self.tmp.name).glob("*.tmp"))
        self.assertEqual(leftovers, [], "原子写不能留下临时文件")

    def test_corrupt_state_does_not_raise(self):
        path = os.path.join(self.tmp.name, "g.json")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("{not json")
        self.assertIsNone(self.g.check("phone", 180, 5))
        self.g.record("phone")
        self.assertIsNone(self.g.pending_expiry())


class TestConfigKeys(unittest.TestCase):
    def test_plugin_toml_declares_guard_keys(self):
        path = os.path.join(ROOT, "plugin.toml")
        with open(path, "r", encoding="utf-8") as fh:
            text = fh.read()
        for key in ("lock_cooldown_sec", "lock_daily_limit",
                    "phone_applock_ttl", "pc_lock_ttl"):
            self.assertIn(key, text)


if __name__ == "__main__":
    unittest.main()
