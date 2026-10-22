#!/bin/zsh
set -e
cd "${0:A:h}"
[[ -f requirements.txt ]] || cd ../..
if ! python3 -c 'import sys; assert sys.version_info >= (3, 11)'; then
  echo '源码运行需要 Python 3.11 或更新版本；使用 Share Monkey.app 无需安装 Python。'
  read '?按回车关闭'
  exit 1
fi
[[ -x .venv/bin/python ]] || python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
echo '安装完成，双击 run-macos.command 启动。'
read '?按回车关闭'
