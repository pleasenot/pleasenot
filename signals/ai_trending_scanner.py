"""基于 XXYY AI 热点列表的信号源"""
import asyncio
from typing import Callable

from signals.base import BaseSignalSource, TradeSignal, TTLSet
from xxyy.client import scanner_client as client
from config import config
from utils.logger import get_logger

logger = get_logger(__name__)


class AiTrendingScanner(BaseSignalSource):
    """
    轮询 XXYY open-ai-trending 接口，获取 AI 热点代币，
    每轮最多触发 max_signals_per_cycle 个买入信号。
    """

    def __init__(
        self,
        chain: str | None = None,
        interval: int | None = None,
        max_signals_per_cycle: int = 1,
    ):
        self.chain = chain or config.default_chain
        self.interval = interval or config.feed_interval
        self.max_signals_per_cycle = max_signals_per_cycle
        self._seen = TTLSet(ttl=1800)  # 30分钟过期

    async def start(self, on_signal: Callable[[TradeSignal], None]) -> None:
        logger.info(
            "AiTrendingScanner started chain=%s interval=%ds",
            self.chain, self.interval,
        )
        while True:
            try:
                tokens = await client.ai_trending(chain=self.chain)
                triggered = 0
                for token in tokens:
                    if triggered >= self.max_signals_per_cycle:
                        break

                    ca = token.get("tokenAddress") or token.get("ca")
                    if not ca or ca in self._seen:
                        continue
                    self._seen.add(ca)

                    symbol = token.get("symbol", "?")
                    name = token.get("name", "?")
                    mc = float(token.get("marketCapUSD", 0) or 0)
                    if mc > 500_000:
                        continue

                    logger.info(
                        "AI trending signal ca=%s name=%s symbol=%s mc=$%.0f",
                        ca, name, symbol, mc,
                    )
                    signal = TradeSignal(
                        chain=self.chain,
                        token_address=ca,
                        action="buy",
                        source="ai_trending",
                        reason=f"AI trending: {name} ({symbol}) mc=${mc:.0f}",
                    )
                    if asyncio.iscoroutinefunction(on_signal):
                        await on_signal(signal)
                    else:
                        on_signal(signal)
                    triggered += 1

            except Exception as e:
                logger.error("AiTrendingScanner error: %s", e)
            await asyncio.sleep(self.interval)
