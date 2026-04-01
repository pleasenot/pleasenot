"""基于 XXYY feed 接口扫描新币信号"""
import asyncio
from typing import Callable

from signals.base import BaseSignalSource, TradeSignal
from signals.ai_keywords import is_ai_related
from xxyy.client import client
from config import config
from utils.logger import get_logger

logger = get_logger(__name__)

# 默认安全过滤条件
DEFAULT_FILTERS = {
    "holder": "5,",         # 最少 5 个持仓人（降低门槛，让早期币进来）
    "mc": "2000,",          # 最低市值 2000 USD（从 5000 降到 2000）
    "insiderHp": ",20",     # 内部人持仓 < 20%（稍微放宽）
    "snipers": ",10",       # 狙击者数量 < 10
}


class FeedScanner(BaseSignalSource):
    """
    持续轮询 XXYY /feed/NEW 接口，每轮最多触发 max_signals_per_cycle 个买入信号。
    内置基础安全过滤，避免盲目买入垃圾币。
    """

    def __init__(
        self,
        chain: str | None = None,
        feed_type: str = "NEW",
        filters: dict | None = None,
        interval: int | None = None,
        max_signals_per_cycle: int = 1,
        ai_only: bool = True,
    ):
        self.chain = chain or config.default_chain
        self.feed_type = feed_type
        self.filters = {**DEFAULT_FILTERS, **(filters or {})}
        self.interval = interval or config.feed_interval
        self.max_signals_per_cycle = max_signals_per_cycle
        self.ai_only = ai_only
        self._seen: set[str] = set()

    def _is_safe(self, token: dict) -> tuple[bool, str]:
        """基础安全检查，返回 (是否安全, 原因)"""
        ca = token.get("tokenAddress", "")

        holders = token.get("holders", 0)
        if holders < 5:
            return False, f"持仓人太少({holders})"

        dev_hp = float(token.get("devHoldPercent", 0) or 0)
        if dev_hp > 50:
            return False, f"Dev持仓过高({dev_hp:.1f}%)"

        mc = float(token.get("marketCapUSD", 0) or 0)
        if mc < 1000:
            return False, f"市值过低(${mc:.0f})"

        return True, "ok"

    async def start(self, on_signal: Callable[[TradeSignal], None]) -> None:
        logger.info(
            "FeedScanner started chain=%s type=%s interval=%ds max_per_cycle=%d",
            self.chain, self.feed_type, self.interval, self.max_signals_per_cycle,
        )
        while True:
            try:
                tokens = await client.feed(self.feed_type, self.chain, self.filters)
                triggered = 0
                for token in tokens:
                    if triggered >= self.max_signals_per_cycle:
                        break

                    ca = token.get("tokenAddress") or token.get("ca")
                    if not ca or ca in self._seen:
                        continue
                    self._seen.add(ca)

                    safe, reason = self._is_safe(token)
                    if not safe:
                        logger.debug("skip ca=%s reason=%s", ca, reason)
                        continue

                    if self.ai_only:
                        name = token.get("name", "")
                        symbol = token.get("symbol", "")
                        desc = token.get("description", "")
                        matched, keyword = is_ai_related(name, symbol, desc)
                        if not matched:
                            logger.debug("skip non-AI ca=%s symbol=%s", ca, symbol)
                            continue
                        logger.info("AI match ca=%s symbol=%s keyword=%s", ca, symbol, keyword)

                    symbol = token.get("symbol", "?")
                    mc = token.get("marketCapUSD", 0)
                    holders = token.get("holders", 0)
                    logger.info(
                        "signal ca=%s symbol=%s mc=$%.0f holders=%d",
                        ca, symbol, float(mc or 0), holders,
                    )
                    signal = TradeSignal(
                        chain=self.chain,
                        token_address=ca,
                        action="buy",
                        source="feed_scanner",
                        reason=f"{symbol} mc=${float(mc or 0):.0f} holders={holders}",
                    )
                    if asyncio.iscoroutinefunction(on_signal):
                        await on_signal(signal)
                    else:
                        on_signal(signal)
                    triggered += 1

            except Exception as e:
                logger.error("FeedScanner error: %s", e)
            await asyncio.sleep(self.interval)
