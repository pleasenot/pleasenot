"""基于 XXYY Tag Holder 买入列表的信号源 — 跟单聪明钱/大户"""
import asyncio
from typing import Callable

from signals.base import BaseSignalSource, TradeSignal, TTLSet
from xxyy.client import scanner_client as client
from config import config
from utils.logger import get_logger

logger = get_logger(__name__)


class SmartMoneyScanner(BaseSignalSource):
    """
    轮询 XXYY /tag-holder-buy-list 接口，获取聪明钱/大户最近买入的代币。
    与 KOL 买入列表互补 — KOL 看影响力，Smart Money 看链上实力。
    """

    def __init__(
        self,
        chain: str | None = None,
        interval: int = 30,
        max_signals_per_cycle: int = 2,
        min_wallet_count: int = 1,      # 至少几个聪明钱买入
        min_market_cap: float = 10000,  # 延迟跟单：市值 ≥ $10k 才买
    ):
        self.chain = chain or config.default_chain
        self.interval = interval
        self.max_signals_per_cycle = max_signals_per_cycle
        self.min_wallet_count = min_wallet_count
        self.min_market_cap = min_market_cap
        self._seen = TTLSet(ttl=1800)

    async def start(self, on_signal: Callable[[TradeSignal], None]) -> None:
        logger.info(
            "SmartMoneyScanner started chain=%s interval=%ds",
            self.chain, self.interval,
        )
        while True:
            try:
                tokens = await client.tag_holder_buy_list(chain=self.chain)
                tokens.sort(key=lambda t: t.get("walletBuyCnt", 0), reverse=True)

                triggered = 0
                for token in tokens:
                    if triggered >= self.max_signals_per_cycle:
                        break

                    ca = (token.get("tokenMeta") or {}).get("mint", "")
                    if not ca or ca in self._seen:
                        continue

                    wallet_count = token.get("walletBuyCnt", 0)
                    if wallet_count < self.min_wallet_count:
                        continue

                    mc = float(token.get("marketCap", 0) or 0)
                    if mc < self.min_market_cap:
                        continue
                    if mc > 500_000:
                        continue

                    self._seen.add(ca)

                    symbol = (token.get("tokenMeta") or {}).get("symbol", "?")
                    holders = token.get("holder", 0)

                    wallet_names = [
                        item.get("walletName", "?")
                        for item in (token.get("walletBuyItemList") or [])[:3]
                    ]
                    names_str = ",".join(wallet_names)

                    logger.info(
                        "SmartMoney signal ca=%s symbol=%s wallets=%d tags=[%s] mc=$%.0f",
                        ca, symbol, wallet_count, names_str, mc,
                    )
                    signal = TradeSignal(
                        chain=self.chain,
                        token_address=ca,
                        action="buy",
                        source="smart_money",
                        reason=f"聪明钱买入x{wallet_count} [{names_str}] mc=${mc:.0f}",
                    )
                    if asyncio.iscoroutinefunction(on_signal):
                        await on_signal(signal)
                    else:
                        on_signal(signal)
                    triggered += 1

            except Exception as e:
                logger.error("SmartMoneyScanner error: %s", e)
            await asyncio.sleep(self.interval)
