#!/usr/bin/env python3
"""
打包脚本：用 PyInstaller 构建独立可执行文件

用法: python build.py
输出: dist/solana-meme-bot/ 目录（包含所有可执行文件）
      dist/solana-meme-bot-v1.0.0-linux-x64.tar.gz（发布包）
"""
import os
import platform
import shutil
import subprocess
import sys

VERSION = "1.0.0"
PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
DIST_DIR = os.path.join(PROJECT_DIR, "dist")
BUILD_DIR = os.path.join(PROJECT_DIR, "build")
BUNDLE_NAME = "solana-meme-bot"

# 系统信息
SYSTEM = platform.system().lower()  # linux / windows / darwin
ARCH = platform.machine().lower()   # x86_64 / amd64 / arm64
if ARCH in ("x86_64", "amd64"):
    ARCH = "x64"
elif ARCH in ("aarch64", "arm64"):
    ARCH = "arm64"


def run(cmd, **kwargs):
    print(f"  > {cmd}")
    subprocess.check_call(cmd, shell=True, **kwargs)


def clean():
    print("[1/5] 清理旧构建...")
    for d in [BUILD_DIR, DIST_DIR]:
        if os.path.exists(d):
            shutil.rmtree(d)


def build_bot():
    print("[2/5] 构建 Bot 主程序...")
    run(
        f"{sys.executable} -m PyInstaller "
        f"--name meme-bot "
        f"--onefile "
        f"--noconfirm "
        f"--clean "
        f"--add-data signals{os.pathsep}signals "
        f"--add-data trading{os.pathsep}trading "
        f"--add-data xxyy{os.pathsep}xxyy "
        f"--add-data llm{os.pathsep}llm "
        f"--add-data utils{os.pathsep}utils "
        f"--add-data config.py{os.pathsep}. "
        f"--hidden-import signals.feed_scanner "
        f"--hidden-import signals.ai_trending_scanner "
        f"--hidden-import signals.twitter_scanner "
        f"--hidden-import signals.social_trend_scanner "
        f"--hidden-import signals.dexscreener_scanner "
        f"--hidden-import signals.whale_tracker "
        f"--hidden-import signals.pumpfun_scanner "
        f"--hidden-import signals.pumpfun_bonding_scanner "
        f"--hidden-import signals.geckoterm_scanner "
        f"--hidden-import signals.kol_scanner "
        f"--hidden-import signals.smart_money_scanner "
        f"--hidden-import signals.trending_scanner "
        f"--hidden-import signals.celebrity_config "
        f"--hidden-import signals.ai_keywords "
        f"--hidden-import signals.meme_keywords "
        f"--hidden-import trading.engine "
        f"--hidden-import trading.position_monitor "
        f"--hidden-import trading.token_analyzer "
        f"--hidden-import trading.safety "
        f"--hidden-import trading.strategy_report "
        f"--hidden-import trading.trade_retrospective "
        f"--hidden-import trading.holding_analyzer "
        f"--hidden-import xxyy.client "
        f"--hidden-import llm.minimax_client "
        f"--hidden-import dotenv "
        f"--hidden-import httpx "
        f"--hidden-import httpcore "
        f"--hidden-import anyio "
        f"--hidden-import sniffio "
        f"--hidden-import certifi "
        f"--hidden-import h11 "
        f"--collect-all httpx "
        f"--collect-all httpcore "
        f"main.py",
        cwd=PROJECT_DIR,
    )


def build_dashboard():
    print("[3/5] 构建 Dashboard...")
    run(
        f"{sys.executable} -m PyInstaller "
        f"--name meme-dashboard "
        f"--onefile "
        f"--noconfirm "
        f"--clean "
        f"--add-data xxyy{os.pathsep}xxyy "
        f"--add-data config.py{os.pathsep}. "
        f"--add-data utils{os.pathsep}utils "
        f"--hidden-import xxyy.client "
        f"--hidden-import dotenv "
        f"--hidden-import httpx "
        f"--hidden-import rich "
        f"--collect-all httpx "
        f"--collect-all rich "
        f"dashboard.py",
        cwd=PROJECT_DIR,
    )


