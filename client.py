"""手机客户端：局域网（asyncio TCP + UDP 自动发现）+ 蓝牙（winrt，可选）。

安全设计：token 不在任何通道上明文传输。控制命令采用挑战-应答认证：
1) 客户端发送控制命令 → 手机返回一次性随机 challenge；
2) 客户端回发 proof = HMAC-SHA256(token, challenge)；
3) 手机校验通过才执行。局域网监听者拿不到 token，也无法重放（challenge 单次有效）。

协议：NDJSON。纯标准库；蓝牙通道需要可选安装 winrt-* 包。
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from typing import Any, Optional

CONTROL_CMDS = {"lock", "applock", "unlock"}


class YuiLockClient:
    def __init__(self, host: Any = "auto", port: int = 48912, token: str = "",
                 transport: str = "auto", bt_device_name: str = "", logger=None):
        self.host = str(host or "auto").strip()
        self.port = int(port)
        self.token = str(token or "")
        self.transport = str(transport or "auto").lower()
        self.bt_device_name = str(bt_device_name or "")
        self.log = logger

    # ---------------- 局域网 ----------------

    async def discover(self, timeout: float = 2.0) -> Optional[str]:
        """UDP 广播发现手机，返回 IP；找不到返回 None。"""
        loop = asyncio.get_running_loop()
        found: asyncio.Queue = asyncio.Queue()

        class P(asyncio.DatagramProtocol):
            def datagram_received(self, data: bytes, addr):
                try:
                    m = json.loads(data.decode("utf-8"))
                    if m.get("yui_lock_service"):
                        found.put_nowait(addr[0])
                except Exception:
                    pass

        transport, _ = await loop.create_datagram_endpoint(
            P, remote_addr=("255.255.255.255", self.port), allow_broadcast=True)
        try:
            transport.sendto(b'{"yui_lock_discover":true}')
            return await asyncio.wait_for(found.get(), timeout)
        except asyncio.TimeoutError:
            return None
        finally:
            transport.close()

    async def _send_lan(self, ip: str, payload: dict, timeout: float) -> dict:
        reader, writer = await asyncio.wait_for(asyncio.open_connection(ip, self.port), timeout)
        try:
            writer.write((json.dumps(payload) + "\n").encode("utf-8"))
            await writer.drain()
            line = await asyncio.wait_for(reader.readline(), timeout)
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
        if not line:
            raise RuntimeError("手机无响应")
        return json.loads(line.decode("utf-8"))

    # ---------------- 蓝牙（可选） ----------------

    async def _send_bt(self, payload: dict, timeout: float) -> dict:
        try:
            import winrt.windows.devices.bluetooth as wbt
            import winrt.windows.devices.bluetooth.rfcomm as wrf
            import winrt.windows.devices.enumeration as wen
            import winrt.windows.networking.sockets as wns
            import winrt.windows.storage.streams as wst
        except Exception:
            return {"ok": False, "error": (
                "蓝牙通道未启用：需要在 N.E.K.O. 的 Python 环境安装 "
                "winrt-runtime winrt-Windows.Devices.Bluetooth winrt-Windows.Devices.Bluetooth.Rfcomm "
                "winrt-Windows.Devices.Enumeration winrt-Windows.Networking.Sockets "
                "winrt-Windows.Storage.Streams（局域网不受影响）")}
        selector = wrf.RfcommDeviceService.get_device_selector(wrf.RfcommServiceId.serial_port())
        devs = await wen.DeviceInformation.find_all_async(selector)
        target = None
        for d in devs:
            if not self.bt_device_name or self.bt_device_name.lower() in (d.name or "").lower():
                target = d
                break
        if target is None:
            return {"ok": False, "error": "未找到已配对的手机（先在 Windows 蓝牙设置里配对）"}
        bd = await wbt.BluetoothDevice.from_device_async(target)
        svc = await wrf.RfcommDeviceService.from_bluetooth_address_async(
            bd.bluetooth_address, wrf.RfcommServiceId.serial_port())
        if svc is None:
            return {"ok": False, "error": "手机蓝牙服务不可达（确认手机上 Yui Lock 服务已启动）"}
        sock = wns.StreamSocket()
        await asyncio.wait_for(sock.connect_async(
            svc.connection_host_name, svc.connection_service_name), timeout)
        writer = wst.DataWriter(sock.output_stream)
        writer.write_bytes((json.dumps(payload) + "\n").encode("utf-8"))
        await writer.store_async()
        await writer.flush_async()
        reader = wst.DataReader(sock.input_stream)
        await asyncio.wait_for(reader.load_async(4096), timeout)
        n = reader.unconsumed_data_length
        data = bytes(reader.read_bytes(n)) if n else b""
        sock.close()
        if not data:
            raise RuntimeError("手机无响应")
        return json.loads(data.decode("utf-8"))

    # ---------------- 对外 ----------------

    async def send(self, payload: dict, timeout: float = 8.0) -> dict:
        """发送命令。控制命令自动完成挑战-应答认证，token 不出网。"""
        payload = dict(payload)
        cmd = payload.get("cmd")
        if cmd in CONTROL_CMDS:
            if not self.token:
                return {"ok": False,
                        "error": "插件未配置 token（手机端已启用配对令牌校验，请在 plugin.toml [yuilock] 填写）"}
            first = await self._transact(dict(payload), timeout)
            if first.get("auth") == "hmac-sha256" and first.get("challenge"):
                proof = hmac.new(self.token.encode("utf-8"),
                                 str(first["challenge"]).encode("utf-8"),
                                 hashlib.sha256).hexdigest()
                return await self._transact(dict(payload, proof=proof), timeout)
            return first
        return await self._transact(payload, timeout)

    async def _transact(self, payload: dict, timeout: float) -> dict:
        channels = ["bt"] if self.transport == "bt" else (
            ["lan"] if self.transport == "lan" else ["lan", "bt"])
        errors = []
        for ch in channels:
            if ch == "lan":
                ip = self.host
                if ip in ("", "auto", "none", None):
                    ip = await self.discover()
                    if not ip:
                        errors.append("局域网：广播未发现手机（同一 Wi-Fi？App 在运行？）")
                        continue
                try:
                    return await self._send_lan(ip, payload, timeout)
                except Exception as e:
                    errors.append(f"局域网({ip}): {e}")
            else:
                try:
                    return await self._send_bt(payload, timeout)
                except Exception as e:
                    errors.append(f"蓝牙: {e}")
        return {"ok": False, "error": "；".join(errors) or "无可用通道"}

    async def ping(self) -> dict:
        return await self.send({"cmd": "ping"})

    async def lock_screen(self) -> dict:
        return await self.send({"cmd": "lock"})

    async def lock_apps(self) -> dict:
        return await self.send({"cmd": "applock"})

    async def unlock(self) -> dict:
        return await self.send({"cmd": "unlock"})
