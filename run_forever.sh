#!/bin/bash
# 守护进程：bot 挂了自动重启（单实例锁）
LOCKFILE="/tmp/trading_bot.lock"
cd /home/user/pleasenot

# 防止多开
if [ -f "$LOCKFILE" ] && kill -0 "$(cat $LOCKFILE)" 2>/dev/null; then
    echo "Bot 守护进程已在运行 PID=$(cat $LOCKFILE)" >> bot_daemon.log
    exit 1
fi
echo $$ > "$LOCKFILE"
trap "rm -f $LOCKFILE" EXIT

while true; do
    echo "[$(date)] Bot 启动..." >> bot_daemon.log
    python main.py >> bot.log 2>&1
    EXIT_CODE=$?
    echo "[$(date)] Bot 退出 code=$EXIT_CODE，5秒后重启..." >> bot_daemon.log
    sleep 5
done
