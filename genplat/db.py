"""SQLite 持久化层：建表、连接与少量行工厂工具。

写操作集中在同一把进程锁内，配合 WAL 供模拟线程与 HTTP 线程并发读取。
互斥、只追加留痕等业务约束在 schema 层即有强制：
  * 同一计划 / 同一机组同时只允许一个 running 会话（部分唯一索引）；
  * events 为只追加审计表，应用代码不提供 update/delete。
"""

import json
import sqlite3
import threading
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    role TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS units (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    building TEXT NOT NULL,
    rated_power_kw REAL NOT NULL,
    battery_voltage REAL NOT NULL DEFAULT 12.6,
    fuel_level REAL NOT NULL DEFAULT 60,          -- 液位 %
    fuel_capacity_l REAL NOT NULL DEFAULT 500,
    status TEXT NOT NULL DEFAULT 'standby',
    fault_code TEXT,
    lockout_reason TEXT,
    lockout_by TEXT,
    lockout_at TEXT,
    sim_mode TEXT NOT NULL DEFAULT 'normal'      -- normal/start_fail/leak
);

CREATE TABLE IF NOT EXISTS plans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    unit_id INTEGER NOT NULL REFERENCES units(id),
    scheduled_at TEXT NOT NULL,                   -- ISO8601 UTC
    note TEXT DEFAULT '',
    status TEXT NOT NULL DEFAULT 'draft',
    created_by TEXT,
    submitted_at TEXT,
    reviewed_by TEXT,
    reviewed_at TEXT,
    cancel_reason TEXT,
    cancelled_by TEXT,
    cancelled_at TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id INTEGER NOT NULL REFERENCES plans(id),
    unit_id INTEGER NOT NULL REFERENCES units(id),
    started_by TEXT,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    status TEXT NOT NULL DEFAULT 'running',       -- running/done/failed/aborted
    duration_sec INTEGER,
    fuel_start_pct REAL,
    fuel_end_pct REAL,
    fuel_consumed_l REAL,                         -- 按液位折算的实际耗油
    fuel_burned_runtime_l REAL,                   -- 按运行时长/负载的账面耗油
    fuel_discrepancy_l REAL,
    summary TEXT DEFAULT ''
);
-- 同一计划只保留一个运行会话；同一机组同时只能运行一个会话。
CREATE UNIQUE INDEX IF NOT EXISTS ux_session_plan_active
    ON sessions(plan_id) WHERE status = 'running';
CREATE UNIQUE INDEX IF NOT EXISTS ux_session_unit_active
    ON sessions(unit_id) WHERE status = 'running';

CREATE TABLE IF NOT EXISTS session_steps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL REFERENCES sessions(id),
    step_code TEXT NOT NULL,
    step_order INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',       -- pending/pass/fail/skipped
    started_at TEXT,
    completed_at TEXT,
    operator_id TEXT,
    measured_json TEXT,
    note TEXT DEFAULT '',
    fail_reason TEXT DEFAULT '',
    UNIQUE(session_id, step_code)
);

CREATE TABLE IF NOT EXISTS readings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    unit_id INTEGER NOT NULL REFERENCES units(id),
    session_id INTEGER,
    ts TEXT NOT NULL,
    voltage REAL,
    frequency REAL,
    load_kw REAL,
    fuel_level REAL,
    battery_voltage REAL,
    oil_pressure REAL,
    flags TEXT DEFAULT ''                          -- 逗号分隔: noise,leak...
);
CREATE INDEX IF NOT EXISTS ix_readings_unit_ts ON readings(unit_id, ts);

-- 只追加审计事件流
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    unit_id INTEGER,
    plan_id INTEGER,
    session_id INTEGER,
    step_code TEXT,
    type TEXT NOT NULL,
    severity TEXT NOT NULL DEFAULT 'info',        -- info/warning/critical
    actor_id TEXT,
    detail_json TEXT DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS ix_events_unit_ts ON events(unit_id, ts);
CREATE INDEX IF NOT EXISTS ix_events_session ON events(session_id);

CREATE TABLE IF NOT EXISTS skip_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL REFERENCES sessions(id),
    step_code TEXT NOT NULL,
    requested_by TEXT,
    reason TEXT DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending',       -- pending/approved/denied
    reviewed_by TEXT,
    reviewed_at TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS fuelings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    unit_id INTEGER NOT NULL REFERENCES units(id),
    ts TEXT NOT NULL,
    liters REAL NOT NULL,
    level_before REAL NOT NULL,
    level_after REAL NOT NULL,
    recorded_by TEXT,
    note TEXT DEFAULT '',
    review_status TEXT NOT NULL DEFAULT 'pending', -- pending/approved/revised/void
    reviewed_by TEXT,
    reviewed_at TEXT,
    supersedes INTEGER REFERENCES fuelings(id),
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_fuelings_unit ON fuelings(unit_id, ts);

CREATE TABLE IF NOT EXISTS attachments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id INTEGER,
    session_id INTEGER,
    unit_id INTEGER,
    kind TEXT NOT NULL DEFAULT 'field',           -- field/leak/noise/other
    filename TEXT NOT NULL,
    stored_path TEXT NOT NULL,
    content_type TEXT DEFAULT 'application/octet-stream',
    uploaded_by TEXT,
    ts TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    kind TEXT NOT NULL,                           -- due_plan/low_fuel
    unit_id INTEGER,
    plan_id INTEGER,
    message TEXT NOT NULL,
    severity TEXT NOT NULL DEFAULT 'warning',
    acknowledged INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_alerts_ts ON alerts(ts);
"""


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Database:
    """薄封装：单连接 + 写锁，行转 dict。"""

    def __init__(self, path=":memory:"):
        self.path = path
        self._wl = threading.RLock()
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    # ---- 基础 ----
    def query(self, sql, args=()):
        with self._wl:
            cur = self.conn.execute(sql, args)
            return [dict(r) for r in cur.fetchall()]

    def one(self, sql, args=()):
        rows = self.query(sql, args)
        return rows[0] if rows else None

    def execute(self, sql, args=()):
        """写操作；返回 lastrowid。"""
        with self._wl:
            cur = self.conn.execute(sql, args)
            self.conn.commit()
            return cur.lastrowid

    def transaction(self):
        return _Tx(self)

    def add_event(self, type_, *, unit_id=None, plan_id=None, session_id=None,
                  step_code=None, severity="info", actor_id=None, detail=None):
        return self.execute(
            "INSERT INTO events(ts,unit_id,plan_id,session_id,step_code,type,"
            "severity,actor_id,detail_json) VALUES(?,?,?,?,?,?,?,?,?)",
            (now_iso(), unit_id, plan_id, session_id, step_code, type_,
             severity, actor_id, json.dumps(detail or {}, ensure_ascii=False)))

    def close(self):
        with self._wl:
            self.conn.close()


class _Tx:
    """显式事务：多个写语句与事件留痕需要原子提交（如会话互斥）。"""

    def __init__(self, db):
        self.db = db

    def __enter__(self):
        self.db._wl.acquire()
        self.db.conn.execute("BEGIN")
        return self.db

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            self.db.conn.commit()
        else:
            self.db.conn.rollback()
        self.db._wl.release()
        return False
