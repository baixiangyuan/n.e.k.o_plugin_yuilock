# Yui 手机/电脑锁（N.E.K.O. Plugin）

apk下载地址：http://8.152.0.186:40033/s/53tE

让 Yui 可以惩罚主人：

- **锁手机**：熄屏锁屏（要密码才能解锁）；或 **应用锁**——手机上打开任何 App 立刻弹回桌面
- **锁电脑**：打开任何程序立刻被关掉、弹回桌面（**N.E.K.O. 自己不受影响**），右上角仅显示 🔒 角标，不提示解锁方式
- **由 Yui 自主决定**：4 个工具注册给 LLM，她生气时自己决定是否调用

解锁：手机应用锁由 Yui 解锁或重启手机；电脑输入秘密按键组合（↑↑↓↓←→←→BA，仅口头告知，界面不显示）、Yui 的 `unlock_pc`、或重启电脑。

## N.E.K.O. 内置面板

插件自带 Hosted UI 面板（`ui/panel.tsx`）：在 N.E.K.O. 插件管理器里打开本插件即可看到——

- 手机在线状态实时刷新（电量 / 应用锁 / 锁屏权限）
- **配对二维码**直接显示在面板上，手机「扫码配对」扫它即可
- 手动控制按钮：锁手机屏幕 / 应用锁 / 解锁手机 / 锁电脑 / 解锁电脑（危险操作带二次确认）

二维码渲染需要 qrcode 库（可选）：`pip install qrcode pillow`；没装也能看到配对码文本。

## 电脑端 Web 面板

不用记命令，浏览器里点点点：

```bash
uv run --with qrcode --with pillow python web_panel.py
```

打开 <http://127.0.0.1:48913>（仅本机可访问）：
- 实时状态：手机在线/电量/应用锁、电脑锁
- 按钮：锁手机屏幕 / 手机应用锁 / 解锁手机 / 锁电脑 / 解锁电脑
- 内置配对二维码，手机「扫码配对」直接扫

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

**APK 下载（分享直链）：<http://8.152.0.186:40033/s/53tE>**

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

## 安全设计

- **挑战-应答认证**：控制命令（lock/applock/unlock）需要手机下发一次性随机 challenge、电脑端回 `HMAC-SHA256(token, challenge)` 才执行。token 不在任何网络通道上明文传输，challenge 单次有效、60 秒过期，无法重放或窃取。
- **空令牌保护**：手机端 token 为空时直接拒绝一切控制命令（只允许 ping）。
- **防 PID 复用误杀**：解锁电脑前先校验目标进程命令行包含本次随机的 instance 标识，才执行 taskkill。
- **电脑锁白名单**：N.E.K.O. 按「启动时扫描到的完整可执行路径」精确放行，不做名字模糊匹配。
- **TCP/蓝牙加固**：指令行上限 4KB、读取 20 秒超时、最多 4 个并发连接，超限直接断开。
- **应用锁权限预检**：缺少"使用情况访问权限"时不会写入开启状态，并明确返回失败。
- 电脑锁**不做防拆卸**（任务管理器/重启可解除）——这是玩家玩法工具，不是恶意软件。
- Web 面板只监听 127.0.0.1，局域网其他设备无法访问。
- Android 签名密钥不入库（`*.keystore` 已忽略），自行用 `keytool` 生成。

## 测试

```bash
python -m unittest discover -s tests -t . -v
```

覆盖：插件结构 / plugin.toml 入口 / 客户端挑战-应答认证 / 错误令牌拒绝 / 空令牌手机端拒绝 / ping 免认证。
