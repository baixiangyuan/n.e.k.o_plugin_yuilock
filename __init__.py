"""Yui 手机/电脑锁 —— N.E.K.O. 插件。

给 Yui 注册 4 个 LLM 工具，由她自己决定何时调用：
- lock_phone(mode=screen|apps)  熄屏锁手机 / 应用锁（打开任何 App 弹回桌面）
- unlock_phone                  解除手机应用锁
- lock_pc                       锁电脑（打开任何程序立即弹回桌面，N.E.K.O. 不受影响）
- unlock_pc                     解锁电脑

安全：控制命令走挑战-应答认证（token 不明文传输）；手机端 token 为空时拒绝控制。
电脑锁由 pc_locker.py 实现（完整路径白名单 + 防误杀的解锁校验）；秘密按键可解锁；
重启电脑即解锁。

防滥用（guard.py）：同一目标有冷却时间、每天有次数上限，应用锁/电脑锁都有自动到期，
插件崩溃或重载也不会把「到点解除」丢掉。
"""
from __future__ import annotations

import asyncio
import base64
import io
from urllib.parse import quote
import socket
import sys
import time
from pathlib import Path
from typing import Any

from plugin.sdk.plugin import (
    Err,
    NekoPluginBase,
    Ok,
    lifecycle,
    llm_tool,
    neko_plugin,
    plugin_entry,
    ui,
)

from . import pclock
from .client import YuiLockClient
from .guard import LockGuard

# 必须大于 client.send 的最坏耗时（timeout*2+4 = 20s），否则工具先超时、
# 手机已经执行了但插件没来得及记账/登记到期时间。
TOOL_TIMEOUT = 45.0

REASON_NOTE = {
    "type": "object",
    "properties": {
        "reason": {"type": "string", "description": "生气或惩罚的原因，一句话"},
    },
}


