#!/bin/bash
# 守护进程：bot 挂了自动重启
cd /home/user/pleasenot

while true; do
    echo "[$(date)] Bot 启动..." >> bot_daemon.log
    python main.py >> bot.log 2>&1
    EXIT_CODE=$?
    echo "[$(date)] Bot 退出 code=$EXIT_CODE，5秒后重启..." >> bot_daemon.log
    sleep 5
done
