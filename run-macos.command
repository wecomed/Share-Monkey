#!/bin/zsh
set -e
cd "${0:A:h}"
if [[ ! -x .venv/bin/python ]]; then
  echo '请先安装 Python 3.11 或更新版本，然后执行 README 中的初始化命令。'
  read '?按回车关闭'
  exit 1
fi
exec .venv/bin/python -m sharemouse.macos
