"""平台领域规则测试：会话互斥、步骤顺序、封锁阻断、燃油平衡、审批权限。

运行：python -m unittest discover -s tests -v
"""
import os
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from genops import config
from genops.db import Database
from genops.service import (GenOpsService, DomainError, STEP_ORDER, now_iso,
                            parse_dt)
from genops.telemetry import TelemetryHub, ReminderScheduler

OP1, OP2, SUP = "zhangwei", "lina", "wangqiang"


class GenOpsTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["GENOPS_HOME"] = self.tmp.name
        config.DB_PATH = Path(self.tmp.name) / "t.db"
        config.ATTACH_DIR = Path(self.tmp.name) / "att"
        self.db = Database(config.DB_PATH)
        self.db.init()
        self.hub = TelemetryHub(self.db, interval=60)  # 不启动线程，手动 tick
        self.hub.start = lambda: None  # 防止误启动
        self.svc = GenOpsService(self.db, self.hub)
        self.hub.svc = self.svc
        self._make_gen("G1", 800.0, 1000.0, 1000.0, "normal")
        self._make_gen("G2", 500.0, 600.0, 300.0, "start_fail", battery=23.0,
                       status="start_failed")
        self._make_gen("G3", 300.0, 500.0, 90.0, "normal")

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    # ---------- 夹具 ----------
    def _make_gen(self, gid, kw, cap, lvl, mode, battery=26.4, locked=0,
                  reason=None, status="standby"):
        self.db.execute(
            "INSERT INTO generators(id,name,building,rated_power_kw,battery_voltage,fuel_capacity_l,"
            "fuel_level_l,status,locked_out,lock_reason,fault,sim_mode)"
            " VALUES(?,?,?,?,?,?,?, ?, ?, ?, NULL, ?)",
            (gid, gid + "号机组", "测试楼", kw, battery, cap, lvl,
             status, locked, reason, mode))
        self.db.execute(
            "INSERT INTO fuel_ledger(generator_id,change_l,balance_l,reason,ref_id,created_at,created_by)"
            " VALUES(?,?,?, 'init','FIX',?, 'system')",
            (gid, lvl, lvl, now_iso()))
        self.hub.ensure(gid, lvl)

    def _plan(self, gid="G1", assignee=OP1, offset_min=10, status=None, approve_by=None):
        when = (datetime.now() + timedelta(minutes=offset_min)).strftime("%Y-%m-%d %H:%M:%S")
        p = self.svc.create_plan(gid, when, 30, assignee, OP1)
        if status == "approved":
            self.svc.decide_plan(p["id"], "approved", "测试批准", approve_by or SUP)
        return p["id"]

    def _full_steps(self, sid, *, leak=False, noise=False, measured=None,
                    run_minutes=30, end=True, user=OP1):
        self.svc.submit_visual(sid, user, not (leak or noise),
                               noise=noise, oil_leak=leak, note="测试")
        self.svc.submit_fuel_check(sid, user)
        self.svc.submit_battery_check(sid, user)
        self.svc.attempt_start(sid, user)
        self.svc.begin_load_test(sid, user)
        self.svc.complete_load_test(sid, user)
        if end:
            return self.svc.end_session(sid, user, measured_remaining_l=measured,
                                        run_minutes=run_minutes)

    def _start(self, pid, user=OP1):
        s, created = self.svc.start_session(pid, user)
        return s["id"], created


