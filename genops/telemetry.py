"""模拟遥测适配器：后台线程持续产生电压、频率、电池电压与燃油液位读数；
并检测运行中的电压/频率越限与漏油异常。"""
from __future__ import annotations

import math
import random
import threading
import time
from collections import deque
from datetime import datetime

from . import config

SERIES_MAX = 360  # 2 秒一点，约 12 分钟


def _iso(t: float | None = None) -> str:
    return (datetime.fromtimestamp(t) if t else datetime.now()).strftime("%Y-%m-%d %H:%M:%S")


class TelemetryHub:
    def __init__(self, db, interval: float = config.TELEMETRY_INTERVAL_SEC):
        self.db = db
        self.interval = interval
        self._lock = threading.RLock()
        self._latest: dict[str, dict] = {}
        self._series: dict[str, deque] = {}
        # gen_id -> {"sid": str|None, "engine": bool, "loaded": bool}
        self._state: dict[str, dict] = {}
        # 物理油箱液位（模拟传感器），与账面油位相互独立 -> 可产生燃油差异
        self._fuel: dict[str, float] = {}
        self._leak_alerted: set[str] = set()
        self._range_alert_at: dict[str, float] = {}
        self._injected: dict[str, dict] = {}   # gen_id -> {"kind":..., "until": ts}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.svc = None  # 由外部注入，用于事件留痕

    # ---------- 生命周期 ----------
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="telemetry", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:  # 模拟线程不能因一次异常退出
                pass
            self._stop.wait(self.interval)

    # ---------- 状态接口 ----------
    def ensure(self, gen_id: str, fuel_l: float) -> None:
        with self._lock:
            self._fuel.setdefault(gen_id, fuel_l)
            self._state.setdefault(gen_id, {"sid": None, "engine": False, "loaded": False})
            self._series.setdefault(gen_id, deque(maxlen=SERIES_MAX))

    def sync_fuel(self, gen_id: str, fuel_l: float) -> None:
        """加油复核入账后把物理液位同步到账面。"""
        with self._lock:
            self._fuel[gen_id] = fuel_l

    def mark_session(self, gen_id: str, sid: str | None, *,
                     running_engine: bool, loaded: bool = False) -> None:
        with self._lock:
            self.ensure(gen_id, self._fuel.get(gen_id, 0.0))
            self._state[gen_id] = {"sid": sid, "engine": running_engine, "loaded": loaded}
            if sid is None:
                self._leak_alerted.discard(gen_id)

    def gauge_fuel(self, gen_id: str) -> float:
        with self._lock:
            return self._fuel.get(gen_id, 0.0)

    def latest(self, gen_id: str) -> dict | None:
        with self._lock:
            r = self._latest.get(gen_id)
            return dict(r) if r else None

    def series(self, gen_id: str, minutes: float = 10.0) -> list[dict]:
        with self._lock:
            cutoff = time.time() - minutes * 60
            return [dict(r) for r in self._series.get(gen_id, ()) if r["t"] >= cutoff]

    def inject(self, gen_id: str, kind: str, seconds: float = 30.0) -> None:
        """注入临时故障波形：voltage_sag / voltage_surge / freq_high / leak。"""
        with self._lock:
            self._injected[gen_id] = {"kind": kind, "until": time.time() + seconds}

    def set_sim_mode(self, gen_id: str, mode: str) -> None:
        self.db.execute("UPDATE generators SET sim_mode=? WHERE id=?", (mode, gen_id))

    # ---------- 启动成败（由空载启动步骤调用） ----------
    def attempt_start(self, gen_id: str) -> str:
        g = self.db.query_one("SELECT * FROM generators WHERE id=?", (gen_id,))
        mode = g["sim_mode"] if g else "normal"
        if mode == "start_fail":
            return "起动机无响应（启动系统故障）"
        return "ok"

    # ---------- 采样 ----------
    def tick(self) -> None:
        gens = self.db.query("SELECT * FROM generators")
        nowt = time.time()
        for g in gens:
            gid = g["id"]
            with self._lock:
                self.ensure(gid, g["fuel_level_l"])
                st = self._state[gid]
                inj = self._injected.get(gid)
                if inj and nowt > inj["until"]:
                    del self._injected[gid]
                    inj = None

                engine = bool(st["engine"]) and not g["locked_out"]
                loaded = engine and st["loaded"]
                mode = g["sim_mode"]

                if engine:
                    voltage, frequency = self._running_values(g, loaded, inj)
                    battery = 26.2 + random.uniform(-0.3, 0.3)
                    lpm = self._fuel_rate(g, loaded, mode, inj)
                    self._fuel[gid] = max(0.0, self._fuel[gid] - lpm * self.interval / 60.0)
                else:
                    voltage = 0.0
                    frequency = 0.0
                    battery = self._battery_rest(g)
                    # 漏油机组静置时缓慢渗漏
                    if mode == "oil_leak":
                        self._fuel[gid] = max(0.0, self._fuel[gid] - 0.02 * self.interval / 60.0)

                fuel_l = self._fuel[gid]
                reading = {
                    "t": nowt, "ts": _iso(nowt),
                    "voltage": round(voltage, 1),
                    "frequency": round(frequency, 2),
                    "battery": round(battery, 2),
                    "fuel": round(fuel_l, 1),
                    "fuel_percent": round(100.0 * fuel_l / g["fuel_capacity_l"], 1),
                    "engine": engine, "loaded": loaded,
                }
                self._latest[gid] = reading
                self._series[gid].append(reading)
                sid = st["sid"]

            if engine:
                self._monitor(g, reading, sid, mode, inj, nowt)

    def _running_values(self, g, loaded: bool, inj) -> tuple[float, float]:
        v = 400.0 + random.uniform(-2.5, 2.5) - (6.0 if loaded else 0.0)
        f = 50.0 + random.uniform(-0.08, 0.08) - (0.08 if loaded else 0.0)
        if inj:
            kind = inj["kind"]
            if kind == "voltage_sag":
                v -= random.uniform(28, 40)
            elif kind == "voltage_surge":
                v += random.uniform(28, 40)
            elif kind == "freq_high":
                f += random.uniform(0.7, 1.0)
        return v, f

    def _battery_rest(self, g) -> float:
        base = g["battery_voltage"]
        return base + math.sin(time.time() / 17.0) * 0.15 + random.uniform(-0.08, 0.08)

    def _fuel_rate(self, g, loaded: bool, mode: str, inj) -> float:
        if loaded:
            rate = g["rated_power_kw"] * config.LOAD_FACTOR * config.SPECIFIC_FUEL_L_PER_KWH / 60.0
        else:
            rate = config.IDLE_L_PER_MIN
        # 漏油：额外耗油，供适配器产生漏油留痕
        if mode == "oil_leak" or (inj and inj["kind"] == "leak"):
            rate += 0.45
        return rate

    # ---------- 越限/漏油监测 ----------
    def _monitor(self, g, reading: dict, sid: str | None, mode: str, inj, nowt: float) -> None:
        if self.svc is None:
            return
        gid = g["id"]
        v, f = reading["voltage"], reading["frequency"]
        bad = not (config.VOLTAGE_MIN <= v <= config.VOLTAGE_MAX and
                   config.FREQ_MIN <= f <= config.FREQ_MAX)
        last = self._range_alert_at.get(gid, 0.0)
        if bad and nowt - last > 15:
            self._range_alert_at[gid] = nowt
            self.svc.log_event(
                "voltage", "critical",
                f"运行参数越限：{g['name']} {v:.1f}V / {f:.2f}Hz"
                f"（合格区间 {config.VOLTAGE_MIN:.0f}-{config.VOLTAGE_MAX:.0f}V，"
                f"{config.FREQ_MIN}-{config.FREQ_MAX}Hz）",
                generator_id=gid, session_id=sid,
                detail={"voltage": v, "frequency": f, "loaded": reading["loaded"]})
        if (mode == "oil_leak" or (inj and inj["kind"] == "leak")) and gid not in self._leak_alerted:
            self._leak_alerted.add(gid)
            self.svc.log_event("oil_leak", "critical",
                               f"运行中监测到燃油异常消耗，疑似漏油：{g['name']}",
                               generator_id=gid, session_id=sid,
                               detail={"fuel_percent": reading["fuel_percent"]})


