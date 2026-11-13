"""锁机风控：冷却、每日次数上限、到期记录。

为什么需要它：LLM 自主调用 lock_phone / lock_pc 时，如果没有节流，一次误判
可能演变成连续锁机，主人只能自己想办法解除。这里做三件事：

1. 冷却：同一目标（phone / pc）在 cooldown 秒内只能锁一次；
2. 每日上限：两个目标合计一天最多 daily_limit 次（0=不限）；
3. 到期记录：锁手机应用锁时记下自动解除时间，插件重载后仍能续上，
   不会因为一次 reload 就把「到点解锁」弄丢。

文件落在 %TEMP%，插件重载不清零；测试可注入 clock 与 path。
"""
from __future__ import annotations

import json
import tempfile
import time
from pathlib import Path

STATE_PATH = Path(tempfile.gettempdir()) / "yuilock_guard.json"


class LockGuard:
    def __init__(self, path: Path | str | None = None, clock=time.time):
        self.path = Path(path) if path else STATE_PATH
        self._clock = clock

    # ---------------- 状态存取 ----------------

    def _load(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _save(self, data: dict) -> None:
        try:
            self.path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass

    def _today(self, now: float) -> str:
        return time.strftime("%Y-%m-%d", time.localtime(now))

    # ---------------- 规则 ----------------

    def check(self, kind: str, cooldown_sec: int, daily_limit: int) -> str | None:
        """返回 None 表示允许；返回字符串表示拒绝原因（直接给 LLM 看）。"""
        now = self._clock()
        data = self._load()
        if data.get("date") == self._today(now):
            count = int(data.get("count", 0) or 0)
            if daily_limit > 0 and count >= daily_limit:
                return (f"今天已经锁过 {count} 次了（上限 {daily_limit} 次），"
                        f"先冷静一下，不能再锁了。")
            last = float((data.get("last_ts") or {}).get(kind, 0) or 0)
            if cooldown_sec > 0 and last > 0:
                left = cooldown_sec - (now - last)
                if left > 0:
                    return (f"刚刚才锁过{'手机' if kind == 'phone' else '电脑'}，"
                            f"冷却中（还剩 {int(left) + 1} 秒），不能连续锁。")
        return None

    def record(self, kind: str) -> None:
        now = self._clock()
        data = self._load()
        today = self._today(now)
        if data.get("date") != today:
            data = {"date": today, "count": 0}
        data["count"] = int(data.get("count", 0) or 0) + 1
        last = data.get("last_ts")
        if not isinstance(last, dict):
            last = {}
        last[kind] = now
        data["last_ts"] = last
        self._save(data)

    # ---------------- 到期记录 ----------------

    def note_expiry(self, kind: str, expires_at: float) -> None:
        data = self._load()
        data["expire_kind"] = kind
        data["expires_at"] = float(expires_at)
        self._save(data)

    def pending_expiry(self) -> tuple[str, float] | None:
        data = self._load()
        kind = str(data.get("expire_kind") or "")
        try:
            ts = float(data.get("expires_at", 0) or 0)
        except (TypeError, ValueError):
            return None
        if not kind or ts <= 0:
            return None
        return kind, ts

    def clear_expiry(self) -> None:
        data = self._load()
        if "expire_kind" in data or "expires_at" in data:
            data.pop("expire_kind", None)
            data.pop("expires_at", None)
            self._save(data)

    def stats(self) -> dict:
        data = self._load()
        return {
            "date": data.get("date"),
            "count": int(data.get("count", 0) or 0),
            "last_ts": data.get("last_ts") or {},
            "expires_at": data.get("expires_at") or 0,
        }
