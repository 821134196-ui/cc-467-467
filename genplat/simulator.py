"""模拟适配器与进程内提醒。

Simulator 后台线程按固定节拍为每台机组生成电压、频率、负载、液位等读数：
  * normal     —— 空载/带载参数落在合格区间；
  * start_fail —— 启动阶段无法建立电压（电池检查正常，空载启动判 E101）；
  * leak       —— 运行后读数携带 leak 标记，液位异常加快下降（易触发燃油差异）。
读数在会话运行时绑定 session_id，空闲时也持续刷新，供机组总览实时显示。

Reminder 周期扫描到期试运行计划与低油位机组，写入 alerts 并留痕。
"""

import random
import threading
import time
from datetime import datetime, timedelta, timezone

from . import config as C
from .db import now_iso


def _iso_now():
    return datetime.now(timezone.utc)


class Simulator:
    def __init__(self, db, interval=1.5, seed=42):
        self.db = db
        self.interval = interval
        self._stop = threading.Event()
        self._thread = None
        self._load_ramp = {}          # unit_id -> 0..1 加载进度
        self._rng = random.Random(seed)

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, name="simulator",
                                        daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _run(self):
        while not self._stop.wait(self.interval):
            try:
                self.tick()
            except Exception:
                # 模拟线程不能因单次异常退出
                pass

    # ---- 单次拍点（可在测试中直接调用） ----
    def tick(self):
        for u in self.db.query("SELECT * FROM units"):
            sess = self.db.one(
                "SELECT * FROM sessions WHERE unit_id=? AND status='running'",
                (u["id"],))
            vals, flags = self._sample(u, sess)
            sid = sess["id"] if sess else None
            self.db.execute(
                "INSERT INTO readings(unit_id,session_id,ts,voltage,frequency,"
                "load_kw,fuel_level,battery_voltage,oil_pressure,flags) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (u["id"], sid, now_iso(), vals["voltage"], vals["frequency"],
                 vals["load_kw"], vals["fuel_level"], vals["battery_voltage"],
                 vals["oil_pressure"], flags))
            self.db.execute(
                "UPDATE units SET fuel_level=?, battery_voltage=? WHERE id=?",
                (vals["fuel_level"], vals["battery_voltage"], u["id"]))

    def _phase(self, sess):
        """依据会话中第一个 pending 步骤判断机组所处阶段。"""
        if not sess:
            return "idle"
        steps = self.db.query(
            "SELECT step_code,status FROM session_steps WHERE session_id=? "
            "ORDER BY step_order", (sess["id"],))
        pending = [s["step_code"] for s in steps if s["status"] == "pending"]
        if not pending:
            return "running_end"
        cur = pending[0]
        if cur in ("visual", "fuel", "battery"):
            return "prestart"
        if cur == "no_load":
            return "no_load"
        if cur == "loaded":
            return "loaded"
        return "prestart"

    def _jitter(self, center, pct):
        return round(center * (1 + self._rng.uniform(-pct, pct)), 2)

    def _sample(self, u, sess):
        phase = self._phase(sess)
        mode = u["sim_mode"]
        level = u["fuel_level"]
        rated = u["rated_power_kw"]
        flags = ""

        if phase == "idle":
            if sess is None:
                self._load_ramp.pop(u["id"], None)
            return {
                "voltage": 0.0, "frequency": 0.0, "load_kw": 0.0,
                "fuel_level": round(level, 2),
                "battery_voltage": self._jitter(12.65, 0.01),
                "oil_pressure": 0.0,
            }, ""

        if phase == "prestart":
            # 启动失败工况：电池静态电压略低但仍过检，启动时才暴露
            bat = 12.55 if mode == "start_fail" else 12.7
            return {
                "voltage": 0.0, "frequency": 0.0, "load_kw": 0.0,
                "fuel_level": round(level, 2),
                "battery_voltage": self._jitter(bat, 0.008),
                "oil_pressure": 0.0,
            }, ""

        # ---- 已启动 ----
        if mode == "start_fail":
            # 盘车运转但建压失败：电压近零，频率不足，油压偏低
            return {
                "voltage": round(self._rng.uniform(0, 18), 2),
                "frequency": 0.0, "load_kw": 0.0,
                "fuel_level": round(max(0, level - 0.01), 2),
                "battery_voltage": self._jitter(10.9, 0.02),
                "oil_pressure": 0.6,
            }, "cranking"

        if phase == "no_load":
            vals = {
                "voltage": self._jitter(400, 0.005),
                "frequency": self._jitter(50.0, 0.003),
                "load_kw": round(rated * 0.05, 2),
                "fuel_level": round(max(0, level - self._drain(mode, False)), 2),
                "battery_voltage": self._jitter(13.4, 0.008),
                "oil_pressure": self._jitter(4.1, 0.03),
            }
            # 漏油工况在空载末期出现渗漏标记
            flags = "leak" if mode == "leak" and self._rng.random() < 0.5 else ""
            return vals, flags

        # loaded
        ramp = min(1.0, self._load_ramp.get(u["id"], 0.0) + 0.12)
        self._load_ramp[u["id"]] = ramp
        load = rated * (0.45 + 0.35 * ramp)
        vals = {
            "voltage": self._jitter(396, 0.006),
            "frequency": self._jitter(49.8, 0.004),
            "load_kw": round(load, 2),
            "fuel_level": round(max(0, level - self._drain(mode, True)), 2),
            "battery_voltage": self._jitter(13.5, 0.008),
            "oil_pressure": self._jitter(3.7, 0.03),
        }
        if mode == "leak":
            flags = "leak"
            # 渗漏时油压偶发波动，供现场判读
            vals["oil_pressure"] = self._jitter(3.1, 0.05)
        return vals, flags

    def _drain(self, mode, loaded):
        # 每拍液位下降（%）；漏油工况显著加快
        if not loaded:
            return 0.18 if mode == "leak" else 0.05
        return 0.55 if mode == "leak" else 0.16


