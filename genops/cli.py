"""本地启动入口。

用法：
    python -m genops.cli demo          # 重置数据库并播种（正常/启动失败/漏油样例），随后启动 Web
    python -m genops.cli seed          # 仅播种（已存在数据则跳过；--reset 可重置）
    python -m genops.cli serve         # 仅启动 Web 服务
    python -m genops.cli reset         # 删除本地数据库

启动后浏览器访问 http://127.0.0.1:8000
"""
from __future__ import annotations

import argparse
import sys
import time
import webbrowser

from . import config
from .db import Database
from .seed import seed_database


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="genops", description="备用发电机试运行与燃油补给平台")
    parser.add_argument("command", choices=["demo", "seed", "serve", "reset"])
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--reset", action="store_true", help="seed/demo 时重置数据库")
    parser.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    args = parser.parse_args(argv)

    if args.command == "reset":
        for p in (config.DB_PATH, config.DB_PATH.with_suffix(".db-wal"),
                  config.DB_PATH.with_suffix(".db-shm")):
            p.unlink(missing_ok=True)
        print(f"[genops] 已删除 {config.DB_PATH}")
        return 0

    db = Database()
    db.init()

    if args.command == "seed":
        result = seed_database(db, reset=args.reset)
        if result.get("skipped"):
            print("[genops] 数据库已有数据，跳过播种（使用 --reset 可重置）")
        else:
            print("[genops] 播种完成：GEN-A 正常 / GEN-B 启动失败 / GEN-C 漏油 / GEN-D 维修封锁")
            print(f"[genops] 数据库：{config.DB_PATH}")
        return 0

    if args.command == "demo":
        result = seed_database(db, reset=True)
        print("[genops] 已创建样例数据：")
        print("         · GEN-A 1号机组（行政楼）—— 正常，带已获批到期计划 PL-NEW-DUE")
        print("         · GEN-B 2号机组（商业裙楼）—— 启动失败样例，计划 PL-NEW-FAIL")
        print("         · GEN-C 3号机组（地下车库）—— 漏油样例，待复核加油 RF-NEW-PEND")
        print("         · GEN-D 4号机组（数据机房）—— 维修封锁，不可启动、不计入可用容量")

    # 延迟导入，保证 seed/reset 不依赖 web 模块
    from .web import make_server, start_background
    from .telemetry import TelemetryHub, ReminderScheduler

    hub = TelemetryHub(db)
    app = make_server(db, hub, args.host, args.port)
    start_background(hub)

    url = f"http://{args.host}:{args.port}/"
    print(f"[genops] 平台运行中：{url}  （Ctrl+C 停止）")
    if not args.no_browser:
        try:
            webbrowser.open(url, new=2)
        except Exception:
            pass
    try:
        app.run()
    except KeyboardInterrupt:
        print("\n[genops] 正在停止…")
    finally:
        hub.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
