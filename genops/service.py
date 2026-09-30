"""领域服务：试运行会话互斥、步骤顺序、审批权限、维修封锁、燃油平衡与事件留痕。"""
from __future__ import annotations

import json
import threading
import uuid
from datetime import datetime, timedelta
from typing import Any

from . import config
from .db import Database


def now() -> datetime:
    return datetime.now()


def now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def parse_dt(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%d %H:%M:%S")


class DomainError(Exception):
    def __init__(self, message: str, code: str = "domain_error", status: int = 400):
        super().__init__(message)
        self.code = code
        self.status = status


class PermissionError_(DomainError):
    def __init__(self, message: str = "权限不足"):
        super().__init__(message, "forbidden", 403)


def _user(uid: str) -> tuple[str, str]:
    u = config.USERS.get(uid)
    if not u:
        raise DomainError(f"未知用户：{uid}", "unknown_user")
    return u  # (name, role)


def require_supervisor(uid: str) -> tuple[str, str]:
    name, role = _user(uid)
    if role != "supervisor":
        raise PermissionError_("该操作仅工程主管可执行")
    return name, role


STEP_BY_CODE = {c: (t, s) for c, t, s in config.STEPS}
STEP_ORDER = [c for c, _, _ in config.STEPS]


class GenOpsService:
    def __init__(self, db: Database, hub: "TelemetryHub | None" = None):  # noqa: F821
        self.db = db
        self.hub = hub

    # ---------- 工具 ----------
    def log_event(self, category: str, severity: str, message: str, *,
                  generator_id: str | None = None, plan_id: str | None = None,
                  session_id: str | None = None, ref_id: str | None = None,
                  detail: dict | None = None) -> int:
        cur = self.db.execute(
            "INSERT INTO events(ts,category,severity,generator_id,plan_id,session_id,ref_id,message,detail_json)"
            " VALUES(?,?,?,?,?,?,?,?,?)",
            (now_iso(), category, severity, generator_id, plan_id, session_id, ref_id, message,
             json.dumps(detail, ensure_ascii=False) if detail else None),
        )
        return cur.lastrowid

    def row(self, table: str, _id: str, id_col: str = "id"):
        r = self.db.query_one(f"SELECT * FROM {table} WHERE {id_col}=?", (_id,))
        if not r:
            raise DomainError(f"{table} 记录不存在：{_id}", "not_found", 404)
        return r

    # ---------- 机组 ----------
    def list_generators(self) -> list[dict]:
        out = []
        for g in self.db.query("SELECT * FROM generators ORDER BY id"):
            d = dict(g)
            d["fuel_percent"] = round(100.0 * d["fuel_level_l"] / d["fuel_capacity_l"], 1)
            latest = self.hub.latest(g["id"]) if self.hub else None
            d["telemetry"] = latest
            d["low_fuel"] = d["fuel_percent"] < config.LOW_FUEL_PERCENT
            out.append(d)
        return out

    def capacity_summary(self) -> dict:
        gens = self.db.query("SELECT * FROM generators")
        total = avail = locked = fault = 0.0
        for g in gens:
            total += g["rated_power_kw"]
            if g["locked_out"]:
                locked += g["rated_power_kw"]
            elif g["status"] in ("start_failed", "shutdown"):
                fault += g["rated_power_kw"]
            else:
                avail += g["rated_power_kw"]
        return {"total_kw": total, "available_kw": avail,
                "locked_kw": locked, "fault_kw": fault,
                "available_percent": round(100 * avail / total, 1) if total else 0.0}

    def set_lockout(self, gen_id: str, reason: str, user: str) -> dict:
        """上锁：值班员与主管均可出于安全上锁；上锁机组立即中止其运行会话。"""
        name, role = _user(user)
        g = self.row("generators", gen_id)
        if not reason.strip():
            raise DomainError("必须填写封锁原因")
        self.db.execute("UPDATE generators SET locked_out=1,lock_reason=?,status='locked_out' WHERE id=?",
                        (reason.strip(), gen_id))
        active = self.db.query_one(
            "SELECT * FROM sessions WHERE generator_id=? AND ended_at IS NULL", (gen_id,))
        if active:
            self._terminate_session(active, "shutdown", user,
                                    note=f"机组维修封锁，运行会话中止：{reason}")
        self.log_event("lockout", "critical", f"机组 {g['name']} 维修封锁：{reason}",
                       generator_id=gen_id, detail={"by": name, "reason": reason})
        return dict(self.row("generators", gen_id))

    def clear_lockout(self, gen_id: str, user: str) -> dict:
        """解封仅工程主管可执行。"""
        name, role = require_supervisor(user)
        g = self.row("generators", gen_id)
        if not g["locked_out"]:
            raise DomainError("机组当前未封锁")
        self.db.execute(
            "UPDATE generators SET locked_out=0,lock_reason=NULL,status='standby',fault=NULL WHERE id=?",
            (gen_id,))
        self.log_event("lockout_clear", "info", f"机组 {g['name']} 解除维修封锁，恢复待命",
                       generator_id=gen_id, detail={"by": name})
        return dict(self.row("generators", gen_id))

    # ---------- 计划与审批 ----------
    def create_plan(self, gen_id: str, scheduled_at: str, duration_min: int,
                    assignee: str, user: str) -> dict:
        g = self.row("generators", gen_id)
        try:
            dt = parse_dt(scheduled_at)
        except (ValueError, TypeError):
            raise DomainError("计划时间格式应为 YYYY-MM-DD HH:MM:SS")
        if duration_min <= 0:
            raise DomainError("试运行时长必须大于 0")
        _user(assignee)
        name, _ = _user(user)
        pid = "PL-" + uuid.uuid4().hex[:8].upper()
        self.db.execute(
            "INSERT INTO plans(id,generator_id,scheduled_at,duration_min,assignee,status,created_at,created_by)"
            " VALUES(?,?,?,?,?,'pending',?,?)",
            (pid, gen_id, scheduled_at, duration_min, assignee, now_iso(), user))
        self.log_event("plan_created", "info",
                       f"新建试运行计划：{g['name']}，{scheduled_at}，时长 {duration_min} 分钟",
                       plan_id=pid, generator_id=gen_id, detail={"by": name})
        return dict(self.row("plans", pid))

    def decide_plan(self, plan_id: str, decision: str, reason: str, user: str) -> dict:
        name, role = require_supervisor(user)
        if decision not in ("approved", "rejected"):
            raise DomainError("审批结论只能是 approved/rejected")
        p = self.row("plans", plan_id)
        if p["status"] != "pending":
            raise DomainError(f"计划当前状态为 {p['status']}，无法审批")
        self.db.execute("UPDATE plans SET status=? WHERE id=?", (decision, plan_id))
        aid = "AP-" + uuid.uuid4().hex[:8].upper()
        self.db.execute(
            "INSERT INTO approvals(id,plan_id,kind,reason,decision,approver,approver_role,created_at)"
            " VALUES(?,?, 'plan', ?, ?, ?, ?, ?)",
            (aid, plan_id, reason or "（未填写）", decision, user, role, now_iso()))
        g = self.row("generators", p["generator_id"])
        self.log_event("plan_approval", "info" if decision == "approved" else "warning",
                       f"试运行计划{('获批' if decision == 'approved' else '被驳回')}：{g['name']}（{name}）",
                       plan_id=plan_id, generator_id=p["generator_id"], ref_id=aid,
                       detail={"by": name, "reason": reason})
        return dict(self.row("approvals", aid))

    def cancel_plan(self, plan_id: str, reason: str, user: str) -> dict:
        name, role = _user(user)
        p = self.row("plans", plan_id)
        if p["status"] in ("canceled", "completed", "failed", "running"):
            raise DomainError(f"计划当前状态为 {p['status']}，不能取消")
        if not reason.strip():
            raise DomainError("取消计划必须填写原因")
        self.db.execute("UPDATE plans SET status='canceled' WHERE id=?", (plan_id,))
        self.db.execute(
            "UPDATE reminders SET acknowledged=1 WHERE kind='plan_due' AND plan_id=?", (plan_id,))
        self.log_event("plan_cancel", "warning", f"试运行计划已取消：{reason}（{name}）",
                       plan_id=plan_id, generator_id=p["generator_id"],
                       detail={"by": name, "reason": reason, "old_status": p["status"]})
        return dict(self.row("plans", plan_id))

    def list_plans(self, status: str | None = None) -> list[dict]:
        sql = ("SELECT p.*, g.name AS generator_name, g.building AS building, g.locked_out AS g_locked,"
               " g.status AS g_status FROM plans p JOIN generators g ON g.id=p.generator_id")
        rows = self.db.query(sql + " ORDER BY p.scheduled_at DESC")
        out = []
        for r in rows:
            if status and r["status"] != status:
                continue
            d = dict(r)
            d["scheduled_dt"] = r["scheduled_at"]
            out.append(d)
        return out

    # ---------- 运行会话（互斥） ----------
    def start_session(self, plan_id: str, user: str) -> tuple[dict, bool]:
        """开始试运行。返回 (session, created)。两个值班员同时开始同一计划只保留一个会话。"""
        name, role = _user(user)
        p = self.row("plans", plan_id)
        g = self.row("generators", p["generator_id"])

        with self.db.lock:
            # 先查活动会话：两个值班员（无论先后）只拿到同一个运行会话
            existing = self.db.query_one(
                "SELECT * FROM sessions WHERE plan_id=? AND ended_at IS NULL", (plan_id,))
            if existing:
                return dict(existing), False

            if p["status"] != "approved":
                raise DomainError("计划尚未获批，不能执行试运行", "plan_not_approved", 409)
            if g["locked_out"]:
                raise DomainError(f"机组处于维修封锁（{g['lock_reason']}），不能启动", "locked_out", 409)
            other = self.db.query_one(
                "SELECT * FROM sessions WHERE generator_id=? AND ended_at IS NULL", (g["id"],))
            if other:
                raise DomainError("该机组已有进行中的运行会话", "session_exists", 409)

            sid = "SE-" + uuid.uuid4().hex[:8].upper()
            start_fuel = self.hub.gauge_fuel(g["id"]) if self.hub else g["fuel_level_l"]
            self.db.execute(
                "INSERT INTO sessions(id,plan_id,generator_id,operator_id,operator_name,started_at,"
                "current_step,start_fuel_l) VALUES(?,?,?,?,?,?, 'visual', ?)",
                (sid, plan_id, g["id"], user, name, now_iso(), start_fuel))
            for code, title, seq in config.STEPS:
                self.db.execute(
                    "INSERT INTO step_records(id,session_id,step_code,seq,status)"
                    " VALUES(?,?,?,?,'pending')",
                    (uuid.uuid4().hex, sid, code, seq))
            self.db.execute("UPDATE plans SET status='running' WHERE id=?", (plan_id,))
            self.db.execute("UPDATE generators SET status='running',fault=NULL WHERE id=?", (g["id"],))
            self.log_event("session_start", "info",
                           f"试运行开始：{g['name']}，值班员 {name}",
                           generator_id=g["id"], plan_id=plan_id, session_id=sid,
                           detail={"operator": name, "start_fuel_l": start_fuel})
            self.db.execute(
                "UPDATE reminders SET acknowledged=1 WHERE kind='plan_due' AND plan_id=?", (plan_id,))
            if self.hub:
                self.hub.mark_session(g["id"], sid, running_engine=False)
        return dict(self.row("sessions", sid)), True

    def get_session(self, session_id: str) -> dict:
        s = self.row("sessions", session_id)
        d = dict(s)
        d["steps"] = [dict(r) for r in self.db.query(
            "SELECT * FROM step_records WHERE session_id=? ORDER BY seq", (session_id,))]
        d["generator"] = dict(self.row("generators", s["generator_id"]))
        return d

    def active_session_for_plan(self, plan_id: str) -> dict | None:
        r = self.db.query_one("SELECT * FROM sessions WHERE plan_id=? AND ended_at IS NULL", (plan_id,))
        return dict(r) if r else None

    def _require_step(self, session, code: str):
        if code not in STEP_BY_CODE:
            raise DomainError("未知检查步骤")
        rec = self.db.query_one("SELECT * FROM step_records WHERE session_id=? AND step_code=?",
                                (session["id"], code))
        if rec["status"] != "pending":
            raise DomainError(f"步骤「{STEP_BY_CODE[code][0]}」状态为 {rec['status']}，不可重复提交")
        seq = STEP_BY_CODE[code][1]
        blockers = self.db.query(
            "SELECT step_code,status FROM step_records WHERE session_id=? AND seq<? AND status='pending'",
            (session["id"], seq))
        if blockers:
            names = "、".join(STEP_BY_CODE[r["step_code"]][0] for r in blockers)
            raise DomainError(f"必须先完成前置检查：{names}", "step_order", 409)
        return rec

    # ---------- 检查步骤 ----------
    def _advance(self, session_id: str) -> None:
        nxt = self.db.query_one(
            "SELECT step_code FROM step_records WHERE session_id=? AND status='pending' ORDER BY seq LIMIT 1",
            (session_id,))
        cur = nxt["step_code"] if nxt else "done"
        self.db.execute("UPDATE sessions SET current_step=? WHERE id=?", (cur, session_id))

    def submit_visual(self, session_id: str, user: str, passed: bool,
                      noise: bool = False, oil_leak: bool = False, note: str = "") -> dict:
        name, _ = _user(user)
        s = self.row("sessions", session_id)
        self._require_step(dict(s), "visual")
        g = self.row("generators", s["generator_id"])
        if oil_leak:
            self._mark_step(s, "visual", "fail", user, "发现机油/燃油渗漏：" + note)
            self.log_event("oil_leak", "critical",
                           f"外观检查发现漏油：{g['name']}，中止试运行",
                           generator_id=g["id"], session_id=session_id, plan_id=s["plan_id"],
                           detail={"by": name, "note": note})
            self._fail_session(dict(self.row("sessions", session_id)), user,
                               fault="外观检查发现漏油", abort_generation=False)
            raise DomainError("发现漏油，试运行已中止，建议立即维修封锁", "oil_leak", 409)
        if noise:
            self._mark_step(s, "visual", "fail", user, "发现异常噪声：" + note)
            self.log_event("noise", "critical",
                           f"外观检查发现异常噪声：{g['name']}，中止试运行",
                           generator_id=g["id"], session_id=session_id, plan_id=s["plan_id"],
                           detail={"by": name, "note": note})
            self._fail_session(dict(self.row("sessions", session_id)), user,
                               fault="机组异常噪声", abort_generation=False)
            raise DomainError("发现异常噪声，试运行已中止", "abnormal_noise", 409)
        if not passed:
            self._mark_step(s, "visual", "fail", user, note or "外观检查不合格")
            self._fail_session(dict(self.row("sessions", session_id)), user, fault="外观检查不合格",
                               abort_generation=False)
            raise DomainError("外观检查不合格，试运行中止", "visual_fail", 409)
        self._mark_step(s, "visual", "pass", user, note)
        self._advance(session_id)
        return self.get_session(session_id)

    def _gauge(self, gen_id: str, key: str, default: float) -> float:
        if self.hub:
            latest = self.hub.latest(gen_id)
            if latest and latest.get(key) is not None:
                return float(latest[key])
        return default

    def submit_fuel_check(self, session_id: str, user: str, note: str = "") -> dict:
        name, _ = _user(user)
        s = self.row("sessions", session_id)
        self._require_step(dict(s), "fuel")
        g = self.row("generators", s["generator_id"])
        level_l = self._gauge(g["id"], "fuel", g["fuel_level_l"])
        pct = 100.0 * level_l / g["fuel_capacity_l"]
        ok = pct >= config.FUEL_READY_PERCENT
        detail = {"level_l": round(level_l, 1), "percent": round(pct, 1),
                  "min_percent": config.FUEL_READY_PERCENT}
        if not ok:
            self._mark_step(s, "fuel", "fail", user,
                            f"油位 {pct:.1f}% 低于 {config.FUEL_READY_PERCENT:.0f}%")
            self.log_event("fuel_low_check", "warning",
                           f"油位检查不合格：{g['name']} 油位 {pct:.1f}%",
                           generator_id=g["id"], session_id=session_id, plan_id=s["plan_id"],
                           detail=detail)
            self._fail_session(dict(self.row("sessions", session_id)), user,
                               fault="燃油不足", abort_generation=False)
            raise DomainError(f"油位 {pct:.1f}% 低于最低要求，试运行中止", "fuel_low", 409)
        self._mark_step(s, "fuel", "pass", user, note or f"油位 {pct:.1f}%")
        self.log_event("step_pass", "info", f"油位检查合格：{g['name']} {pct:.1f}%",
                       generator_id=g["id"], session_id=session_id, detail=detail)
        self._advance(session_id)
        return self.get_session(session_id)

    def submit_battery_check(self, session_id: str, user: str, note: str = "") -> dict:
        s = self.row("sessions", session_id)
        self._require_step(dict(s), "battery")
        g = self.row("generators", s["generator_id"])
        bv = self._gauge(g["id"], "battery", g["battery_voltage"])
        ok = config.BATTERY_MIN <= bv <= config.BATTERY_MAX
        if not ok:
            self._mark_step(s, "battery", "fail", user, f"电池电压 {bv:.1f}V 越限")
            self.log_event("battery", "warning",
                           f"启动电池电压越限：{g['name']} {bv:.1f}V"
                           f"（合格区间 {config.BATTERY_MIN}-{config.BATTERY_MAX}V）",
                           generator_id=g["id"], session_id=session_id, plan_id=s["plan_id"],
                           detail={"voltage": bv})
            self._fail_session(dict(self.row("sessions", session_id)), user,
                               fault="启动电池电压越限", abort_generation=False)
            raise DomainError(f"电池电压 {bv:.1f}V 不在合格区间", "battery_fail", 409)
        self._mark_step(s, "battery", "pass", user, note or f"电池 {bv:.1f}V")
        self._advance(session_id)
        return self.get_session(session_id)

    def attempt_start(self, session_id: str, user: str) -> dict:
        """空载启动：由模拟适配器决定启动成败。"""
        name, _ = _user(user)
        s = self.row("sessions", session_id)
        self._require_step(dict(s), "start_noload")
        g = self.row("generators", s["generator_id"])
        if self.hub:
            result = self.hub.attempt_start(g["id"])
        else:
            result = "ok" if g["sim_mode"] != "start_fail" else "fail"

        if result != "ok":
            self._mark_step(s, "start_noload", "fail", user, "启动失败：" + result)
            self.log_event("start_fail", "critical",
                           f"启动失败：{g['name']}（{result}）",
                           generator_id=g["id"], session_id=session_id, plan_id=s["plan_id"],
                           detail={"reason": result, "by": name})
            self._fail_session(dict(self.row("sessions", session_id)), user,
                               fault=f"启动失败：{result}", abort_generation=False,
                               gen_status="start_failed")
            raise DomainError(f"机组启动失败：{result}", "start_failed", 409)

        self._mark_step(s, "start_noload", "pass", user, "一次启动成功，空载电压/频率正常")
        self.log_event("start_ok", "info", f"空载启动成功：{g['name']}",
                       generator_id=g["id"], session_id=session_id, plan_id=s["plan_id"])
        if self.hub:
            self.hub.mark_session(g["id"], session_id, running_engine=True, loaded=False)
        self._advance(session_id)
        return self.get_session(session_id)

    def begin_load_test(self, session_id: str, user: str) -> dict:
        name, _ = _user(user)
        s = self.row("sessions", session_id)
        self._require_step(dict(s), "load_test")
        g = self.row("generators", s["generator_id"])
        self.db.execute("UPDATE sessions SET load_started_at=? WHERE id=?",
                        (now_iso(), session_id))
        if self.hub:
            self.hub.mark_session(g["id"], session_id, running_engine=True, loaded=True)
        self.log_event("load_begin", "info", f"带载测试开始：{g['name']}，值班员 {name}",
                       generator_id=g["id"], session_id=session_id, plan_id=s["plan_id"])
        return self.get_session(session_id)

    def abort_load_test(self, session_id: str, user: str, reason: str) -> dict:
        name, _ = _user(user)
        s = self.row("sessions", session_id)
        rec = self.db.query_one("SELECT * FROM step_records WHERE session_id=? AND step_code='load_test'",
                                (session_id,))
        g = self.row("generators", s["generator_id"])
        if rec["status"] != "pending":
            raise DomainError("带载测试已结束")
        self._mark_step(s, "load_test", "fail", user, f"带载中断：{reason}")
        self.log_event("load_abort", "critical", f"带载中断：{g['name']}（{reason}）",
                       generator_id=g["id"], session_id=session_id, plan_id=s["plan_id"],
                       detail={"reason": reason, "by": name})
        self._fail_session(dict(self.row("sessions", session_id)), user,
                           fault=f"带载中断：{reason}", abort_generation=True,
                           gen_status="shutdown")
        raise DomainError("带载测试已中断，机组故障停机", "load_abort", 409)

    def complete_load_test(self, session_id: str, user: str, note: str = "") -> dict:
        name, _ = _user(user)
        s = self.row("sessions", session_id)
        self._require_step(dict(s), "load_test")
        g = self.row("generators", s["generator_id"])
        latest = self.hub.latest(g["id"]) if self.hub else None
        v = latest["voltage"] if latest else 400.0
        f = latest["frequency"] if latest else 50.0
        if not (config.VOLTAGE_MIN <= v <= config.VOLTAGE_MAX and
                config.FREQ_MIN <= f <= config.FREQ_MAX):
            self._mark_step(s, "load_test", "fail", user,
                            f"带载参数越限：{v:.1f}V / {f:.2f}Hz")
            self.log_event("voltage", "critical",
                           f"带载参数越限：{g['name']} {v:.1f}V / {f:.2f}Hz，故障停机",
                           generator_id=g["id"], session_id=session_id, plan_id=s["plan_id"],
                           detail={"voltage": v, "frequency": f})
            self._fail_session(dict(self.row("sessions", session_id)), user,
                               fault="电压/频率越限", abort_generation=True, gen_status="shutdown")
            raise DomainError("带载参数越限，机组故障停机", "out_of_range", 409)
        self._mark_step(s, "load_test", "pass", user, note or f"带载参数 {v:.1f}V / {f:.2f}Hz")
        if self.hub:
            self.hub.mark_session(g["id"], session_id, running_engine=True, loaded=False)
        self._advance(session_id)
        return self.get_session(session_id)

    def skip_step(self, session_id: str, step_code: str, user: str, reason: str) -> dict:
        """越过检查步骤：仅工程主管批准后生效，并留审批记录。"""
        name, role = require_supervisor(user)
        s = self.row("sessions", session_id)
        if s["ended_at"]:
            raise DomainError("会话已结束")
        rec = self.db.query_one("SELECT * FROM step_records WHERE session_id=? AND step_code=?",
                                (session_id, step_code))
        if not rec:
            raise DomainError("未知步骤")
        if rec["status"] != "pending":
            raise DomainError("该步骤已处理，无需越过")
        aid = "AP-" + uuid.uuid4().hex[:8].upper()
        self.db.execute(
            "INSERT INTO approvals(id,plan_id,kind,step_code,reason,decision,approver,approver_role,created_at)"
            " VALUES(?,?,'skip_step',?,?, 'approved', ?, ?, ?)",
            (aid, s["plan_id"], step_code, reason.strip() or "（未填写）", user, role, now_iso()))
        self.db.execute(
            "UPDATE step_records SET status='skipped',checked_at=?,operator_id=?,note=? WHERE id=?",
            (now_iso(), user, f"主管批准越步：{reason}", rec["id"]))
        self.db.execute("UPDATE sessions SET skip_granted=1 WHERE id=?", (session_id,))
        g = self.row("generators", s["generator_id"])
        self.log_event("step_skip", "warning",
                       f"工程主管批准越过检查步骤「{STEP_BY_CODE[step_code][0]}」：{g['name']}",
                       generator_id=g["id"], session_id=session_id, plan_id=s["plan_id"],
                       ref_id=aid, detail={"by": name, "step": step_code, "reason": reason})
        self._advance(session_id)
        return self.get_session(session_id)

    # ---------- 结束会话与燃油结算 ----------
    def end_session(self, session_id: str, user: str, *,
                    measured_remaining_l: float | None = None,
                    run_minutes: float | None = None, note: str = "") -> dict:
        name, _ = _user(user)
        s = self.row("sessions", session_id)
        if s["ended_at"]:
            raise DomainError("会话已结束")
        g = self.row("generators", s["generator_id"])

        pending = self.db.query(
            "SELECT step_code FROM step_records WHERE session_id=? AND status='pending'", (session_id,))
        if pending:
            names = "、".join(STEP_BY_CODE[r["step_code"]][0] for r in pending)
            raise DomainError(f"尚有步骤未完成：{names}", "steps_pending", 409)
        all_pass = self.db.query(
            "SELECT COUNT(*) c FROM step_records WHERE session_id=? AND status='fail'",
            (session_id,))[0]["c"] == 0

        started = parse_dt(s["started_at"])
        total_min = run_minutes if run_minutes is not None else \
            max(0.0, (now() - started).total_seconds() / 60.0)
        # 带载时长：带载完成时刻 - 带载开始时刻
        lr = self.db.query_one(
            "SELECT checked_at FROM step_records WHERE session_id=? AND step_code='load_test'",
            (session_id,))
        load_min = 0.0
        if s["load_started_at"] and lr and lr["checked_at"]:
            load_min = max(0.0, (parse_dt(lr["checked_at"]) -
                                 parse_dt(s["load_started_at"])).total_seconds() / 60.0)
        noload_min = max(0.0, total_min - load_min)

        start_fuel = s["start_fuel_l"] if s["start_fuel_l"] is not None else g["fuel_level_l"]
        if measured_remaining_l is None:
            measured_remaining_l = self.hub.gauge_fuel(g["id"]) if self.hub else g["fuel_level_l"]
        measured_remaining_l = round(
            max(0.0, min(float(measured_remaining_l), start_fuel)), 2)
        actual_consumed = round(start_fuel - measured_remaining_l, 2)

        loaded_lpm = g["rated_power_kw"] * config.LOAD_FACTOR * config.SPECIFIC_FUEL_L_PER_KWH / 60.0
        expected = round(noload_min * config.IDLE_L_PER_MIN + load_min * loaded_lpm, 2)
        diff = round(actual_consumed - expected, 2)
        discrepancy = abs(diff) > config.FUEL_TOLERANCE_L

        outcome = "completed" if all_pass else "failed"
        with self.db.lock:
            self.db.execute(
                "UPDATE sessions SET ended_at=?,outcome=?,end_fuel_l=?,measured_remaining_l=?,"
                "fuel_delta_l=?,run_minutes=? WHERE id=?",
                (now_iso(), outcome, measured_remaining_l, measured_remaining_l,
                 actual_consumed, round(total_min, 1), session_id))
            self._ledger(g["id"], -actual_consumed, "run_consumption", session_id, user,
                         f"试运行消耗：{g['name']}，运行 {total_min:.1f} 分钟（带载 {load_min:.1f} 分钟）")
            if discrepancy:
                self.log_event("fuel_discrepancy", "warning",
                               f"燃油差异：{g['name']} 实耗 {actual_consumed:.1f}L，"
                               f"理论 {expected:.1f}L，差额 {diff:+.1f}L",
                               generator_id=g["id"], session_id=session_id, plan_id=s["plan_id"],
                               detail={"actual_l": actual_consumed, "expected_l": expected,
                                       "diff_l": diff, "tolerance_l": config.FUEL_TOLERANCE_L,
                                       "run_minutes": round(total_min, 1),
                                       "load_minutes": round(load_min, 1)})
            self.db.execute("UPDATE plans SET status=? WHERE id=?",
                            ("completed" if all_pass else "failed", s["plan_id"]))
            if all_pass:
                self.db.execute(
                    "UPDATE generators SET status='standby',fuel_level_l=?,fault=NULL WHERE id=?",
                    (measured_remaining_l, g["id"]))
                if self.hub:
                    self.hub.sync_fuel(g["id"], measured_remaining_l)
                self.log_event("session_complete", "info",
                               f"试运行完成，机组恢复待命：{g['name']}，运行 {total_min:.1f} 分钟，"
                               f"耗油 {actual_consumed:.1f}L",
                               generator_id=g["id"], session_id=session_id, plan_id=s["plan_id"])
            else:
                self.db.execute("UPDATE generators SET fuel_level_l=? WHERE id=?",
                                (measured_remaining_l, g["id"]))
                if self.hub:
                    self.hub.sync_fuel(g["id"], measured_remaining_l)
                self.log_event("session_fail", "critical",
                               f"试运行未全部合格：{g['name']}，机组未恢复待命",
                               generator_id=g["id"], session_id=session_id, plan_id=s["plan_id"])
            if self.hub:
                self.hub.mark_session(g["id"], None, running_engine=False, loaded=False)
        return self.get_session(session_id)

    def _terminate_session(self, s, outcome: str, user: str, note: str = "") -> None:
        """外部原因（封锁等）中止会话。"""
        with self.db.lock:
            self.db.execute(
                "UPDATE sessions SET ended_at=?,outcome=? WHERE id=? AND ended_at IS NULL",
                (now_iso(), outcome, s["id"]))
            self.db.execute("UPDATE plans SET status='failed' WHERE id=?", (s["plan_id"],))
            if self.hub:
                self.hub.mark_session(s["generator_id"], None, running_engine=False)
            self.log_event("session_aborted", "critical", note,
                           generator_id=s["generator_id"], session_id=s["id"], plan_id=s["plan_id"])

    def _fail_session(self, s, user: str, *, fault: str, abort_generation: bool,
                      gen_status: str = "shutdown") -> None:
        with self.db.lock:
            self.db.execute("UPDATE sessions SET ended_at=?,outcome='failed',current_step='aborted' WHERE id=?",
                            (now_iso(), s["id"]))
            self.db.execute("UPDATE plans SET status='failed' WHERE id=?", (s["plan_id"],))
            self.db.execute("UPDATE generators SET status=?,fault=? WHERE id=?",
                            (gen_status, fault, s["generator_id"]))
            if self.hub:
                self.hub.mark_session(s["generator_id"], None, running_engine=False, loaded=False)

    def _mark_step(self, s, code: str, status: str, user: str, note: str) -> None:
        name, _ = _user(user)
        self.db.execute(
            "UPDATE step_records SET status=?,checked_at=?,operator_id=?,note=? WHERE session_id=? AND step_code=?",
            (status, now_iso(), user, note, s["id"], code))

    # ---------- 故障停机 ----------
    def list_shutdowns(self) -> list[dict]:
        rows = self.db.query(
            "SELECT e.*, g.name AS generator_name FROM events e LEFT JOIN generators g ON g.id=e.generator_id"
            " WHERE e.category IN ('start_fail','voltage','noise','oil_leak','load_abort','battery')"
            " ORDER BY e.ts DESC")
        return [dict(r) for r in rows]

    # ---------- 加油台账 ----------
    def _ledger(self, gen_id: str, change_l: float, reason: str, ref_id: str,
                user: str, note: str = "") -> None:
        bal = self.db.query_one(
            "SELECT balance_l FROM fuel_ledger WHERE generator_id=? ORDER BY id DESC LIMIT 1",
            (gen_id,))
        balance = (bal["balance_l"] if bal else
                   self.db.query_one("SELECT fuel_level_l FROM generators WHERE id=?", (gen_id,))[
                       "fuel_level_l"]) + change_l
        self.db.execute(
            "INSERT INTO fuel_ledger(generator_id,change_l,balance_l,reason,ref_id,created_at,created_by)"
            " VALUES(?,?,?,?,?,?,?)",
            (gen_id, round(change_l, 2), round(balance, 2), reason, ref_id, now_iso(), user))
        self.db.execute("UPDATE generators SET fuel_level_l=? WHERE id=?",
                        (round(balance, 2), gen_id))

    def fuel_balance_ok(self, gen_id: str) -> bool:
        g = self.row("generators", gen_id)
        s = self.db.query_one(
            "SELECT COALESCE(SUM(change_l),0) s FROM fuel_ledger WHERE generator_id=?", (gen_id,))
        # 初始台账以 init 记录锚定，故累计和应等于当前油位
        return abs(s["s"] - g["fuel_level_l"]) < 0.01

    def create_refuel(self, gen_id: str, liters: float, user: str, note: str = "") -> dict:
        """登记加油：进入待第二人复核状态，暂不改动账面。"""
        name, _ = _user(user)
        g = self.row("generators", gen_id)
        if liters <= 0:
            raise DomainError("加油数量必须大于 0")
        rid = "RF-" + uuid.uuid4().hex[:8].upper()
        before = g["fuel_level_l"]
        self.db.execute(
            "INSERT INTO refuel_records(id,generator_id,liters,recorded_balance_l,before_l,after_l,"
            "note,status,recorder_id,recorder_name,recorded_at) VALUES(?,?,?,?,?,?,"
            "?, 'pending_second_check', ?, ?, ?)",
            (rid, gen_id, liters, before, before, min(before + liters, g["fuel_capacity_l"]),
             note, user, name, now_iso()))
        self.log_event("refuel_recorded", "info",
                       f"加油登记待复核：{g['name']} {liters:.1f}L（登记人 {name}）",
                       generator_id=gen_id, ref_id=rid, detail={"liters": liters, "by": name})
        return dict(self.row("refuel_records", rid))

    def review_refuel(self, rid: str, user: str, approved: bool, *,
                      revised_liters: float | None = None, note: str = "") -> dict:
        """第二人复核：复核人不得是登记人；修订数量也由该第二人确认后入账。"""
        name, role = _user(user)
        r = self.row("refuel_records", rid)
        if r["status"] != "pending_second_check":
            raise DomainError("该加油记录已复核")
        if user == r["recorder_id"]:
            raise PermissionError_("加油数量必须由第二人复核，登记人不能复核本人记录")
        g = self.row("generators", r["generator_id"])
        if not approved:
            self.db.execute(
                "UPDATE refuel_records SET status='rejected',reviewer_id=?,reviewer_name=?,"
                "reviewed_at=?,review_note=? WHERE id=?",
                (user, name, now_iso(), note, rid))
            self.log_event("refuel_rejected", "warning",
                           f"加油记录被复核驳回：{g['name']} {r['liters']:.1f}L（{name}）",
                           generator_id=g["id"], ref_id=rid)
            return dict(self.row("refuel_records", rid))

        final_liters = float(revised_liters) if revised_liters is not None else r["liters"]
        if final_liters <= 0:
            raise DomainError("复核后加油数量必须大于 0")
        revised = abs(final_liters - r["liters"]) > 0.001
        status = "revised" if revised else "confirmed"
        before = g["fuel_level_l"]
        accepted_liters = min(final_liters, g["fuel_capacity_l"] - before)
        if accepted_liters <= 0:
            raise DomainError("油箱已满，无法加油")
        overflow = final_liters - accepted_liters
        after = before + accepted_liters
        if overflow > 0.01:
            # 实际入库按可容纳量截断，记录按实收数量保存
            revised = True
            status = "revised"
        with self.db.lock:
            self.db.execute(
                "UPDATE refuel_records SET status=?,reviewer_id=?,reviewer_name=?,reviewed_at=?,"
                "review_note=?,revised_liters=?,liters=?,after_l=? WHERE id=?",
                (status, user, name, now_iso(), note,
                 r["liters"] if revised else None, accepted_liters, after, rid))
            self._ledger(g["id"], accepted_liters, "refuel", rid, user,
                         f"加油入库：{g['name']} {accepted_liters:.1f}L"
                         + (f"（申报 {final_liters:.1f}L，超容量截断/修订）" if revised else ""))
            if overflow > 0.01:
                self.log_event("refuel_overflow", "warning",
                               f"加油超出油箱容量 {overflow:.1f}L，实际入库 {accepted_liters:.1f}L",
                               generator_id=g["id"], ref_id=rid)
            if self.hub:
                self.hub.sync_fuel(g["id"], after)
            if overflow > 0.01:
                self.log_event("refuel_overflow", "warning",
                               f"加油超出油箱容量 {overflow:.1f}L，已按容量截断",
                               generator_id=g["id"], ref_id=rid)
        self.log_event("refuel_confirmed", "info",
                       f"加油复核通过：{g['name']} 入库 {final_liters:.1f}L"
                       + (f"（原登记 {r['liters']:.1f}L，已修订）" if revised else "")
                       + f"，复核人 {name}",
                       generator_id=g["id"], ref_id=rid,
                       detail={"final_liters": final_liters,
                               "original_liters": r["liters"], "revised": revised})
        return dict(self.row("refuel_records", rid))

    def list_refuels(self) -> list[dict]:
        rows = self.db.query(
            "SELECT r.*, g.name AS generator_name FROM refuel_records r"
            " JOIN generators g ON g.id=r.generator_id ORDER BY r.recorded_at DESC")
        return [dict(r) for r in rows]

    def fuel_ledger_rows(self, gen_id: str | None = None) -> list[dict]:
        sql = ("SELECT l.*, g.name AS generator_name FROM fuel_ledger l"
               " JOIN generators g ON g.id=l.generator_id")
        if gen_id:
            rows = self.db.query(sql + " WHERE l.generator_id=? ORDER BY l.id DESC", (gen_id,))
        else:
            rows = self.db.query(sql + " ORDER BY l.id DESC LIMIT 200")
        return [dict(r) for r in rows]

    # ---------- 审批 / 事件 / 提醒 ----------
    def list_approvals(self) -> list[dict]:
        rows = self.db.query(
            "SELECT a.*, p.generator_id, g.name AS generator_name FROM approvals a"
            " JOIN plans p ON p.id=a.plan_id JOIN generators g ON g.id=p.generator_id"
            " ORDER BY a.created_at DESC")
        return [dict(r) for r in rows]

    def list_events(self, category: str | None = None, limit: int = 200) -> list[dict]:
        if category:
            rows = self.db.query(
                "SELECT e.*, g.name AS generator_name FROM events e"
                " LEFT JOIN generators g ON g.id=e.generator_id WHERE e.category=?"
                " ORDER BY e.id DESC LIMIT ?", (category, limit))
        else:
            rows = self.db.query(
                "SELECT e.*, g.name AS generator_name FROM events e"
                " LEFT JOIN generators g ON g.id=e.generator_id ORDER BY e.id DESC LIMIT ?",
                (limit,))
        return [dict(r) for r in rows]

    def list_reminders(self, include_acked: bool = False) -> list[dict]:
        sql = "SELECT * FROM reminders"
        if not include_acked:
            sql += " WHERE acknowledged=0"
        rows = self.db.query(sql + " ORDER BY id DESC LIMIT 100")
        return [dict(r) for r in rows]

    def ack_reminder(self, rid: int, user: str) -> None:
        _user(user)
        self.db.execute("UPDATE reminders SET acknowledged=1 WHERE id=?", (rid,))

    def due_plans(self) -> list[dict]:
        """到期窗口内、已获批且未开始/未取消的计划。"""
        out = []
        hi = (now() + timedelta(minutes=config.PLAN_DUE_WINDOW_MIN)).strftime("%Y-%m-%d %H:%M:%S")
        for p in self.db.query(
                "SELECT p.*, g.name AS generator_name FROM plans p"
                " JOIN generators g ON g.id=p.generator_id"
                " WHERE p.status='approved' AND p.scheduled_at<=? ORDER BY p.scheduled_at", (hi,)):
            out.append(dict(p))
        return out

    # ---------- 附件 ----------
    def save_attachment(self, category: str, ref_id: str, filename: str, data: bytes,
                        content_type: str, user: str) -> dict:
        _user(user)
        aid = uuid.uuid4().hex
        stored = f"{aid}_{filename}"
        path = config.ATTACH_DIR / stored
        with open(path, "wb") as f:
            f.write(data)
        self.db.execute(
            "INSERT INTO attachments(id,category,ref_id,filename,stored_name,content_type,size,"
            "uploaded_by,uploaded_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (aid, category, ref_id, filename, stored, content_type, len(data), user, now_iso()))
        self.log_event("attachment", "info", f"现场附件已保存：{filename}", ref_id=aid,
                       detail={"category": category, "ref": ref_id, "size": len(data)})
        return {"id": aid, "filename": filename, "size": len(data)}

    def list_attachments(self, category: str | None = None, ref_id: str | None = None) -> list[dict]:
        sql, params = "SELECT * FROM attachments WHERE 1=1", []
        if category:
            sql += " AND category=?"; params.append(category)
        if ref_id:
            sql += " AND ref_id=?"; params.append(ref_id)
        return [dict(r) for r in self.db.query(sql + " ORDER BY id DESC", params)]
