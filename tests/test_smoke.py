"""
Yui 手机/电脑锁 - 冒烟测试

校验插件结构、plugin.toml 入口、手机客户端协议逻辑（不依赖 plugin.sdk）。
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
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

        for name in ("__init__.py", "client.py", "pc_locker.py"):
            path = os.path.join(ROOT, name)
            with open(path, encoding="utf-8") as f:
                ast.parse(f.read(), path)
        self.assertTrue(True)


class TestClientProtocol(unittest.TestCase):
    def test_send_roundtrip(self):
        client_mod = load_module("yuilock_client_test", os.path.join(ROOT, "client.py"))

        async def scenario():
            async def handle(reader, writer):
                try:
                    line = await reader.readline()
                    req = json.loads(line.decode("utf-8"))
                    allowed = req.get("token") == "TESTTOKEN"
                    resp = {"ok": allowed, "action": req.get("cmd")}
                    if not allowed:
                        resp["error"] = "令牌不匹配"
                    elif req.get("cmd") == "ping":
                        resp.update({"device": "MockPhone", "battery": 88})
                    writer.write((json.dumps(resp) + "\n").encode("utf-8"))
                    await writer.drain()
                finally:
                    writer.close()

            server = await asyncio.start_server(handle, "127.0.0.1", 0)
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

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main(verbosity=2)
