#!/bin/zsh

PROJECT_DIR="/Users/kunge/workspace/PanWatch"

command -v conda >/dev/null 2>&1 && conda deactivate 2>/dev/null
[ -x /opt/homebrew/bin/brew ] && eval "$(/opt/homebrew/bin/brew shellenv)"
[ -x /usr/local/bin/brew ] && eval "$(/usr/local/bin/brew shellenv)"

cd "$PROJECT_DIR" || { echo "找不到项目目录：$PROJECT_DIR"; read -k 1; exit 1; }

# 清理上次遗留的前后端进程
pkill -f "PanWatch/frontend/node_modules" 2>/dev/null
lsof -ti:8000 | xargs kill 2>/dev/null
sleep 1

cleanup() {
  echo "\n正在停止 PanWatch..."
  [ -n "$BACKEND_PID" ] && kill $BACKEND_PID 2>/dev/null
  pkill -f "PanWatch/frontend/node_modules" 2>/dev/null
  exit 0
}
trap cleanup INT TERM HUP EXIT

echo "▶ 启动后端..."
source venv/bin/activate
python server.py &
BACKEND_PID=$!

echo "▶ 启动前端..."
cd frontend
( sleep 6 && open "http://localhost:5183" ) &
pnpm dev --port 5183 --strictPort
