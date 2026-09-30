"""SQLite schema 与连接管理。所有运行事件均落库。"""
from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS generators (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,                    -- 机组名称
    building TEXT NOT NULL,                -- 所在楼宇
    rated_power_kw REAL NOT NULL,          -- 额定功率 kW
    battery_voltage REAL NOT NULL,         -- 启动电池电压 V
    fuel_capacity_l REAL NOT NULL,         -- 油箱容量 L
    fuel_level_l REAL NOT NULL,            -- 当前油位 L
    status TEXT NOT NULL DEFAULT 'standby',-- standby/running/start_failed/shutdown/locked_out
    locked_out INTEGER NOT NULL DEFAULT 0, -- 维修封锁
    lock_reason TEXT,
    fault TEXT,                            -- 当前故障描述
    sim_mode TEXT NOT NULL DEFAULT 'normal'-- normal/start_fail/oil_leak
);

CREATE TABLE IF NOT EXISTS plans (
    id TEXT PRIMARY KEY,
    generator_id TEXT NOT NULL REFERENCES generators(id),
    scheduled_at TEXT NOT NULL,            -- ISO 本地时间
    duration_min INTEGER NOT NULL,
    assignee TEXT NOT NULL,                -- 计划值班员
    status TEXT NOT NULL DEFAULT 'pending',-- pending/approved/rejected/canceled/running/completed/failed
    created_at TEXT NOT NULL,
    created_by TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS approvals (
    id TEXT PRIMARY KEY,
    plan_id TEXT NOT NULL REFERENCES plans(id),
    kind TEXT NOT NULL,                    -- plan / skip_step
    step_code TEXT,                        -- skip_step 时记录被越过的步骤
    reason TEXT NOT NULL,
    decision TEXT NOT NULL,                -- approved/rejected
    approver TEXT NOT NULL,                -- 工号
    approver_role TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    plan_id TEXT NOT NULL UNIQUE REFERENCES plans(id),  -- 同一计划只有一个运行会话
    generator_id TEXT NOT NULL REFERENCES generators(id),
    operator_id TEXT NOT NULL,
    operator_name TEXT NOT NULL,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    outcome TEXT,                          -- completed/failed/shutdown
    current_step TEXT NOT NULL DEFAULT 'visual',
    load_started_at TEXT,                   -- 带载测试开始时刻
    start_fuel_l REAL,                     -- 会话开始时账面油位
    end_fuel_l REAL,                       -- 结束时账面油位
    measured_remaining_l REAL,             -- 结束时人工实测剩余
    fuel_delta_l REAL,                     -- 运行时长×单耗推算消耗
    run_minutes REAL,
    skip_granted INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS step_records (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    step_code TEXT NOT NULL,
    seq INTEGER NOT NULL,
    status TEXT NOT NULL,                  -- pending/pass/fail/skipped
    checked_at TEXT,
    operator_id TEXT,
    note TEXT,
    UNIQUE(session_id, step_code)
);

CREATE TABLE IF NOT EXISTS fuel_ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    generator_id TEXT NOT NULL REFERENCES generators(id),
    change_l REAL NOT NULL,                -- 正=加油，负=运行消耗
    balance_l REAL NOT NULL,               -- 记账后余额
    reason TEXT NOT NULL,                  -- run_consumption/refuel/fuel_discrepancy/init
    ref_id TEXT,                           -- 会话或审批关联
    created_at TEXT NOT NULL,
    created_by TEXT NOT NULL
);

-- 加油记录（每次加油先以草稿金额入账，复核后生效）
CREATE TABLE IF NOT EXISTS refuel_records (
    id TEXT PRIMARY KEY,
    generator_id TEXT NOT NULL REFERENCES generators(id),
    liters REAL NOT NULL,
    recorded_balance_l REAL NOT NULL,      -- 记录时账面
    before_l REAL NOT NULL,
    after_l REAL NOT NULL,
    note TEXT,
    status TEXT NOT NULL DEFAULT 'pending_second_check', -- pending_second_check/confirmed/revised
    recorder_id TEXT NOT NULL,
    recorder_name TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    reviewer_id TEXT,
    reviewer_name TEXT,
    reviewed_at TEXT,
    review_note TEXT,
    revision_of TEXT REFERENCES refuel_records(id),
    revised_liters REAL
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    category TEXT NOT NULL,                -- start_fail/voltage/noise/oil_leak/load_abort/fuel_discrepancy/plan_cancel/...
    severity TEXT NOT NULL,                -- info/warning/critical
    generator_id TEXT,
    plan_id TEXT,
    session_id TEXT,
    ref_id TEXT,
    message TEXT NOT NULL,
    detail_json TEXT
);

CREATE TABLE IF NOT EXISTS attachments (
    id TEXT PRIMARY KEY,
    category TEXT NOT NULL,                -- fault/refuel/step
    ref_id TEXT,
    filename TEXT NOT NULL,
    stored_name TEXT NOT NULL,
    content_type TEXT,
    size INTEGER,
    uploaded_by TEXT,
    uploaded_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reminders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    kind TEXT NOT NULL,                    -- plan_due / low_fuel
    generator_id TEXT,
    plan_id TEXT,
    message TEXT NOT NULL,
    acknowledged INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
CREATE INDEX IF NOT EXISTS idx_events_cat ON events(category);
CREATE INDEX IF NOT EXISTS idx_ledger_gen ON fuel_ledger(generator_id, created_at);
CREATE INDEX IF NOT EXISTS idx_rem_ack ON reminders(acknowledged);
"""


class Database:
    """每线程一个连接（check_same_thread=False + 锁串行化写）。"""

    def __init__(self, path: Path | str | None = None):
        self.path = str(path or config.DB_PATH)
        self._lock = threading.RLock()
        self._local = threading.local()

    def _conn(self) -> sqlite3.Connection:
        if not hasattr(self._local, "conn"):
            conn = sqlite3.connect(self.path, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA journal_mode=WAL")
            self._local.conn = conn
        return self._local.conn

    @property
    def lock(self) -> threading.RLock:
        return self._lock

    def init(self) -> None:
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        config.ATTACH_DIR.mkdir(parents=True, exist_ok=True)
        with self._lock:
            self._conn().executescript(SCHEMA)
            self._conn().commit()

    def query(self, sql: str, params=()):
        with self._lock:
            cur = self._conn().execute(sql, params)
            return cur.fetchall()

    def query_one(self, sql: str, params=()):
        with self._lock:
            cur = self._conn().execute(sql, params)
            return cur.fetchone()

    def execute(self, sql: str, params=()):
        with self._lock:
            conn = self._conn()
            cur = conn.execute(sql, params)
            conn.commit()
            return cur

    def close(self) -> None:
        with self._lock:
            if hasattr(self._local, "conn"):
                self._local.conn.close()
                del self._local.conn