@neko_plugin
class YuiLockPlugin(NekoPluginBase):

    def __init__(self, ctx):
        super().__init__(ctx)
        self._cfg: dict[str, Any] = {}
        self.client: YuiLockClient | None = None
        self.guard = LockGuard()
        self._phone_expiry_task: asyncio.Task | None = None
        self._restore_task: asyncio.Task | None = None
        # 串行化「检查额度 → 下发命令 → 记账」：并发两次调用不能都通过冷却
        self._lock_gate = asyncio.Lock()

    # ---------------- 生命周期 ----------------

    @lifecycle(id="startup")
    async def startup(self, **_):
        cfg = await self.config.dump(timeout=5.0)
        cfg = cfg if isinstance(cfg, dict) else {}
        section = cfg.get("yuilock")
        self._cfg = section if isinstance(section, dict) else {}
        self.client = YuiLockClient(
            host=self._cfg.get("host", "auto"),
            port=self._safe_port(),
            token=str(self._cfg.get("token", "") or ""),
            transport=str(self._cfg.get("transport", "auto") or "auto"),
            bt_device_name=str(self._cfg.get("bt_device_name", "") or ""),
            logger=getattr(self, "logger", None),
        )
        # 插件重载后把没执行完的「到点解锁」接上（保留引用，shutdown 时能等它收尾）
        self._restore_task = asyncio.create_task(self._restore_expiry())
        return Ok({"status": "ready"})

    @lifecycle(id="shutdown")
    async def shutdown(self, **_):
        self._cancel_phone_expiry()
        task = self._restore_task
        self._restore_task = None
        if task and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        return Ok({"status": "stopped"})

    def _log(self, msg: str, exc: Exception | None = None) -> None:
        logger = getattr(self, "logger", None)
        if logger is None:
            return
        try:
            if exc is not None:
                logger.warning(f"{msg}: {type(exc).__name__}: {exc}")
            else:
                logger.info(msg)
        except Exception:
            pass

    def _safe_port(self) -> int:
        try:
            port = int(self._cfg.get("port", 48912))
        except (TypeError, ValueError):
            return 48912
        return port if 1 <= port <= 65535 else 48912

    def _int_cfg(self, key: str, default: int) -> int:
        try:
            return int(self._cfg.get(key, default))
        except (TypeError, ValueError):
            return default

    # ---------------- 内部实现 ----------------

    def _locker_script(self) -> Path:
        return Path(__file__).resolve().parent / "pc_locker.py"

    def _pc_lock_alive(self) -> bool:
        return pclock.is_locker_alive()

    @staticmethod
    def _lan_ip() -> str:
        """探测局域网 IP；失败返回空串（不要回退 127.0.0.1——那生成的二维码是废的）。"""
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("223.5.5.5", 80))
            ip = s.getsockname()[0]
            if ip and not ip.startswith("127."):
                return ip
        except Exception:
            pass
        finally:
            s.close()
        # 纯内网 / 没有默认路由时再用主机名兜底
        try:
            ip = socket.gethostbyname(socket.gethostname())
            return "" if ip.startswith("127.") else ip
        except Exception:
            return ""

    def _pair_host(self) -> str:
        host = str(self._cfg.get("host", "auto") or "auto")
        if host in ("", "auto", "none"):
            host = self._lan_ip()
        return host

    def _pair_info(self) -> dict:
        """面板默认上下文：只有状态，**不下发含令牌的 URI / 二维码**。"""
        host = self._pair_host()
        token = str(self._cfg.get("token", "") or "")
        return {
            "pair_uri": "",
            "qr_data_url": None,
            "host": host,
            "host_ok": bool(host) and not host.startswith("127."),
            "port": int(self._cfg.get("port", 48912) or 48912),
            "token_set": bool(token),
            "pc_lock": pclock.is_locker_alive(),
        }

    def _pair_payload(self) -> dict:
        """显式点击「显示配对二维码」时才生成：URI 与二维码都含明文令牌。"""
        host = self._pair_host()
        port = int(self._cfg.get("port", 48912) or 48912)
        token = str(self._cfg.get("token", "") or "")
        if not host or host.startswith("127."):
            return {"summary": "没探测到局域网 IP：请在 plugin.toml 的 [yuilock] 里把 "
                               "host 填成电脑的局域网地址后再试。", "is_error": True}
        if not token:
            return {"summary": "还没有设置配对令牌：先运行 pair_qr.py 生成并写入 token。",
                    "is_error": True}
        uri = f"yuilock://pair?h={quote(host, safe='')}&p={port}&t={quote(token, safe='')}"
        qr = None
        try:
            import qrcode

            code = qrcode.QRCode(border=2)
            code.add_data(uri)
            code.make(fit=True)
            buf = io.BytesIO()
            code.make_image().save(buf, format="PNG")
            qr = "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")
        except Exception:
            qr = None
        return {
            "summary": "配对二维码已生成（含明文令牌，请勿截图外发）",
            "pair_uri": uri,
            "qr_data_url": qr,
        }

    @ui.context(id="dashboard", title="Yui Lock")
    async def dashboard_context(self, **_):
        """Hosted UI 面板的轻量上下文：状态 + 电脑锁（探测丢到线程里）。"""
        try:
            info = self._pair_info()
            info["pc_lock"] = await asyncio.to_thread(pclock.is_locker_alive)
            return info
        except Exception:
            return {"pair_uri": "", "qr_data_url": None, "token_set": False, "pc_lock": False}

    # ---- 自动到期：手机端 TTL 是主保障，插件侧定时器是旧版 APK 的兜底 ----

    def _cancel_phone_expiry(self) -> None:
        task = self._phone_expiry_task
        self._phone_expiry_task = None
        if task and not task.done():
            task.cancel()

    def _schedule_phone_expiry(self, delay: float, expires_at: float) -> None:
        self._cancel_phone_expiry()
        if delay <= 0:
            return
        try:
            self._phone_expiry_task = asyncio.create_task(
                self._expire_phone(delay, expires_at))
        except RuntimeError:  # 没有运行中的事件循环（测试/极早期调用）
            self._phone_expiry_task = None

    def _clear_expiry_if_current(self, expires_at: float) -> None:
        """只有「当前任务」且「记录仍是本次到期时间」才清：
        重新锁手机时旧任务被取消，不能把新写入的记录抹掉。"""
        current = self._phone_expiry_task
        if current is not None and current is asyncio.current_task():
            pending = self.guard.pending_expiry()
            if pending and abs(pending[1] - expires_at) < 0.001:
                self.guard.clear_expiry()

    async def _unlock_phone_checked(self) -> bool:
        """真正检查解锁结果：返回 ok=False 时不能当作已解除。"""
        try:
            r = await self.client.unlock()
            return isinstance(r, dict) and bool(r.get("ok"))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._log("自动解锁请求失败", exc)
            return False

    async def _expire_phone(self, delay: float, expires_at: float) -> None:
        try:
            await asyncio.sleep(delay)
            for attempt in range(10):
                if await self._unlock_phone_checked():
                    self._clear_expiry_if_current(expires_at)
                    return
                if attempt == 9:
                    break
                await asyncio.sleep(60)  # 失败保留记录，退避重试
            self._log("自动解除连续失败，已停止重试（设备侧 TTL 仍会生效）")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._log("自动解除任务异常", exc)

    async def _restore_expiry(self) -> None:
        """插件重载后恢复未完成的自动解锁。"""
        try:
            info = self.guard.pending_expiry()
            if not info:
                return
            kind, expires_at = info
            if kind != "phone":
                self.guard.clear_expiry()
                return
            remain = expires_at - time.time()
            if remain <= 0:
                if await self._unlock_phone_checked():
                    self.guard.clear_expiry()
                else:
                    self._schedule_phone_expiry(60, expires_at)  # 保留记录并重试
            else:
                self._schedule_phone_expiry(remain, expires_at)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._log("恢复自动解锁任务失败", exc)

    async def _do_lock_phone(self, mode: str, reason: str, manual: bool = False) -> dict:
        cooldown = self._int_cfg("lock_cooldown_sec", 180)
        limit = self._int_cfg("lock_daily_limit", 5)
        # 整个「检查 → 下发 → 记账」串行化：两次并发调用不会都通过冷却。
        # 等不到锁就立刻返回：排队 20 秒只会拖爆工具超时，而且手机可能已经执行了。
        try:
            await asyncio.wait_for(self._lock_gate.acquire(), timeout=3.0)
        except asyncio.TimeoutError:
            msg = "另一个锁机操作还在进行，请过几秒再试。"
            return {"summary": msg, "is_error": True, "error": msg}
        try:
            if not manual:
                block = self.guard.check("phone", cooldown, limit)
                if block:
                    return {"summary": block, "is_error": True, "error": block}
            ttl = 0
            if mode == "apps":
                ttl = max(0, self._int_cfg("phone_applock_ttl", 600))
                r = await self.client.lock_apps(ttl=ttl)
                if ttl > 0:
                    act = (f"手机应用锁已开启（{ttl // 60} 分钟后自动解除）："
                           f"打开任何 App 都会被立刻弹回桌面")
                else:
                    # 不下发 ttl 时由手机端按默认 10 分钟执行，绝不是永久
                    act = "手机应用锁已开启（手机端默认 10 分钟后自动解除）：打开任何 App 都会被立刻弹回桌面"
            else:
                r = await self.client.lock_screen()
                act = "手机已熄屏锁屏（需要密码才能解锁）"
            if isinstance(r, dict) and r.get("ok"):
                expires_at = time.time() + ttl if ttl > 0 else None
                if not manual:
                    # 配额与到期时间一次落盘，避免崩溃时「扣了额度却没留记录」
                    self.guard.record_with_expiry("phone", expires_at)
                elif expires_at:
                    self.guard.note_expiry("phone", expires_at)
                if expires_at:
                    self._schedule_phone_expiry(ttl, expires_at)
                return {"summary": act + (f"。原因：{reason}" if reason else "")}
            err = (r or {}).get("error", "手机无响应")
            msg = f"锁手机失败：{err}。检查：手机 Yui Lock 服务是否启动、是否与电脑同一 Wi-Fi、plugin.toml 的 token 是否与手机一致。"
            return {"summary": msg, "is_error": True, "error": msg}
        finally:
            self._lock_gate.release()

    async def _do_unlock_phone(self, reason: str) -> dict:
        try:
            await asyncio.wait_for(self._lock_gate.acquire(), timeout=3.0)
        except asyncio.TimeoutError:
            msg = "另一个锁机操作还在进行，请过几秒再试。"
            return {"summary": msg, "is_error": True, "error": msg}
        try:
            r = await self.client.unlock()
            if isinstance(r, dict) and r.get("ok"):
                self._cancel_phone_expiry()
                self.guard.clear_expiry()
                return {"summary": "手机应用锁已解除，主人自由了。" + (f"（{reason}）" if reason else "")}
            err = (r or {}).get("error", "手机无响应")
            msg = f"解锁失败：{err}"
            return {"summary": msg, "is_error": True, "error": msg}
        finally:
            self._lock_gate.release()

    async def _do_lock_pc(self, reason: str, manual: bool = False) -> tuple[bool, str]:
        """返回 (ok, 给 LLM 的话)。阻塞调用全部丢到线程里，避免卡事件循环。"""
        cooldown = self._int_cfg("lock_cooldown_sec", 180)
        limit = self._int_cfg("lock_daily_limit", 5)
        try:
            await asyncio.wait_for(self._lock_gate.acquire(), timeout=3.0)
        except asyncio.TimeoutError:
            return False, "另一个锁机操作还在进行，请过几秒再试。"
        try:
            if await asyncio.to_thread(self._pc_lock_alive):
                return True, "电脑已经在锁定状态了。" + (f"原因：{reason}" if reason else "")
            if not manual:
                block = self.guard.check("pc", cooldown, limit)
                if block:
                    return False, block
            script = self._locker_script()
            if not script.is_file():
                return False, "找不到 pc_locker.py，无法锁定电脑。"
            allow = [sys.executable] + [str(p) for p in (self._cfg.get("pc_allow") or [])]
            ttl = max(0, self._int_cfg("pc_lock_ttl", 300))
            force = bool(self._cfg.get("pc_force_kill", False))
            r = await asyncio.to_thread(pclock.spawn_locker, script,
                                        allow_exes=allow, ttl_sec=ttl, force_kill=force)
            if r.get("ok"):
                if not manual:
                    self.guard.record("pc")
                note = f"（{ttl // 60} 分钟后自动解锁）" if ttl > 0 else "（重启电脑即解锁）"
                return True, ("电脑已锁定：主人打开的任何程序都会被立即弹回桌面（N.E.K.O. 不受影响）"
                              + note + (f"。原因：{reason}" if reason else ""))
            return False, f"锁定电脑失败：{r.get('error')}"
        finally:
            self._lock_gate.release()

    async def _do_unlock_pc(self, reason: str) -> tuple[bool, str]:
        try:
            await asyncio.wait_for(self._lock_gate.acquire(), timeout=3.0)
        except asyncio.TimeoutError:
            return False, "另一个锁机操作还在进行，请过几秒再试。"
        try:
            status, msg = await asyncio.to_thread(pclock.kill_locker)
        finally:
            self._lock_gate.release()
        ok = status in pclock.UNLOCK_OK_STATES
        out = (msg if ok else f"解锁失败：{msg}") + "。"
        if reason:
            out += f"（{reason}）"
        return ok, out

    # ---------------- LLM 工具 ----------------

    @llm_tool(
        name="lock_phone",
        description=(
            "锁定主人的手机（惩罚用）。mode=screen：直接熄屏锁屏，主人必须输密码；"
            "mode=apps：应用锁，手机上打开任何 App 都会被立刻弹回桌面，到点会自动解除。"
            "只有当你真的生气、被惹恼、决定惩罚主人时才调用；普通玩笑或正常聊天不要用。"
            "同一目标有冷却时间、每天有次数上限，被拒绝时不要立刻重试，把返回的话告诉主人即可。"
            "手机不在线或失败时，把返回的 summary 告诉主人。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "mode": {
                    "type": "string",
                    "enum": ["screen", "apps"],
                    "description": "screen=熄屏锁屏；apps=应用锁（弹回桌面）",
                },
                "reason": REASON_NOTE["properties"]["reason"],
            },
        },
        timeout=TOOL_TIMEOUT,
    )
    async def lock_phone(self, *, mode: Any = "screen", reason: Any = None, **_):
        mode_s = str(mode or "screen")
        if mode_s not in ("screen", "apps"):
            msg = "mode 只能是 screen 或 apps。"
            return {"summary": msg, "is_error": True, "error": msg}
        return await self._do_lock_phone(mode_s, str(reason) if reason else "")

    @llm_tool(
        name="unlock_phone",
        description=(
            "解除主人手机的应用锁（当你消气了、原谅主人、或他表现好的时候用）。"
            "对 screen 模式无效——熄屏锁屏只有主人自己输密码能解。"
        ),
        parameters=REASON_NOTE,
        timeout=TOOL_TIMEOUT,
    )
    async def unlock_phone(self, *, reason: Any = None, **_):
        return await self._do_unlock_phone(str(reason) if reason else "")

    @llm_tool(
        name="lock_pc",
        description=(
            "锁定这台电脑（惩罚用）：开启后主人在电脑上打开的任何程序都会被立即关闭弹回桌面，"
            "N.E.K.O. 自己不受影响；锁会在一段时间后自动解除，重启电脑也能解除。"
            "只有当你真的生气、决定惩罚主人时才调用；普通聊天不要用。"
            "同一目标有冷却时间、每天有次数上限，被拒绝时不要立刻重试。"
        ),
        parameters=REASON_NOTE,
        timeout=TOOL_TIMEOUT,
    )
    async def lock_pc(self, *, reason: Any = None, **_):
        ok, msg = await self._do_lock_pc(str(reason) if reason else "")
        return {"summary": msg} if ok else {"summary": msg, "is_error": True, "error": msg}

    @llm_tool(
        name="unlock_pc",
        description="解除电脑锁定（当你消气了、原谅主人时用）。",
        parameters=REASON_NOTE,
        timeout=TOOL_TIMEOUT,
    )
    async def unlock_pc(self, *, reason: Any = None, **_):
        ok, msg = await self._do_unlock_pc(str(reason) if reason else "")
        return {"summary": msg} if ok else {"summary": msg, "is_error": True, "error": msg}

    # ---------------- 手动测试入口（插件管理器里可直接触发） ----------------

    @ui.action(id="yuilock_status", label="刷新状态", tone="info", group="状态")
    @plugin_entry(id="yuilock_status", name="锁机状态",
                  description="查询手机是否在线（电量/应用锁状态）和电脑锁状态。",
                  llm_result_fields=["summary"],
                  input_schema={"type": "object", "properties": {}})
    async def yuilock_status(self, **_):
        try:
            p = await self.client.ping()
            pc = await asyncio.to_thread(self._pc_lock_alive)
            online = isinstance(p, dict) and bool(p.get("ok"))
            if online:
                token_note = "" if p.get("token_set") else "（⚠ 手机未设令牌，控制命令被拒绝）"
                s = (f"手机：在线（{p.get('device', '?')}，电量 {p.get('battery', '?')}%，"
                     f"锁屏权限{'有' if p.get('admin') else '未激活'}，"
                     f"应用锁{'开' if p.get('applock') else '关'}）{token_note}；"
                     f"电脑锁：{'开' if pc else '关'}")
                note = str(p.get("applock_note") or "")
                if note:
                    s += f"\n手机备注：{note}"
            else:
                s = f"手机：不在线（{(p or {}).get('error', '无响应')}）；电脑锁：{'开' if pc else '关'}"
            # phone_online 单独成字段：面板只根据它上色，RPC 成功 ≠ 手机在线
            return Ok({"summary": s, "phone_online": online})
        except Exception as exc:
            return Err(f"{type(exc).__name__}: {exc}")

    @ui.action(id="pair_qr_live", label="显示配对二维码", tone="default", group="配对",
               confirm="二维码里含明文配对令牌，请勿截图外发。确定显示？")
    @plugin_entry(id="pair_qr_live", name="显示配对二维码",
                  description="点击后生成含令牌的配对二维码（默认不随面板下发）。",
                  llm_result_fields=["summary"],
                  input_schema={"type": "object", "properties": {}})
    async def pair_qr_live(self, **_):
        try:
            return Ok(self._pair_payload())
        except Exception as exc:
            return Err(f"{type(exc).__name__}: {exc}")

    @ui.action(id="lock_phone_now", label="锁手机屏幕", tone="danger", group="手机",
               confirm="确定要熄屏锁定手机吗？主人需要输密码才能解锁。")
    @plugin_entry(id="lock_phone_now", name="测试：锁手机屏幕",
                  description="手动触发熄屏锁屏（测试连通性用）。",
                  llm_result_fields=["summary"],
                  input_schema={"type": "object", "properties": {}})
    async def lock_phone_now(self, **_):
        try:
            r = await self._do_lock_phone("screen", "", manual=True)
            return Err(r["summary"]) if r.get("is_error") else Ok(r)
        except Exception as exc:
            return Err(f"{type(exc).__name__}: {exc}")

    @ui.action(id="lock_phone_apps_now", label="手机应用锁", tone="danger", group="手机",
               confirm="确定开启应用锁？锁定期间打开任何 App 都会被立刻弹回桌面。")
    @plugin_entry(id="lock_phone_apps_now", name="测试：手机应用锁",
                  description="手动开启手机应用锁（测试连通性用）。",
                  llm_result_fields=["summary"],
                  input_schema={"type": "object", "properties": {}})
    async def lock_phone_apps_now(self, **_):
        try:
            r = await self._do_lock_phone("apps", "", manual=True)
            return Err(r["summary"]) if r.get("is_error") else Ok(r)
        except Exception as exc:
            return Err(f"{type(exc).__name__}: {exc}")

    @ui.action(id="unlock_phone_now", label="解锁手机", tone="default", group="手机")
    @plugin_entry(id="unlock_phone_now", name="测试：解锁手机应用锁",
                  description="手动解除手机应用锁。",
                  llm_result_fields=["summary"],
                  input_schema={"type": "object", "properties": {}})
    async def unlock_phone_now(self, **_):
        try:
            r = await self._do_unlock_phone("")
            return Err(r["summary"]) if r.get("is_error") else Ok(r)
        except Exception as exc:
            return Err(f"{type(exc).__name__}: {exc}")

    @ui.action(id="lock_pc_now", label="锁电脑", tone="danger", group="电脑",
               confirm="确定锁定这台电脑？锁定期间打开任何程序都会被立即弹回桌面。")
    @plugin_entry(id="lock_pc_now", name="测试：锁电脑",
                  description="手动锁定电脑（测试用）。",
                  llm_result_fields=["summary"],
                  input_schema={"type": "object", "properties": {}})
    async def lock_pc_now(self, **_):
        ok, msg = await self._do_lock_pc("", manual=True)
        return Ok({"summary": msg}) if ok else Err(msg)

    @ui.action(id="unlock_pc_now", label="解锁电脑", tone="default", group="电脑")
    @plugin_entry(id="unlock_pc_now", name="测试：解锁电脑",
                  description="手动解锁电脑（测试用）。",
                  llm_result_fields=["summary"],
                  input_schema={"type": "object", "properties": {}})
    async def unlock_pc_now(self, **_):
        ok, msg = await self._do_unlock_pc("")
        return Ok({"summary": msg}) if ok else Err(msg)
