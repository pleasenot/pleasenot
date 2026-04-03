"""
土狗交易机器人入口

用法:
    python main.py              # 启动全部信号源（feed + twitter）
    python main.py --feed-only  # 仅启动 feed 扫描
    python main.py --dry-run    # 模拟模式（不实际下单）
"""
import asyncio
import argparse

from config import config
from xxyy.client import client
from signals.feed_scanner import FeedScanner, SMART_FILTERS, TIER_A_FILTERS, TIER_B_FILTERS, TIER_C_FILTERS
from signals.ai_trending_scanner import AiTrendingScanner
from signals.twitter_scanner import TwitterScanner
from signals.social_trend_scanner import SocialTrendScanner
from signals.dexscreener_scanner import DexScreenerScanner
from signals.whale_tracker import WhaleTracker
from signals.pumpfun_scanner import PumpFunScanner
from signals.pumpfun_bonding_scanner import PumpFunBondingScanner
from signals.geckoterm_scanner import GeckoTermScanner
from signals.kol_scanner import KolBuyScanner
from signals.smart_money_scanner import SmartMoneyScanner
from signals.trending_scanner import TrendingScanner
from signals.base import TradeSignal
from trading.engine import TradingEngine, TradeRecord
from trading.strategy_report import StrategyReporter
from trading.trade_retrospective import TradeRetrospective
from utils.logger import get_logger

logger = get_logger("main")


def on_trade_result(record: TradeRecord) -> None:
    emoji = "✅" if record.status == "success" else "❌"
    logger.info(
        "%s 交易结果 txId=%s status=%s ca=%s",
        emoji, record.tx_id, record.status, record.signal.token_address,
    )


