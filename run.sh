#!/usr/bin/env bash
# 一条本地启动命令：重置数据库、播种（正常/启动失败/漏油/封锁样例）并启动平台。
set -e
cd "$(dirname "$0")"
PORT="${1:-8000}"
exec python3 -m genops.cli demo --port "$PORT"
