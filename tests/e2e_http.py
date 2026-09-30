"""端到端 HTTP 冒烟：完整正常试运行 + 启动失败 + 漏油 + 越步 + 加油复核。

用法：先 `python -m genplat demo --reset`，再 `python tests/e2e_http.py`。
端口可用 GENPLAT_E2E_BASE 覆盖（默认 http://127.0.0.1:8000）。
"""
import json
import os
import time
import urllib.request

BASE = os.environ.get("GENPLAT_E2E_BASE", "http://127.0.0.1:8000")


def call(method, path, uid="u-op1", body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"X-User-Id": uid,
                                          "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def wait_reading(sid, unit, tries=10, pred=None):
    for _ in range(tries):
        _, rows = call("GET", f"/api/units/{unit}/readings?session_id={sid}&limit=1")
        if rows and (pred is None or pred(rows[0])):
            return rows[0]
        time.sleep(0.6)
    return rows[0] if rows else None


def run_plan(plan_id, unit, expect, leak_incident=False):
    _, s = call("POST", f"/api/plans/{plan_id}/sessions")
    assert s.get("status") == "running", s
    sid = s["id"]
    call("POST", f"/api/sessions/{sid}/steps/visual", body={"visual_confirmed": True})
    wait_reading(sid, unit, pred=lambda r: r["fuel_level"] is not None)
    call("POST", f"/api/sessions/{sid}/steps/fuel")
    call("POST", f"/api/sessions/{sid}/steps/battery")
    # 等模拟器进入启动阶段：正常机组建压 >100V；启动失败工况为盘车标记
    wait_reading(sid, unit, tries=12,
                 pred=lambda r: (r["voltage"] or 0) > 100 or r["flags"] == "cranking")
    st, r = call("POST", f"/api/sessions/{sid}/steps/no_load")
    sess = r
    if expect == "start_fail":
        assert r["status"] == "failed", r["status"]
        _, evs = call("GET", f"/api/events?session_id={sid}")
        assert "start_failure" in [e["type"] for e in evs]
        print(f"  plan {plan_id}: 启动失败留痕 OK，机组故障码 {next(u for u in call('GET','/api/overview')[1]['units'] if u['id']==unit)['fault_code']}")
        return
    if leak_incident:
        # 人工报告漏油 -> 紧急停机
        st, r2 = call("POST", f"/api/sessions/{sid}/incidents",
                      body={"kind": "leak", "note": "油底壳下方发现油迹"})
        assert r2["status"] == "failed", r2
        _, evs = call("GET", f"/api/events?session_id={sid}")
        assert {"oil_leak", "fault_shutdown"} <= {e["type"] for e in evs}
        print(f"  plan {plan_id}: 漏油停机留痕 OK")
        return
    wait_reading(sid, unit, tries=12, pred=lambda r: (r["load_kw"] or 0) > 100)
    st, r = call("POST", f"/api/sessions/{sid}/steps/loaded")
    assert r["status"] == "running", r
    st, done = call("POST", f"/api/sessions/{sid}/finish")
    assert done["status"] == "done", done
    assert done["fuel_consumed_l"] is not None
    print(f"  plan {plan_id}: 全部合格，时长 {done['duration_sec']}s，"
          f"实耗 {done['fuel_consumed_l']}L，账面 {done['fuel_burned_runtime_l']}L，"
          f"差异 {done['fuel_discrepancy_l']}L")


def main():
    assert call("GET", "/api/health")[1]["ok"]
    ov = call("GET", "/api/overview")[1]
    print(f"应急可用 {ov['available_kw']}kW / 封锁 {ov['locked_out_kw']}kW")

    print("正常链路（GEN-A1, plan 1）:")
    run_plan(1, 1, "ok")
    print("启动失败链路（GEN-B2, plan 2）:")
    run_plan(2, 2, "start_fail")
    print("漏油链路（GEN-C3, plan 3）:")
    run_plan(3, 3, "ok", leak_incident=True)

    # 故障恢复后机组才能再启动（此处仅验证封锁阻断已在单测覆盖）
    print("审批权限:")
    st, r = call("POST", "/api/plans/4/review", uid="u-op1",
                 body={"approve": True})
    assert st == 403, r
    print("  值班员批准计划 -> 403 OK")
    st, p = call("POST", "/api/plans/4/review", uid="u-sup",
                 body={"approve": True})
    assert p["status"] == "approved"
    print("  主管批准计划 OK")

    print("加油第二人复核:")
    _, f = call("POST", "/api/fuelings", uid="u-fuel",
                body={"unit_id": 1, "liters": 100, "note": "E2E 补给"})
    _, rev = call("POST", f"/api/fuelings/{f['id']}/revise", uid="u-fuel",
                  body={"liters": 80, "note": "流量计校正"})
    st, r = call("POST", f"/api/fuelings/{rev['id']}/review", uid="u-fuel",
                 body={"approve": True})
    assert st == 403 and r["error"] == "self_review", r
    print("  提交人自审 -> 403 OK")
    st, r = call("POST", f"/api/fuelings/{rev['id']}/review", uid="u-sup",
                 body={"approve": True})
    assert r["review_status"] == "approved"
    print("  第二人复核通过，台账更新 OK")

    print("越步审批:")
    # plan 4 已批准（E2 机组低油位 18%），油位步骤必失败；改为申请越过
    _, s = call("POST", "/api/plans/4/sessions", uid="u-op2")
    sid = s["id"]
    call("POST", f"/api/sessions/{sid}/steps/visual", uid="u-op2",
         body={"visual_confirmed": True})
    _, sk = call("POST", "/api/skips", uid="u-op2",
                 body={"session_id": sid, "step_code": "fuel", "reason": "油位计送检"})
    st, r = call("POST", f"/api/skips/{sk['id']}/review", uid="u-op2",
                 body={"approve": True})
    assert st == 403
    _, r = call("POST", f"/api/skips/{sk['id']}/review", uid="u-sup",
                body={"approve": True})
    assert r["status"] == "approved"
    sess = call("GET", f"/api/sessions/{sid}")[1]
    assert next(x for x in sess["steps"] if x["step_code"] == "fuel")["status"] == "skipped"
    print("  油位步骤越步经主管批准 OK")

    print("\n全部 HTTP 端到端检查通过 ✔")


if __name__ == "__main__":
    main()
