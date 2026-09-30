"""HTTP 层：标准库 http.server 提供 JSON API 与静态页面。

演示用身份切换：请求头 X-User-Id（缺省 u-op1），无真实鉴权。
"""

import json
import mimetypes
import os
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import config as C
from .service import Service, ServiceError

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")


def _match(path, pattern):
    """极简路径模板：/api/plans/{id}/review -> (id,)。"""
    pp = [x for x in path.strip("/").split("/") if x]
    qp = [x for x in pattern.strip("/").split("/") if x]
    if len(pp) != len(qp):
        return None
    out = {}
    for a, b in zip(pp, qp):
        if b.startswith("{") and b.endswith("}"):
            out[b[1:-1]] = urllib.parse.unquote(a)
        elif a != b:
            return None
    return out


class App:
    def __init__(self, db, attach_dir=C.ATTACH_DIR, reminder=None):
        self.service = Service(db, attach_dir=attach_dir)
        self.reminder = reminder

    def routes(self):
        s = self.service
        return [
            ("GET", "/api/health", lambda q, b, h: {"ok": True}),
            ("GET", "/api/me", lambda q, b, h: s.user(h["uid"])),
            ("GET", "/api/users", lambda q, b, h: s.list_users()),
            ("GET", "/api/overview", lambda q, b, h: s.list_units()),
            ("GET", "/api/approvals", lambda q, b, h: s.approval_queue()),
            ("GET", "/api/alerts", lambda q, b, h:
             (self.reminder.list_alerts(q.get("all") == "1")
              if self.reminder else [])),
            ("POST", "/api/alerts/{id}/ack", lambda q, b, h:
             self.reminder.acknowledge(int(q["id"])) or {"ok": True}
             if self.reminder else {"ok": False}),

            ("POST", "/api/units/{id}/sim-mode", lambda q, b, h:
             s.set_sim_mode(int(q["id"]), b["mode"], h["uid"]) or {"ok": True}),
            ("POST", "/api/units/{id}/lockout", lambda q, b, h:
             s.set_lockout(int(q["id"]), b.get("reason", ""), h["uid"])
             or {"ok": True}),
            ("DELETE", "/api/units/{id}/lockout", lambda q, b, h:
             s.clear_lockout(int(q["id"]), h["uid"]) or {"ok": True}),
            ("GET", "/api/units/{id}/readings", lambda q, b, h:
             s.readings_series(int(q["id"]),
                               int(q["session_id"]) if q.get("session_id")
                               else None, int(q.get("limit", 120)))),
            ("GET", "/api/units/{id}/fuel-balance", lambda q, b, h:
             s.fuel_balance(int(q["id"]))),
            ("GET", "/api/units/{id}/events", lambda q, b, h:
             s.list_events(unit_id=int(q["id"]),
                           type_=q.get("type"), limit=int(q.get("limit", 100)))),

            ("GET", "/api/plans", lambda q, b, h:
             s.list_plans(q.get("status"))),
            ("POST", "/api/plans", lambda q, b, h:
             s.create_plan(int(b["unit_id"]), b["scheduled_at"],
                           b.get("note", ""), h["uid"])),
            ("GET", "/api/plans/{id}", lambda q, b, h:
             s.get_plan(int(q["id"]))),
            ("POST", "/api/plans/{id}/submit", lambda q, b, h:
             s.submit_plan(int(q["id"]), h["uid"])),
            ("POST", "/api/plans/{id}/review", lambda q, b, h:
             s.review_plan(int(q["id"]), bool(b.get("approve")), h["uid"],
                           b.get("reason", ""))),
            ("POST", "/api/plans/{id}/cancel", lambda q, b, h:
             s.cancel_plan(int(q["id"]), h["uid"], b.get("reason", ""))),
            ("POST", "/api/plans/{id}/sessions", lambda q, b, h:
             s.start_session(int(q["id"]), h["uid"])),

            ("GET", "/api/sessions", lambda q, b, h:
             s.list_sessions(int(q["unit_id"]) if q.get("unit_id") else None,
                             q.get("status"))),
            ("GET", "/api/sessions/{id}", lambda q, b, h:
             s.get_session(int(q["id"]))),
            ("POST", "/api/sessions/{id}/steps/{code}", lambda q, b, h:
             s.complete_step(int(q["id"]), q["code"], h["uid"],
                             b.get("note", ""),
                             b.get("visual_confirmed"),
                             b.get("fail_reason", ""))),
            ("POST", "/api/sessions/{id}/finish", lambda q, b, h:
             s.finish_session(int(q["id"]), h["uid"], b.get("note", ""))),
            ("POST", "/api/sessions/{id}/abort", lambda q, b, h:
             s.abort_session(int(q["id"]), h["uid"], b.get("reason", ""))),
            ("POST", "/api/sessions/{id}/incidents", lambda q, b, h:
             s.report_incident(int(q["id"]), h["uid"], b["kind"],
                               b.get("note", ""), b.get("attachment_id"))),

            ("GET", "/api/skips", lambda q, b, h:
             s.list_skips(q.get("status"))),
            ("POST", "/api/skips", lambda q, b, h:
             s.request_skip(int(b["session_id"]), b["step_code"], h["uid"],
                            b.get("reason", ""))),
            ("POST", "/api/skips/{id}/review", lambda q, b, h:
             s.review_skip(int(q["id"]), bool(b.get("approve")), h["uid"])),

            ("GET", "/api/events", lambda q, b, h:
             s.list_events(type_=q.get("type"),
                           unit_id=int(q["unit_id"]) if q.get("unit_id")
                           else None, limit=int(q.get("limit", 200)))),
            ("GET", "/api/fuelings", lambda q, b, h:
             s.list_fuelings(int(q["unit_id"]) if q.get("unit_id") else None)),
            ("POST", "/api/fuelings", lambda q, b, h:
             s.add_fueling(int(b["unit_id"]), float(b["liters"]), h["uid"],
                           b.get("note", ""))),
            ("POST", "/api/fuelings/{id}/revise", lambda q, b, h:
             s.revise_fueling(int(q["id"]), float(b["liters"]), h["uid"],
                              b.get("note", ""))),
            ("POST", "/api/fuelings/{id}/review", lambda q, b, h:
             s.review_fueling(int(q["id"]), bool(b.get("approve")), h["uid"],
                              b.get("note", ""))),
            ("GET", "/api/attachments", lambda q, b, h:
             s.list_attachments(int(q["session_id"]) if q.get("session_id")
                                else None,
                                int(q["unit_id"]) if q.get("unit_id")
                                else None)),
        ]


