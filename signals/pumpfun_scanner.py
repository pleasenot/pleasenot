"""
Pump.fun 新币扫描器

Pump.fun 是 Solana 上最大的 meme coin 发射台，80%+ 的热门土狗从这里诞生。
通过 DexScreener API 间接获取 Pump.fun 上线到 Raydium 的代币。

策略：
- 扫描刚从 Pump.fun 毕业（bonding curve 完成）进入 Raydium 的代币
- 这些币有初始社区支持，比随机新币质量高
- 关注 bonding curve 完成后短时间内的爆发期
"""
import asyncio
import time
from typing import Callable

import httpx

from signals.base import BaseSignalSource, TradeSignal
from utils.logger import get_logger

logger = get_logger(__name__)

# Pump.fun 毕业币通过 DexScreener 的 pair 数据识别
# dexId = "raydium" 且 labels 包含 "pump.fun" 或 pairCreatedAt 很新
DEXS_BASE = "https://api.dexscreener.com"

# 扫描间隔
SCAN_INTERVAL = 30  # 秒


class PumpFunScanner(BaseSignalSource):
    """
    扫描 Pump.fun 毕业币（已进入 Raydium 的代币）。
    通过 DexScreener pairs API 获取最新创建的 Solana 交易对。
    """

    def __init__(
        self,
        max_signals_per_cycle: int = 3,
        min_liquidity: float = 1000,
        min_volume_5m: float = 500,
        max_age_minutes: int = 30,
    ):
        self.max_signals_per_cycle = max_signals_per_cycle
        self.min_liquidity = min_liquidity
        self.min_volume_5m = min_volume_5m
        self.max_age_minutes = max_age_minutes
        self._seen: set[str] = set()

    async def start(self, on_signal: Callable[[TradeSignal], None]) -> None:
        logger.info(
            "PumpFunScanner started min_liq=$%.0f min_vol5m=$%.0f max_age=%dm",
            self.min_liquidity, self.min_volume_5m, self.max_age_minutes,
        )
        async with httpx.AsyncClient(
            timeout=15.0,
            verify=False,
            headers={"User-Agent": "MemeBot/2.0"},
        ) as http:
            while True:
                try:
                    triggered = 0
                    pairs = await self._fetch_new_pairs(http)

                    for pair in pairs:
                        if triggered >= self.max_signals_per_cycle:
                            break
                        sig = self._evaluate_pair(pair)
                        if sig:
                            if asyncio.iscoroutinefunction(on_signal):
                                await on_signal(sig)
                            else:
                                on_signal(sig)
                            triggered += 1

                    if triggered > 0:
                        logger.info("PumpFun 本轮触发 %d 个信号", triggered)

                except Exception as e:
                    logger.error("PumpFunScanner error: %s", e)

                await asyncio.sleep(SCAN_INTERVAL)

    async def _fetch_new_pairs(self, http: httpx.AsyncClient) -> list[dict]:
        """通过搜索 pump.fun 相关新币获取数据"""
        all_pairs = []

        # 方法1：搜索最近的 Solana pairs
        try:
            resp = await http.get(
                f"{DEXS_BASE}/latest/dex/search",
                params={"q": "pump"},
            )
            if resp.status_code == 200:
                data = resp.json()
                pairs = data.get("pairs", [])
                sol_pairs = [
                    p for p in pairs
                    if p.get("chainId") == "solana"
                    and p.get("dexId") in ("raydium", "orca", "pump")
                ]
                all_pairs.extend(sol_pairs)
        except Exception as e:
            logger.debug("pump search error: %s", e)

        await asyncio.sleep(2)

        # 方法2：搜索常见 meme 关键词的新币
        for keyword in ["sol", "dog", "cat", "ai"]:
            try:
                resp = await http.get(
                    f"{DEXS_BASE}/latest/dex/search",
                    params={"q": keyword},
                )
                if resp.status_code == 200:
                    data = resp.json()
                    pairs = data.get("pairs", [])
                    sol_pairs = [
                        p for p in pairs
                        if p.get("chainId") == "solana"
                        and self._is_new_pair(p)
                    ]
                    all_pairs.extend(sol_pairs)
            except Exception as e:
                logger.debug("keyword search error q=%s: %s", keyword, e)
            await asyncio.sleep(1)

        # 去重
        seen_addrs = set()
        unique = []
        for p in all_pairs:
            addr = p.get("baseToken", {}).get("address", "")
            if addr and addr not in seen_addrs:
                seen_addrs.add(addr)
                unique.append(p)

        return unique

    def _is_new_pair(self, pair: dict) -> bool:
        """判断是否是新创建的交易对"""
        created = pair.get("pairCreatedAt", 0)
        if not created:
            return False
        # pairCreatedAt 是毫秒时间戳
        age_minutes = (time.time() * 1000 - created) / 60000
        return age_minutes <= self.max_age_minutes

    def _evaluate_pair(self, pair: dict) -> TradeSignal | None:
        """评估交易对是否值得买入"""
        base = pair.get("baseToken", {})
        ca = base.get("address", "")

        if not ca or ca in self._seen:
            return None

        # 流动性检查
        liq = float(pair.get("liquidity", {}).get("usd", 0) or 0)
        if liq < self.min_liquidity:
            return None

        # 交易量检查（5分钟 或 1小时）
        vol_5m = float(pair.get("volume", {}).get("m5", 0) or 0)
        vol_1h = float(pair.get("volume", {}).get("h1", 0) or 0)
        if vol_5m < self.min_volume_5m and vol_1h < self.min_volume_5m * 5:
            return None

        # 价格变化（正在涨的更好）
        price_5m = float(pair.get("priceChange", {}).get("m5", 0) or 0)
        price_1h = float(pair.get("priceChange", {}).get("h1", 0) or 0)

        # 市值
        mc = float(pair.get("marketCap", 0) or pair.get("fdv", 0) or 0)

        # 交易笔数（活跃度指标）
        txns_5m = pair.get("txns", {}).get("m5", {})
        buys_5m = int(txns_5m.get("buys", 0) or 0)
        sells_5m = int(txns_5m.get("sells", 0) or 0)
        total_txns = buys_5m + sells_5m

        # 至少要有一些交易
        if total_txns < 3:
            return None

        # 买压比卖压大 = 好信号
        buy_ratio = buys_5m / max(buys_5m + sells_5m, 1)

        self._seen.add(ca)
        symbol = base.get("symbol", "?")
        name = base.get("name", "?")

        source = "pump_graduate" if self._is_new_pair(pair) else "pump_search"

        logger.info(
            "🚀 PumpFun %s %s(%s) mc=$%.0f liq=$%.0f vol5m=$%.0f "
            "5m=%+.1f%% buy_ratio=%.0f%% txns=%d",
            source, name, symbol, mc, liq, vol_5m,
            price_5m, buy_ratio * 100, total_txns,
        )
        return TradeSignal(
            chain="sol",
            token_address=ca,
            action="buy",
            source=source,
            reason=(
                f"PumpFun {name}({symbol}) mc=${mc:.0f} liq=${liq:.0f} "
                f"vol5m=${vol_5m:.0f} 5m={price_5m:+.1f}% buy={buy_ratio:.0%}"
            ),
        )
