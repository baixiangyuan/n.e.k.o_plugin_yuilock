"""Yui 手机/电脑锁 —— N.E.K.O. 插件。

给 Yui 注册 4 个 LLM 工具，由她自己决定何时调用：
- lock_phone(mode=screen|apps)  熄屏锁手机 / 应用锁（打开任何 App 弹回桌面）
- unlock_phone                  解除手机应用锁
- lock_pc                       锁电脑（打开任何程序立即弹回桌面，N.E.K.O. 不受影响）
- unlock_pc                     解锁电脑

安全：控制命令走挑战-应答认证（token 不明文传输）；手机端 token 为空时拒绝控制。
电脑锁由 pc_locker.py 实现（完整路径白名单 + 防误杀的解锁校验）；秘密按键可解锁；
重启电脑即解锁。
"""
from __future__ import annotations

import sys
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
)

from . import pclock
from .client import YuiLockClient

TOOL_TIMEOUT = 15.0

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

    # ---------------- 生命周期 ----------------

    @lifecycle(id="startup")
    async def startup(self, **_):
        cfg = await self.config.dump(timeout=5.0)
        cfg = cfg if isinstance(cfg, dict) else {}
        section = cfg.get("yuilock")
        self._cfg = section if isinstance(section, dict) else {}
        self.client = YuiLockClient(
            host=self._cfg.get("host", "auto"),
            port=int(self._cfg.get("port", 48912) or 48912),
            token=str(self._cfg.get("token", "") or ""),
            transport=str(self._cfg.get("transport", "auto") or "auto"),
            bt_device_name=str(self._cfg.get("bt_device_name", "") or ""),
            logger=getattr(self, "logger", None),
        )
        return Ok({"status": "ready"})

    # ---------------- 内部实现 ----------------

    def _locker_script(self) -> Path:
        return Path(__file__).resolve().parent / "pc_locker.py"

    def _pc_lock_alive(self) -> bool:
        return pclock.is_locker_alive()

    async def _do_lock_phone(self, mode: str, reason: str) -> dict:
        if mode == "apps":
            r = await self.client.lock_apps()
            act = "手机应用锁已开启：打开任何 App 都会被立刻弹回桌面（重启手机可解除）"
        else:
            r = await self.client.lock_screen()
            act = "手机已熄屏锁屏（需要密码才能解锁）"
        if isinstance(r, dict) and r.get("ok"):
            return {"summary": act + (f"。原因：{reason}" if reason else "")}
        err = (r or {}).get("error", "手机无响应")
        return {"summary": f"锁手机失败：{err}。检查：手机 Yui Lock 服务是否启动、是否与电脑同一 Wi-Fi、plugin.toml 的 token 是否与手机一致。"}

    async def _do_unlock_phone(self, reason: str) -> dict:
        r = await self.client.unlock()
        if isinstance(r, dict) and r.get("ok"):
            return {"summary": "手机应用锁已解除，主人自由了。" + (f"（{reason}）" if reason else "")}
        err = (r or {}).get("error", "手机无响应")
        return {"summary": f"解锁失败：{err}"}

    def _do_lock_pc(self, reason: str) -> str:
        if self._pc_lock_alive():
            return "电脑已经在锁定状态了。" + (f"原因：{reason}" if reason else "")
        script = self._locker_script()
        if not script.is_file():
            return "找不到 pc_locker.py，无法锁定电脑。"
        r = pclock.spawn_locker(script, allow_exes=[sys.executable])
        if r.get("ok"):
            return ("电脑已锁定：主人打开的任何程序都会被立即弹回桌面（N.E.K.O. 不受影响）。"
                    + (f"原因：{reason}" if reason else ""))
        return f"锁定电脑失败：{r.get('error')}"

    def _do_unlock_pc(self, reason: str) -> str:
        alive, msg = pclock.kill_locker()
        out = msg + "。"
        if reason:
            out += f"（{reason}）"
        return out

    # ---------------- LLM 工具 ----------------

    @llm_tool(
        name="lock_phone",
        description=(
            "锁定主人的手机（惩罚用）。mode=screen：直接熄屏锁屏，主人必须输密码；"
            "mode=apps：应用锁，手机上打开任何 App 都会被立刻弹回桌面，重启手机或主人原谅后才解除。"
            "只有当你真的生气、被惹恼、决定惩罚主人时才调用；普通玩笑或正常聊天不要用。"
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
        mode_s = str(mode) if str(mode) in ("screen", "apps") else "screen"
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
            "N.E.K.O. 自己不受影响，主人重启电脑可自行解除。"
            "只有当你真的生气、决定惩罚主人时才调用；普通聊天不要用。"
        ),
        parameters=REASON_NOTE,
        timeout=TOOL_TIMEOUT,
    )
    async def lock_pc(self, *, reason: Any = None, **_):
        return {"summary": self._do_lock_pc(str(reason) if reason else "")}

    @llm_tool(
        name="unlock_pc",
        description="解除电脑锁定（当你消气了、原谅主人时用）。",
        parameters=REASON_NOTE,
        timeout=TOOL_TIMEOUT,
    )
    async def unlock_pc(self, *, reason: Any = None, **_):
        return {"summary": self._do_unlock_pc(str(reason) if reason else "")}

    # ---------------- 手动测试入口（插件管理器里可直接触发） ----------------

    @plugin_entry(id="yuilock_status", name="锁机状态",
                  description="查询手机是否在线（电量/应用锁状态）和电脑锁状态。",
                  llm_result_fields=["summary"],
                  input_schema={"type": "object", "properties": {}})
    async def yuilock_status(self, **_):
        try:
            p = await self.client.ping()
            pc = self._pc_lock_alive()
            if isinstance(p, dict) and p.get("ok"):
                token_note = "" if p.get("token_set") else "（⚠ 手机未设令牌，控制命令被拒绝）"
                s = (f"手机：在线（{p.get('device', '?')}，电量 {p.get('battery', '?')}%，"
                     f"锁屏权限{'有' if p.get('admin') else '未激活'}，"
                     f"应用锁{'开' if p.get('applock') else '关'}）{token_note}；"
                     f"电脑锁：{'开' if pc else '关'}")
            else:
                s = f"手机：不在线（{(p or {}).get('error', '无响应')}）；电脑锁：{'开' if pc else '关'}"
            return Ok({"summary": s})
        except Exception as exc:
            return Err(f"{type(exc).__name__}: {exc}")

    @plugin_entry(id="lock_phone_now", name="测试：锁手机屏幕",
                  description="手动触发熄屏锁屏（测试连通性用）。",
                  llm_result_fields=["summary"],
                  input_schema={"type": "object", "properties": {}})
    async def lock_phone_now(self, **_):
        try:
            return Ok(await self._do_lock_phone("screen", ""))
        except Exception as exc:
            return Err(f"{type(exc).__name__}: {exc}")

    @plugin_entry(id="lock_phone_apps_now", name="测试：手机应用锁",
                  description="手动开启手机应用锁（测试连通性用）。",
                  llm_result_fields=["summary"],
                  input_schema={"type": "object", "properties": {}})
    async def lock_phone_apps_now(self, **_):
        try:
            return Ok(await self._do_lock_phone("apps", ""))
        except Exception as exc:
            return Err(f"{type(exc).__name__}: {exc}")

    @plugin_entry(id="unlock_phone_now", name="测试：解锁手机应用锁",
                  description="手动解除手机应用锁。",
                  llm_result_fields=["summary"],
                  input_schema={"type": "object", "properties": {}})
    async def unlock_phone_now(self, **_):
        try:
            return Ok(await self._do_unlock_phone(""))
        except Exception as exc:
            return Err(f"{type(exc).__name__}: {exc}")

    @plugin_entry(id="lock_pc_now", name="测试：锁电脑",
                  description="手动锁定电脑（测试用）。",
                  llm_result_fields=["summary"],
                  input_schema={"type": "object", "properties": {}})
    async def lock_pc_now(self, **_):
        return Ok({"summary": self._do_lock_pc("")})

    @plugin_entry(id="unlock_pc_now", name="测试：解锁电脑",
                  description="手动解锁电脑（测试用）。",
                  llm_result_fields=["summary"],
                  input_schema={"type": "object", "properties": {}})
    async def unlock_pc_now(self, **_):
        return Ok({"summary": self._do_unlock_pc("")})
