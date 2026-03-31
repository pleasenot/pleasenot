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
from signals.feed_scanner import FeedScanner
from signals.ai_trending_scanner import AiTrendingScanner
from signals.twitter_scanner import TwitterScanner
from signals.social_trend_scanner import SocialTrendScanner
from signals.base import TradeSignal
from trading.engine import TradingEngine, TradeRecord
from trading.strategy_report import StrategyReporter
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

    # Feed 扫描（SOL 新币，仅 AI 相关）
    feed = FeedScanner(chain=config.default_chain, feed_type="NEW", ai_only=True)
    tasks.append(asyncio.create_task(feed.start(handle)))

    # AI 热点信号源
    ai_trending = AiTrendingScanner(chain=config.default_chain)
    tasks.append(asyncio.create_task(ai_trending.start(handle)))

    # 仓位监控（止盈）+ 策略报告
    if not dry_run:
        tasks.append(asyncio.create_task(engine.position_monitor.start()))
        tasks.append(asyncio.create_task(reporter.start()))

    # Meme 趋势扫描（Reddit / TikTok / 链上匹配）
    meme_scanner = SocialTrendScanner(chain=config.default_chain)
    tasks.append(asyncio.create_task(meme_scanner.start(handle)))

    # 名人推文 + KOL 跟单监控（内置名人列表 + 自定义账号）
    if not feed_only:
        extra_accounts = config.twitter_accounts if config.twitter_accounts else None
        twitter = TwitterScanner(accounts=extra_accounts, chain=config.default_chain)
        tasks.append(asyncio.create_task(twitter.start(handle)))
        logger.info("名人推文 + KOL 跟单监控已启动")

    logger.info("Bot 已启动，dry_run=%s", dry_run)
    await asyncio.gather(*tasks)


def main():
    parser = argparse.ArgumentParser(description="土狗交易机器人")
    parser.add_argument("--feed-only", action="store_true", help="仅使用 feed 信号源")
    parser.add_argument("--dry-run", action="store_true", help="模拟模式，不实际下单")
    args = parser.parse_args()

    try:
        asyncio.run(run(feed_only=args.feed_only, dry_run=args.dry_run))
    except KeyboardInterrupt:
        logger.info("Bot 已停止")


if __name__ == "__main__":
    main()
