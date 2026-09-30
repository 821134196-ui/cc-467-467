"""领域服务：试运行审批门控、会话互斥、步骤顺序、维修封锁、
燃油平衡与加油复核、故障停机，以及全部事件留痕。

所有跨表写操作在事务内完成；并发约束同时由 SQLite 部分唯一索引兜底。
"""

import json
import os
import re
import sqlite3
import uuid
from datetime import datetime

from . import config as C
from .db import now_iso


class ServiceError(Exception):
    """业务规则拒绝；http 层映射为 409/403。"""

    def __init__(self, message, code="rule_violation", status=409):
        super().__init__(message)
        self.code = code
        self.status = status


def _parse_ts(s):
    return datetime.fromisoformat(s)


def _rated_burn_lph(kw):
    # 柴油机组近似满负荷油耗：0.25 L/KWh
    return kw * 0.25


class Service:
    def __init__(self, db, attach_dir=C.ATTACH_DIR, clock=None):
        self.db = db
        self.attach_dir = attach_dir
        self.clock = clock or now_iso

    # ================= 人员 =================
    def user(self, uid):
        u = self.db.one("SELECT * FROM users WHERE id=?", (uid,))
        if not u:
            raise ServiceError("未知用户", "unknown_user", 401)
        return u

    def _require_role(self, uid, *roles):
        u = self.user(uid)
        if u["role"] not in roles:
            raise ServiceError(
                f"当前身份「{u['name']}」无权执行该操作（需要 {'/'.join(roles)}）",
                "forbidden", 403)
        return u

    def list_users(self):
        return self.db.query("SELECT * FROM users ORDER BY role, id")

    # ================= 机组 =================
    def list_units(self):
        units = self.db.query("SELECT * FROM units ORDER BY building, code")
        available = sum(u["rated_power_kw"] for u in units
                        if u["status"] == C.UNIT_STANDBY)
        locked_kw = sum(u["rated_power_kw"] for u in units
                        if u["status"] == C.UNIT_LOCKED)
        for u in units:
            u["available_for_emergency"] = u["status"] == C.UNIT_STANDBY
            u["low_fuel"] = u["fuel_level"] < C.LOW_FUEL_PCT
        return {"units": units, "available_kw": available,
                "locked_out_kw": locked_kw,
                "total_kw": sum(u["rated_power_kw"] for u in units)}

    def get_unit(self, unit_id):
        u = self.db.one("SELECT * FROM units WHERE id=?", (unit_id,))
        if not u:
            raise ServiceError("机组不存在", "not_found", 404)
        return u

    def set_sim_mode(self, unit_id, mode, uid):
        if mode not in ("normal", "start_fail", "leak"):
            raise ServiceError("未知工况", status=400)
        self.get_unit(unit_id)
        self.db.execute("UPDATE units SET sim_mode=? WHERE id=?", (mode, unit_id))
        self.db.add_event("sim_mode_changed", unit_id=unit_id, actor_id=uid,
                          detail={"mode": mode}, severity="info")

    # ---- 维修封锁 ----
    def set_lockout(self, unit_id, reason, uid):
        u = self.get_unit(unit_id)
        self._require_role(uid, C.ROLE_SUPERVISOR)
        if u["status"] == C.UNIT_RUNNING:
            raise ServiceError("机组运行中，须先停机再封锁", "unit_running")
        with self.db.transaction() as tx:
            tx.execute(
                "UPDATE units SET status=?, lockout_reason=?, lockout_by=?, "
                "lockout_at=?, fault_code=NULL WHERE id=?",
                (C.UNIT_LOCKED, reason, uid, self.clock(), unit_id))
            tx.add_event(C.EV["LOCKOUT_ON"], unit_id=unit_id, actor_id=uid,
                         severity="warning", detail={"reason": reason})

    def clear_lockout(self, unit_id, uid):
        u = self.get_unit(unit_id)
        self._require_role(uid, C.ROLE_SUPERVISOR)
        if u["status"] != C.UNIT_LOCKED:
            raise ServiceError("机组当前不在封锁状态", "not_locked")
        with self.db.transaction() as tx:
            tx.execute(
                "UPDATE units SET status=?, lockout_reason=NULL, lockout_by=NULL, "
                "lockout_at=NULL WHERE id=?", (C.UNIT_STANDBY, unit_id))
            tx.add_event(C.EV["LOCKOUT_OFF"], unit_id=unit_id, actor_id=uid)

    # ================= 计划与审批 =================
    def create_plan(self, unit_id, scheduled_at, note, uid):
        self._require_role(uid, C.ROLE_OPERATOR, C.ROLE_SUPERVISOR)
        self.get_unit(unit_id)
        datetime.fromisoformat(scheduled_at)  # 校验
        pid = self.db.execute(
            "INSERT INTO plans(unit_id,scheduled_at,note,status,created_by,"
            "created_at) VALUES(?,?,?,?,?,?)",
            (unit_id, scheduled_at, note or "", C.PLAN_DRAFT, uid, self.clock()))
        return self.get_plan(pid)

    def submit_plan(self, plan_id, uid):
        self._require_role(uid, C.ROLE_OPERATOR, C.ROLE_SUPERVISOR)
        p = self.get_plan(plan_id)
        if p["status"] != C.PLAN_DRAFT:
            raise ServiceError("仅草稿状态可提交审批", "bad_state")
        with self.db.transaction() as tx:
            tx.execute("UPDATE plans SET status=?, submitted_at=? WHERE id=?",
                       (C.PLAN_PENDING, self.clock(), plan_id))
            tx.add_event(C.EV["PLAN_SUBMITTED"], unit_id=p["unit_id"],
                         plan_id=plan_id, actor_id=uid)
        return self.get_plan(plan_id)

    def review_plan(self, plan_id, approve, uid, reason=""):
        """工程主管批准/驳回；批准是执行试运行的前置门。"""
        self._require_role(uid, C.ROLE_SUPERVISOR)
        p = self.get_plan(plan_id)
        if p["status"] != C.PLAN_PENDING:
            raise ServiceError("仅待批准计划可审批", "bad_state")
        new_status = C.PLAN_APPROVED if approve else C.PLAN_DRAFT
        etype = C.EV["PLAN_APPROVED"] if approve else C.EV["PLAN_REJECTED"]
        with self.db.transaction() as tx:
            tx.execute("UPDATE plans SET status=?, reviewed_by=?, reviewed_at=? "
                       "WHERE id=?", (new_status, uid, self.clock(), plan_id))
            tx.add_event(etype, unit_id=p["unit_id"], plan_id=plan_id,
                         actor_id=uid,
                         severity="info" if approve else "warning",
                         detail={"reason": reason})
        return self.get_plan(plan_id)

    def cancel_plan(self, plan_id, uid, reason):
        """取消留痕：待批准/已批准未执行的计划均可取消。"""
        p = self.get_plan(plan_id)
        active = self.db.one(
            "SELECT id FROM sessions WHERE plan_id=? AND status='running'",
            (plan_id,))
        if active:
            raise ServiceError("已有运行会话，不能取消计划", "has_session")
        if p["status"] not in (C.PLAN_PENDING, C.PLAN_APPROVED, C.PLAN_DRAFT):
            raise ServiceError("当前状态不可取消", "bad_state")
        with self.db.transaction() as tx:
            tx.execute("UPDATE plans SET status=?, cancel_reason=?, cancelled_by=?, "
                       "cancelled_at=? WHERE id=?",
                       (C.PLAN_CANCELLED, reason, uid, self.clock(), plan_id))
            tx.add_event(C.EV["PLAN_CANCELLED"], unit_id=p["unit_id"],
                         plan_id=plan_id, actor_id=uid, severity="warning",
                         detail={"reason": reason})
        return self.get_plan(plan_id)

    def get_plan(self, plan_id):
        p = self.db.one(
            "SELECT p.*, u.code AS unit_code, u.name AS unit_name, u.building "
            "FROM plans p JOIN units u ON u.id=p.unit_id WHERE p.id=?",
            (plan_id,))
        if not p:
            raise ServiceError("计划不存在", "not_found", 404)
        return p

    def list_plans(self, status=None):
        sql = ("SELECT p.*, u.code AS unit_code, u.name AS unit_name, u.building "
               "FROM plans p JOIN units u ON u.id=p.unit_id")
        args = ()
        if status:
            sql += " WHERE p.status=?"
            args = (status,)
        sql += " ORDER BY p.scheduled_at"
        return self.db.query(sql, args)

    # ================= 试运行会话 =================
    def active_session(self, unit_id=None, plan_id=None):
        sql = "SELECT * FROM sessions WHERE status='running'"
        args = []
        if unit_id:
            sql += " AND unit_id=?"; args.append(unit_id)
        if plan_id:
            sql += " AND plan_id=?"; args.append(plan_id)
        return self.db.one(sql + " LIMIT 1", tuple(args))

    def start_session(self, plan_id, uid):
        self._require_role(uid, C.ROLE_OPERATOR)
        p = self.get_plan(plan_id)
        u = self.get_unit(p["unit_id"])

        # 门控 1：计划必须已获批
        if p["status"] != C.PLAN_APPROVED:
            raise ServiceError(
                "计划尚未获批，不能执行试运行", "plan_not_approved", 403)
        # 门控 2：维修封锁 / 故障机组不能启动，并留痕
        if u["status"] in (C.UNIT_LOCKED, C.UNIT_FAULT):
            with self.db.transaction() as tx:
                tx.add_event(C.EV["SESSION_BLOCKED"], unit_id=u["id"],
                             plan_id=plan_id, actor_id=uid, severity="critical",
                             detail={"unit_status": u["status"],
                                     "lockout_reason": u["lockout_reason"]})
            label = C.UNIT_STATUS[u["status"]]
            raise ServiceError(f"机组处于{label}状态，禁止启动",
                               "unit_unavailable", 403)

        with self.db.transaction() as tx:
            try:
                sid = tx.execute(
                    "INSERT INTO sessions(plan_id,unit_id,started_by,started_at,"
                    "status,fuel_start_pct) VALUES(?,?,?,?, 'running',?)",
                    (plan_id, u["id"], uid, self.clock(), u["fuel_level"]))
            except sqlite3.IntegrityError:
                # 部分唯一索引：同一计划/同一机组已有运行会话
                raise ServiceError(
                    "该计划（或机组）已有运行会话，同一时刻只保留一个",
                    "session_exists", 409)
            for s in C.STEPS:
                tx.execute(
                    "INSERT INTO session_steps(session_id,step_code,step_order,"
                    "status) VALUES(?,?,?,'pending')",
                    (sid, s["code"], s["order"]))
            tx.execute("UPDATE units SET status=? WHERE id=?",
                       (C.UNIT_RUNNING, u["id"]))
            tx.add_event(C.EV["SESSION_STARTED"], unit_id=u["id"],
                         plan_id=plan_id, session_id=sid, actor_id=uid)
        return self.get_session(sid)

    def get_session(self, sid):
        s = self.db.one(
            "SELECT s.*, p.scheduled_at, u.code AS unit_code, u.name AS unit_name,"
            " u.building, u.sim_mode FROM sessions s "
            "JOIN plans p ON p.id=s.plan_id JOIN units u ON u.id=s.unit_id "
            "WHERE s.id=?", (sid,))
        if not s:
            raise ServiceError("会话不存在", "not_found", 404)
        s["steps"] = self.db.query(
            "SELECT * FROM session_steps WHERE session_id=? ORDER BY step_order",
            (sid,))
        return s

    def list_sessions(self, unit_id=None, status=None):
        sql = ("SELECT s.*, u.code AS unit_code, u.name AS unit_name "
               "FROM sessions s JOIN units u ON u.id=s.unit_id WHERE 1=1")
        args = []
        if unit_id:
            sql += " AND s.unit_id=?"; args.append(unit_id)
        if status:
            sql += " AND s.status=?"; args.append(status)
        sql += " ORDER BY s.started_at DESC LIMIT 100"
        return self.db.query(sql, tuple(args))

    # ---- 步骤执行 ----
    # 配置指标名 -> readings 列名（空载/带载共用电压频率列，靠会话阶段区分）
    _METRIC_COL = {
        "fuel_level": "fuel_level",
        "battery_voltage": "battery_voltage",
        "no_load_v": "voltage", "no_load_f": "frequency",
        "load_v": "voltage", "load_f": "frequency",
    }

    def _latest_reading(self, unit_id, session_id):
        return self.db.one(
            "SELECT * FROM readings WHERE unit_id=? AND session_id=? "
            "ORDER BY ts DESC, id DESC LIMIT 1", (unit_id, session_id))

    def _rv(self, reading, metric):
        if reading is None:
            return None
        return reading[self._METRIC_COL.get(metric, metric)]

    def _evaluate(self, step_code, reading):
        """返回 (measured dict, ok bool|None)。visual 无数值核对。"""
        spec = C.STEP_BY_CODE[step_code]
        if "metric" not in spec:
            return {}, None
        val = self._rv(reading, spec["metric"])
        measured = {spec["metric"]: val}
        ok = val is not None and spec["min"] <= val <= spec["max"]
        if spec.get("extra"):
            ex = spec["extra"]
            ev = self._rv(reading, ex["metric"])
            measured[ex["metric"]] = ev
            ok = ok and ev is not None and ex["min"] <= ev <= ex["max"]
        return measured, ok

    def complete_step(self, session_id, step_code, uid, note="",
                      visual_confirmed=None, fail_reason=""):
        self._require_role(uid, C.ROLE_OPERATOR)
        s = self.get_session(session_id)
        if s["status"] != C.SESS_RUNNING:
            raise ServiceError("会话已结束，不能再执行步骤", "session_closed")
        step = next((x for x in s["steps"] if x["step_code"] == step_code), None)
        if not step:
            raise ServiceError("步骤不存在", "not_found", 404)
        if step["status"] != "pending":
            raise ServiceError("该步骤已完成", "step_done")

        # 严格顺序：前一步必须合格，或凭主管批准跳过
        idx = C.STEP_ORDER.index(step_code)
        for prev_code in C.STEP_ORDER[:idx]:
            prev = next(x for x in s["steps"] if x["step_code"] == prev_code)
            if prev["status"] == "pass":
                continue
            if prev["status"] == "skipped":
                continue
            raise ServiceError(
                f"须先完成上一步「{C.STEP_BY_CODE[prev_code]['name']}」；"
                "若要越过检查步骤，须由工程主管批准",
                "out_of_order", 403)

        spec = C.STEP_BY_CODE[step_code]
        reading = self._latest_reading(s["unit_id"], session_id)
        measured, ok = self._evaluate(step_code, reading)

        # 外观检查为人工确认项
        if step_code == "visual":
            if fail_reason:
                return self._prestart_fail(s, step, uid, fail_reason, measured,
                                           note)
            if not visual_confirmed:
                raise ServiceError("请逐项确认外观无异常后提交", "confirm_required")
            ok = True

        # 启动失败：空载步骤电压/频率起不来
        if step_code == "no_load" and measured:
            v = measured.get("no_load_v")
            if v is None or v < 100:
                return self._fault_shutdown(
                    s, step, uid, C.EV["START_FAILURE"], "E101",
                    "启动失败：机组未建立电压", measured, note)
        # 电压越限
        if ok is False and step_code in ("no_load", "loaded") and \
                measured.get(spec["metric"]) is not None:
            v = measured[spec["metric"]]
            if not (spec["min"] <= v <= spec["max"]):
                return self._fault_shutdown(
                    s, step, uid, C.EV["VOLTAGE_OUT"], "E202",
                    f"电压越限：{v:.1f}V 超出 [{spec['min']}, {spec['max']}]V",
                    measured, note,
                    extra_event=(C.EV["LOAD_INTERRUPT"], "带载中断")
                    if step_code == "loaded" else None)
        # 带载频率异常也算带载中断
        if ok is False and step_code == "loaded":
            return self._fault_shutdown(
                s, step, uid, C.EV["LOAD_INTERRUPT"], "E303",
                "带载测试中断：运行参数超出合格区间", measured, note)
        # 油位/电池前置检查不达标：中止会话（非机械故障）
        if ok is False:
            return self._prestart_fail(
                s, step, uid,
                f"{spec['label']} {measured.get(spec['metric'])}"
                f"{spec['unit']} 不在 [{spec['min']}, {spec['max']}]{spec['unit']}",
                measured, note)

        with self.db.transaction() as tx:
            tx.execute(
                "UPDATE session_steps SET status='pass', started_at=COALESCE("
                "started_at,?), completed_at=?, operator_id=?, measured_json=?, "
                "note=? WHERE id=?",
                (self.clock(), self.clock(), uid,
                 json.dumps(measured, ensure_ascii=False), note, step["id"]))
            tx.add_event(C.EV["STEP_DONE"], unit_id=s["unit_id"],
                         session_id=session_id, step_code=step_code,
                         actor_id=uid, detail={"measured": measured})
        return self.get_session(session_id)

    def _prestart_fail(self, s, step, uid, reason, measured, note):
        """启动前检查不通过：会话中止，机组保持待命，步骤记 fail。"""
        with self.db.transaction() as tx:
            tx.execute("UPDATE session_steps SET status='fail', completed_at=?, "
                       "operator_id=?, measured_json=?, note=?, fail_reason=? "
                       "WHERE id=?",
                       (self.clock(), uid,
                        json.dumps(measured, ensure_ascii=False), note, reason,
                        step["id"]))
            tx.execute("UPDATE sessions SET status='aborted', ended_at=?, "
                       "summary=? WHERE id=?",
                       (self.clock(), reason, s["id"]))
            tx.execute("UPDATE units SET status=? WHERE id=?",
                       (C.UNIT_STANDBY, s["unit_id"]))
            tx.add_event(C.EV["STEP_FAILED"], unit_id=s["unit_id"],
                         session_id=s["id"], step_code=step["step_code"],
                         actor_id=uid, severity="warning",
                         detail={"reason": reason, "measured": measured})
            tx.add_event(C.EV["SESSION_ABORTED"], unit_id=s["unit_id"],
                         session_id=s["id"], actor_id=uid, severity="warning",
                         detail={"reason": reason})
        return self.get_session(s["id"])

    def _fault_shutdown(self, s, step, uid, event_type, fault_code, reason,
                        measured, note, extra_event=None):
        """运行阶段故障：步骤失败、机组故障停机并留痕。"""
        with self.db.transaction() as tx:
            tx.execute("UPDATE session_steps SET status='fail', completed_at=?, "
                       "operator_id=?, measured_json=?, note=?, fail_reason=? "
                       "WHERE id=?",
                       (self.clock(), uid,
                        json.dumps(measured, ensure_ascii=False), note, reason,
                        step["id"]))
            tx.execute("UPDATE sessions SET status='failed', ended_at=?, "
                       "summary=? WHERE id=?",
                       (self.clock(), reason, s["id"]))
            tx.execute("UPDATE units SET status=?, fault_code=? WHERE id=?",
                       (C.UNIT_FAULT, fault_code, s["unit_id"]))
            tx.add_event(event_type, unit_id=s["unit_id"], session_id=s["id"],
                         step_code=step["step_code"], actor_id=uid,
                         severity="critical",
                         detail={"reason": reason, "measured": measured})
            if extra_event:
                tx.add_event(extra_event[0], unit_id=s["unit_id"],
                             session_id=s["id"], step_code=step["step_code"],
                             actor_id=uid, severity="critical",
                             detail={"reason": extra_event[1]})
            tx.add_event(C.EV["SHUTDOWN_FAULT"], unit_id=s["unit_id"],
                         session_id=s["id"], actor_id=uid, severity="critical",
                         detail={"fault_code": fault_code, "reason": reason})
        return self.get_session(s["id"])

    # ---- 越过步骤：主管审批 ----
    def request_skip(self, session_id, step_code, uid, reason):
        self._require_role(uid, C.ROLE_OPERATOR)
        s = self.get_session(session_id)
        if s["status"] != C.SESS_RUNNING:
            raise ServiceError("会话已结束", "session_closed")
        if step_code not in C.STEP_BY_CODE:
            raise ServiceError("步骤不存在", "not_found", 404)
        step = next(x for x in s["steps"] if x["step_code"] == step_code)
        if step["status"] != "pending":
            raise ServiceError("只能申请越过尚未完成的检查步骤", "step_done")
        # 只能越过当前最早待执行的步骤，不能借越步跳过多个步骤
        first_pending = next((x["step_code"] for x in s["steps"]
                              if x["status"] == "pending"), None)
        if first_pending != step_code:
            raise ServiceError(
                "只能越过当前步骤；前面的检查须先完成或逐次获批",
                "out_of_order", 403)
        rid = self.db.execute(
            "INSERT INTO skip_requests(session_id,step_code,requested_by,reason,"
            "status,created_at) VALUES(?,?,?,?,'pending',?)",
            (session_id, step_code, uid, reason, self.clock()))
        self.db.add_event(C.EV["SKIP_REQUESTED"], unit_id=s["unit_id"],
                          session_id=session_id, step_code=step_code,
                          actor_id=uid, detail={"reason": reason})
        return self.get_skip(rid)

    def review_skip(self, request_id, approve, uid):
        self._require_role(uid, C.ROLE_SUPERVISOR)
        req = self.get_skip(request_id)
        if req["status"] != "pending":
            raise ServiceError("该越步申请已处理", "bad_state")
        status = "approved" if approve else "denied"
        with self.db.transaction() as tx:
            tx.execute("UPDATE skip_requests SET status=?, reviewed_by=?, "
                       "reviewed_at=? WHERE id=?",
                       (status, uid, self.clock(), request_id))
            tx.add_event(C.EV["SKIP_APPROVED"] if approve else C.EV["SKIP_DENIED"],
                         session_id=req["session_id"],
                         step_code=req["step_code"], actor_id=uid,
                         severity="warning" if approve else "info",
                         detail={"reason": req["reason"]})
            if approve:
                # 批准后该步骤以 skipped 落账；仍受顺序约束
                tx.execute(
                    "UPDATE session_steps SET status='skipped', completed_at=?, "
                    "operator_id=?, note=? WHERE session_id=? AND step_code=? "
                    "AND status='pending'",
                    (self.clock(), uid,
                     f"工程主管批准越过：{req['reason']}",
                     req["session_id"], req["step_code"]))
                tx.add_event(C.EV["STEP_SKIPPED"], session_id=req["session_id"],
                             step_code=req["step_code"], actor_id=uid,
                             severity="warning", detail={"reason": req["reason"]})
        return self.get_skip(request_id)

    def get_skip(self, rid):
        r = self.db.one("SELECT * FROM skip_requests WHERE id=?", (rid,))
        if not r:
            raise ServiceError("越步申请不存在", "not_found", 404)
        return r

    def list_skips(self, status=None):
        sql = ("SELECT r.*, u.name AS requester_name FROM skip_requests r "
               "JOIN users u ON u.id=r.requested_by")
        if status:
            sql += f" WHERE r.status='{status}'"
        return self.db.query(sql + " ORDER BY r.created_at DESC")

    # ---- 运行中异常上报（噪声 / 漏油） ----
    def report_incident(self, session_id, uid, kind, note, attachment_id=None):
        self._require_role(uid, C.ROLE_OPERATOR)
        s = self.get_session(session_id)
        if s["status"] != C.SESS_RUNNING:
            raise ServiceError("会话已结束", "session_closed")
        type_map = {"noise": C.EV["ABNORMAL_NOISE"], "leak": C.EV["OIL_LEAK"]}
        if kind not in type_map:
            raise ServiceError("异常类型须为 noise/leak", status=400)
        fault_code = "E404" if kind == "noise" else "E505"
        with self.db.transaction() as tx:
            tx.add_event(type_map[kind], unit_id=s["unit_id"],
                         session_id=session_id, actor_id=uid, severity="critical",
                         detail={"note": note, "attachment_id": attachment_id})
            if attachment_id:
                tx.execute("UPDATE attachments SET event_id=?, session_id=? "
                           "WHERE id=?", (None, session_id, attachment_id))
            tx.add_event(C.EV["SHUTDOWN_FAULT"], unit_id=s["unit_id"],
                         session_id=session_id, actor_id=uid, severity="critical",
                         detail={"fault_code": fault_code,
                                 "reason": ("异常噪声" if kind == "noise"
                                            else "漏油") + "，紧急停机"})
            tx.execute("UPDATE sessions SET status='failed', ended_at=?, "
                       "summary=? WHERE id=?",
                       (self.clock(), note or type_map[kind], session_id))
            tx.execute("UPDATE units SET status=?, fault_code=? WHERE id=?",
                       (C.UNIT_FAULT, fault_code, s["unit_id"]))
        return self.get_session(session_id)

    # ---- 合格结束：运行时长 + 燃油差额 ----
    def finish_session(self, session_id, uid, note=""):
        self._require_role(uid, C.ROLE_OPERATOR)
        s = self.get_session(session_id)
        if s["status"] != C.SESS_RUNNING:
            raise ServiceError("会话已结束", "session_closed")
        pending = [x for x in s["steps"] if x["status"] == "pending"]
        if pending:
            raise ServiceError(
                f"尚有 {len(pending)} 个步骤未完成，不能结束", "steps_pending")

        started = _parse_ts(s["started_at"])
        ended = _parse_ts(self.clock())
        duration = max(0, int((ended - started).total_seconds()))
        reading = self._latest_reading(s["unit_id"], session_id)
        end_pct = reading["fuel_level"] if reading else s["fuel_start_pct"]
        unit = self.get_unit(s["unit_id"])
        consumed = round((s["fuel_start_pct"] - end_pct) / 100.0
                         * unit["fuel_capacity_l"], 2)
        consumed = max(0.0, consumed)

        # 账面耗油：空载/带载分段计时 × 近似油耗率
        steps = {x["step_code"]: x for x in s["steps"]}
        nl, ld = steps["no_load"], steps["loaded"]
        burned = 0.0
        rate = _rated_burn_lph(unit["rated_power_kw"])
        if nl["completed_at"] and ld["completed_at"]:
            t_nl = (_parse_ts(ld["completed_at"]) -
                    _parse_ts(nl["completed_at"])).total_seconds()
            burned += t_nl / 3600.0 * rate * 0.25
            t_ld = (ended - _parse_ts(ld["completed_at"])).total_seconds()
            burned += t_ld / 3600.0 * rate * 0.8
        burned = round(burned, 2)
        discrepancy = round(consumed - burned, 2)
        discrepancy_flag = abs(discrepancy) > C.FUEL_TOLERANCE_L

        with self.db.transaction() as tx:
            tx.execute(
                "UPDATE sessions SET status='done', ended_at=?, duration_sec=?, "
                "fuel_end_pct=?, fuel_consumed_l=?, fuel_burned_runtime_l=?, "
                "fuel_discrepancy_l=?, summary=? WHERE id=?",
                (self.clock(), duration, end_pct, consumed, burned, discrepancy,
                 note or "全部检查合格", session_id))
            tx.execute("UPDATE units SET status=?, fault_code=NULL WHERE id=?",
                       (C.UNIT_STANDBY, s["unit_id"]))
            detail = {"duration_sec": duration, "fuel_start_pct": s["fuel_start_pct"],
                      "fuel_end_pct": end_pct, "consumed_l": consumed,
                      "expected_l": burned, "discrepancy_l": discrepancy}
            tx.add_event(C.EV["SESSION_DONE"], unit_id=s["unit_id"],
                         session_id=session_id, actor_id=uid, detail=detail)
            if discrepancy_flag:
                tx.add_event(C.EV["FUEL_DISCREPANCY"], unit_id=s["unit_id"],
                             session_id=session_id, actor_id=uid,
                             severity="warning", detail=detail)
        return self.get_session(session_id)

    def abort_session(self, session_id, uid, reason):
        s = self.get_session(session_id)
        if s["status"] != C.SESS_RUNNING:
            raise ServiceError("会话已结束", "session_closed")
        with self.db.transaction() as tx:
            tx.execute("UPDATE sessions SET status='aborted', ended_at=?, "
                       "summary=? WHERE id=?", (self.clock(), reason, session_id))
            tx.execute("UPDATE units SET status=? WHERE id=?",
                       (C.UNIT_STANDBY, s["unit_id"]))
            tx.add_event(C.EV["SESSION_ABORTED"], unit_id=s["unit_id"],
                         session_id=session_id, actor_id=uid, severity="warning",
                         detail={"reason": reason})
        return self.get_session(session_id)

    # ================= 加油台账 =================
    def list_fuelings(self, unit_id=None):
        sql = ("SELECT f.*, u.code AS unit_code, r.name AS recorded_name, "
               "v.name AS reviewed_name FROM fuelings f "
               "JOIN units u ON u.id=f.unit_id "
               "LEFT JOIN users r ON r.id=f.recorded_by "
               "LEFT JOIN users v ON v.id=f.reviewed_by")
        args = []
        if unit_id:
            sql += " WHERE f.unit_id=?"; args.append(unit_id)
        sql += " ORDER BY f.ts DESC, f.id DESC"
        return self.db.query(sql, tuple(args))

    def add_fueling(self, unit_id, liters, uid, note=""):
        self._require_role(uid, C.ROLE_FUEL, C.ROLE_SUPERVISOR)
        if liters <= 0:
            raise ServiceError("加油数量须为正数", status=400)
        u = self.get_unit(unit_id)
        before = u["fuel_level"]
        after = min(100.0, round(before + liters / u["fuel_capacity_l"] * 100, 2))
        applied = round((after - before) / 100 * u["fuel_capacity_l"], 2)
        with self.db.transaction() as tx:
            fid = tx.execute(
                "INSERT INTO fuelings(unit_id,ts,liters,level_before,level_after,"
                "recorded_by,note,review_status,created_at) "
                "VALUES(?,?,?,?,?,?,?, 'approved',?)",
                (unit_id, self.clock(), applied, before, after, uid, note,
                 self.clock()))
            tx.execute("UPDATE units SET fuel_level=? WHERE id=?", (after, unit_id))
            tx.add_event(C.EV["FUELING_CREATED"], unit_id=unit_id, actor_id=uid,
                         detail={"liters": applied, "before": before,
                                 "after": after, "note": note,
                                 "requested_liters": liters})
        return self.db.one("SELECT * FROM fuelings WHERE id=?", (fid,))

    def revise_fueling(self, fueling_id, new_liters, uid, note=""):
        """修订数量：生成一条待复核记录，须由第二人复核后才改动液位。"""
        self._require_role(uid, C.ROLE_FUEL, C.ROLE_SUPERVISOR)
        if new_liters <= 0:
            raise ServiceError("加油数量须为正数", status=400)
        old = self.db.one("SELECT * FROM fuelings WHERE id=?", (fueling_id,))
        if not old or old["review_status"] not in ("approved",):
            raise ServiceError("仅已生效的加油记录可修订", "bad_state")
        u = self.get_unit(old["unit_id"])
        # 复核通过后的目标液位（先按当前账面预估，复核时再按旧记录冲回）
        delta_l = new_liters - old["liters"]
        proj_after = min(100.0, round(u["fuel_level"] + delta_l /
                                      u["fuel_capacity_l"] * 100, 2))
        with self.db.transaction() as tx:
            rid = tx.execute(
                "INSERT INTO fuelings(unit_id,ts,liters,level_before,level_after,"
                "recorded_by,note,review_status,supersedes,created_at) "
                "VALUES(?,?,?,?,?,?,?, 'pending',?,?)",
                (old["unit_id"], self.clock(), new_liters, old["level_before"],
                 proj_after, uid, note, old["id"], self.clock()))
            tx.execute("UPDATE fuelings SET review_status='revised' WHERE id=?",
                       (fueling_id,))
            tx.add_event(C.EV["FUELING_REVISED"], unit_id=old["unit_id"],
                         actor_id=uid, severity="warning",
                         detail={"fueling_id": fueling_id,
                                 "old_liters": old["liters"],
                                 "new_liters": new_liters,
                                 "revision_id": rid, "note": note})
        return self.db.one("SELECT * FROM fuelings WHERE id=?", (rid,))

    def review_fueling(self, revision_id, approve, uid, note=""):
        """第二人复核：复核人不得是提交修订的同一人。"""
        rev = self.db.one("SELECT * FROM fuelings WHERE id=?", (revision_id,))
        if not rev or rev["review_status"] != "pending":
            raise ServiceError("待复核修订不存在", "not_found", 404)
        if rev["recorded_by"] == uid:
            raise ServiceError("加油数量修订须由第二人复核，不能自审",
                               "self_review", 403)
        old = self.db.one("SELECT * FROM fuelings WHERE id=?",
                          (rev["supersedes"],))
        u = self.get_unit(rev["unit_id"])
        with self.db.transaction() as tx:
            if approve:
                # 冲回旧记录影响，再施加修订数量
                level = max(0.0, min(100.0, u["fuel_level"]
                                     + (rev["liters"] - old["liters"])
                                     / u["fuel_capacity_l"] * 100))
                level = round(level, 2)
                tx.execute("UPDATE fuelings SET review_status='approved', "
                           "reviewed_by=?, reviewed_at=?, level_after=? WHERE id=?",
                           (uid, self.clock(), level, revision_id))
                tx.execute("UPDATE fuelings SET review_status='void' WHERE id=?",
                           (old["id"],))
                tx.execute("UPDATE units SET fuel_level=? WHERE id=?",
                           (level, rev["unit_id"]))
                tx.add_event(C.EV["FUELING_REVIEWED"], unit_id=rev["unit_id"],
                             actor_id=uid, detail={
                                 "revision_id": revision_id, "approved": True,
                                 "old_liters": old["liters"],
                                 "new_liters": rev["liters"], "note": note})
            else:
                tx.execute("UPDATE fuelings SET review_status='denied', "
                           "reviewed_by=?, reviewed_at=? WHERE id=?",
                           (uid, self.clock(), revision_id))
                tx.execute("UPDATE fuelings SET review_status='approved' WHERE id=?",
                           (old["id"],))
                tx.add_event(C.EV["FUELING_REVIEWED"], unit_id=rev["unit_id"],
                             actor_id=uid, severity="warning", detail={
                                 "revision_id": revision_id, "approved": False,
                                 "note": note})
        return self.db.one("SELECT * FROM fuelings WHERE id=?", (revision_id,))

    def fuel_balance(self, unit_id):
        """台账平衡：当前液位 vs 入油-耗油推算液位。"""
        u = self.get_unit(unit_id)
        added = self.db.one(
            "SELECT COALESCE(SUM(liters),0) s FROM fuelings "
            "WHERE unit_id=? AND review_status='approved'", (unit_id,))["s"]
        consumed = self.db.one(
            "SELECT COALESCE(SUM(fuel_consumed_l),0) s FROM sessions "
            "WHERE unit_id=? AND status='done'", (unit_id,))["s"]
        return {"unit_id": unit_id, "current_pct": u["fuel_level"],
                "capacity_l": u["fuel_capacity_l"],
                "added_l": round(added, 2), "consumed_l": round(consumed, 2),
                "current_l": round(u["fuel_level"] / 100 * u["fuel_capacity_l"], 2)}

    # ================= 读数 / 事件 =================
    def push_reading(self, unit_id, values, session_id=None, flags=""):
        return self.db.execute(
            "INSERT INTO readings(unit_id,session_id,ts,voltage,frequency,"
            "load_kw,fuel_level,battery_voltage,oil_pressure,flags) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (unit_id, session_id, self.clock(), values.get("voltage"),
             values.get("frequency"), values.get("load_kw"),
             values.get("fuel_level"), values.get("battery_voltage"),
             values.get("oil_pressure"), flags))

    def latest_readings(self, unit_id, limit=1):
        return self.db.query(
            "SELECT * FROM readings WHERE unit_id=? ORDER BY ts DESC, id DESC "
            "LIMIT ?", (unit_id, limit))

    def readings_series(self, unit_id, session_id=None, limit=120):
        sql = "SELECT * FROM readings WHERE unit_id=?"
        args = [unit_id]
        if session_id:
            sql += " AND session_id=?"; args.append(session_id)
        sql += " ORDER BY ts DESC, id DESC LIMIT ?"; args.append(limit)
        rows = self.db.query(sql, tuple(args))
        return list(reversed(rows))

    def list_events(self, unit_id=None, session_id=None, type_=None, limit=200):
        sql = ("SELECT e.*, u.name AS actor_name FROM events e "
               "LEFT JOIN users u ON u.id=e.actor_id WHERE 1=1")
        args = []
        if unit_id:
            sql += " AND e.unit_id=?"; args.append(unit_id)
        if session_id:
            sql += " AND e.session_id=?"; args.append(session_id)
        if type_:
            sql += " AND e.type=?"; args.append(type_)
        sql += " ORDER BY e.id DESC LIMIT ?"; args.append(limit)
        rows = self.db.query(sql, tuple(args))
        for r in rows:
            r["detail"] = json.loads(r.get("detail_json") or "{}")
        return rows

    # ================= 附件 =================
    def save_attachment(self, data, filename, unit_id, uid, kind="field",
                        session_id=None):
        safe = re.sub(r"[^A-Za-z0-9._-]", "_", filename) or "file"
        d = os.path.join(self.attach_dir, f"unit_{unit_id}")
        os.makedirs(d, exist_ok=True)
        stored = os.path.join(d, f"{uuid.uuid4().hex[:10]}_{safe}")
        with open(stored, "wb") as f:
            f.write(data)
        aid = self.db.execute(
            "INSERT INTO attachments(session_id,unit_id,kind,filename,stored_path,"
            "uploaded_by,ts) VALUES(?,?,?,?,?,?,?)",
            (session_id, unit_id, kind, safe, stored, uid, self.clock()))
        return self.db.one("SELECT * FROM attachments WHERE id=?", (aid,))

    def list_attachments(self, session_id=None, unit_id=None):
        sql = "SELECT * FROM attachments WHERE 1=1"
        args = []
        if session_id:
            sql += " AND session_id=?"; args.append(session_id)
        if unit_id:
            sql += " AND unit_id=?"; args.append(unit_id)
        return self.db.query(sql + " ORDER BY id DESC")

    # ================= 审批待办汇总 =================
    def approval_queue(self):
        return {
            "plans": self.db.query(
                "SELECT p.*, u.code AS unit_code FROM plans p JOIN units u "
                "ON u.id=p.unit_id WHERE p.status='pending' "
                "ORDER BY p.submitted_at"),
            "skips": self.list_skips("pending"),
            "fueling_revisions": self.db.query(
                "SELECT f.*, u.code AS unit_code, r.name AS recorded_name "
                "FROM fuelings f JOIN units u ON u.id=f.unit_id "
                "JOIN users r ON r.id=f.recorded_by "
                "WHERE f.review_status='pending' ORDER BY f.id"),
        }
