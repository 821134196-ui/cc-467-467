"""种子数据：一条本地启动命令（python -m genplat demo）创建
正常、启动失败、漏油三类样例机组，并放入日历/审批所需的计划。
幂等：仅在库为空时写入。
"""

from datetime import datetime, timedelta, timezone

from . import config as C
from .db import now_iso


def seed(db):
    if db.one("SELECT COUNT(*) c FROM units")["c"]:
        return False

    for uid, info in C.USERS.items():
        db.execute("INSERT INTO users(id,name,role) VALUES(?,?,?)",
                   (uid, info["name"], info["role"]))

    now = datetime.now(timezone.utc)

    def iso(dt):
        return dt.isoformat(timespec="seconds")

    units = [
        # code, name, building, kw, battery, fuel%, cap, status, mode
        ("GEN-A1", "1号主机", "A座·地下机房", 500, 12.7, 62.0, 800,
         C.UNIT_STANDBY, "normal", None),
        ("GEN-B2", "2号主机", "B座·地下机房", 800, 12.5, 55.0, 1200,
         C.UNIT_STANDBY, "start_fail", None),
        ("GEN-C3", "3号主机", "C座·裙房屋面", 300, 12.8, 48.0, 500,
         C.UNIT_STANDBY, "leak", None),
        ("GEN-D1", "4号主机", "D座·地下机房", 400, 12.6, 71.0, 600,
         C.UNIT_LOCKED, "normal", "等待更换节温器，工程主管方工挂牌"),
        ("GEN-E2", "5号辅机", "E座·地下机房", 180, 12.4, 18.0, 300,
         C.UNIT_STANDBY, "normal", None),
    ]
    ids = {}
    for i, (code, name, bld, kw, bat, fuel, cap, status, mode, lock) in enumerate(
            units, start=1):
        db.execute(
            "INSERT INTO units(id,code,name,building,rated_power_kw,"
            "battery_voltage,fuel_level,fuel_capacity_l,status,sim_mode,"
            "lockout_reason,lockout_by,lockout_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (i, code, name, bld, kw, bat, fuel, cap, status, mode, lock,
             "u-sup" if lock else None, now_iso() if lock else None))
        ids[code] = i

    def add_plan(unit_code, offset_min, status, note, created_by="u-op1"):
        pid = db.execute(
            "INSERT INTO plans(unit_id,scheduled_at,note,status,created_by,"
            "created_at,submitted_at,reviewed_by,reviewed_at) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (ids[unit_code], iso(now + timedelta(minutes=offset_min)), note,
             status, created_by, now_iso(),
             now_iso() if status in (C.PLAN_PENDING, C.PLAN_APPROVED) else None,
             "u-sup" if status == C.PLAN_APPROVED else None,
             now_iso() if status == C.PLAN_APPROVED else None))
        return pid

    # 立即可以执行的三条已批准计划（分别对应三种工况）
    add_plan("GEN-A1", 1, C.PLAN_APPROVED, "月度空载/带载试运行（正常样例）")
    add_plan("GEN-B2", 2, C.PLAN_APPROVED, "月度试运行（启动失败样例）")
    add_plan("GEN-C3", 3, C.PLAN_APPROVED, "月度试运行（漏油样例）")
    # 待批准计划（审批队列 + 日历）
    add_plan("GEN-E2", 120, C.PLAN_PENDING, "低油位机组补油后试运行申请")
    # 草稿
    add_plan("GEN-A1", 24 * 60, C.PLAN_DRAFT, "下周季度带载试验（草稿）")
    # 已取消计划（留痕演示）
    cp = add_plan("GEN-B2", -60, C.PLAN_CANCELLED, "雷雨天取消的试运行")
    db.execute("UPDATE plans SET submitted_at=?, reviewed_by=?, reviewed_at=?, "
               "cancelled_by=?, cancelled_at=?, cancel_reason=? WHERE id=?",
               (now_iso(), "u-sup", now_iso(), "u-op1", now_iso(),
                "气象橙色预警，户外作业取消", cp))
    for ev, sev, detail, plan in [
        (C.EV["PLAN_SUBMITTED"], "info", {}, cp),
        (C.EV["PLAN_APPROVED"], "info", {}, cp),
        (C.EV["PLAN_CANCELLED"], "warning",
         {"reason": "气象橙色预警，户外作业取消"}, cp),
    ]:
        db.add_event(ev, unit_id=2, plan_id=plan, actor_id="u-op1",
                     severity=sev, detail=detail)

    # 一条历史完成会话 + 加油记录，让台账/趋势首屏不空
    hist = db.execute(
        "INSERT INTO sessions(plan_id,unit_id,started_by,started_at,ended_at,"
        "status,duration_sec,fuel_start_pct,fuel_end_pct,fuel_consumed_l,"
        "fuel_burned_runtime_l,fuel_discrepancy_l,summary) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (cp, ids["GEN-A1"], "u-op1",
         iso(now - timedelta(days=7, minutes=20)),
         iso(now - timedelta(days=7)), "done", 1200, 70.0, 64.5,
         44.0, 42.6, 1.4, "全部检查合格"))
    db.execute(
        "INSERT INTO fuelings(unit_id,ts,liters,level_before,level_after,"
        "recorded_by,note,review_status,reviewed_by,reviewed_at,created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (ids["GEN-A1"], iso(now - timedelta(days=3)), 200.0, 40.0, 65.0,
         "u-fuel", "月度补给", "approved", "u-sup", now_iso(), now_iso()))
    # 历史读数（供趋势示例）
    base = now - timedelta(days=7)
    for k in range(30):
        db.execute(
            "INSERT INTO readings(unit_id,session_id,ts,voltage,frequency,"
            "load_kw,fuel_level,battery_voltage,oil_pressure,flags) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (ids["GEN-A1"], hist,
             iso(base + timedelta(seconds=20 * k)),
             396 + (k % 5) * 0.8, 49.9 + (k % 3) * 0.05,
             500 * (0.2 + 0.6 * k / 30), 70 - 5.5 * k / 30, 13.4, 3.8, ""))
    return True
