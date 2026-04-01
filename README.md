# Solana Meme Coin Trading Bot

> 广撒网策略，靠一笔 10x-100x 覆盖所有亏损

Solana 链上 Meme Coin 自动化交易机器人。15 个并行信号源 + MiniMax AI 智能研判 + 7 种卖出策略。

## 快速安装

```bash
git clone https://github.com/pleasenot/pleasenot.git
cd pleasenot
bash install.sh
```

安装脚本会引导你配置 API Key、钱包地址等。

## 用法

```bash
python main.py --daemon    # 实盘守护模式（崩溃自动重启）
python main.py --dry-run   # 模拟运行（不实际下单）
python dashboard.py        # 实时终端监控面板
```

## 核心特性

### 15 个并行信号源
| 信号源 | 来源 | 间隔 |
|--------|------|------|
| FeedScanner (NEW) | XXYY feed 新币 | 30s |
| FeedScanner (KOL) | 有 KOL 买入的新币 | 45s |
| FeedScanner (DexPaid) | DexScreener 付费推广毕业币 | 60s |
| FeedScanner (ALMOST) | 即将毕业币 | 45s |
| AiTrendingScanner | XXYY AI 热点 | 10s |
| DexScreenerScanner | DexScreener 热门 | 30s |
| PumpFunScanner | Pump.fun 毕业币 | 30s |
| PumpFunBondingScanner | Pump.fun 即将毕业 | 20s |
| GeckoTermScanner | GeckoTerminal 热门 | 30s |
| WhaleTracker | 鲸鱼钱包链上追踪 | 30s |
| KolBuyScanner | XXYY KOL 买入列表 | 30s |
| SmartMoneyScanner | XXYY 聪明钱买入列表 | 30s |
| TrendingScanner | XXYY 5分钟热度榜 | 60s |
| TwitterScanner | 名人推文 + KOL 跟单 | 60s |
| SocialTrendScanner | Reddit + TikTok 趋势 | 60s |

### Token 8 维评分系统
买入前全面打分（0-100），低于阈值不买：
1. 安全性 — 蜜罐检测、合约权限、税率
2. 筹码分布 — Dev 持仓、Top10 集中度
3. 流动性 — 池子深度
4. 市值定位 — 打新甜区 $2k-$500k
5. 交易热度 — 成交量、买卖比
6. 社交信号 — 官网、Twitter、Telegram
7. 聪明钱 — Smart Money 持仓/买入
8. AI 研判 — MiniMax M2.7 叙事分析

### 7 种卖出策略
1. **分批止盈** — 2x→30%, 5x→20%, 10x→20%, 50x→30%, 100x→50%
2. **移动止盈** — 首次 TP 后启动，最高点回撤 30% 清仓
3. **时间止损** — 45 分钟不涨清仓
4. **动量衰退** — 成交量降至 30% 以下清仓
5. **破位止损** — 跌至入场价 50% 清仓
6. **死币清理** — mc<$1k + holders<3 + vol<$30
7. **AI 持仓分析** — MiniMax 每 30 分钟分析（confidence>=92 才卖）

### 安全护栏
- 单日最大亏损：1.5 SOL
- 最大持仓数：20
- 最低余额保护：0.3 SOL
- 连续失败冷却：5 次 → 2 分钟
- 连续亏损冷却：5 笔 → 10 分钟
- 单笔上限：0.1 SOL

### 动态仓位
- 基础：钱包余额 × 1%，clamp [0.01, 0.1] SOL
- 评分加码：顶级(90+)×2, 人上人(75+)×1.5, NPC(50+)×1
- 信号强度：2 源×1.5, 3 源×2, 4+ 源×3

## 环境要求

- Python 3.10+
- [XXYY API Key](https://www.xxyy.io/apikey)（必需）
- Solana 钱包（XXYY 平台）
- [MiniMax API Key](https://platform.minimaxi.com/)（推荐，AI 研判）

## 环境变量

```bash
XXYY_API_KEY=xxyy_ak_xxxx        # XXYY API（必需）
WALLET_ADDRESS=xxx                # Solana 钱包地址
BUY_AMOUNT=0.03                   # 默认买入金额
ANALYZER_MIN_SCORE=55             # 最低买入评分
MINIMAX_API_KEY=sk-xxx            # MiniMax AI（推荐）
MINIMAX_MODEL=MiniMax-M2.7-highspeed
TWITTER_BEARER_TOKEN=xxx          # Twitter API（可选）
```

## 项目结构

```
main.py                 # 入口，注册所有信号源
dashboard.py            # 实时终端监控面板
install.sh              # 一键安装脚本
config.py               # 配置（从 .env 读取）
xxyy/client.py          # XXYY API 客户端
trading/engine.py       # 交易引擎
trading/position_monitor.py  # 仓位监控（7种卖出策略）
trading/token_analyzer.py    # 买入前评分
trading/safety.py            # 安全护栏
trading/trade_retrospective.py # 交易复盘 + AI 优化
signals/*.py            # 15个信号源扫描器
llm/minimax_client.py   # MiniMax AI 客户端
```

## License

MIT