class TestPlanApproval(GenOpsTestCase):
    def test_unapproved_plan_cannot_start(self):
        pid = self._plan(status=None)
        with self.assertRaises(DomainError) as c:
            self._start(pid)
        self.assertEqual(c.exception.code, "plan_not_approved")

    def test_operator_cannot_approve(self):
        pid = self._plan(status=None)
        with self.assertRaises(DomainError) as c:
            self.svc.decide_plan(pid, "approved", "值班员越权", OP1)
        self.assertEqual(c.exception.status, 403)
        # 计划仍为待批
        self.assertEqual(self.svc.row("plans", pid)["status"], "pending")

    def test_supervisor_approval_opens_execution(self):
        pid = self._plan(status="approved")
        s, created = self._start(pid)
        self.assertTrue(created)
        self.assertTrue(self.svc.list_approvals())

    def test_plan_cancel_is_logged(self):
        pid = self._plan(status="approved")
        self.svc.cancel_plan(pid, "当日停电窗口取消", OP2)
        with self.assertRaises(DomainError):
            self._start(pid)
        cats = [e["category"] for e in self.svc.list_events("plan_cancel")]
        self.assertIn("plan_cancel", cats)

    def test_cancel_requires_reason(self):
        pid = self._plan(status="approved")
        with self.assertRaises(DomainError):
            self.svc.cancel_plan(pid, "  ", OP1)


class TestSessionMutex(GenOpsTestCase):
    def test_concurrent_start_keeps_single_session(self):
        pid = self._plan(status="approved")
        barrier = threading.Barrier(2)
        results, errors = [], []

        def go(uid):
            try:
                barrier.wait()
                results.append(self.svc.start_session(pid, uid))
            except Exception as e:  # 任何一方都不应抛错
                errors.append(e)

        with ThreadPoolExecutor(2) as ex:
            list(ex.map(go, [OP1, OP2]))

        self.assertFalse(errors, errors)
        self.assertEqual(len(results), 2)
        ids = {r[0]["id"] for r in results}
        self.assertEqual(len(ids), 1, "两个值班员必须只得到同一个运行会话")
        self.assertEqual(sorted(r[1] for r in results), [False, True])
        rows = self.db.query("SELECT COUNT(*) c FROM sessions WHERE plan_id=?", (pid,))
        self.assertEqual(rows[0]["c"], 1)
        self.assertEqual(self.svc.row("plans", pid)["status"], "running")

    def test_repeated_start_reuses_session(self):
        pid = self._plan(status="approved")
        s1, c1 = self._start(pid, OP1)
        s2, c2 = self._start(pid, OP2)
        self.assertEqual(s1, s2)
        self.assertTrue(c1)
        self.assertFalse(c2)


class TestStepOrder(GenOpsTestCase):
    def test_steps_must_follow_sequence(self):
        pid = self._plan(status="approved")
        sid, _ = self._start(pid)
        # 未完成外观，直接油位/电池/启动均拒绝
        for fn in (lambda: self.svc.submit_fuel_check(sid, OP1),
                   lambda: self.svc.submit_battery_check(sid, OP1),
                   lambda: self.svc.attempt_start(sid, OP1),
                   lambda: self.svc.begin_load_test(sid, OP1)):
            with self.assertRaises(DomainError) as c:
                fn()
            self.assertEqual(c.exception.code, "step_order")

    def test_skip_step_requires_supervisor_and_leaves_approval(self):
        pid = self._plan(status="approved")
        sid, _ = self._start(pid)
        with self.assertRaises(DomainError) as c:
            self.svc.skip_step(sid, "visual", OP1, "来不及看")
        self.assertEqual(c.exception.status, 403)
        # 主管批准后可越过，且下一步解锁
        self.svc.skip_step(sid, "visual", SUP, "目检已于班前完成")
        rec = self.db.query_one(
            "SELECT * FROM step_records WHERE session_id=? AND step_code='visual'", (sid,))
        self.assertEqual(rec["status"], "skipped")
        # 油位步骤现在可以做（不再被前置拦截）
        self.svc.submit_fuel_check(sid, OP1)
        kinds = [(a["kind"], a["step_code"], a["approver_role"])
                 for a in self.svc.list_approvals() if a["plan_id"] == pid]
        self.assertIn(("skip_step", "visual", "supervisor"), kinds)

    def test_completed_step_cannot_submit_twice(self):
        pid = self._plan(status="approved")
        sid, _ = self._start(pid)
        self.svc.submit_visual(sid, OP1, True)
        with self.assertRaises(DomainError):
            self.svc.submit_visual(sid, OP1, True)


