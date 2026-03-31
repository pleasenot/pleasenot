"""基于 XXYY feed 接口扫描新币信号"""
import asyncio
from typing import Callable

from signals.base import BaseSignalSource, TradeSignal
from xxyy.client import client
from config import config
from utils.logger import get_logger

logger = get_logger(__name__)


class FeedScanner(BaseSignalSource):
    """
    持续轮询 XXYY /feed/NEW 接口，发现符合条件的新币时触发买入信号。
    默认过滤条件可在 config 或 filters 参数中调整。
    """

    def __init__(
        self,
        chain: str | None = None,
        feed_type: str = "NEW",
        filters: dict | None = None,
        interval: int | None = None,
    ):
        self.chain = chain or config.default_chain
        self.feed_type = feed_type
        self.filters = filters or {}
        self.interval = interval or config.feed_interval
        self._seen: set[str] = set()

    async def start(self, on_signal: Callable[[TradeSignal], None]) -> None:
        logger.info("FeedScanner started chain=%s type=%s interval=%ds", self.chain, self.feed_type, self.interval)
        while True:
            try:
                tokens = await client.feed(self.feed_type, self.chain, self.filters)
                for token in tokens:
                    ca = token.get("tokenAddress") or token.get("ca")
                    if not ca or ca in self._seen:
                        continue
                    self._seen.add(ca)
                    logger.info("new token detected ca=%s chain=%s", ca, self.chain)
                    signal = TradeSignal(
                        chain=self.chain,
                        token_address=ca,
                        action="buy",
                        source="feed_scanner",
                        reason=f"feed/{self.feed_type} 新发现代币",
                    )
                    await asyncio.coroutine(on_signal)(signal) if asyncio.iscoroutinefunction(on_signal) else on_signal(signal)
            except Exception as e:
                logger.error("FeedScanner error: %s", e)
            await asyncio.sleep(self.interval)
