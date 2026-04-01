# ⚠️ 最重要：API 密钥安全规则

**XXYY_API_KEY 绝对不能提交到 GitHub 或任何版本控制系统。**

- 用户会在对话中提供 `XXYY_API_KEY`，配置到本地环境变量即可
- 每次 `git add` / `git commit` / `git push` 前，必须确认以下文件未被包含：
  - `.env`
  - 任何包含 `XXYY_API_KEY` 或 `xxyy_ak_` 的文件
- `.env` 必须在 `.gitignore` 中

---

# 项目说明

Solana Meme Coin 自动化交易机器人。广撒网策略，靠一笔 10x-100x 覆盖所有亏损。

## 技术栈
- 运行时：Python 3.12 + asyncio（单线程协程，无真正并发竞态）
- 交易执行：XXYY Open API（Solana 为主，支持 ETH/BSC/Base）
- AI 研判：MiniMax M2.7（代币叙事分析 + 持仓深度分析 + 交易复盘）
- 信号来源：13 个并行扫描器（见下方架构）

## 环境变量
```bash
export XXYY_API_KEY=xxyy_ak_xxxx        # 从 https://www.xxyy.io/apikey 获取
export XXYY_API_BASE_URL=https://www.xxyy.io
export WALLET_ADDRESS=xxx                 # Solana 钱包地址
export BUY_AMOUNT=0.03                    # fallback 买入金额（动态仓位启用后仅作兜底）
export ANALYZER_MIN_SCORE=55              # 最低买入评分
export MINIMAX_API_KEY=sk-xxx             # MiniMax AI
export TWITTER_BEARER_TOKEN=xxx           # Twitter API（可选）
```

## 架构概览

### 信号源（signals/）
| 扫描器 | 来源 | 间隔 |
|--------|------|------|
| FeedScanner (NEW) | XXYY feed 新币 | 30s |
| FeedScanner (NEW+KOL) | XXYY feed 有KOL买入的新币 | 45s |
| FeedScanner (COMPLETED+DexPaid) | DexScreener付费推广毕业币 | 60s |
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
| TwitterScanner | 名人推文+KOL跟单 | 60s |
| SocialTrendScanner | Reddit+TikTok 趋势 | 60s |

### 交易引擎（trading/engine.py）
- 信号队列 + 3 并发分析器，swap 串行（Semaphore(1)）
- 跨信号源去重（TTL 5min）+ 信号强度追踪（多源命中加码）
- Token Analyzer 买入前全面评分（安全性+筹码+流动性+热度+AI）

### 仓位管理策略
- **动态仓位**：钱包余额 × 1%，clamp [0.01, 0.1] SOL
- **评分分档**：顶级(90+)×2, 人上人(75+)×1.5, NPC(50+)×1, 探路(40+)×0.5
- **信号强度加码**：2源×1.5, 3源×2, 4+源×3
- **最终上限**：MAX_SINGLE_BUY_SOL = 0.1 SOL

### 卖出策略（trading/position_monitor.py）
1. 分批止盈：2x→30%, 5x→20%, 10x→20%, 50x→30%, 100x→50%
2. 移动止盈：首次TP后启动，从最高点回撤30%清仓
3. 时间止损：45分钟不涨清仓
4. 动量衰退：成交量降至30%以下清仓
5. 破位止损：跌至入场价50%清仓
6. 死币清理：mc<$1k + holders<3 + vol<$30
7. AI 持仓分析：MiniMax 每30分钟分析一次（confidence>=92才执行卖出）
8. 墓地复活：已卖出的币24h内如果复活触发重新买入

### 安全护栏（trading/safety.py）
- 单日最大亏损：1.5 SOL
- 最大持仓数：20
- 最低余额保护：0.3 SOL
- 连续失败冷却：5次→2分钟
- 连续亏损冷却：5笔亏损卖出→10分钟（市场不行就停手）

### 交易复盘（trading/trade_retrospective.py）
- 每小时解析 trade_signals.log，统计退出原因/信号源/评分档位
- 每3小时 MiniMax AI 深度复盘，自动调参（confidence>=80）
- 真实 PNL API 补充准确盈亏数据