class ReminderScheduler:
    """进程内任务：到期计划提醒 + 低油位提醒，去重后落库。"""

    def __init__(self, svc, hub: TelemetryHub | None = None,
                 interval: float = config.REMIND_INTERVAL_SEC):
        self.svc = svc
        self.hub = hub
        self.interval = interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="reminders", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.scan()
            except Exception:
                pass
            self._stop.wait(self.interval)

    def scan_once(self) -> list[dict]:
        """供测试调用：执行一次扫描并返回新产生的提醒。"""
        before = {r["id"] for r in self.svc.list_reminders()}
        self.scan()
        return [r for r in self.svc.list_reminders() if r["id"] not in before]

    def scan(self) -> None:
        # 到期计划
        for p in self.svc.due_plans():
            exists = self.svc.db.query_one(
                "SELECT id FROM reminders WHERE kind='plan_due' AND plan_id=? AND acknowledged=0",
                (p["id"],))
            if not exists:
                self.svc.db.execute(
                    "INSERT INTO reminders(ts,kind,generator_id,plan_id,message) VALUES(?,?,?,?,?)",
                    (_iso(), "plan_due", p["generator_id"], p["id"],
                     f"试运行计划到期：{p['generator_name']}，计划 {p['scheduled_at']}，请值班员执行"))

        # 低油位（以物理液位计为准）
        for g in self.svc.db.query("SELECT * FROM generators"):
            fuel_l = self.hub.gauge_fuel(g["id"]) if self.hub else g["fuel_level_l"]
            pct = 100.0 * fuel_l / g["fuel_capacity_l"]
            if pct < config.LOW_FUEL_PERCENT:
                exists = self.svc.db.query_one(
                    "SELECT id FROM reminders WHERE kind='low_fuel' AND generator_id=?"
                    " AND acknowledged=0", (g["id"],))
                if not exists:
                    self.svc.db.execute(
                        "INSERT INTO reminders(ts,kind,generator_id,message) VALUES(?,?,?,?)",
                        (_iso(), "low_fuel", g["id"],
                         f"低油位告警：{g['name']} 液位 {pct:.1f}%（阈值 {config.LOW_FUEL_PERCENT:.0f}%），请安排加油"))
