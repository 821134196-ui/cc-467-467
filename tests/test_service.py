"""领域规则测试：会话互斥、步骤顺序与越步审批、维修封锁阻断与容量、
燃油平衡、加油第二人复核、审批权限、各类故障留痕，以及模拟适配器/提醒。"""

import os
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from genplat.db import Database
from genplat.seed import seed
from genplat.service import Service, ServiceError
from genplat.simulator import Reminder, Simulator

OP1, OP2, SUP, FUEL = "u-op1", "u-op2", "u-sup", "u-fuel"


class TestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(os.path.join(self.tmp.name, "t.db"))
        seed(self.db)
        self.svc = Service(self.db, attach_dir=self.tmp.name)

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def reading(self, unit, sid, **kw):
        base = dict(voltage=400.0, frequency=50.0, load_kw=20.0,
                    fuel_level=60.0, battery_voltage=12.7, oil_pressure=4.0)
        base.update(kw)
        self.svc.push_reading(unit, base, session_id=sid)

    def run_normal(self, sid, unit=1, fuel_end=61.5):
        """按序完成一台正常机组的全部步骤。"""
        self.svc.complete_step(sid, "visual", OP1, visual_confirmed=True)
        self.reading(unit, sid, fuel_level=62.0, battery_voltage=12.7)
        self.svc.complete_step(sid, "fuel", OP1)
        self.svc.complete_step(sid, "battery", OP1)
        self.reading(unit, sid, voltage=400.0, frequency=50.0, load_kw=25)
        self.svc.complete_step(sid, "no_load", OP1)
        self.reading(unit, sid, voltage=396.0, frequency=49.8,
                     load_kw=400, fuel_level=fuel_end)
        self.svc.complete_step(sid, "loaded", OP1)

    # ---------- 审批门控 ----------
    def test_plan_requires_approval(self):
        # 草稿/待批准均不能启动
        pid = self.svc.create_plan(1, "2026-10-01T02:00:00+00:00", "t", OP1)["id"]
        with self.assertRaises(ServiceError) as c:
            self.svc.start_session(pid, OP1)
        self.assertEqual(c.exception.code, "plan_not_approved")
        self.svc.submit_plan(pid, OP1)
        with self.assertRaises(ServiceError):
            self.svc.start_session(pid, OP1)
        # 值班员无权批准
        with self.assertRaises(ServiceError) as c:
            self.svc.review_plan(pid, True, OP1)
        self.assertEqual(c.exception.status, 403)
        self.svc.review_plan(pid, True, SUP)
        sess = self.svc.start_session(pid, OP1)
        self.assertEqual(sess["status"], "running")

    # ---------- 会话互斥 ----------
    def test_session_mutex_same_plan(self):
        s1 = self.svc.start_session(1, OP1)
        with self.assertRaises(ServiceError) as c:
            self.svc.start_session(1, OP2)   # 第二个值班员
        self.assertEqual(c.exception.code, "session_exists")
        self.assertEqual(
            len(self.db.query("SELECT * FROM sessions WHERE status='running'")), 1)

    def test_session_mutex_concurrent(self):
        """两个线程同时启动同一计划，只保留一个运行会话。"""
        results = []

        def worker(uid):
            try:
                results.append(self.svc.start_session(1, uid)["id"])
            except ServiceError as e:
                results.append(("err", e.code))

        threads = [threading.Thread(target=worker, args=(u,))
                   for u in (OP1, OP2)]
        for t in threads: t.start()
        for t in threads: t.join()
        ok = [r for r in results if isinstance(r, int)]
        errs = [r for r in results if isinstance(r, tuple)]
        self.assertEqual(len(ok), 1)
        self.assertEqual(len(errs), 1)
        self.assertEqual(errs[0][1], "session_exists")

    def test_one_running_session_per_unit_across_plans(self):
        self.svc.start_session(1, OP1)  # 机组1 占用
        pid = self.svc.create_plan(1, "2026-10-02T02:00:00+00:00", "x", OP1)["id"]
        self.svc.submit_plan(pid, OP1)
        self.svc.review_plan(pid, True, SUP)
        with self.assertRaises(ServiceError) as c:
            self.svc.start_session(pid, OP2)
        self.assertEqual(c.exception.code, "session_exists")

    # ---------- 步骤顺序与越步审批 ----------
    def test_step_order_enforced(self):
        sid = self.svc.start_session(1, OP1)["id"]
        with self.assertRaises(ServiceError) as c:
            self.svc.complete_step(sid, "loaded", OP1)
        self.assertEqual(c.exception.code, "out_of_order")
        # 外观步骤需要人工确认
        with self.assertRaises(ServiceError):
            self.svc.complete_step(sid, "visual", OP1)

    def test_skip_step_requires_supervisor(self):
        sid = self.svc.start_session(1, OP1)["id"]
        self.svc.complete_step(sid, "visual", OP1, visual_confirmed=True)
        rid = self.svc.request_skip(sid, "fuel", OP1, "油位计故障，目视确认")["id"]
        # 值班员不能批准越步
        with self.assertRaises(ServiceError):
            self.svc.review_skip(rid, True, OP1)
        self.svc.review_skip(rid, True, SUP)
        sess = self.svc.get_session(sid)
        self.assertEqual(
            {x["step_code"]: x["status"] for x in sess["steps"]}["fuel"],
            "skipped")
        # 越过后可继续电池步骤，且不能越过非当前步骤
        self.reading(1, sid, battery_voltage=12.7, fuel_level=62)
        self.svc.complete_step(sid, "battery", OP1)
        with self.assertRaises(ServiceError) as c:
            self.svc.request_skip(sid, "loaded", OP1, "提前越过")
        self.assertEqual(c.exception.code, "out_of_order")
        # 留痕齐全
        types = {e["type"] for e in self.svc.list_events(session_id=sid)}
        self.assertIn("skip_requested", types)
        self.assertIn("skip_approved", types)
        self.assertIn("step_skipped", types)

    # ---------- 维修封锁 ----------
    def test_lockout_blocks_start_and_excludes_capacity(self):
        avail0 = self.svc.list_units()["available_kw"]
        # 值班员无权封锁
        with self.assertRaises(ServiceError):
            self.svc.set_lockout(1, "检修", OP1)
        self.svc.set_lockout(1, "等待更换水泵", SUP)
        ov = self.svc.list_units()
        self.assertNotIn(1, [u["id"] for u in ov["units"]
                             if u["available_for_emergency"]])
        self.assertEqual(ov["available_kw"], avail0 - 500)
        self.assertEqual(ov["locked_out_kw"], 500 + 400)  # 种子已封 D1
        # 已批准计划也无法启动，阻断留痕
        with self.assertRaises(ServiceError) as c:
            self.svc.start_session(1, OP1)
        self.assertEqual(c.exception.code, "unit_unavailable")
        ev = self.db.one(
            "SELECT * FROM events WHERE type='session_blocked' ORDER BY id DESC")
        self.assertIsNotNone(ev)
        # 解除后恢复
        self.svc.clear_lockout(1, SUP)
        self.assertEqual(self.svc.list_units()["available_kw"], avail0)

    # ---------- 合格完成 + 燃油平衡 ----------
    def test_finish_normal_returns_standby_and_balances_fuel(self):
        sid = self.svc.start_session(1, OP1)["id"]
        self.run_normal(sid, fuel_end=61.5)
        done = self.svc.finish_session(sid, OP1)
        self.assertEqual(done["status"], "done")
        self.assertEqual(self.svc.get_unit(1)["status"], "standby")
        # 62% -> 61.5% × 800L = 4.0L
        self.assertAlmostEqual(done["fuel_consumed_l"], 4.0, places=1)
        self.assertIsNotNone(done["duration_sec"])
        # 差异在 5L 容差内：无燃油差异事件
        evs = self.svc.list_events(session_id=sid, type_="fuel_discrepancy")
        self.assertEqual(evs, [])

    def test_fuel_discrepancy_flagged(self):
        sid = self.svc.start_session(1, OP1)["id"]
        self.run_normal(sid, fuel_end=50.0)   # 实耗 96L，远快于账面
        self.svc.finish_session(sid, OP1)
        evs = self.svc.list_events(session_id=sid, type_="fuel_discrepancy")
        self.assertEqual(len(evs), 1)
        self.assertGreater(abs(evs[0]["detail"]["discrepancy_l"]), 5.0)

    def test_cannot_finish_with_pending_steps(self):
        sid = self.svc.start_session(1, OP1)["id"]
        self.svc.complete_step(sid, "visual", OP1, visual_confirmed=True)
        with self.assertRaises(ServiceError) as c:
            self.svc.finish_session(sid, OP1)
        self.assertEqual(c.exception.code, "steps_pending")

    # ---------- 故障留痕 ----------
    def test_start_failure_shutdown(self):
        sid = self.svc.start_session(2, OP1)["id"]   # GEN-B2 start_fail
        self.svc.complete_step(sid, "visual", OP1, visual_confirmed=True)
        self.reading(2, sid, fuel_level=55, battery_voltage=12.55)
        self.svc.complete_step(sid, "fuel", OP1)
        self.svc.complete_step(sid, "battery", OP1)
        self.reading(2, sid, voltage=0.0, frequency=0.0)
        sess = self.svc.complete_step(sid, "no_load", OP1)
        self.assertEqual(sess["status"], "failed")
        self.assertEqual(self.svc.get_unit(2)["status"], "fault")
        types = [e["type"] for e in self.svc.list_events(session_id=sid)]
        self.assertIn("start_failure", types)
        self.assertIn("fault_shutdown", types)

    def test_voltage_out_of_range_shutdown(self):
        sid = self.svc.start_session(1, OP1)["id"]
        self.run_until(sid, "no_load")
        self.reading(1, sid, voltage=432.0, frequency=50.2)  # 越上限
        sess = self.svc.complete_step(sid, "no_load", OP1)
        self.assertEqual(sess["status"], "failed")
        evs = self.svc.list_events(session_id=sid, type_="voltage_out_of_range")
        self.assertEqual(len(evs), 1)

    def test_loaded_voltage_out_emits_load_interrupt(self):
        sid = self.svc.start_session(1, OP1)["id"]
        self.run_until(sid, "loaded")
        self.reading(1, sid, voltage=360.0, frequency=49.4, load_kw=380)
        self.svc.complete_step(sid, "loaded", OP1)
        types = [e["type"] for e in self.svc.list_events(session_id=sid)]
        self.assertIn("voltage_out_of_range", types)
        self.assertIn("load_interrupted", types)

    def test_noise_and_leak_incidents(self):
        sid = self.svc.start_session(3, OP1)["id"]  # GEN-C3 leak 工况
        self.run_until(sid, "no_load", unit=3)
        s2 = self.svc.report_incident(sid, OP1, "leak", "地面发现柴油油迹")
        self.assertEqual(s2["status"], "failed")
        self.assertEqual(self.svc.get_unit(3)["fault_code"], "E505")
        types = [e["type"] for e in self.svc.list_events(session_id=sid)]
        self.assertIn("oil_leak", types)
        self.assertIn("fault_shutdown", types)

    def test_precheck_failure_aborts_without_fault(self):
        # 低油位机组：油位检查不通过 → 中止，机组仍待命
        pid = self.svc.create_plan(5, "2026-10-03T02:00:00+00:00", "t", OP2)["id"]
        self.svc.submit_plan(pid, OP2)
        self.svc.review_plan(pid, True, SUP)
        sid = self.svc.start_session(pid, OP2)["id"]
        self.svc.complete_step(sid, "visual", OP2, visual_confirmed=True)
        self.reading(5, sid, fuel_level=18.0)
        out = self.svc.complete_step(sid, "fuel", OP2)
        self.assertEqual(out["status"], "aborted")
        self.assertEqual(self.svc.get_unit(5)["status"], "standby")

    def run_until(self, sid, target, unit=1):
        self.svc.complete_step(sid, "visual", OP1, visual_confirmed=True)
        self.reading(unit, sid, fuel_level=62, battery_voltage=12.7)
        self.svc.complete_step(sid, "fuel", OP1)
        self.svc.complete_step(sid, "battery", OP1)
        if target == "no_load":
            return
        self.reading(unit, sid, voltage=400, frequency=50, load_kw=25)
        self.svc.complete_step(sid, "no_load", OP1)

    def plan_for(self, unit):
        return self.db.one(
            "SELECT id FROM plans WHERE unit_id=? AND status='approved'",
            (unit,))

    # ---------- 计划取消留痕 ----------
    def test_plan_cancel_audited_and_blocks_when_running(self):
        self.svc.cancel_plan(4, OP1, "台风预警")  # 种子待批准的 E2 计划
        p = self.svc.get_plan(4)
        self.assertEqual(p["status"], "cancelled")
        self.assertEqual(p["cancel_reason"], "台风预警")
        ev = self.db.one(
            "SELECT * FROM events WHERE type='plan_cancelled' ORDER BY id DESC")
        self.assertIsNotNone(ev)
        # 运行中计划不能取消
        self.svc.start_session(1, OP1)
        with self.assertRaises(ServiceError):
            self.svc.cancel_plan(1, OP1, "x")

    # ---------- 加油台账与第二人复核 ----------
    def test_fueling_updates_level(self):
        before = self.svc.get_unit(1)["fuel_level"]
        f = self.svc.add_fueling(1, 160.0, FUEL, "月度补给")  # 800L 箱 = 20%
        self.assertEqual(f["review_status"], "approved")
        self.assertAlmostEqual(self.svc.get_unit(1)["fuel_level"], before + 20)
        # 值班员不能登记加油
        with self.assertRaises(ServiceError):
            self.svc.add_fueling(1, 10, OP1)

    def test_fueling_revision_needs_second_person(self):
        f = self.svc.add_fueling(1, 160.0, FUEL, "登记 160L")["id"]
        rev = self.svc.revise_fueling(f, 120.0, FUEL, "流量计复核 120L")
        self.assertEqual(rev["review_status"], "pending")
        level_after_revise = self.svc.get_unit(1)["fuel_level"]
        # 复核前液位不变
        # 提交人不能自审
        with self.assertRaises(ServiceError) as c:
            self.svc.review_fueling(rev["id"], True, FUEL)
        self.assertEqual(c.exception.code, "self_review")
        # 另一值班员也可作第二人复核
        self.svc.review_fueling(rev["id"], True, OP1, "确认票据 120L")
        self.assertEqual(self.svc.get_unit(1)["fuel_level"],
                         level_after_revise - 5.0)  # 40L/800L = 5%
        old = self.db.one("SELECT * FROM fuelings WHERE id=?", (f,))
        self.assertEqual(old["review_status"], "void")
        types = [e["type"] for e in self.svc.list_events(unit_id=1)]
        self.assertIn("fueling_revised", types)
        self.assertIn("fueling_reviewed", types)

    def test_fueling_revision_denied_keeps_original(self):
        f = self.svc.add_fueling(1, 80.0, FUEL)["id"]
        rev = self.svc.revise_fueling(f, 800.0, FUEL, "误录")["id"]
        level = self.svc.get_unit(1)["fuel_level"]
        self.svc.review_fueling(rev, False, SUP)
        self.assertEqual(self.svc.get_unit(1)["fuel_level"], level)
        self.assertEqual(
            self.db.one("SELECT review_status s FROM fuelings WHERE id=?",
                        (f,))["s"], "approved")

    # ---------- 模拟适配器 ----------
    def test_simulator_modes(self):
        sim = Simulator(self.db, seed=7)
        # 空闲拍点：电压为 0
        sim.tick()
        r = self.svc.latest_readings(2, 1)[0]
        self.assertEqual(r["voltage"], 0.0)
        # start_fail 机组进入空载阶段：建压失败
        sid = self.svc.start_session(2, OP1)["id"]
        sim.tick()  # 启动前拍点：提供油位/电池读数
        self.svc.complete_step(sid, "visual", OP1, visual_confirmed=True)
        self.svc.complete_step(sid, "fuel", OP1)
        self.svc.complete_step(sid, "battery", OP1)
        for _ in range(8):
            sim.tick()
        r = self.svc.latest_readings(2, 1)[0]
        self.assertLess(r["voltage"], 100)
        self.assertEqual(r["flags"], "cranking")

    def test_simulator_leak_flags_and_drains(self):
        sim = Simulator(self.db, seed=9)
        sid = self.svc.start_session(3, OP1)["id"]
        sim.tick()  # 启动前拍点
        self.svc.complete_step(sid, "visual", OP1, visual_confirmed=True)
        self.svc.complete_step(sid, "fuel", OP1)
        self.svc.complete_step(sid, "battery", OP1)
        flagged = False
        for _ in range(12):
            sim.tick()
            r = self.svc.latest_readings(3, 1)[0]
            if "leak" in (r["flags"] or ""):
                flagged = True
        self.assertTrue(flagged)

    # ---------- 提醒 ----------
    def test_reminders_due_and_low_fuel(self):
        rem = Reminder(self.db, due_window_min=5)
        rem.scan()
        alerts = rem.list_alerts()
        kinds = {a["kind"] for a in alerts}
        self.assertIn("low_fuel", kinds)   # GEN-E2 18%
        self.assertIn("due_plan", kinds)   # 种子已批准计划 1~3 分钟后
        # 不重复提醒
        rem.scan()
        self.assertEqual(len(rem.list_alerts()), len(alerts))

    # ---------- 附件 ----------
    def test_attachment_stored_locally(self):
        att = self.svc.save_attachment(b"fake-image", "leak.jpg", 3, OP1,
                                       kind="leak")
        self.assertTrue(os.path.isfile(att["stored_path"]))
        with open(att["stored_path"], "rb") as f:
            self.assertEqual(f.read(), b"fake-image")


if __name__ == "__main__":
    unittest.main(verbosity=2)
