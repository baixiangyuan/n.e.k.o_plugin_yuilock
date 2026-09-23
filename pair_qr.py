"""电脑端配对二维码：运行后弹出二维码窗口，用手机 Yui Lock「扫码配对」扫一扫，
端口和配对令牌就会自动填好，不用手动输入。

用法（在插件目录下）：
    uv run --with qrcode --with pillow python pair_qr.py
或已安装依赖时：
    python pair_qr.py [--host 局域网IP] [--port 48912] [--token 令牌]

不带参数时自动读取同目录 plugin.toml 的 [yuilock] 配置；
host=auto 时自动探测本机局域网 IP；token 为空时随机生成一个并回显（记得同步到 plugin.toml）。
"""
from __future__ import annotations

import argparse
import secrets
import socket
import sys
from pathlib import Path

import tomllib

ROOT = Path(__file__).resolve().parent


def detect_lan_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("223.5.5.5", 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


def load_config() -> dict:
    try:
        with open(ROOT / "plugin.toml", "rb") as f:
            data = tomllib.load(f)
        section = data.get("yuilock")
        return section if isinstance(section, dict) else {}
    except Exception:
        return {}


def main() -> None:
    ap = argparse.ArgumentParser(description="Yui Lock 配对二维码")
    ap.add_argument("--host", help="局域网 IP（默认自动探测）")
    ap.add_argument("--port", type=int, help="端口（默认读配置，其次 48912）")
    ap.add_argument("--token", help="配对令牌（默认读配置，为空则随机生成）")
    args = ap.parse_args()

    cfg = load_config()
    host = args.host or detect_lan_ip()
    port = args.port or int(cfg.get("port", 48912) or 48912)
    token = args.token or str(cfg.get("token", "") or "")
    if not token:
        token = "".join(secrets.choice("ABCDEFGHJKMNPQRSTUVWXYZ23456789") for _ in range(12))
        print(f"[!] 配置里没有 token，已随机生成：{token}")
        print("    请把它同步写入 plugin.toml 的 [yuilock] token = \"...\"，然后重载插件。")

    payload = f"yuilock://pair?h={host}&p={port}&t={token}"
    print(f"配对信息：{payload}")
    print(f"如果手机扫码不便，也可以手动在 App 里填：端口 {port}，令牌 {token}")

    try:
        import qrcode
    except ImportError:
        print("[!] 缺少依赖：uv run --with qrcode --with pillow python pair_qr.py")
        print("    或先安装：pip install qrcode pillow")
        sys.exit(1)

    qr = qrcode.QRCode(border=2)
    qr.add_data(payload)
    qr.make(fit=True)
    img = qr.make_image()

    png_path = ROOT / "pair_qr.png"
    img.save(png_path)
    print(f"二维码已保存：{png_path}")

    shown = False
    try:
        import tkinter as tk

        from PIL import ImageTk

        root = tk.Tk()
        root.title("Yui Lock 配对二维码")
        root.attributes("-topmost", True)
        tk.Label(root, text=f"{host}:{port} · 用手机 Yui Lock「扫码配对」",
                 font=("Microsoft YaHei UI", 11)).pack(padx=16, pady=(12, 6))
        photo = ImageTk.PhotoImage(img)
        tk.Label(root, image=photo).pack(padx=16, pady=(0, 12))
        root.mainloop()
        shown = True
    except Exception as exc:
        print(f"[!] 窗口显示失败（{exc}），改用终端字符二维码：")
        qr.print_ascii(invert=True)

    if not shown:
        input("按回车退出...")


if __name__ == "__main__":
    main()