async def run(feed_only: bool = False, dry_run: bool = False) -> None:
    # 验证 API 连通性
    if not dry_run:
        pong = await client.ping()
        logger.info("XXYY API 连接正常: %s", pong)
    else:
        logger.info("[DRY-RUN] 跳过 API 连通性检查")

    engine = TradingEngine(on_result=on_trade_result)
    engine.start_consumer()  # 启动信号队列消费者（串行处理，避免 429）

    # 策略报告（测试时60秒出一份，正式运行可改为300秒）
    reporter = StrategyReporter(engine, interval=60)
    engine.reporter = reporter

    async def handle(signal: TradeSignal) -> None:
        if dry_run:
            logger.info("[DRY-RUN] 信号 action=%s ca=%s chain=%s source=%s",
                        signal.action, signal.token_address, signal.chain, signal.source)
            return
        await engine.handle_signal(signal)

    tasks = []

    # Tier A: 新币打新（1-70分钟内，核心策略）
    feed = FeedScanner(
        chain=config.default_chain, feed_type="NEW",
        filters=TIER_A_FILTERS, ai_only=False, interval=30, max_signals_per_cycle=3,
    )
    tasks.append(asyncio.create_task(feed.start(handle)))

    # KOL 买入的新币（条件放宽，KOL 背书）
    feed_kol = FeedScanner(
        chain=config.default_chain, feed_type="NEW",
        filters=SMART_FILTERS, ai_only=False, interval=45, max_signals_per_cycle=2,
    )
    tasks.append(asyncio.create_task(feed_kol.start(handle)))

    # Tier B: 即将毕业币（DexScreener 付费推广，1-120分钟内）
    feed_almost = FeedScanner(
        chain=config.default_chain, feed_type="ALMOST",
        filters=TIER_B_FILTERS, ai_only=False, interval=45, max_signals_per_cycle=2,
    )
    tasks.append(asyncio.create_task(feed_almost.start(handle)))

    # Tier C: 毕业币（高质量，300+持仓人，$20k-$160k）
    feed_completed = FeedScanner(
        chain=config.default_chain, feed_type="COMPLETED",
        filters=TIER_C_FILTERS, ai_only=False, interval=60, max_signals_per_cycle=2,
    )
    tasks.append(asyncio.create_task(feed_completed.start(handle)))

    # AI 热点信号源（提高每轮信号数）
    ai_trending = AiTrendingScanner(chain=config.default_chain, max_signals_per_cycle=2)
    tasks.append(asyncio.create_task(ai_trending.start(handle)))

    # DexScreener 热门扫描（免费，覆盖面广）
    dex_scanner = DexScreenerScanner(chain="solana", max_signals_per_cycle=3)
    tasks.append(asyncio.create_task(dex_scanner.start(handle)))

    # Pump.fun 毕业币扫描（SOL meme 主要发射台）
    pump_scanner = PumpFunScanner(max_signals_per_cycle=3)
    tasks.append(asyncio.create_task(pump_scanner.start(handle)))

    # Pump.fun Bonding Curve 即将毕业扫描（毕业前抢跑，每20秒扫一次）
    bonding_scanner = PumpFunBondingScanner(max_signals_per_cycle=2)
    tasks.append(asyncio.create_task(bonding_scanner.start(handle)))

    # GeckoTerminal 热门池子（与 DexScreener 互补）
    gecko_scanner = GeckoTermScanner(network="solana", max_signals_per_cycle=2)
    tasks.append(asyncio.create_task(gecko_scanner.start(handle)))

    # 鲸鱼钱包追踪（监控已知聪明钱的链上买入）
    whale_tracker = WhaleTracker(scan_interval=30, max_signals_per_cycle=3)
    tasks.append(asyncio.create_task(whale_tracker.start(handle)))

    # KOL 买入信号（XXYY API，跟单 KOL 大佬）
    kol_scanner = KolBuyScanner(chain=config.default_chain, interval=30, max_signals_per_cycle=2)
    tasks.append(asyncio.create_task(kol_scanner.start(handle)))

    # 聪明钱/大户买入信号（XXYY API，跟单 Smart Money）
    smart_money = SmartMoneyScanner(chain=config.default_chain, interval=30, max_signals_per_cycle=2)
    tasks.append(asyncio.create_task(smart_money.start(handle)))

    # 热门代币信号（XXYY API，5分钟热度榜）
    trending = TrendingScanner(chain=config.default_chain, interval=60, period="5M", max_signals_per_cycle=2)
    tasks.append(asyncio.create_task(trending.start(handle)))

    # 仓位监控（止盈）+ 策略报告 + 交易复盘
    if not dry_run:
        # 启动前从 positions.json 恢复持仓
        loaded = engine.position_monitor.load_positions()
        logger.info("从文件恢复了 %d 个持仓", loaded)
        tasks.append(asyncio.create_task(engine.position_monitor.start()))
        tasks.append(asyncio.create_task(reporter.start()))
        # 交易复盘（每小时分析历史交易，AI 深度分析每3小时一次）
        retrospective = TradeRetrospective(engine=engine)
        tasks.append(asyncio.create_task(retrospective.start()))

    # Meme 趋势扫描（Reddit / TikTok / 链上匹配）
    meme_scanner = SocialTrendScanner(chain=config.default_chain, feed_interval=60)
    tasks.append(asyncio.create_task(meme_scanner.start(handle)))

    # 名人推文 + KOL 跟单监控（内置名人列表 + 自定义账号）
    if not feed_only:
        extra_accounts = config.twitter_accounts if config.twitter_accounts else None
        twitter = TwitterScanner(accounts=extra_accounts, chain=config.default_chain)
        tasks.append(asyncio.create_task(twitter.start(handle)))
        logger.info("名人推文 + KOL 跟单监控已启动")

    logger.info("Bot 已启动，dry_run=%s", dry_run)

    # 用 return_exceptions=True 防止单个 task 崩溃导致整体退出
    # 加一个永久存活的 keepalive 确保 gather 永不返回
    async def _keepalive():
        while True:
            await asyncio.sleep(3600)

    tasks.append(asyncio.create_task(_keepalive()))
    results = await asyncio.gather(*tasks, return_exceptions=True)

    # 如果走到这里说明有 task 异常退出了，记录并让守护脚本重启
    for i, r in enumerate(results):
        if isinstance(r, Exception):
            logger.error("Task %d 异常退出: %s", i, r)


def main():
    parser = argparse.ArgumentParser(description="土狗交易机器人")
    parser.add_argument("--feed-only", action="store_true", help="仅使用 feed 信号源")
    parser.add_argument("--dry-run", action="store_true", help="模拟模式，不实际下单")
    parser.add_argument("--daemon", action="store_true", help="守护模式，崩溃自动重启")
    args = parser.parse_args()

    if args.daemon:
        # 守护模式：自己管理自己，无限重启
        import time
        import signal
        if hasattr(signal, 'SIGHUP'):
            signal.signal(signal.SIGHUP, signal.SIG_IGN)  # Linux: 忽略 SIGHUP

        while True:
            logger.info("[DAEMON] Bot 启动...")
            try:
                asyncio.run(run(feed_only=args.feed_only, dry_run=args.dry_run))
            except KeyboardInterrupt:
                logger.info("[DAEMON] Bot 被手动停止")
                break
            except Exception as e:
                logger.error("[DAEMON] Bot 异常退出: %s", e)
            logger.info("[DAEMON] 5秒后重启...")
            time.sleep(5)
    else:
        try:
            asyncio.run(run(feed_only=args.feed_only, dry_run=args.dry_run))
        except KeyboardInterrupt:
            logger.info("Bot 已停止")


if __name__ == "__main__":
    main()
