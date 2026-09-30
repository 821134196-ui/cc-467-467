"""种子数据：一条命令创建正常、启动失败、漏油三类样例及配套计划/台账/审批。"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from pathlib import Path

from . import config
from .db import Database
from .service import GenOpsService, now_iso, parse_dt

OP1, OP2, SUP = "zhangwei", "lina", "wangqiang"


def _dt(offset_min: float) -> str:
    return (datetime.now() + timedelta(minutes=offset_min)).strftime("%Y-%m-%d %H:%M:%S")


def _ledger(db, gen_id: str, change: float, reason: str, ref: str, who: str,
            ts: str) -> float:
    bal = db.query_one(
        "SELECT balance_l FROM fuel_ledger WHERE generator_id=? ORDER BY id DESC LIMIT 1",
        (gen_id,))
    balance = (bal["balance_l"] if bal else 0.0) + change
    db.execute(
        "INSERT INTO fuel_ledger(generator_id,change_l,balance_l,reason,ref_id,created_at,created_by)"
        " VALUES(?,?,?,?,?,?,?)",
        (gen_id, round(change, 2), round(balance, 2), reason, ref, ts, who))
    return round(balance, 2)


def seed_database(db: Database, *, reset: bool = False) -> dict:
    if reset:
        db.close()
        Path(db.path).unlink(missing_ok=True)
        for suffix in ("-wal", "-shm"):
            Path(db.path + suffix).unlink(missing_ok=True)
    db.init()
    existing = db.query_one("SELECT COUNT(*) c FROM generators")["c"]
    if existing and not reset:
        return {"skipped": True}

    svc = GenOpsService(db)

    # ---------------- 机组 ----------------
    gens = [
        # id, 名称, 楼宇, kW, 电池V, 容量L, 当前L, 状态, sim_mode, 封锁, 台账初始锚点L
        ("GEN-A", "1号机组", "行政楼", 800.0, 26.4, 1000.0, 620.0,
         "standby", "normal", 0, None, None, 800.0),
        ("GEN-B", "2号机组", "商业裙楼", 500.0, 23.2, 600.0, 300.0,
         "start_failed", "start_fail", 0, None, "起动机无响应（启动系统故障）", 300.0),
        ("GEN-C", "3号机组", "地下车库B1", 300.0, 26.0, 500.0, 90.0,
         "shutdown", "oil_leak", 0, None, "高压油管接头渗油", 96.0),
        ("GEN-D", "4号机组", "数据机房", 1200.0, 27.0, 800.0, 400.0,
         "locked_out", "normal", 1, "喷油泵维修，等待厂家备件（预计 10-03 完成）", None, 400.0),
    ]
    for gid, name, bld, kw, bv, cap, lvl, st, mode, locked, lock_reason, fault, init_l in gens:
        db.execute(
            "INSERT INTO generators(id,name,building,rated_power_kw,battery_voltage,fuel_capacity_l,"
            "fuel_level_l,status,locked_out,lock_reason,fault,sim_mode)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (gid, name, bld, kw, bv, cap, lvl, st, locked, lock_reason, fault, mode))
        _ledger(db, gid, init_l, "init", "SEED", "system", _dt(-60 * 24 * 30))

    # ---------------- 正常样例：GEN-A 历史完成的试运行 ----------------
    t_start = _dt(-60 * 24 * 2)
    t_end = _dt(-60 * 24 * 2 + 35)
    plan_ok = "PL-SEED-OK"
    sess_ok = "SE-SEED-OK"
    db.execute(
        "INSERT INTO plans(id,generator_id,scheduled_at,duration_min,assignee,status,created_at,created_by)"
        " VALUES(?,?,?,30,?, 'completed', ?, ?)",
        (plan_ok, "GEN-A", t_start, OP1, _dt(-60 * 24 * 2 - 200), OP1))
    db.execute(
        "INSERT INTO approvals(id,plan_id,kind,reason,decision,approver,approver_role,created_at)"
        " VALUES('AP-SEED-OK',?, 'plan','月度例行试运行，同意', 'approved', ?, 'supervisor', ?)",
        (plan_ok, SUP, _dt(-60 * 24 * 2 - 180)))
    db.execute(
        "INSERT INTO sessions(id,plan_id,generator_id,operator_id,operator_name,started_at,ended_at,"
        "outcome,current_step,load_started_at,start_fuel_l,end_fuel_l,measured_remaining_l,"
        "fuel_delta_l,run_minutes) VALUES(?,?,?,?,?,?,?, 'completed','done',?,?,?,?,?,?)",
        (sess_ok, plan_ok, "GEN-A", OP1, "张伟", t_start, t_end,
         _dt(-60 * 24 * 2 + 20), 875.0, 620.0, 620.0, 255.0, 35.0))
    for code, title, seq in config.STEPS:
        db.execute(
            "INSERT INTO step_records(id,session_id,step_code,seq,status,checked_at,operator_id,note)"
            " VALUES(?,?,?,?,'pass',?,?,'合格')",
            (uuid.uuid4().hex, sess_ok, code, seq,
             _dt(-60 * 24 * 2 + seq * 5), OP1))
    # 账面演变：800 →（更早一次例行）755 → 加油 +120 → 875 → 本次消耗 255 → 620
    _ledger(db, "GEN-A", -45.0, "run_consumption", "SE-SEED-OLD", OP1, _dt(-60 * 24 * 20))
    rf_seed = "RF-SEED-01"
    db.execute(
        "INSERT INTO refuel_records(id,generator_id,liters,recorded_balance_l,before_l,after_l,note,"
        "status,recorder_id,recorder_name,recorded_at,reviewer_id,reviewer_name,reviewed_at,review_note)"
        " VALUES(?,?,120,755,755,875,'月度补给','confirmed',?,?,?,?,?,?,'数量无误')",
        (rf_seed, "GEN-A", OP2, "李娜", _dt(-60 * 24 * 10),
         SUP, "王强", _dt(-60 * 24 * 10 + 30)))
    _ledger(db, "GEN-A", 120.0, "refuel", rf_seed, SUP, _dt(-60 * 24 * 10 + 30))
    _ledger(db, "GEN-A", -255.0, "run_consumption", sess_ok, OP1, t_end)

    # ---------------- 启动失败样例：GEN-B ----------------
    t_b = _dt(-60 * 26)
    plan_b = "PL-SEED-FAIL"
    sess_b = "SE-SEED-FAIL"
    db.execute(
        "INSERT INTO plans(id,generator_id,scheduled_at,duration_min,assignee,status,created_at,created_by)"
        " VALUES(?,?,?,30,?, 'failed', ?, ?)",
        (plan_b, "GEN-B", t_b, OP2, _dt(-60 * 26 - 120), OP2))
    db.execute(
        "INSERT INTO approvals(id,plan_id,kind,reason,decision,approver,approver_role,created_at)"
        " VALUES('AP-SEED-FAIL',?, 'plan','裙楼月度试机，同意', 'approved', ?, 'supervisor', ?)",
        (plan_b, SUP, _dt(-60 * 26 - 60)))
    db.execute(
        "INSERT INTO sessions(id,plan_id,generator_id,operator_id,operator_name,started_at,ended_at,"
        "outcome,current_step,start_fuel_l) VALUES(?,?,?,?,?,?,?, 'failed','aborted',?)",
        (sess_b, plan_b, "GEN-B", OP2, "李娜", t_b, _dt(-60 * 26 + 2), 300.0))
    for code, title, seq in config.STEPS:
        status = "fail" if code == "start_noload" else ("pass" if seq < 4 else "pending")
        db.execute(
            "INSERT INTO step_records(id,session_id,step_code,seq,status,checked_at,operator_id,note)"
            " VALUES(?,?,?,?," + f"'{status}'" + ",?,?,?)",
            (uuid.uuid4().hex, sess_b, code, seq,
             _dt(-60 * 26 + seq) if status != "pending" else None,
             OP2, "起动机无响应" if status == "fail" else "合格"))
    db.execute(
        "INSERT INTO events(ts,category,severity,generator_id,plan_id,session_id,message,detail_json)"
        " VALUES(?, 'start_fail','critical','GEN-B','PL-SEED-FAIL','SE-SEED-FAIL',?,?)",
        (t_b, "启动失败：2号机组（起动机无响应（启动系统故障））",
         '{"reason": "起动机无响应（启动系统故障）", "by": "李娜"}'))

    # ---------------- 漏油样例：GEN-C ----------------
    t_c = _dt(-60 * 8)
    plan_c = "PL-SEED-LEAK"
    sess_c = "SE-SEED-LEAK"
    db.execute(
        "INSERT INTO plans(id,generator_id,scheduled_at,duration_min,assignee,status,created_at,created_by)"
        " VALUES(?,?,?,20,?, 'failed', ?, ?)",
        (plan_c, "GEN-C", t_c, OP1, _dt(-60 * 8 - 100), OP1))
    db.execute(
        "INSERT INTO approvals(id,plan_id,kind,reason,decision,approver,approver_role,created_at)"
        " VALUES('AP-SEED-LEAK',?, 'plan','车库机组季度试机', 'approved', ?, 'supervisor', ?)",
        (plan_c, SUP, _dt(-60 * 8 - 60)))
    db.execute(
        "INSERT INTO sessions(id,plan_id,generator_id,operator_id,operator_name,started_at,ended_at,"
        "outcome,current_step,start_fuel_l) VALUES(?,?,?,?,?,?,?, 'failed','aborted',?)",
        (sess_c, plan_c, "GEN-C", OP1, "张伟", t_c, _dt(-60 * 8 + 3), 96.0))
    db.execute(
        "INSERT INTO step_records(id,session_id,step_code,seq,status,checked_at,operator_id,note)"
        " VALUES(?,?,'visual',1,'fail',?,?,'高压油管接头处有新鲜油迹，地面有油滴')",
        (uuid.uuid4().hex, sess_c, _dt(-60 * 8 + 3), OP1))
    for code, title, seq in config.STEPS[1:]:
        db.execute(
            "INSERT INTO step_records(id,session_id,step_code,seq,status) VALUES(?,?,?,?,'pending')",
            (uuid.uuid4().hex, sess_c, code, seq))
    db.execute(
        "INSERT INTO events(ts,category,severity,generator_id,plan_id,session_id,message,detail_json)"
        " VALUES(?, 'oil_leak','critical','GEN-C','PL-SEED-LEAK','SE-SEED-LEAK',"
        "'外观检查发现漏油：3号机组，中止试运行',"
        "'{\"by\": \"张伟\", \"note\": \"高压油管接头渗油，地面有油滴\"}')",
        (t_c,))
    db.execute(
        "INSERT INTO events(ts,category,severity,generator_id,message,detail_json)"
        " VALUES(?, 'fuel_discrepancy','warning','GEN-C',"
        "'燃油差异：3号机组 近一周账面无运行记录，液位却由 96L 降至 90L，疑似持续渗漏，已按实测调账',"
        "'{\"actual_l\": 6.0, \"expected_l\": 0.0, \"diff_l\": 6.0}')",
        (_dt(-60 * 24 * 2),))
    _ledger(db, "GEN-C", -6.0, "fuel_discrepancy", "SEED-LEAK-ADJ", "system",
            _dt(-60 * 24 * 2))

    # ---------------- 维修封锁样例：GEN-D ----------------
    db.execute(
        "INSERT INTO events(ts,category,severity,generator_id,message,detail_json)"
        " VALUES(?, 'lockout','critical','GEN-D',"
        "'机组 4号机组 维修封锁：喷油泵维修，等待厂家备件（预计 10-03 完成）',"
        "'{\"by\": \"王强\"}')",
        (_dt(-60 * 5),))

    # ---------------- 当前可操作的计划 ----------------
    # 待审批
    db.execute(
        "INSERT INTO plans(id,generator_id,scheduled_at,duration_min,assignee,status,created_at,created_by)"
        " VALUES('PL-NEW-PEND','GEN-A',?, 30, ?, 'pending', ?, ?)",
        (_dt(60 * 26), OP1, now_iso(), OP1))
    # 已获批且立即到期 → 提醒 + 可执行（正常路径）
    db.execute(
        "INSERT INTO plans(id,generator_id,scheduled_at,duration_min,assignee,status,created_at,created_by)"
        " VALUES('PL-NEW-DUE','GEN-A',?, 30, ?, 'approved', ?, ?)",
        (_dt(2), OP2, _dt(-40), OP1))
    db.execute(
        "INSERT INTO approvals(id,plan_id,kind,reason,decision,approver,approver_role,created_at)"
        " VALUES('AP-NEW-DUE','PL-NEW-DUE','plan','月度试机，按规程执行','approved',?,'supervisor',?)",
        (SUP, _dt(-20)))
    # 已获批的启动失败机组计划（执行即复现启动失败）
    db.execute(
        "INSERT INTO plans(id,generator_id,scheduled_at,duration_min,assignee,status,created_at,created_by)"
        " VALUES('PL-NEW-FAIL','GEN-B',?, 30, ?, 'approved', ?, ?)",
        (_dt(60), OP1, _dt(-30), OP2))
    db.execute(
        "INSERT INTO approvals(id,plan_id,kind,reason,decision,approver,approver_role,created_at)"
        " VALUES('AP-NEW-FAIL','PL-NEW-FAIL','plan','复验启动系统检修效果','approved',?,'supervisor',?)",
        (SUP, _dt(-15)))
    # 已取消计划样例
    db.execute(
        "INSERT INTO plans(id,generator_id,scheduled_at,duration_min,assignee,status,created_at,created_by)"
        " VALUES('PL-NEW-CANCEL','GEN-C',?, 20, ?, 'canceled', ?, ?)",
        (_dt(60 * 50), OP1, _dt(-60 * 20), OP1))
    db.execute(
        "INSERT INTO events(ts,category,severity,generator_id,plan_id,message,detail_json)"
        " VALUES(?, 'plan_cancel','warning','GEN-C','PL-NEW-CANCEL',"
        "'试运行计划已取消：漏油未修复，暂缓试机（张伟）',"
        "'{\"by\": \"张伟\", \"reason\": \"漏油未修复，暂缓试机\"}')",
        (_dt(-60 * 20),))

    # ---------------- 待第二人复核的加油登记 ----------------
    db.execute(
        "INSERT INTO refuel_records(id,generator_id,liters,recorded_balance_l,before_l,after_l,note,"
        "status,recorder_id,recorder_name,recorded_at)"
        " VALUES('RF-NEW-PEND','GEN-C',200,90,90,290,'车库机组补油，200L 桶装 x2',"
        "'pending_second_check',?, '张伟', ?)",
        (OP1, now_iso()))

    # ---------------- 现场附件样例 ----------------
    sample_note = config.ATTACH_DIR / "seed_oil_leak_note.txt"
    sample_note.write_text(
        "现场记录（样例）：3号机组高压油管接头渗油，地面见直径约 20cm 油迹，已围堵并挂锁。\n",
        encoding="utf-8")
    db.execute(
        "INSERT INTO attachments(id,category,ref_id,filename,stored_name,content_type,size,"
        "uploaded_by,uploaded_at) VALUES(?, 'fault','SE-SEED-LEAK',?,?, 'text/plain', ?, ?, ?)",
        (uuid.uuid4().hex, "漏油现场记录.txt", sample_note.name,
         sample_note.stat().st_size, OP1, _dt(-60 * 8)))

    return {"skipped": False, "ids": {
        "normal_plan": "PL-NEW-DUE", "start_fail_plan": "PL-NEW-FAIL",
        "pending_approval": "PL-NEW-PEND", "pending_refuel": "RF-NEW-PEND"}}
