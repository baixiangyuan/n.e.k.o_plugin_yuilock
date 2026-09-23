"""模拟手机端 Yui Lock 协议（含挑战-应答认证），用于本地测试插件客户端。

用法：python mock_phone.py [端口] [token]   （token 为空 = 模拟手机未设令牌）
"""
import asyncio
import hashlib
import hmac
import json
import secrets
import sys

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 48912
TOKEN = sys.argv[2] if len(sys.argv) > 2 else ""
state = {"challenge": ""}  # 全局：真实手机把挑战存在服务里，跨连接有效


async def handle(reader, writer):
    try:
        while True:
            line = await reader.readline()
            if not line:
                break
            try:
                req = json.loads(line.decode("utf-8"))
            except Exception:
                continue
            cmd = req.get("cmd")
            if cmd in ("lock", "applock", "unlock"):
                if not TOKEN:
                    resp = {"ok": False, "error": "手机未设置令牌，拒绝控制命令"}
                elif not req.get("proof"):
                    state["challenge"] = secrets.token_hex(16)
                    resp = {"ok": False, "error": "需要配对验证", "auth": "hmac-sha256",
                            "challenge": state["challenge"]}
                else:
                    want = hmac.new(TOKEN.encode(), state["challenge"].encode(),
                                    hashlib.sha256).hexdigest()
                    if hmac.compare_digest(want, str(req.get("proof")).lower()):
                        resp = {"ok": True, "action": cmd}
                    else:
                        resp = {"ok": False, "error": "令牌验证失败"}
            else:
                resp = {"ok": True, "action": cmd}
                if cmd == "ping":
                    resp.update({"device": "MockPhone", "android": "14", "admin": True,
                                 "applock": False, "battery": 88, "token_set": bool(TOKEN)})
            print("recv:", req, flush=True)
            writer.write((json.dumps(resp) + "\n").encode("utf-8"))
            await writer.drain()
    finally:
        writer.close()


async def main():
    server = await asyncio.start_server(handle, "127.0.0.1", PORT)
    print(f"mock phone listening on {PORT} (token={'set' if TOKEN else 'EMPTY'})", flush=True)
    async with server:
        await server.serve_forever()


asyncio.run(main())
