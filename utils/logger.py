import logging
import os
import sys

LOG_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bot.log")
_file_handler = None


def get_logger(name: str) -> logging.Logger:
    global _file_handler
    logger = logging.getLogger(name)
    if not logger.handlers:
        fmt = logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s - %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        # 控制台输出
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(fmt)
        logger.addHandler(handler)

        # 文件输出（追加模式，重启不丢日志）
        if _file_handler is None:
            _file_handler = logging.FileHandler(LOG_FILE, mode="a", encoding="utf-8")
            _file_handler.setFormatter(fmt)
        logger.addHandler(_file_handler)

        logger.setLevel(logging.INFO)
    return logger