def make_handler(app):
    routes = app.routes()

    class Handler(BaseHTTPRequestHandler):
        server_version = "GenPlat/1.0"

        def log_message(self, fmt, *args):
            pass  # 静默；事件已落库

        def _send_json(self, obj, status=200):
            body = json.dumps(obj, ensure_ascii=False, default=str).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _static(self, rel):
            if rel == "":
                rel = "index.html"
            path = os.path.normpath(os.path.join(STATIC_DIR, rel))
            if not path.startswith(STATIC_DIR) or not os.path.isfile(path):
                self.send_error(404)
                return
            ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
            with open(path, "rb") as f:
                body = f.read()
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _read_body(self):
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length) if length else b""
            if not raw:
                return {}
            ctype = self.headers.get("Content-Type", "")
            if "application/json" in ctype:
                try:
                    return json.loads(raw.decode()) or {}
                except json.JSONDecodeError:
                    raise ServiceError("请求体不是合法 JSON", "bad_json", 400)
            return {"__raw__": raw}

        def _dispatch(self, method):
            parsed = urllib.parse.urlparse(self.path)
            path, query_s = parsed.path, parsed.query
            query = {k: v[0] for k, v in
                     urllib.parse.parse_qs(query_s).items()}

            if method == "GET" and not path.startswith("/api/"):
                return self._static(path.lstrip("/"))

            uid = self.headers.get("X-User-Id", "u-op1")
            body = {}
            if method in ("POST", "PUT", "PATCH", "DELETE"):
                try:
                    body = self._read_body()
                except ServiceError as e:
                    return self._send_json({"error": e.code,
                                            "message": str(e)}, e.status)

            # 附件上传：原始字节 + query 参数
            if method == "POST" and path == "/api/attachments":
                raw = body.get("__raw__", b"")
                if not raw:
                    return self._send_json(
                        {"error": "empty", "message": "附件内容为空"}, 400)
                try:
                    att = app.service.save_attachment(
                        raw, query.get("filename", "attachment.bin"),
                        int(query["unit_id"]), uid,
                        kind=query.get("kind", "field"),
                        session_id=int(query["session_id"])
                        if query.get("session_id") else None)
                    return self._send_json(att, 201)
                except ServiceError as e:
                    return self._send_json(
                        {"error": e.code, "message": str(e)}, e.status)

            for m, pattern, fn in routes:
                if m != method:
                    continue
                kw = _match(path, pattern)
                if kw is None:
                    continue
                try:
                    # 路径参数优先，同时保留 query string 参数
                    result = fn({**query, **kw}, body, {"uid": uid})
                    if result is None:
                        result = {"ok": True}
                    return self._send_json(result, 200 if method == "GET"
                                           else 200)
                except ServiceError as e:
                    return self._send_json(
                        {"error": e.code, "message": str(e)}, e.status)
                except (KeyError, ValueError) as e:
                    return self._send_json(
                        {"error": "bad_request", "message": f"参数错误：{e}"},
                        400)
            self._send_json({"error": "not_found", "message": "未找到路由"}, 404)

        def do_GET(self):
            self._dispatch("GET")

        def do_POST(self):
            self._dispatch("POST")

        def do_DELETE(self):
            self._dispatch("DELETE")

    return Handler


def run(db, host="127.0.0.1", port=8000, attach_dir=C.ATTACH_DIR,
        reminder=None):
    app = App(db, attach_dir=attach_dir, reminder=reminder)
    httpd = ThreadingHTTPServer((host, port), make_handler(app))
    return httpd, app
