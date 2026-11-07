#!/bin/bash
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
if ! xcode-select -p >/dev/null 2>&1; then
  echo "请先安装 Apple Command Line Tools，再运行安装。"
  exit 1
fi
if [ ! -d "$ROOT_DIR/dist/UUDisplayAutomation.app" ]; then
  echo "请先运行 bash script/build_and_run.sh --build-only，或下载完整安装包。"
  exit 1
fi
/usr/bin/python3 "$ROOT_DIR/script/install.py"
