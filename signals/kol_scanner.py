"""基于 XXYY KOL 买入列表的信号源 — 跟单 KOL 大佬"""
import asyncio
from typing import Callable

from signals.base import BaseSignalSource, TradeSignal, TTLSet
from xxyy.client import scanner_client as client
from config import config
from utils.logger import get_logger

logger = get_logger(__name__)


class KolBuyScanner(BaseSignalSource):
    """
    轮询 XXYY /kol-buy-list 接口，获取 KOL 最近买入的代币。
    多个 KOL 同时买入的代币优先级更高。
    """

    def __init__(
        self,
        chain: str | None = None,
        interval: int = 30,
        max_signals_per_cycle: int = 2,
        min_kol_count: int = 1,        # 至少几个 KOL 买入才触发
        min_market_cap: float = 2000,   # 最低市值 USD
    ):
        self.chain = chain or config.default_chain
        self.interval = interval
        self.max_signals_per_cycle = max_signals_per_cycle
        self.min_kol_count = min_kol_count
        self.min_market_cap = min_market_cap
        self._seen = TTLSet(ttl=1800)  # 30分钟去重

    async def start(self, on_signal: Callable[[TradeSignal], None]) -> None:
        logger.info(
            "KolBuyScanner started chain=%s interval=%ds min_kol=%d",
            self.chain, self.interval, self.min_kol_count,
        )
        while True:
            try:
                tokens = await client.kol_buy_list(chain=self.chain)
                # 按 KOL 买入数量降序排列
                tokens.sort(key=lambda t: t.get("walletBuyCnt", 0), reverse=True)

                triggered = 0
                for token in tokens:
                    if triggered >= self.max_signals_per_cycle:
                        break

                    ca = (token.get("tokenMeta") or {}).get("mint", "")
                    if not ca or ca in self._seen:
                        continue

                    kol_count = token.get("walletBuyCnt", 0)
                    if kol_count < self.min_kol_count:
                        continue

                    mc = float(token.get("marketCap", 0) or 0)
                    if mc < self.min_market_cap:
                        continue
                    if mc > 500_000:
                        continue

                    self._seen.add(ca)

                    symbol = (token.get("tokenMeta") or {}).get("symbol", "?")
                    holders = token.get("holder", 0)

                    # 构建 KOL 名单
                    kol_names = [
                        item.get("walletName", "?")
                        for item in (token.get("walletBuyItemList") or [])[:3]
                    ]
                    kol_str = ",".join(kol_names)

                    logger.info(
                        "KOL signal ca=%s symbol=%s kol_count=%d kols=[%s] mc=$%.0f holders=%d",
                        ca, symbol, kol_count, kol_str, mc, holders,
                    )
                    signal = TradeSignal(
                        chain=self.chain,
                        token_address=ca,
                        action="buy",
                        source="kol_buy",
                        reason=f"KOL买入x{kol_count} [{kol_str}] mc=${mc:.0f}",
                    )
                    if asyncio.iscoroutinefunction(on_signal):
                        await on_signal(signal)
                    else:
                        on_signal(signal)
                    triggered += 1

            except Exception as e:
                logger.error("KolBuyScanner error: %s", e)
            await asyncio.sleep(self.interval)
