"""锁机风控：冷却、每日次数上限、到期记录。

为什么需要它：LLM 自主调用 lock_phone / lock_pc 时，如果没有节流，一次误判
可能演变成连续锁机，主人只能自己想办法解除。这里做三件事：

1. 冷却：同一目标（phone / pc）在 cooldown 秒内只能锁一次；
2. 每日上限：两个目标合计一天最多 daily_limit 次（0=不限）；
3. 到期记录：锁手机应用锁时记下自动解除时间，插件重载后仍能续上，
   不会因为一次 reload 就把「到点解锁」弄丢。

可靠性取舍：
- 写状态用「临时文件 + os.replace」，不会出现半截 JSON；
- 同一进程内用 threading.Lock 串行化；跨进程（插件 + Web 面板）仍是
  best-effort —— check 与 record 之间有一个很短的窗口，属于软性风控可接受范围；
- 冷却优先用 monotonic（同进程内改系统时间也绕不过），跨进程重启后回落到墙上时钟。
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from pathlib import Path

def _state_dir() -> Path:
    """状态放用户数据目录（%LOCALAPPDATA%\\yuilock），不放在全员可写的 %TEMP%。"""
    base = os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()
    d = Path(base) / "yuilock"
    try:
        d.mkdir(parents=True, exist_ok=True)
        return d
    except Exception:
        return Path(tempfile.gettempdir())


STATE_PATH = _state_dir() / "guard.json"
_STATE_LOCK = threading.Lock()


class LockGuard:
    def __init__(self, path: Path | str | None = None, clock=time.time,
                 mono_clock=time.monotonic):
        self.path = Path(path) if path else STATE_PATH
        self._clock = clock
        self._mono = mono_clock
        # 本次进程内每个目标的最后锁定时刻（monotonic），用于抵抗改系统时间
        self._last_mono: dict[str, float] = {}

    # ---------------- 状态存取 ----------------

    def _load(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _save(self, data: dict) -> None:
        tmp = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(f"{self.path.name}.{os.getpid()}.tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self.path)   # 原子替换：不会留下半截文件
        except Exception:
            if tmp is not None:
                try:
                    tmp.unlink()
                except Exception:
                    pass

    def _today(self, now: float) -> str:
        return time.strftime("%Y-%m-%d", time.localtime(now))

    # ---------------- 规则 ----------------

    def check(self, kind: str, cooldown_sec: int, daily_limit: int) -> str | None:
        """返回 None 表示允许；返回字符串表示拒绝原因（直接给 LLM 看）。"""
        now = self._clock()
        mono = self._mono()
        with _STATE_LOCK:
            data = self._load()
        # 冷却先判，且不看日期：否则跨零点就能立刻再锁一次
        if cooldown_sec > 0:
            # 优先用本进程的 monotonic：即使把系统时间往前调也绕不过冷却
            if kind in self._last_mono:
                elapsed = max(0.0, mono - self._last_mono[kind])
            else:
                last = float((data.get("last_ts") or {}).get(kind, 0) or 0)
                if last > 0:
                    elapsed = max(0.0, now - last)
                else:
                    elapsed = None
            if elapsed is not None:
                left = cooldown_sec - elapsed
                if left > 0:
                    return (f"刚刚才锁过{'手机' if kind == 'phone' else '电脑'}，"
                            f"冷却中（还剩 {int(left) + 1} 秒），不能连续锁。")
        # 每日次数按自然日重置
        if data.get("date") != self._today(now):
            return None
        count = int(data.get("count", 0) or 0)
        if daily_limit > 0 and count >= daily_limit:
            return (f"今天已经锁过 {count} 次了（上限 {daily_limit} 次），"
                    f"先冷静一下，不能再锁了。")
        return None

    def record(self, kind: str) -> None:
        now = self._clock()
        mono = self._mono()
        with _STATE_LOCK:
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
            self._last_mono[kind] = mono

    # ---------------- 到期记录 ----------------

    def note_expiry(self, kind: str, expires_at: float) -> None:
        with _STATE_LOCK:
            data = self._load()
            data["expire_kind"] = kind
            data["expires_at"] = float(expires_at)
            self._save(data)

    def pending_expiry(self) -> tuple[str, float] | None:
        with _STATE_LOCK:
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
        with _STATE_LOCK:
            data = self._load()
            if "expire_kind" in data or "expires_at" in data:
                data.pop("expire_kind", None)
                data.pop("expires_at", None)
                self._save(data)

    def stats(self) -> dict:
        with _STATE_LOCK:
            data = self._load()
        return {
            "date": data.get("date"),
            "count": int(data.get("count", 0) or 0),
            "last_ts": data.get("last_ts") or {},
            "expires_at": data.get("expires_at") or 0,
        }