### 安全特性
- Swap 默认 model=1 防夹子模式（防 MEV 三明治攻击）
- 蜜罐检测（honeyPot 一票否决）
- 税率检查（>10% 一票否决）
- bundleHp/newWalletHp 过滤（防批量操控/sybil）

## 关键文件
- `main.py` — 入口，注册所有信号源和任务
- `xxyy/client.py` — XXYY API 客户端（带节流、缓存、健康监测）
- `trading/engine.py` — 交易引擎（信号处理、动态仓位、买入执行）
- `trading/position_monitor.py` — 仓位监控（7种卖出策略）
- `trading/token_analyzer.py` — 买入前评分（安全+筹码+AI）
- `trading/safety.py` — 安全护栏
- `trading/trade_retrospective.py` — 交易复盘+AI优化
- `signals/*.py` — 各信号源扫描器
- `config.py` — 配置（从 .env 读取）

## 注意事项
- asyncio 单线程，不存在真正的并发竞态条件
- 所有去重集合使用 TTLSet 防止内存泄漏
- positions.json 使用原子写入（os.replace）防损坏
- XXYY API 限频 1 QPS，客户端 _min_interval=3s
- 每次修改代码后需要 review：检查蜜罐/税率/内存泄漏/边界条件

---

## 开发进度与当前状态（2026-04-01 更新）

### 当前运行状态
- **Bot 状态**：daemon 模式运行中（`nohup python main.py --daemon`）
- **钱包余额**：约 3 SOL
- **主分支**：`main`（所有代码已合并）
- **目标**：广撒网策略，靠少数 10x-100x 覆盖所有亏损，实现整体盈利

### 已完成的功能（按时间顺序）
1. **基础框架搭建** — 交易引擎 + 信号系统 + 持仓监控
2. **多信号源集成** — 15 个并行扫描器全部就绪（见上方信号源表）
3. **鲸鱼跟踪 + 毕业预警** — WhaleTracker、PumpFunBondingScanner
4. **并发分析器** — 3 并发分析 + swap 串行（Semaphore(1)）
5. **动量评分 + 动态仓位** — 评分分档下注
6. **代码审查修复（第一轮）** — 日亏损限额、时间止损、去重内存泄漏、评分封顶
7. **交易复盘系统** — 每小时统计 + 每3小时 AI 深度复盘 + 自动调参
8. **AI 复盘 JSON 解析修复** — 兼容 ```json 代码块 + 增大 token 限制
9. **AI 过度卖出修复** — 基于复盘数据调优策略参数
10. **XXYY 新 API 集成** — KOL 跟单、聪明钱、热门榜、真实 PNL
11. **动态仓位策略** — 固定比例(1%) + 信号强度分级(1x/1.5x/2x/3x) + 连续亏损冷却(5笔→暂停10分钟)
12. **安全特性补充** — 防夹子(model=1)、蜜罐检测、税率检查、Feed 智能过滤(bundleHp/newWalletHp/snipers)
13. **代码审查修复（第二轮）** — 3个Bug：
    - `_calc_buy_amount()` 上限 clamp（防高分信号超限被拒）
    - Position `buy_amount` 字段（PNL 回退用实际买入额）
    - `_rejected_cache` / `_history` 容量限制（防内存无限增长）

### 已知问题 & 待优化
- Bot 盈利情况待观察，可能需要根据实际交易数据进一步调参
- 交易复盘的自动调参需要积累足够数据才能发挥作用
- 可考虑增加更多卖出策略微调（如根据不同信号源调整止盈/止损参数）

### 设计决策记录
- **不限制持仓数量**：meme 币是广撒网策略，不是价值投资，20个上限已足够宽松
- **动态仓位而非固定金额**：钱包余额 × 1%，随资金增减自动调整
- **信号强度加码**：多个信号源同时命中同一个 CA，说明热度高，加大投入
- **连续亏损冷却**：市场不好时自动暂停，避免连续亏损耗尽资金
