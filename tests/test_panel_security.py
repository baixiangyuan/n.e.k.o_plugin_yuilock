"""web_panel 防 DNS rebinding / CSRF 的回归测试。

历史问题：Host 用 startswith 判断，`127.0.0.1.evil.com` 也能通过；
配合 DNS rebinding 就能读 /qr.png、POST 控制命令。
"""
from __future__ import annotations

import importlib.util
import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_panel():
    path = os.path.join(ROOT, "web_panel.py")
    spec = importlib.util.spec_from_file_location("yuilock_web_panel", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


panel = load_panel()
PORT = 48913


class TestHostAllow(unittest.TestCase):
    def test_exact_forms_allowed(self):
        self.assertTrue(panel.host_allowed(f"127.0.0.1:{PORT}", PORT))
        self.assertTrue(panel.host_allowed(f"LOCALHOST:{PORT}", PORT))

    def test_rebinding_suffix_rejected(self):
        for host in (f"127.0.0.1.evil.com:{PORT}",
                     f"localhost.evil.com:{PORT}",
                     f"127.0.0.1:{PORT}.evil.com",
                     f"127.0.0.10:{PORT}",
                     f"evil.com:{PORT}"):
            self.assertFalse(panel.host_allowed(host, PORT), host)

    def test_wrong_port_rejected(self):
        self.assertFalse(panel.host_allowed("127.0.0.1:5000", PORT))

    def test_empty_rejected(self):
        self.assertFalse(panel.host_allowed("", PORT))


class TestOriginAllow(unittest.TestCase):
    def test_exact_origin_allowed(self):
        self.assertTrue(panel.origin_allowed(f"http://127.0.0.1:{PORT}", PORT))
        self.assertTrue(panel.origin_allowed(f"http://localhost:{PORT}", PORT))

    def test_foreign_origin_rejected(self):
        for origin in ("http://evil.com", f"http://127.0.0.1:{PORT}.evil.com",
                       f"https://127.0.0.1:{PORT}", f"http://127.0.0.1:{PORT + 1}"):
            self.assertFalse(panel.origin_allowed(origin, PORT), origin)

    def test_missing_origin_rejected(self):
        self.assertFalse(panel.origin_allowed("", PORT))


class TestSessionToken(unittest.TestCase):
    def test_session_is_random_and_long(self):
        self.assertGreaterEqual(len(panel.SESSION), 24)

    def test_page_embeds_session_placeholder(self):
        self.assertIn("__SESSION__", panel.PAGE)

    def test_post_headers_include_session(self):
        self.assertIn("X-YuiLock-Session", panel.PAGE)

    def test_qr_image_carries_session(self):
        # 二维码带明文令牌，必须凭会话拿，不能裸奔给本机任意进程
        self.assertIn("/qr.png?s=__SESSION__", panel.PAGE)

    def test_status_fetch_sends_session(self):
        self.assertIn("'/api/status',{headers:{'X-YuiLock-Session':SESSION}}", panel.PAGE)


class TestUnlockPcReportsTruth(unittest.TestCase):
    def _run_with(self, result):
        orig = panel.pclock.kill_locker
        panel.pclock.kill_locker = lambda: result
        try:
            return panel.run_action("unlock_pc")
        finally:
            panel.pclock.kill_locker = orig

    def test_success(self):
        r = self._run_with((True, "电脑已解锁，恢复自由"))
        self.assertTrue(r["ok"])

    def test_not_locked_is_ok(self):
        r = self._run_with((False, "电脑当前没有锁定"))
        self.assertTrue(r["ok"])

    def test_failure_is_not_ok(self):
        r = self._run_with((False, "结束锁机进程失败（可能权限不足），状态已保留"))
        self.assertFalse(r["ok"], "杀不掉时面板不能报成功")
        self.assertIn("失败", r["message"])


if __name__ == "__main__":
    unittest.main()
