"""
DexScreener 热门代币扫描器（免费 API，无需 API Key）

数据源：
1. 涨幅榜 — 24h/6h/1h 涨幅最大的代币
2. 热度榜 — 最近交易最活跃的代币（按交易笔数）
3. 新上线池子 — 刚创建的交易对

这是最重要的信号源之一：DexScreener 覆盖全链，数据实时，完全免费。
"""
import asyncio
import time
from typing import Callable

import httpx

from signals.base import BaseSignalSource, TradeSignal, TTLSet
from utils.logger import get_logger

logger = get_logger(__name__)

# DexScreener 免费 API 端点
DEXS_BASE = "https://api.dexscreener.com"

# 扫描间隔
SCAN_INTERVAL = 45  # 秒，太快会被限流


class DexScreenerScanner(BaseSignalSource):
    """
    从 DexScreener 抓取热门代币：
    - Boosted tokens（推广/热门）
    - Token profiles（新上线）
    - 链上搜索高涨幅代币
    """

    def __init__(
        self,
        chain: str = "solana",
        max_signals_per_cycle: int = 3,
    ):
        self.chain = chain
        self.max_signals_per_cycle = max_signals_per_cycle
        self._seen = TTLSet(ttl=1800)  # 30分钟过期
        self._last_scan: dict[str, float] = {}

    async def start(self, on_signal: Callable[[TradeSignal], None]) -> None:
        logger.info("DexScreenerScanner started chain=%s", self.chain)
        async with httpx.AsyncClient(
            timeout=15.0,
            verify=False,
            headers={"User-Agent": "MemeBot/2.0"},
        ) as http:
            while True:
                try:
                    triggered = 0

                    # 1. Boosted tokens（被推广的热门币）
                    boosted = await self._fetch_boosted(http)
                    for token in boosted:
                        if triggered >= self.max_signals_per_cycle:
                            break
                        sig = self._process_token(token, "dex_boosted")
                        if sig:
                            await self._emit(on_signal, sig)
                            triggered += 1

                    # 2. 最新 token profiles
                    await asyncio.sleep(2)
                    profiles = await self._fetch_profiles(http)
                    for token in profiles:
                        if triggered >= self.max_signals_per_cycle:
                            break
                        sig = self._process_token(token, "dex_new_profile")
                        if sig:
                            await self._emit(on_signal, sig)
                            triggered += 1

                    # 3. 搜索当前热门关键词
                    await asyncio.sleep(2)
                    for query in ["pump", "meme", "ai", "trump", "pepe"]:
                        if triggered >= self.max_signals_per_cycle:
                            break
                        results = await self._search_tokens(http, query)
                        for token in results[:3]:
                            if triggered >= self.max_signals_per_cycle:
                                break
                            sig = self._process_search_result(token, f"dex_search/{query}")
                            if sig:
                                await self._emit(on_signal, sig)
                                triggered += 1
                        await asyncio.sleep(1)

                    if triggered > 0:
                        logger.info("DexScreener 本轮触发 %d 个信号", triggered)

                except Exception as e:
                    logger.error("DexScreenerScanner error: %s", e)

                await asyncio.sleep(SCAN_INTERVAL)

    async def _fetch_boosted(self, http: httpx.AsyncClient) -> list[dict]:
        """获取被推广的热门代币"""
        try:
            resp = await http.get(f"{DEXS_BASE}/token-boosts/top/v1")
            if resp.status_code == 200:
                data = resp.json()
                if isinstance(data, list):
                    return [t for t in data if t.get("chainId") == self.chain]
            return []
        except Exception as e:
            logger.debug("fetch boosted error: %s", e)
            return []

    async def _fetch_profiles(self, http: httpx.AsyncClient) -> list[dict]:
        """获取最新 token profiles"""
        try:
            resp = await http.get(f"{DEXS_BASE}/token-profiles/latest/v1")
            if resp.status_code == 200:
                data = resp.json()
                if isinstance(data, list):
                    return [t for t in data if t.get("chainId") == self.chain]
            return []
        except Exception as e:
            logger.debug("fetch profiles error: %s", e)
            return []

    async def _search_tokens(self, http: httpx.AsyncClient, query: str) -> list[dict]:
        """搜索代币"""
        try:
            resp = await http.get(
                f"{DEXS_BASE}/latest/dex/search",
                params={"q": query},
            )
            if resp.status_code == 200:
                data = resp.json()
                pairs = data.get("pairs", [])
                # 只要 Solana 的，按流动性排序
                sol_pairs = [p for p in pairs if p.get("chainId") == self.chain]
                return sol_pairs
            return []
        except Exception as e:
            logger.debug("search error q=%s: %s", query, e)
            return []

    def _process_token(self, token: dict, source: str) -> TradeSignal | None:
        """处理 boosted/profile 类型的数据"""
        ca = token.get("tokenAddress", "")
        if not ca or ca in self._seen:
            return None
        self._seen.add(ca)

        desc = token.get("description", "")
        url = token.get("url", "")

        logger.info("DexScreener %s ca=%s", source, ca[:16])
        return TradeSignal(
            chain="sol",
            token_address=ca,
            action="buy",
            source=source,
            reason=f"DexScreener {source}: {desc[:60] if desc else ca[:16]}",
        )

    def _process_search_result(self, pair: dict, source: str) -> TradeSignal | None:
        """处理搜索结果（pair 格式）"""
        base = pair.get("baseToken", {})
        ca = base.get("address", "")
        if not ca or ca in self._seen:
            return None

        # 基础过滤
        liq = float(pair.get("liquidity", {}).get("usd", 0) or 0)
        mc = float(pair.get("marketCap", 0) or pair.get("fdv", 0) or 0)
        vol_24h = float(pair.get("volume", {}).get("h24", 0) or 0)
        price_change_1h = float(pair.get("priceChange", {}).get("h1", 0) or 0)

        # 太小的跳过
        if liq < 1000 or mc < 2000:
            return None
        # 打新策略：太大的不碰
        if mc > 500_000:
            return None
        # 要有一定活跃度
        if vol_24h < 500:
            return None

        self._seen.add(ca)
        symbol = base.get("symbol", "?")
        name = base.get("name", "?")

        logger.info(
            "DexScreener %s %s(%s) mc=$%.0f liq=$%.0f vol24=$%.0f 1h=%+.1f%%",
            source, name, symbol, mc, liq, vol_24h, price_change_1h,
        )
        return TradeSignal(
            chain="sol",
            token_address=ca,
            action="buy",
            source=source,
            reason=f"{name}({symbol}) mc=${mc:.0f} liq=${liq:.0f} 1h={price_change_1h:+.1f}%",
        )

    async def _emit(self, on_signal: Callable, signal: TradeSignal) -> None:
        if asyncio.iscoroutinefunction(on_signal):
            await on_signal(signal)
        else:
            on_signal(signal)