class TestLockout(GenOpsTestCase):
    def test_locked_generator_blocks_start(self):
        self.db.execute(
            "INSERT INTO generators(id,name,building,rated_power_kw,battery_voltage,fuel_capacity_l,"
            "fuel_level_l,status,locked_out,lock_reason,sim_mode)"
            " VALUES('G4','G4','楼',400,26,500,200,'locked_out',1,'等待备件','normal')")
        pid = self._plan(gid="G4", status="approved")
        with self.assertRaises(DomainError) as c:
            self._start(pid)
        self.assertEqual(c.exception.code, "locked_out")

    def test_locked_capacity_excluded(self):
        cap = self.svc.capacity_summary()
        # G1 800 + G2 500 + G3 300 可用；G2 处于 start_failed 也算故障剔除
        self.assertEqual(cap["total_kw"], 1600.0)
        self.assertEqual(cap["available_kw"], 1100.0)   # G1 + G3
        self.assertEqual(cap["fault_kw"], 500.0)       # G2
        # 封锁 G3 后：可用只剩 G1，封锁 300
        self.svc.set_lockout("G3", "油管检修", SUP)
        cap2 = self.svc.capacity_summary()
        self.assertEqual(cap2["locked_kw"], 300.0)
        self.assertEqual(cap2["available_kw"], 800.0)

    def test_lockout_terminates_active_session(self):
        pid = self._plan(status="approved")
        sid, _ = self._start(pid)
        self.svc.set_lockout("G1", "运行中发现异常，立即封锁", OP2)
        s = self.svc.get_session(sid)
        self.assertIsNotNone(s["ended_at"])
        self.assertEqual(s["outcome"], "shutdown")
        self.assertEqual(self.svc.row("plans", pid)["status"], "failed")

    def test_clear_lockout_supervisor_only(self):
        self.svc.set_lockout("G1", "检修", OP1)
        with self.assertRaises(DomainError) as c:
            self.svc.clear_lockout("G1", OP1)
        self.assertEqual(c.exception.status, 403)
        self.svc.clear_lockout("G1", SUP)
        self.assertEqual(self.svc.row("generators", "G1")["status"], "standby")

    def test_lockout_requires_reason(self):
        with self.assertRaises(DomainError):
            self.svc.set_lockout("G1", "  ", OP1)


class TestFaults(GenOpsTestCase):
    def test_start_failure_aborts_and_logs(self):
        pid = self._plan(gid="G2", status="approved")
        sid, _ = self._start(pid, OP2)
        self.svc.submit_visual(sid, OP2, True)
        self.svc.submit_fuel_check(sid, OP2)
        # G2 电池 23V 不合格，先演示电池拦截
        with self.assertRaises(DomainError) as c:
            self.svc.submit_battery_check(sid, OP2)
        self.assertEqual(c.exception.code, "battery_fail")
        # 换一种：直接造一台启动失败但电池正常的机组
        self._make_gen("G5", 400.0, 500.0, 400.0, "start_fail")
        pid5 = self._plan(gid="G5", status="approved")
        sid5, _ = self._start(pid5, OP2)
        self.svc.submit_visual(sid5, OP2, True)
        self.svc.submit_fuel_check(sid5, OP2)
        self.svc.submit_battery_check(sid5, OP2)
        with self.assertRaises(DomainError) as c:
            self.svc.attempt_start(sid5, OP2)
        self.assertEqual(c.exception.code, "start_failed")
        s = self.svc.get_session(sid5)
        self.assertEqual(s["outcome"], "failed")
        self.assertEqual(self.svc.row("generators", "G5")["status"], "start_failed")
        self.assertTrue(any(e["category"] == "start_fail" for e in self.svc.list_events()))

    def test_oil_leak_at_visual_aborts(self):
        pid = self._plan(status="approved")
        sid, _ = self._start(pid)
        with self.assertRaises(DomainError) as c:
            self._full_steps(sid, leak=True, end=False)
        self.assertEqual(c.exception.code, "oil_leak")
        s = self.svc.get_session(sid)
        self.assertEqual(s["outcome"], "failed")
        self.assertTrue(any(e["category"] == "oil_leak" for e in self.svc.list_events()))
        self.assertEqual(self.svc.row("generators", "G1")["status"], "shutdown")

    def test_abnormal_noise_aborts(self):
        pid = self._plan(status="approved")
        sid, _ = self._start(pid)
        with self.assertRaises(DomainError) as c:
            self._full_steps(sid, noise=True, end=False)
        self.assertEqual(c.exception.code, "abnormal_noise")
        self.assertTrue(any(e["category"] == "noise" for e in self.svc.list_events()))

    def test_load_abort_logs_and_shuts_down(self):
        pid = self._plan(status="approved")
        sid, _ = self._start(pid)
        self.svc.submit_visual(sid, OP1, True)
        self.svc.submit_fuel_check(sid, OP1)
        self.svc.submit_battery_check(sid, OP1)
        self.svc.attempt_start(sid, OP1)
        self.svc.begin_load_test(sid, OP1)
        with self.assertRaises(DomainError) as c:
            self.svc.abort_load_test(sid, OP1, "排烟温度骤升")
        self.assertEqual(c.exception.code, "load_abort")
        self.assertTrue(any(e["category"] == "load_abort" for e in self.svc.list_events()))
        self.assertEqual(self.svc.row("generators", "G1")["status"], "shutdown")

    def test_cannot_end_with_pending_steps(self):
        pid = self._plan(status="approved")
        sid, _ = self._start(pid)
        self.svc.submit_visual(sid, OP1, True)
        with self.assertRaises(DomainError) as c:
            self.svc.end_session(sid, OP1, run_minutes=10)
        self.assertEqual(c.exception.code, "steps_pending")


