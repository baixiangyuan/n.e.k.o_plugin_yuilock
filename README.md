# Yui 手机/电脑锁（N.E.K.O. Plugin）

apk下载地址：http://8.152.0.186:40033/s/yPUj

让 Yui 可以惩罚主人：

- **锁手机**：熄屏锁屏（要密码才能解锁）；或 **应用锁**——手机上打开任何 App 立刻弹回桌面
- **锁电脑**：打开任何程序立刻被关掉、弹回桌面（**N.E.K.O. 自己不受影响**），右上角仅显示 🔒 角标，不提示解锁方式
- **由 Yui 自主决定**：4 个工具注册给 LLM，她生气时自己决定是否调用

解锁：手机应用锁由 Yui 解锁或重启手机；电脑输入秘密按键组合（↑↑↓↓←→←→BA，仅口头告知，界面不显示）、Yui 的 `unlock_pc`、或重启电脑。

## LLM 工具

| 工具 | 作用 |
|---|---|
| `lock_phone(mode=screen/apps)` | 熄屏锁屏 / 应用锁 |
| `unlock_phone` | 解除手机应用锁 |
| `lock_pc` | 锁电脑（程序全弹回桌面） |
| `unlock_pc` | 解锁电脑 |

另有手动测试入口：`yuilock_status` / `lock_phone_now` / `lock_phone_apps_now` / `unlock_phone_now` / `lock_pc_now` / `unlock_pc_now`。

## 安装（插件端）

1. N.E.K.O. 插件管理器 → 「开发模式」→ 开启 → 「加载未打包插件」选择本目录（`plugin.toml` 所在目录）。
2. 校验 → 加载 → 启动插件（`auto_start = true`，随 N.E.K.O. 自启）。
3. 编辑 `plugin.toml` 的 `[yuilock]` 段并重载插件：
   - `token`：手机 App 显示的配对令牌（推荐设置）；
   - `host = "auto"`：UDP 广播自动发现手机，或固定填手机 IP；
   - `transport`：`auto`（先局域网后蓝牙）/ `lan` / `bt`。
4. 蓝牙通道（可选）：先在 Windows 蓝牙设置配对手机，再在 N.E.K.O. 的 Python 环境安装：
   `pip install winrt-runtime winrt-Windows.Devices.Bluetooth winrt-Windows.Devices.Bluetooth.Rfcomm winrt-Windows.Devices.Enumeration winrt-Windows.Networking.Sockets winrt-Windows.Storage.Streams`

## 安装（手机端 APK）

**APK 下载（分享直链）：<http://8.152.0.186:40033/s/yPUj>**

也可以自己编译：`android-app/` 目录（`build.ps1` 一键编译，产物在 `android-app/out/YuiLock.apk`）。

1. 安装 APK，按顺序：① 激活锁屏权限（设备管理器）→ ② 授予使用情况访问权限（应用锁需要）→ ③ 扫码配对 → ④ 启动监听服务；建议开启"忽略电池优化"。
2. 手机与电脑同一 Wi-Fi（局域网）或完成蓝牙配对（SPP）。

## 扫码配对（推荐，不用手打令牌）

1. 在电脑上、本插件目录里运行：
   ```bash
   uv run --with qrcode --with pillow python pair_qr.py
   ```
   （首次没有依赖时会提示；也可 `pip install qrcode pillow` 后直接 `python pair_qr.py`）
2. 电脑会弹出二维码窗口（自动探测局域网 IP、读取/生成配对令牌）。
3. 手机 Yui Lock 点 **「③ 扫码配对」** 对准二维码 → 端口和令牌自动填好并保存。
4. 二维码同时保存为 `pair_qr.png`，也兼容系统相机扫码（识别 `yuilock://pair?...` 深链）。

## CI

- `verify.yml`：push/PR 时调用 N.E.K.O. 官方 `plugin-market-verify` 做市场校验
- `release.yml`：推送 `v*` tag 或手动触发时调用官方 `plugin-market-release` 发布

## 测试

```bash
python -m unittest discover -s tests -t . -v
```

## 安全说明

- 手机端指令带令牌校验；电脑锁**不做防拆卸**（任务管理器/重启可解除）——这是玩家玩法工具，不是恶意软件。
- Android 签名密钥不入库（`*.keystore` 已忽略），自行用 `keytool` 生成。