class Reminder:
    """到期计划 / 低油位提醒。"""

    def __init__(self, db, interval=20.0, due_window_min=2.0):
        self.db = db
        self.interval = interval
        self.due_window = timedelta(minutes=due_window_min)
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, name="reminder",
                                        daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _run(self):
        while not self._stop.wait(self.interval):
            try:
                self.scan()
            except Exception:
                pass

    def scan(self):
        now = _iso_now()
        due_limit = (now + self.due_window).isoformat(timespec="seconds")
        # 1) 到期/临近的待执行计划（已批准、无运行会话、未提醒过）
        plans = self.db.query(
            "SELECT p.*, u.code AS unit_code FROM plans p JOIN units u "
            "ON u.id=p.unit_id WHERE p.status IN ('approved','pending') "
            "AND p.scheduled_at<=? ORDER BY p.scheduled_at", (due_limit,))
        for p in plans:
            exists = self.db.one(
                "SELECT id FROM alerts WHERE plan_id=? AND kind='due_plan'",
                (p["id"],))
            if exists:
                continue
            self._alert(
                "due_plan", p["unit_id"], p["id"], "warning",
                f"机组 {p['unit_code']} 试运行计划到期（{p['scheduled_at']}），"
                f"状态：{C.PLAN_STATUS[p['status']]}")
            self.db.add_event(C.EV["ALERT_DUE"], unit_id=p["unit_id"],
                              plan_id=p["id"], severity="warning",
                              detail={"scheduled_at": p["scheduled_at"]})

        # 2) 低油位（未封锁、未恢复前不重复提醒）
        for u in self.db.query("SELECT * FROM units WHERE fuel_level<?",
                               (C.LOW_FUEL_PCT,)):
            if u["status"] == C.UNIT_LOCKED:
                continue
            open_alert = self.db.one(
                "SELECT id FROM alerts WHERE unit_id=? AND kind='low_fuel' "
                "AND acknowledged=0 ORDER BY id DESC LIMIT 1", (u["id"],))
            if open_alert:
                continue
            self._alert(
                "low_fuel", u["id"], None, "critical",
                f"机组 {u['code']} 燃油液位 {u['fuel_level']:.1f}% 低于 "
                f"{C.LOW_FUEL_PCT:.0f}%，请安排补给")
            self.db.add_event(C.EV["ALERT_LOW_FUEL"], unit_id=u["id"],
                              severity="warning",
                              detail={"fuel_level": u["fuel_level"]})

    def _alert(self, kind, unit_id, plan_id, severity, message):
        return self.db.execute(
            "INSERT INTO alerts(ts,kind,unit_id,plan_id,message,severity) "
            "VALUES(?,?,?,?,?,?)",
            (now_iso(), kind, unit_id, plan_id, message, severity))

    def list_alerts(self, include_acked=False):
        sql = "SELECT * FROM alerts"
        if not include_acked:
            sql += " WHERE acknowledged=0"
        return self.db.query(sql + " ORDER BY id DESC LIMIT 50")

    def acknowledge(self, alert_id):
        self.db.execute("UPDATE alerts SET acknowledged=1 WHERE id=?",
                        (alert_id,))
