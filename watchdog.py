"""
极简 watchdog：每 30 秒检查 bot 是否存活，不在就拉起来。
自身占用极低（几MB内存），独立于 bot 运行。

用法:
  Linux:   nohup python3 watchdog.py &
  Windows: start /b python watchdog.py
"""
import subprocess
import sys
import time
import os
import signal
import platform

if hasattr(signal, 'SIGHUP'):
    signal.signal(signal.SIGHUP, signal.SIG_IGN)

IS_WINDOWS = platform.system() == "Windows"
PYTHON = sys.executable
WORK_DIR = os.path.dirname(os.path.abspath(__file__))
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
        if IS_WINDOWS:
            result = subprocess.run(
                ["wmic", "process", "where",
                 "commandline like '%main.py%--daemon%'",
                 "get", "processid"],
                capture_output=True, text=True,
            )
            pids = [p.strip() for p in result.stdout.split("\n") if p.strip().isdigit()]
            return len(pids) > 0
        else:
            result = subprocess.run(
                ["pgrep", "-f", "python.*main.py.*--daemon"],
                capture_output=True, text=True,
            )
            return result.returncode == 0
    except Exception:
        return False


def start_bot():
    """拉起 bot"""
    bot_log_path = os.path.join(WORK_DIR, "bot.log")
    kwargs = {}
    if not IS_WINDOWS:
        kwargs["start_new_session"] = True
    else:
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP

    with open(bot_log_path, "a") as bot_log:
        proc = subprocess.Popen(
            [PYTHON, "main.py", "--daemon"],
            cwd=WORK_DIR,
            stdout=bot_log,
            stderr=bot_log,
            stdin=subprocess.DEVNULL,
            **kwargs,
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
