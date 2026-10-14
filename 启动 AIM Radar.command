#!/bin/bash
cd "$(dirname "$0")" || exit 1
if ! command -v python3 >/dev/null 2>&1; then
  echo "未找到 Python 3。请安装 Python 3.9 或更新版本后重试。"
  read -r -p "按回车退出…"
  exit 1
fi
python3 launch.py
read -r -p "服务已停止。按回车关闭窗口…"

