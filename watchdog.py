"""
极简 watchdog：每 30 秒检查 bot 是否存活，不在就拉起来。
自身占用极低（几MB内存），独立于 bot 运行。

用法: nohup python3 watchdog.py &
"""
import subprocess
import time
import os
import signal

signal.signal(signal.SIGHUP, signal.SIG_IGN)

BOT_CMD = ["python3", "main.py", "--daemon"]
WORK_DIR = "/home/user/pleasenot"
LOG = os.path.join(WORK_DIR, "bot_daemon.log")
CHECK_INTERVAL = 30


def log(msg):
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] [WATCHDOG] {msg}\n"
    with open(LOG, "a") as f:
        f.write(line)


def is_bot_running():
    """检查 bot 进程是否在跑"""
    try:
        result = subprocess.run(
            ["pgrep", "-f", "python3 main.py --daemon"],
            capture_output=True, text=True,
        )
        return result.returncode == 0
    except Exception:
        return False


def start_bot():
    """拉起 bot"""
    bot_log = open(os.path.join(WORK_DIR, "bot.log"), "a")
    proc = subprocess.Popen(
        BOT_CMD,
        cwd=WORK_DIR,
        stdout=bot_log,
        stderr=bot_log,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
    )
    log(f"Bot 已拉起 PID={proc.pid}")
    return proc.pid


if __name__ == "__main__":
    log("Watchdog 启动")

    while True:
        if not is_bot_running():
            log("Bot 不在运行，正在拉起...")
            start_bot()
        time.sleep(CHECK_INTERVAL)
