#!/bin/bash
# 守护进程：bot 挂了自动重启（单实例锁 + 信号免疫）
LOCKFILE="/tmp/trading_bot.lock"
LOGDIR="/home/user/pleasenot"
cd /home/user/pleasenot

# 防止多开
if [ -f "$LOCKFILE" ] && kill -0 "$(cat $LOCKFILE)" 2>/dev/null; then
    echo "Bot 守护进程已在运行 PID=$(cat $LOCKFILE)" >> "$LOGDIR/bot_daemon.log"
    exit 1
fi
echo $$ > "$LOCKFILE"
trap "rm -f $LOCKFILE" EXIT

# 忽略 SIGHUP（shell 断开时不被杀）
trap '' HUP

while true; do
    echo "[$(date)] Bot 启动..." >> "$LOGDIR/bot_daemon.log"
    # setsid 让 python 脱离当前 session，不受 shell 清理影响
    setsid python main.py >> "$LOGDIR/bot.log" 2>&1 &
    BOT_PID=$!
    echo "[$(date)] Bot PID=$BOT_PID" >> "$LOGDIR/bot_daemon.log"

    # 等 bot 进程结束
    wait $BOT_PID
    EXIT_CODE=$?

    echo "[$(date)] Bot 退出 code=$EXIT_CODE，5秒后重启..." >> "$LOGDIR/bot_daemon.log"
    sleep 5
done
