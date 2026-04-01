"""
GeckoTerminal 热门池子扫描器（免费 API，无需 API Key）

GeckoTerminal（CoinGecko 旗下）提供：
- 热门池子排行（按交易量、价格变化）
- 新上线池子
- 多链数据

与 DexScreener 互补，覆盖面更广。
"""
import asyncio
from typing import Callable

import httpx

from signals.base import BaseSignalSource, TradeSignal
from utils.logger import get_logger

logger = get_logger(__name__)

GECKO_BASE = "https://api.geckoterminal.com/api/v2"
SCAN_INTERVAL = 60  # 秒，GeckoTerminal 限流比较严


class GeckoTermScanner(BaseSignalSource):
    """
    从 GeckoTerminal 获取热门和新上线的 Solana 池子。
    """

    def __init__(
        self,
        network: str = "solana",
        max_signals_per_cycle: int = 3,
    ):
        self.network = network
        self.max_signals_per_cycle = max_signals_per_cycle
        self._seen: set[str] = set()

    async def start(self, on_signal: Callable[[TradeSignal], None]) -> None:
        logger.info("GeckoTermScanner started network=%s", self.network)
        async with httpx.AsyncClient(
            timeout=15.0,
            verify=False,
            headers={
                "Accept": "application/json",
                "User-Agent": "MemeBot/2.0",
            },
        ) as http:
            while True:
                try:
                    triggered = 0

                    # 1. 热门池子（按交易量排序）
                    trending = await self._fetch_trending(http)
                    for pool in trending:
                        if triggered >= self.max_signals_per_cycle:
                            break
                        sig = self._process_pool(pool, "gecko_trending")
                        if sig:
                            await self._emit(on_signal, sig)
                            triggered += 1

                    await asyncio.sleep(3)

                    # 2. 新上线池子
                    new_pools = await self._fetch_new_pools(http)
                    for pool in new_pools:
                        if triggered >= self.max_signals_per_cycle:
                            break
                        sig = self._process_pool(pool, "gecko_new")
                        if sig:
                            await self._emit(on_signal, sig)
                            triggered += 1

                    if triggered > 0:
                        logger.info("GeckoTerminal 本轮触发 %d 个信号", triggered)

                except Exception as e:
                    logger.error("GeckoTermScanner error: %s", e)

                await asyncio.sleep(SCAN_INTERVAL)

    async def _fetch_trending(self, http: httpx.AsyncClient) -> list[dict]:
        """获取热门池子"""
        try:
            resp = await http.get(
                f"{GECKO_BASE}/networks/{self.network}/trending_pools",
                params={"page": 1},
            )
            if resp.status_code == 200:
                data = resp.json()
                return data.get("data", [])
            return []
        except Exception as e:
            logger.debug("gecko trending error: %s", e)
            return []

    async def _fetch_new_pools(self, http: httpx.AsyncClient) -> list[dict]:
        """获取新上线池子"""
        try:
            resp = await http.get(
                f"{GECKO_BASE}/networks/{self.network}/new_pools",
                params={"page": 1},
            )
            if resp.status_code == 200:
                data = resp.json()
                return data.get("data", [])
            return []
        except Exception as e:
            logger.debug("gecko new pools error: %s", e)
            return []

    def _process_pool(self, pool: dict, source: str) -> TradeSignal | None:
        """处理 GeckoTerminal 池子数据"""
        attrs = pool.get("attributes", {})

        # 提取 base token 地址
        ca = attrs.get("base_token_address", "")
        if not ca or ca in self._seen:
            return None

        # 基础指标
        vol_24h = float(attrs.get("volume_usd", {}).get("h24", 0) or 0)
        mc = float(attrs.get("market_cap_usd") or attrs.get("fdv_usd") or 0)
        reserve = float(attrs.get("reserve_in_usd", 0) or 0)  # 池子 TVL
        price_change_1h = float(attrs.get("price_change_percentage", {}).get("h1", 0) or 0)

        # 过滤条件：太小/太冷的不要
        if reserve < 1000:
            return None
        if vol_24h < 500 and mc < 3000:
            return None

        # 交易笔数
        txns_1h = attrs.get("transactions", {}).get("h1", {})
        buys_1h = int(txns_1h.get("buys", 0) or 0)
        sells_1h = int(txns_1h.get("sells", 0) or 0)

        self._seen.add(ca)
        name = attrs.get("name", "?")

        logger.info(
            "GeckoTerm %s %s mc=$%.0f tvl=$%.0f vol24=$%.0f 1h=%+.1f%% txns=%d/%d",
            source, name, mc, reserve, vol_24h, price_change_1h, buys_1h, sells_1h,
        )
        return TradeSignal(
            chain="sol",
            token_address=ca,
            action="buy",
            source=source,
            reason=(
                f"GeckoTerm {name} mc=${mc:.0f} tvl=${reserve:.0f} "
                f"vol24=${vol_24h:.0f} 1h={price_change_1h:+.1f}%"
            ),
        )

    async def _emit(self, on_signal: Callable, signal: TradeSignal) -> None:
        if asyncio.iscoroutinefunction(on_signal):
            await on_signal(signal)
        else:
            on_signal(signal)