class TestFuelBalance(GenOpsTestCase):
    def test_consumption_posts_and_balances(self):
        pid = self._plan(status="approved")
        sid, _ = self._start(pid)
        s = self._full_steps(sid, measured=900.0, run_minutes=30)
        self.assertEqual(s["outcome"], "completed")
        # 台账累计 = 当前油位
        self.assertTrue(self.svc.fuel_balance_ok("G1"))
        self.assertEqual(self.svc.row("generators", "G1")["fuel_level_l"], 900.0)
        row = self.db.query_one(
            "SELECT * FROM fuel_ledger WHERE reason='run_consumption' ORDER BY id DESC LIMIT 1")
        self.assertEqual(row["change_l"], -100.0)
        # 合格后机组回待命
        self.assertEqual(self.svc.row("generators", "G1")["status"], "standby")

    def test_fuel_discrepancy_event_when_beyond_tolerance(self):
        pid = self._plan(status="approved")
        sid, _ = self._start(pid)
        # 实耗 100L、30 分钟理论消耗仅数升 → 超 5L 容差
        self._full_steps(sid, measured=900.0, run_minutes=30)
        ev = self.svc.list_events("fuel_discrepancy")
        self.assertTrue(ev)
        self.assertIn("燃油差异", ev[0]["message"])

    def test_failed_run_does_not_return_to_standby(self):
        pid = self._plan(status="approved")
        sid, _ = self._start(pid)
        with self.assertRaises(DomainError):
            self._full_steps(sid, noise=True, end=False)
        self.assertNotEqual(self.svc.row("generators", "G1")["status"], "standby")

    def test_refuel_requires_second_person(self):
        before = self.svc.row("generators", "G3")["fuel_level_l"]
        r = self.svc.create_refuel("G3", 200.0, OP1, "200L 桶装 x1")
        self.assertEqual(r["status"], "pending_second_check")
        # 待复核时账面不变
        self.assertEqual(self.svc.row("generators", "G3")["fuel_level_l"], before)
        # 登记人不能复核自己的记录
        with self.assertRaises(DomainError) as c:
            self.svc.review_refuel(r["id"], OP1, True)
        self.assertEqual(c.exception.status, 403)
        # 值班员（第二人）即可复核，不必主管
        confirmed = self.svc.review_refuel(r["id"], OP2, True, note="核对油单")
        self.assertEqual(confirmed["status"], "confirmed")
        self.assertEqual(self.svc.row("generators", "G3")["fuel_level_l"], before + 200.0)
        self.assertTrue(self.svc.fuel_balance_ok("G3"))
        # 不能重复复核
        with self.assertRaises(DomainError):
            self.svc.review_refuel(r["id"], SUP, True)

    def test_refuel_revision_by_reviewer(self):
        before = self.svc.row("generators", "G3")["fuel_level_l"]
        r = self.svc.create_refuel("G3", 200.0, OP1)
        rev = self.svc.review_refuel(r["id"], SUP, True, revised_liters=180.0, note="实收 180L")
        self.assertEqual(rev["status"], "revised")
        self.assertEqual(rev["liters"], 180.0)
        self.assertEqual(rev["revised_liters"], 200.0)
        self.assertEqual(self.svc.row("generators", "G3")["fuel_level_l"], before + 180.0)
        self.assertTrue(self.svc.fuel_balance_ok("G3"))
        self.assertTrue(any(e["category"] == "refuel_confirmed" for e in self.svc.list_events()))

    def test_refuel_rejection_keeps_balance(self):
        before = self.svc.row("generators", "G3")["fuel_level_l"]
        r = self.svc.create_refuel("G3", 500.0, OP1)
        out = self.svc.review_refuel(r["id"], OP2, False, note="油单与实物不符")
        self.assertEqual(out["status"], "rejected")
        self.assertEqual(self.svc.row("generators", "G3")["fuel_level_l"], before)

    def test_overflow_is_clipped(self):
        # G1 容量 1000、现有 1000（先消耗 50 造空间），再申报 100，只能入库 50
        self.db.execute(
            "INSERT INTO fuel_ledger(generator_id,change_l,balance_l,reason,ref_id,created_at,created_by)"
            " VALUES('G1',-50,950,'run_consumption','T',?,?)", (now_iso(), OP1))
        self.db.execute("UPDATE generators SET fuel_level_l=950 WHERE id='G1'")
        self.hub.sync_fuel("G1", 950)
        r = self.svc.create_refuel("G1", 100.0, OP1)
        self.svc.review_refuel(r["id"], OP2, True)
        self.assertEqual(self.svc.row("generators", "G1")["fuel_level_l"], 1000.0)
        self.assertTrue(self.svc.fuel_balance_ok("G1"))


