"""领域常量：步骤定义、合格区间、角色与状态。

界面配色（设备灰 / 柴油黄 / 运行绿，辅以状态红）集中在此，
供后端告警分级与前端样式共同参照。
"""

# ---- 配色（CSS 变量同步在 static/styles.css） ----
COLOR = {
    "equipment": "#3A434A",   # 设备灰：结构/待命
    "equipment_dark": "#272E33",
    "diesel": "#E0A329",      # 柴油黄：待处理/燃油/审批中
    "diesel_dark": "#9A6E12",
    "run": "#2F9E44",         # 运行绿：合格/运行中
    "run_dark": "#1E6B2E",
    "fault": "#C62828",       # 故障红：停机/越限/漏油
    "surface": "#F4F5F3",
    "ink": "#20262B",
    "muted": "#69727A",
}

# ---- 人员角色 ----
ROLE_OPERATOR = "operator"      # 值班员：执行步骤
ROLE_SUPERVISOR = "supervisor"  # 工程主管：批准计划、批准越步
ROLE_FUEL = "fuel_clerk"        # 加油员：登记加油

# 演示账号（无真实鉴权，前端用 X-User-Id 切换身份）
USERS = {
    "u-op1":  {"name": "值班员·周明", "role": ROLE_OPERATOR},
    "u-op2":  {"name": "值班员·陈立", "role": ROLE_OPERATOR},
    "u-sup":  {"name": "工程主管·方工", "role": ROLE_SUPERVISOR},
    "u-fuel": {"name": "加油员·高源", "role": ROLE_FUEL},
}

# ---- 试运行步骤（严格顺序） ----
# code 唯一标识；limits 给出该步骤实时数值的合格区间（None 表示无数值核对）。
#   check 字段用于与模拟读数比对：
#     fuel  -> 液位 >= 阈值(%)
#     battery -> 电池电压落在区间(V)
#     no_load_v / no_load_f -> 空载电压/频率区间
#     load_v / load_f -> 带载电压/频率区间
STEPS = [
    {"code": "visual",  "order": 1, "name": "外观检查",
     "desc": "机组外观、排烟、接地与周边环境，确认无渗漏痕迹。"},
    {"code": "fuel",    "order": 2, "name": "油位检查",
     "desc": "核对柴油液位，低于 25% 不得继续带载测试。",
     "metric": "fuel_level", "label": "燃油液位", "unit": "%",
     "min": 25.0, "max": 100.0},
    {"code": "battery", "order": 3, "name": "启动电池",
     "desc": "测量启动电池端电压，低于 12.0V 判为亏电。",
     "metric": "battery_voltage", "label": "电池电压", "unit": "V",
     "min": 12.4, "max": 13.8},
    {"code": "no_load", "order": 4, "name": "空载启动",
     "desc": "空载启动并稳定至额定转速，核对电压与频率。",
     "metric": "no_load_v", "label": "空载电压", "unit": "V",
     "min": 390.0, "max": 410.0,
     "extra": {"metric": "no_load_f", "label": "频率", "unit": "Hz",
               "min": 49.5, "max": 50.5}},
    {"code": "loaded",  "order": 5, "name": "带载测试",
     "desc": "分级加至额定负载的 80% 运行，核对带载电压、频率与油压。",
     "metric": "load_v", "label": "带载电压", "unit": "V",
     "min": 385.0, "max": 415.0,
     "extra": {"metric": "load_f", "label": "频率", "unit": "Hz",
               "min": 49.0, "max": 51.0}},
]
STEP_BY_CODE = {s["code"]: s for s in STEPS}
STEP_ORDER = [s["code"] for s in STEPS]

# ---- 计划 / 会话 / 机组状态 ----
PLAN_DRAFT, PLAN_PENDING, PLAN_APPROVED, PLAN_CANCELLED = (
    "draft", "pending", "approved", "cancelled")
PLAN_STATUS = {
    PLAN_DRAFT: "草稿", PLAN_PENDING: "待批准",
    PLAN_APPROVED: "已批准", PLAN_CANCELLED: "已取消",
}

SESS_RUNNING, SESS_DONE, SESS_FAILED, SESS_ABORTED = (
    "running", "done", "failed", "aborted")
SESS_STATUS = {
    SESS_RUNNING: "运行中", SESS_DONE: "合格完成",
    SESS_FAILED: "故障停机", SESS_ABORTED: "中止",
}

STEP_PENDING, Step_PASS, STEP_FAIL, STEP_SKIPPED = (
    "pending", "pass", "fail", "skipped")

UNIT_STANDBY, UNIT_RUNNING, UNIT_FAULT, UNIT_LOCKED = (
    "standby", "running", "fault", "locked")
UNIT_STATUS = {
    UNIT_STANDBY: "待命", UNIT_RUNNING: "运行中",
    UNIT_FAULT: "故障", UNIT_LOCKED: "维修封锁",
}

# ---- 事件类型（全部留痕） ----
EV = {
    "PLAN_SUBMITTED": "plan_submitted",
    "PLAN_APPROVED": "plan_approved",
    "PLAN_REJECTED": "plan_rejected",
    "PLAN_CANCELLED": "plan_cancelled",
    "SESSION_STARTED": "session_started",
    "SESSION_BLOCKED": "session_blocked",
    "SESSION_DONE": "session_done",
    "SESSION_ABORTED": "session_aborted",
    "STEP_DONE": "step_completed",
    "STEP_FAILED": "step_failed",
    "STEP_SKIPPED": "step_skipped",
    "SKIP_REQUESTED": "skip_requested",
    "SKIP_APPROVED": "skip_approved",
    "SKIP_DENIED": "skip_denied",
    "START_FAILURE": "start_failure",
    "VOLTAGE_OUT": "voltage_out_of_range",
    "ABNORMAL_NOISE": "abnormal_noise",
    "OIL_LEAK": "oil_leak",
    "LOAD_INTERRUPT": "load_interrupted",
    "FUEL_DISCREPANCY": "fuel_discrepancy",
    "SHUTDOWN_FAULT": "fault_shutdown",
    "FUELING_CREATED": "fueling_created",
    "FUELING_REVISED": "fueling_revised",
    "FUELING_REVIEWED": "fueling_reviewed",
    "LOCKOUT_ON": "lockout_on",
    "LOCKOUT_OFF": "lockout_off",
    "ALERT_DUE": "alert_due_plan",
    "ALERT_LOW_FUEL": "alert_low_fuel",
}

# 低油位阈值（%），低于则进入提醒且不满足带载前置
LOW_FUEL_PCT = 25.0
# 结算时账面差异允许带（L），超出记燃油差异事件
FUEL_TOLERANCE_L = 5.0

# 附件本地目录（相对工作目录）
ATTACH_DIR = "attachments"
