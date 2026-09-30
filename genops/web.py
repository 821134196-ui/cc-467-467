"""HTTP 服务：JSON API + 静态前端（标准库 http.server，无第三方依赖）。"""
from __future__ import annotations

import json
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import config
from .service import GenOpsService, DomainError, PermissionError_
from .telemetry import TelemetryHub, ReminderScheduler

WEB_DIR = Path(__file__).resolve().parent / "static"

STATUS_TEXT = {
    "standby": "待命", "running": "运行中", "start_failed": "启动失败",
    "shutdown": "故障停机", "locked_out": "维修封锁",
}
STEP_TEXT = {c: t for c, t, _ in config.STEPS}


def start_background(hub: TelemetryHub) -> ReminderScheduler:
    """确保各机组遥测存在并启动后台线程。"""
    svc = hub.svc
    if svc is None:
        raise RuntimeError("hub.svc 未注入")
    for g in hub.db.query("SELECT * FROM generators"):
        hub.ensure(g["id"], g["fuel_level_l"])
    hub.start()
    reminders = ReminderScheduler(svc, hub)
    reminders.start()
    return reminders


def create_app(db, hub: TelemetryHub | None = None, addr=("127.0.0.1", 8000)):
    svc = GenOpsService(db, hub)
    if hub is not None:
        hub.svc = svc

    class Handler(BaseHTTPRequestHandler):
        server_version = "GenOps/1.0"

        def log_message(self, fmt, *args):
            pass  # 静默；运行事件均落 SQLite

        # ---------- 基础 ----------
        def _send_json(self, obj, status: int = 200):
            body = json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _send_file(self, path: Path, ctype: str):
            if not path.is_file():
                self._send_json({"error": "not found"}, 404)
                return
            body = path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _body(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            if not length:
                return {}
            raw = self.rfile.read(length)
            try:
                return json.loads(raw.decode("utf-8"))
            except json.JSONDecodeError:
                raise DomainError("请求体不是合法 JSON", "bad_json")

        def _user(self, body: dict | None = None, qs: dict | None = None) -> str:
            uid = (body or {}).get("user") or (qs or {}).get("user") \
                or self.headers.get("X-Operator") or config.DEFAULT_USER
            if uid not in config.USERS:
                raise DomainError(f"未知用户：{uid}", "unknown_user", 403)
            return uid

        def _handle(self, fn):
            try:
                fn()
            except PermissionError_ as e:
                self._send_json({"error": str(e), "code": e.code}, e.status)
            except DomainError as e:
                self._send_json({"error": str(e), "code": e.code}, e.status)
            except Exception as e:  # 兜底，避免 500 白屏
                self._send_json({"error": f"服务器错误：{e}", "code": "internal"}, 500)

        def _qs(self) -> dict:
            q = urllib.parse.urlparse(self.path).query
            return {k: v[0] for k, v in urllib.parse.parse_qs(q).items()}

        # ---------- 路由 ----------
        def do_GET(self):
            self._handle(self._route_get)

        def do_POST(self):
            self._handle(self._route_post)

        def _route_get(self):
            path = urllib.parse.urlparse(self.path).path.rstrip("/") or "/"
            qs = self._qs()

            if path == "/":
                return self._send_file(WEB_DIR / "index.html", "text/html; charset=utf-8")
            static_map = {
                "/app.js": ("app.js", "application/javascript; charset=utf-8"),
                "/styles.css": ("styles.css", "text/css; charset=utf-8"),
            }
            if path in static_map:
                fname, ctype = static_map[path]
                return self._send_file(WEB_DIR / fname, ctype)

            if path == "/api/meta":
                return self._send_json({
                    "users": {uid: {"name": n, "role": r} for uid, (n, r) in config.USERS.items()},
                    "steps": [{"code": c, "title": t, "seq": s} for c, t, s in config.STEPS],
                    "ranges": {
                        "voltage": [config.VOLTAGE_MIN, config.VOLTAGE_MAX],
                        "frequency": [config.FREQ_MIN, config.FREQ_MAX],
                        "battery": [config.BATTERY_MIN, config.BATTERY_MAX],
                        "fuel_ready_percent": config.FUEL_READY_PERCENT,
                        "low_fuel_percent": config.LOW_FUEL_PERCENT,
                        "fuel_tolerance_l": config.FUEL_TOLERANCE_L,
                    },
                    "theme": config.THEME,
                    "status_text": STATUS_TEXT,
                    "step_text": STEP_TEXT,
                })

            if path == "/api/generators":
                return self._send_json({"generators": svc.list_generators(),
                                        "capacity": svc.capacity_summary()})
            if path == "/api/plans":
                return self._send_json({"plans": svc.list_plans(qs.get("status"))})
            if path == "/api/session":
                sid = qs.get("id")
                if sid:
                    return self._send_json({"session": svc.get_session(sid)})
                if qs.get("plan"):
                    return self._send_json({"session": svc.active_session_for_plan(qs["plan"])})
                raise DomainError("缺少 id 或 plan 参数")
            if path == "/api/telemetry":
                gid = qs.get("generator")
                if not gid:
                    raise DomainError("缺少 generator 参数")
                return self._send_json({
                    "latest": hub.latest(gid) if hub else None,
                    "series": hub.series(gid, float(qs.get("minutes", 10))) if hub else [],
                })
            if path == "/api/shutdowns":
                return self._send_json({"events": svc.list_shutdowns()})
            if path == "/api/fuel/ledger":
                return self._send_json({"rows": svc.fuel_ledger_rows(qs.get("generator"))})
            if path == "/api/fuel/refuels":
                return self._send_json({"refuels": svc.list_refuels()})
            if path == "/api/approvals":
                return self._send_json({"approvals": svc.list_approvals()})
            if path == "/api/events":
                return self._send_json({"events": svc.list_events(qs.get("category"))})
            if path == "/api/reminders":
                return self._send_json({"reminders": svc.list_reminders()})
            if path == "/api/attachments":
                return self._send_json({"attachments": svc.list_attachments(
                    qs.get("category"), qs.get("ref_id"))})
            if path.startswith("/api/attachments/"):
                aid = path.rsplit("/", 1)[-1]
                a = svc.db.query_one("SELECT * FROM attachments WHERE id=?", (aid,))
                if not a:
                    raise DomainError("附件不存在", "not_found", 404)
                import urllib.parse as _up
                fname = _up.quote(a["filename"])
                self.send_response(200)
                self.send_header("Content-Type", a["content_type"] or "application/octet-stream")
                self.send_header("Content-Disposition",
                                 f"inline; filename*=UTF-8''{fname}")
                self.send_header("Content-Length", str(a["size"] or 0))
                self.end_headers()
                with open(config.ATTACH_DIR / a["stored_name"], "rb") as fh:
                    self.wfile.write(fh.read())
                return
            raise DomainError("未知路径", "not_found", 404)

        def _route_post(self):
            path = urllib.parse.urlparse(self.path).path.rstrip("/")
            body = self._body()
            user = self._user(body)

            # 计划
            if path == "/api/plans":
                p = svc.create_plan(body["generator"], body["scheduled_at"],
                                    int(body.get("duration_min", 30)),
                                    body.get("assignee", user), user)
                return self._send_json({"plan": p}, 201)
            if path == "/api/plans/approve":
                a = svc.decide_plan(body["plan"], body.get("decision", "approved"),
                                    body.get("reason", ""), user)
                return self._send_json({"approval": a})
            if path == "/api/plans/cancel":
                p = svc.cancel_plan(body["plan"], body.get("reason", ""), user)
                return self._send_json({"plan": p})

            # 封锁
            if path == "/api/lockout":
                g = svc.set_lockout(body["generator"], body.get("reason", ""), user)
                return self._send_json({"generator": g})
            if path == "/api/lockout/clear":
                g = svc.clear_lockout(body["generator"], user)
                return self._send_json({"generator": g})

            # 会话与步骤
            if path == "/api/session/start":
                s, created = svc.start_session(body["plan"], user)
                return self._send_json({"session": s, "created": created},
                                       201 if created else 200)
            sid = body.get("session")
            if path == "/api/steps/visual":
                return self._send_json({"session": svc.submit_visual(
                    sid, user, bool(body.get("passed", True)),
                    noise=bool(body.get("noise")), oil_leak=bool(body.get("oil_leak")),
                    note=body.get("note", ""))})
            if path == "/api/steps/fuel":
                return self._send_json({"session": svc.submit_fuel_check(
                    sid, user, body.get("note", ""))})
            if path == "/api/steps/battery":
                return self._send_json({"session": svc.submit_battery_check(
                    sid, user, body.get("note", ""))})
            if path == "/api/steps/start":
                return self._send_json({"session": svc.attempt_start(sid, user)})
            if path == "/api/steps/load/begin":
                return self._send_json({"session": svc.begin_load_test(sid, user)})
            if path == "/api/steps/load/abort":
                return self._send_json({"session": svc.abort_load_test(
                    sid, user, body.get("reason", "未说明"))})
            if path == "/api/steps/load/complete":
                return self._send_json({"session": svc.complete_load_test(
                    sid, user, body.get("note", ""))})
            if path == "/api/steps/skip":
                return self._send_json({"session": svc.skip_step(
                    sid, body["step"], user, body.get("reason", ""))})
            if path == "/api/session/end":
                m = body.get("measured_remaining_l")
                rm = body.get("run_minutes")
                return self._send_json({"session": svc.end_session(
                    sid, user, measured_remaining_l=float(m) if m not in (None, "") else None,
                    run_minutes=float(rm) if rm not in (None, "") else None,
                    note=body.get("note", ""))})

            # 遥测注入（模拟）
            if path == "/api/sim/inject":
                if hub is None:
                    raise DomainError("遥测适配器未运行")
                hub.inject(body["generator"], body["kind"], float(body.get("seconds", 30)))
                return self._send_json({"ok": True})
            if path == "/api/sim/mode":
                if hub is None:
                    raise DomainError("遥测适配器未运行")
                hub.set_sim_mode(body["generator"], body["mode"])
                return self._send_json({"ok": True})

            # 加油
            if path == "/api/fuel/refuel":
                r = svc.create_refuel(body["generator"], float(body["liters"]), user,
                                      body.get("note", ""))
                return self._send_json({"refuel": r}, 201)
            if path == "/api/fuel/review":
                r = svc.review_refuel(
                    body["refuel"], user, bool(body.get("approved", True)),
                    revised_liters=(float(body["revised_liters"])
                                    if body.get("revised_liters") not in (None, "") else None),
                    note=body.get("note", ""))
                return self._send_json({"refuel": r})

            # 提醒 / 附件
            if path == "/api/reminders/ack":
                svc.ack_reminder(int(body["id"]), user)
                return self._send_json({"ok": True})
            if path == "/api/attachments":
                import base64
                data = base64.b64decode(body["data_b64"])
                a = svc.save_attachment(body.get("category", "fault"),
                                        body.get("ref_id", ""), body["filename"], data,
                                        body.get("content_type", "application/octet-stream"), user)
                return self._send_json({"attachment": a}, 201)

            raise DomainError("未知路径", "not_found", 404)

    class App(ThreadingHTTPServer):
        daemon_threads = True
        allow_reuse_address = True

        def __init__(self, addr):
            super().__init__(addr, Handler)
            self.db = db
            self.hub = hub
            self.svc = svc

        def run(self):
            host, port = self.server_address[:2]
            print(f"[genops] HTTP 监听 {host}:{port}")
            try:
                self.serve_forever()
            finally:
                self.server_close()

    return App(addr)


def make_server(db, hub, host: str, port: int):
    return create_app(db, hub, (host, port))
