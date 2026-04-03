@echo off
chcp 65001 >nul
echo ========================================
echo   Solana Meme Bot - Windows 启动器
echo ========================================
echo.

REM 检查 Python
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [错误] 未找到 Python，请安装 Python 3.10+ 并勾选 "Add to PATH"
    echo 下载: https://www.python.org/downloads/
    pause
    exit /b 1
)

REM 检查 .env
if not exist .env (
    echo [错误] 未找到 .env 文件
    echo 请复制 .env.example 为 .env 并填入你的 API Key
    pause
    exit /b 1
)

REM 安装依赖
echo [1/2] 安装依赖...
python -m pip install -q -r requirements.txt
echo.

REM 启动
echo [2/2] 启动 Bot...
echo.
echo   实盘模式: python main.py --daemon
echo   模拟模式: python main.py --dry-run
echo   监控面板: python dashboard.py
echo.

python main.py --daemon
pause
