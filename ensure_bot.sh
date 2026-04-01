#!/bin/bash
# crontab 每分钟执行：确保 bot 在跑，不在就拉起来
if ! pgrep -f "python.*main.py.*--daemon" > /dev/null 2>&1; then
    cd /home/user/pleasenot
    nohup python main.py --daemon >> bot_daemon_out.log 2>&1 &
    echo "[$(date)] Bot 被拉起 PID=$!" >> bot_daemon.log
fi
