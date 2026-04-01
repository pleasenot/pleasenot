#!/bin/bash
set -e

# ─── Solana Meme Bot 一键安装 ───────────────────────────────
# 用法: curl -sSL <release_url>/install.sh | bash
# 或:   bash install.sh

CYAN='\033[0;36m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

echo -e "${CYAN}"
echo "╔══════════════════════════════════════════════╗"
echo "║     Solana Meme Coin Trading Bot v1.0.0      ║"
echo "║     广撒网策略 · 靠一笔 10x-100x 覆盖亏损     ║"
echo "╚══════════════════════════════════════════════╝"
echo -e "${NC}"

# ── 1. 检查 Python ─────────────────────────────────────────
echo -e "${CYAN}[1/5] 检查 Python 环境...${NC}"
if command -v python3 &>/dev/null; then
    PY=python3
elif command -v python &>/dev/null; then
    PY=python
else
    echo -e "${RED}错误: 未找到 Python，请先安装 Python 3.10+${NC}"
    exit 1
fi

PY_VER=$($PY -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
echo -e "  Python: ${GREEN}$PY_VER${NC} ($($PY --version))"

# 检查版本 >= 3.10
PY_MAJOR=$($PY -c 'import sys; print(sys.version_info.major)')
PY_MINOR=$($PY -c 'import sys; print(sys.version_info.minor)')
if [ "$PY_MAJOR" -lt 3 ] || ([ "$PY_MAJOR" -eq 3 ] && [ "$PY_MINOR" -lt 10 ]); then
    echo -e "${RED}错误: 需要 Python 3.10+，当前为 $PY_VER${NC}"
    exit 1
fi

# ── 2. 安装依赖 ─────────────────────────────────────────────
echo -e "${CYAN}[2/5] 安装依赖...${NC}"
$PY -m pip install -q -r requirements.txt
echo -e "  ${GREEN}依赖安装完成${NC}"

# ── 3. 配置环境变量 ─────────────────────────────────────────
echo -e "${CYAN}[3/5] 配置 API Keys...${NC}"

if [ -f .env ]; then
    echo -e "  ${YELLOW}.env 文件已存在，跳过配置${NC}"
    echo -e "  如需修改请编辑 .env 文件"
else
    echo ""
    # XXYY API Key
    echo -e "  ${YELLOW}XXYY API Key${NC} (从 https://www.xxyy.io/apikey 获取)"
    read -rp "  输入 XXYY_API_KEY: " XXYY_KEY
    if [ -z "$XXYY_KEY" ]; then
        echo -e "${RED}错误: XXYY_API_KEY 不能为空${NC}"
        exit 1
    fi

    # 钱包地址
    echo ""
    echo -e "  ${YELLOW}Solana 钱包地址${NC} (XXYY 平台上的钱包)"
    read -rp "  输入 WALLET_ADDRESS: " WALLET

    # MiniMax (可选)
    echo ""
    echo -e "  ${YELLOW}MiniMax API Key${NC} (可选，用于 AI 智能研判，回车跳过)"
    read -rp "  输入 MINIMAX_API_KEY: " MINIMAX_KEY

    # 买入金额
    echo ""
    echo -e "  ${YELLOW}单笔买入金额${NC} (SOL，默认 0.03)"
    read -rp "  输入 BUY_AMOUNT [0.03]: " BUY_AMT
    BUY_AMT=${BUY_AMT:-0.03}

    # 写入 .env
    cat > .env << EOF
XXYY_API_KEY=${XXYY_KEY}
XXYY_API_BASE_URL=https://www.xxyy.io
DEFAULT_CHAIN=sol
WALLET_ADDRESS=${WALLET}
BUY_AMOUNT=${BUY_AMT}
ANALYZER_MIN_SCORE=55
EOF

    if [ -n "$MINIMAX_KEY" ]; then
        echo "MINIMAX_API_KEY=${MINIMAX_KEY}" >> .env
        echo "MINIMAX_MODEL=MiniMax-M2.7-highspeed" >> .env
    fi

    echo -e "  ${GREEN}.env 已生成${NC}"
fi

# ── 4. 验证 API 连通性 ──────────────────────────────────────
echo -e "${CYAN}[4/5] 验证 API 连通性...${NC}"
$PY -c "
from xxyy.client import client
import asyncio
async def t():
    r = await client.ping()
    print(f'  XXYY API: OK ({r})')
    await client.close()
asyncio.run(t())
" 2>/dev/null

if [ $? -ne 0 ]; then
    echo -e "  ${RED}XXYY API 连接失败，请检查 API Key${NC}"
    echo -e "  可以稍后手动编辑 .env 修正后再启动"
fi

# ── 5. 完成 ─────────────────────────────────────────────────
echo -e "${CYAN}[5/5] 安装完成！${NC}"
echo ""
echo -e "${GREEN}╔══════════════════════════════════════════════╗${NC}"
echo -e "${GREEN}║              安装成功！                       ║${NC}"
echo -e "${GREEN}╚══════════════════════════════════════════════╝${NC}"
echo ""
echo -e "  启动 Bot:       ${CYAN}python main.py --daemon${NC}"
echo -e "  模拟运行:       ${CYAN}python main.py --dry-run${NC}"
echo -e "  实时监控:       ${CYAN}python dashboard.py${NC}"
echo -e "  查看日志:       ${CYAN}tail -f bot_daemon_out.log${NC}"
echo ""
echo -e "  ${YELLOW}提示: 首次运行建议先用 --dry-run 观察信号质量${NC}"
echo ""