class TestReminders(GenOpsTestCase):
    def test_due_and_low_fuel_reminders_dedup(self):
        self._plan(gid="G1", status="approved", offset_min=5)   # 30 分钟窗口内到期
        sched = ReminderScheduler(self.svc, self.hub, interval=60)
        first = sched.scan_once()
        kinds = {r["kind"] for r in first}
        self.assertIn("plan_due", kinds)
        self.assertIn("low_fuel", kinds)   # G3 18% < 20%
        # 再扫一次不重复产生
        again = sched.scan_once()
        self.assertEqual(again, [])


class TestTelemetryGuards(GenOpsTestCase):
    def test_out_of_range_load_completion_shuts_down(self):
        pid = self._plan(status="approved")
        sid, _ = self._start(pid)
        self.svc.submit_visual(sid, OP1, True)
        self.svc.submit_fuel_check(sid, OP1)
        self.svc.submit_battery_check(sid, OP1)
        self.svc.attempt_start(sid, OP1)
        self.svc.begin_load_test(sid, OP1)
        self.hub.mark_session("G1", sid, running_engine=True, loaded=True)
        # 手动放一个越限读数
        self.hub._latest["G1"] = {"t": 1, "ts": now_iso(), "voltage": 455.0,
                                  "frequency": 50.0, "battery": 26.0, "fuel": 990.0,
                                  "fuel_percent": 99.0, "engine": True, "loaded": True}
        with self.assertRaises(DomainError) as c:
            self.svc.complete_load_test(sid, OP1)
        self.assertEqual(c.exception.code, "out_of_range")
        self.assertTrue(any(e["category"] == "voltage" for e in self.svc.list_events()))


if __name__ == "__main__":
    unittest.main()