def package():
    print("[4/5] 打包发布文件...")
    bundle_dir = os.path.join(DIST_DIR, BUNDLE_NAME)
    os.makedirs(bundle_dir, exist_ok=True)

    # 复制可执行文件
    ext = ".exe" if SYSTEM == "windows" else ""
    for name in [f"meme-bot{ext}", f"meme-dashboard{ext}"]:
        src = os.path.join(DIST_DIR, name)
        if os.path.exists(src):
            shutil.copy2(src, bundle_dir)

    # 复制配置模板
    env_example = os.path.join(bundle_dir, ".env.example")
    with open(env_example, "w") as f:
        f.write("""# Solana Meme Bot 配置
# 复制此文件为 .env 并填入你的 API Key

XXYY_API_KEY=xxyy_ak_你的key
XXYY_API_BASE_URL=https://www.xxyy.io
DEFAULT_CHAIN=sol
WALLET_ADDRESS=你的Solana钱包地址
BUY_AMOUNT=0.03
ANALYZER_MIN_SCORE=55

# MiniMax AI（推荐，提升评分精度）
# MINIMAX_API_KEY=sk-你的key
# MINIMAX_MODEL=MiniMax-M2.7-highspeed

# Twitter（可选）
# TWITTER_BEARER_TOKEN=你的token
""")

    # 复制启动脚本
    if SYSTEM == "windows":
        # Windows 批处理
        with open(os.path.join(bundle_dir, "启动Bot.bat"), "w", encoding="utf-8") as f:
            f.write('@echo off\nchcp 65001 >nul\necho 启动 Solana Meme Bot...\nmeme-bot.exe --daemon\npause\n')
        with open(os.path.join(bundle_dir, "模拟运行.bat"), "w", encoding="utf-8") as f:
            f.write('@echo off\nchcp 65001 >nul\necho 模拟模式（不实际下单）...\nmeme-bot.exe --dry-run\npause\n')
        with open(os.path.join(bundle_dir, "监控面板.bat"), "w", encoding="utf-8") as f:
            f.write('@echo off\nchcp 65001 >nul\nmeme-dashboard.exe\npause\n')
    else:
        # Linux/Mac shell
        for name, args, desc in [
            ("start-bot.sh", "--daemon", "启动 Bot（守护模式）"),
            ("dry-run.sh", "--dry-run", "模拟运行（不实际下单）"),
            ("dashboard.sh", "", "实时监控面板"),
        ]:
            path = os.path.join(bundle_dir, name)
            exe = "./meme-dashboard" if "dashboard" in name else f"./meme-bot {args}"
            with open(path, "w") as f:
                f.write(f'#!/bin/bash\n# {desc}\ncd "$(dirname "$0")"\n{exe}\n')
            os.chmod(path, 0o755)

    # README
    with open(os.path.join(bundle_dir, "README.txt"), "w", encoding="utf-8") as f:
        f.write("""╔══════════════════════════════════════════════╗
║     Solana Meme Coin Trading Bot v1.0.0      ║
║     广撒网策略 · 靠一笔 10x-100x 覆盖亏损     ║
╚══════════════════════════════════════════════╝

使用步骤：
1. 复制 .env.example 为 .env
2. 编辑 .env 填入你的 XXYY API Key 和钱包地址
3. 运行 Bot

Windows:
  - 双击「启动Bot.bat」开始实盘
  - 双击「模拟运行.bat」先观察信号
  - 双击「监控面板.bat」查看实时状态

Linux/Mac:
  ./start-bot.sh      # 实盘守护模式
  ./dry-run.sh         # 模拟运行
  ./dashboard.sh       # 实时监控

获取 API Key:
  XXYY:    https://www.xxyy.io/apikey
  MiniMax: https://platform.minimaxi.com/
""")

    # 压缩
    archive_name = f"{BUNDLE_NAME}-v{VERSION}-{SYSTEM}-{ARCH}"
    if SYSTEM == "windows":
        archive_path = os.path.join(DIST_DIR, f"{archive_name}.zip")
        shutil.make_archive(os.path.join(DIST_DIR, archive_name), "zip", DIST_DIR, BUNDLE_NAME)
        print(f"  打包完成: {archive_path}")
    else:
        archive_path = os.path.join(DIST_DIR, f"{archive_name}.tar.gz")
        shutil.make_archive(os.path.join(DIST_DIR, archive_name), "gztar", DIST_DIR, BUNDLE_NAME)
        print(f"  打包完成: {archive_path}")

    return archive_path


def summary(archive_path):
    print("[5/5] 构建完成！")
    print()
    size_mb = os.path.getsize(archive_path) / 1024 / 1024
    print(f"  发布包: {archive_path}")
    print(f"  大小:   {size_mb:.1f} MB")
    print(f"  平台:   {SYSTEM}-{ARCH}")
    print(f"  版本:   v{VERSION}")
    print()
    print("  上传到 GitHub Release 即可分发！")
    print(f"  https://github.com/pleasenot/pleasenot/releases/new")


def main():
    print(f"=== Solana Meme Bot v{VERSION} 构建 ({SYSTEM}-{ARCH}) ===\n")
    clean()
    build_bot()
    build_dashboard()
    archive = package()
    summary(archive)


if __name__ == "__main__":
    main()
