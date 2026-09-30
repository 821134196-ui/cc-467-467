"""本地启动入口。

一条命令创建正常 / 启动失败 / 漏油样例并启动平台：

    python -m genplat demo            # 初始化 data/genplat.db（幂等）+ 起服务
    python -m genplat demo --reset    # 清空重建样例
    python -m genplat serve           # 使用既有数据库，不重置
    python -m genplat testdata        # 只建样例不启动服务（供测试/脚本）

可选环境变量：GENPLAT_HOST(默认127.0.0.1) GENPLAT_PORT(默认8000)
GENPLAT_DB(默认 data/genplat.db) GENPLAT_ATTACH(默认 attachments)
"""

import os
import sys

from . import seed as seed_mod
from .app import run
from .db import Database
from .simulator import Reminder, Simulator


def open_db(path, reset=False):
    if reset and os.path.exists(path):
        os.remove(path)
    for suffix in ("-wal", "-shm"):
        p = path + suffix
        if reset and os.path.exists(p):
            os.remove(p)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    return Database(path)


def build(reset=False, with_threads=True):
    db_path = os.environ.get("GENPLAT_DB", os.path.join("data", "genplat.db"))
    attach_dir = os.environ.get("GENPLAT_ATTACH", "attachments")
    os.makedirs(attach_dir, exist_ok=True)
    db = open_db(db_path, reset=reset)
    created = seed_mod.seed(db)
    sim = Simulator(db)
    reminder = Reminder(db)
    if with_threads:
        sim.start()
        reminder.start()
        reminder.scan()  # 启动即生成到期/低油位提醒
    return db, sim, reminder, attach_dir, created


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    cmd = argv[0] if argv else "demo"
    reset = "--reset" in argv or "--fresh" in argv

    if cmd == "testdata":
        db, *_ = build(reset=reset, with_threads=False)
        print(f"样例数据已就绪：{db.path}（正常 / 启动失败 / 漏油）")
        return 0
    if cmd not in ("demo", "serve"):
        print(__doc__)
        return 2

    db, sim, reminder, attach_dir, created = build(
        reset=reset or cmd == "demo" and "--fresh" in argv,
        with_threads=True)
    host = os.environ.get("GENPLAT_HOST", "127.0.0.1")
    port = int(os.environ.get("GENPLAT_PORT", "8000"))
    httpd, _app = run(db, host=host, port=port, attach_dir=attach_dir,
                      reminder=reminder)
    state = "已重建样例" if created else "使用既有数据"
    print("=" * 64)
    print(" 物业备用发电机试运行与燃油补给平台")
    print(f" 数据：{db.path}（{state}）  附件：{attach_dir}/")
    print(f" 模拟适配器：1.5s/拍    到期/低油位提醒：20s 扫描")
    print(f" 打开 http://{host}:{port}/   （Ctrl+C 停止）")
    print(" 身份切换：页面右上角；或请求头 X-User-Id: u-op1/u-sup/u-fuel")
    print("=" * 64)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n正在停止…")
    finally:
        httpd.server_close()
        sim.stop()
        reminder.stop()
        db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
