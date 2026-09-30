"""全局配置：路径、人员角色、合格区间与平台常量。"""
from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(os.environ.get("GENOPS_HOME", Path(__file__).resolve().parent.parent / "data"))
DB_PATH = BASE_DIR / "genops.db"
ATTACH_DIR = BASE_DIR / "attachments"

# 现场人员：工号 -> (姓名, 角色)。operator=值班员，supervisor=工程主管
USERS = {
    "zhangwei": ("张伟", "operator"),
    "lina": ("李娜", "operator"),
    "wangqiang": ("王强", "supervisor"),
}
DEFAULT_USER = "zhangwei"

# ---- 试运行步骤（顺序固定，不可跳号） ----
STEPS = [
    ("visual", "外观检查", 1),
    ("fuel", "油位检查", 2),
    ("battery", "启动电池", 3),
    ("start_noload", "空载启动", 4),
    ("load_test", "带载测试", 5),
]

# ---- 实时参数合格区间 ----
VOLTAGE_MIN, VOLTAGE_MAX = 380.0, 420.0          # V，额定 400V
FREQ_MIN, FREQ_MAX = 49.5, 50.5                  # Hz，额定 50Hz
BATTERY_MIN, BATTERY_MAX = 24.0, 28.0            # V，24V 启动电池组
FUEL_READY_PERCENT = 25.0                        # 试运行前最低油位 %
LOW_FUEL_PERCENT = 20.0                          # 低油位告警 %

# ---- 燃油模型（模拟） ----
# 带载测试按 50% 额定功率，柴油机比油耗约 0.25 L/KWh
LOAD_FACTOR = 0.5
SPECIFIC_FUEL_L_PER_KWH = 0.25
IDLE_L_PER_MIN = 0.15
FUEL_TOLERANCE_L = 5.0                           # 实测/账面差额超此值留痕

# ---- 计划提醒 ----
PLAN_DUE_WINDOW_MIN = 30                          # 30 分钟内到期即提醒
REMIND_INTERVAL_SEC = 10
TELEMETRY_INTERVAL_SEC = 2

# ---- 界面主题：设备灰 / 柴油黄 / 运行绿 ----
THEME = {
    "gray": "#5A6169",
    "gray_dark": "#3D434A",
    "gray_light": "#E7E9EB",
    "yellow": "#E5A800",   # 柴油黄（面/点缀）
    "yellow_deep": "#9A6F00",  # 加深，保证文字/线条对比
    "green": "#2F7D32",    # 运行绿
    "red": "#B42318",
}
