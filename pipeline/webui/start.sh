#!/bin/bash
# aidev dashboard launcher — kills any existing instance on the port, then
# starts fresh in the background. Run this any time the server isn't
# responding at http://127.0.0.1:8765/.
PORT="${1:-8765}"
pkill -f "webui/server.py --port $PORT" 2>/dev/null
sleep 1
cd "$(dirname "$0")"
nohup /usr/bin/python3 server.py --port "$PORT" > /tmp/aidev-webui.log 2>&1 &
disown
sleep 1
if curl -s -o /dev/null -w "%{http_code}" "http://127.0.0.1:$PORT/" | grep -q 200; then
  echo "aidev dashboard running: http://127.0.0.1:$PORT/"
else
  echo "failed to start — check /tmp/aidev-webui.log"
  exit 1
fi
