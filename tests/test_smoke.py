"""
Yui 手机/电脑锁 - 冒烟测试

校验插件结构、plugin.toml 入口、手机客户端协议（含挑战-应答认证，不依赖 plugin.sdk）。
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import importlib.util
import json
import os
import secrets
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLUGIN_ID = "yuilock"


def load_module(name: str, path: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestPluginStructure(unittest.TestCase):
    def test_plugin_toml_exists(self):
        self.assertTrue(
            os.path.isfile(os.path.join(ROOT, "plugin.toml")),
            "根目录 plugin.toml 缺失",
        )

    def test_entry_module_exists(self):
        entry = os.path.join(ROOT, "__init__.py")
        self.assertTrue(os.path.isfile(entry), f"入口模块缺失: {entry}")

    def test_plugin_toml_entry(self):
        import tomllib

        with open(os.path.join(ROOT, "plugin.toml"), "rb") as f:
            data = tomllib.load(f)
        plugin = data["plugin"]
        self.assertEqual(plugin["id"], PLUGIN_ID)
        for field in ("name", "version", "entry"):
            self.assertTrue(str(plugin.get(field, "")).strip(), f"{field} 必填")
        self.assertEqual(
            plugin["entry"],
            f"plugins.{PLUGIN_ID}:YuiLockPlugin",
            f"plugin.toml entry 应为 plugins.{PLUGIN_ID}:YuiLockPlugin",
        )

    def test_sources_parse(self):
        import ast

        for name in ("__init__.py", "client.py", "pclock.py",
                     "pc_locker.py", "pair_qr.py", "web_panel.py"):
            path = os.path.join(ROOT, name)
            self.assertTrue(os.path.isfile(path), f"缺少文件: {name}")
            with open(path, encoding="utf-8") as f:
                ast.parse(f.read(), path)


class TestClientProtocol(unittest.TestCase):
    """对内置模拟手机验证：ping 免认证；控制命令走挑战-应答；错 token 被拒；
    客户端未配 token 时拒绝发起控制。"""

    @staticmethod
    async def _phone(reader, writer, token: str, shared: dict):
        try:
            while True:
                line = await reader.readline()
                if not line:
                    break
                req = json.loads(line.decode("utf-8"))
                cmd = req.get("cmd")
                if cmd in ("lock", "applock", "unlock"):
                    if not token:
                        resp = {"ok": False, "error": "手机未设置令牌，拒绝控制命令"}
                    elif not req.get("proof"):
                        shared["challenge"] = secrets.token_hex(16)
                        resp = {"ok": False, "error": "需要配对验证", "auth": "hmac-sha256",
                                "challenge": shared["challenge"]}
                    else:
                        want = hmac.new(token.encode(), shared["challenge"].encode(),
                                        hashlib.sha256).hexdigest()
                        if hmac.compare_digest(want, str(req.get("proof")).lower()):
                            resp = {"ok": True, "action": cmd}
                        else:
                            resp = {"ok": False, "error": "令牌验证失败"}
                else:
                    resp = {"ok": True, "action": cmd}
                    if cmd == "ping":
                        resp.update({"device": "MockPhone", "battery": 88,
                                     "token_set": bool(token)})
                writer.write((json.dumps(resp) + "\n").encode("utf-8"))
                await writer.drain()
        finally:
            writer.close()

    def test_full_flow(self):
        client_mod = load_module("yuilock_client_test", os.path.join(ROOT, "client.py"))

        async def scenario():
            shared = {"challenge": ""}
            server = await asyncio.start_server(
                lambda r, w: self._phone(r, w, "TESTTOKEN", shared), "127.0.0.1", 0)
            port = server.sockets[0].getsockname()[1]
            async with server:
                good = client_mod.YuiLockClient(
                    host="127.0.0.1", port=port, token="TESTTOKEN", transport="lan")
                ping = await good.ping()
                self.assertTrue(ping.get("ok"), ping)
                self.assertEqual(ping.get("device"), "MockPhone")

                lock = await good.lock_screen()
                self.assertTrue(lock.get("ok"), lock)
                apps = await good.lock_apps()
                self.assertTrue(apps.get("ok"), apps)
                unlock = await good.unlock()
                self.assertTrue(unlock.get("ok"), unlock)

                bad = client_mod.YuiLockClient(
                    host="127.0.0.1", port=port, token="WRONG", transport="lan")
                denied = await bad.lock_screen()
                self.assertFalse(denied.get("ok"), denied)
                self.assertIn("令牌", denied.get("error", ""))

                noauth = client_mod.YuiLockClient(
                    host="127.0.0.1", port=port, token="", transport="lan")
                rejected = await noauth.lock_screen()
                self.assertFalse(rejected.get("ok"), rejected)

        asyncio.run(scenario())

    def test_empty_token_phone_rejects(self):
        client_mod = load_module("yuilock_client_test2", os.path.join(ROOT, "client.py"))

        async def scenario():
            shared = {"challenge": ""}
            server = await asyncio.start_server(
                lambda r, w: self._phone(r, w, "", shared), "127.0.0.1", 0)
            port = server.sockets[0].getsockname()[1]
            async with server:
                c = client_mod.YuiLockClient(
                    host="127.0.0.1", port=port, token="TESTTOKEN", transport="lan")
                r = await c.lock_screen()
                self.assertFalse(r.get("ok"), r)
                self.assertIn("未设置令牌", r.get("error", ""))

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main(verbosity=2)
