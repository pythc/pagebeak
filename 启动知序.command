#!/bin/zsh
cd "$(dirname "$0")" || exit 1
./.venv/bin/python scripts/zhixu.py start
result=$?
if [ "$result" -eq 0 ]; then
  echo "知序已启动。请在浏览器打开 http://localhost:5878/start"
else
  echo "启动未完成。请查看上方提示；需要先打开 Docker Desktop。"
fi
read "?按回车关闭此窗口。"
exit "$result"
