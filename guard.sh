#!/bin/bash
# 守护脚本：每 30 秒检查 bot 是否在跑，不在就拉起来
# 用法: nohup bash guard.sh &

cd /home/user/pleasenot

echo "[$(date)] Guard 启动" >> bot_daemon.log

while true; do
    if ! pgrep -f "python.*main.py.*--daemon" > /dev/null 2>&1; then
        echo "[$(date)] Bot 不在运行，正在拉起..." >> bot_daemon.log
        nohup python main.py --daemon >> bot_daemon_out.log 2>&1 &
        echo "[$(date)] Bot 已拉起 PID=$!" >> bot_daemon.log
    fi
    sleep 30
done
