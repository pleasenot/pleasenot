"""
Pump.fun Bonding Curve 即将毕业扫描器

策略核心：
- 在 bonding curve 完成前（85-98%）发现代币，抢在毕业进入 Raydium 之前买入
- 毕业后通常有一波流动性涌入带来的价格冲击，提前布局利润空间更大
- Pump.fun bonding curve 在市值约 $69k 时完成（100%）

数据来源：
- Pump.fun 公开前端 API（currently-live / 排序接口）
- 通过 market cap 推算 bonding curve 进度
"""
import asyncio
import time
from typing import Callable

import httpx

from signals.base import BaseSignalSource, TradeSignal
from utils.logger import get_logger

logger = get_logger(__name__)

# Pump.fun bonding curve 在约 $69k market cap 时完成（100%）
BONDING_CURVE_COMPLETION_MC = 69_000

# 进度阈值：只关注 85%-98% 的代币（即将毕业但还没毕业）
MIN_PROGRESS_PCT = 85
MAX_PROGRESS_PCT = 98

# 扫描间隔（秒）- bonding curve 完成是时间敏感的
SCAN_INTERVAL = 20

# 去重窗口（秒）
DEDUP_WINDOW = 30 * 60  # 30 分钟内不重复发信号

# Pump.fun API 端点（按优先级排列）
PUMP_ENDPOINTS = [
    "https://frontend-api-v3.pump.fun/coins/currently-live",
    "https://client-api-2-74b1891ee9f9.herokuapp.com/coins",
    "https://frontend-api-v3.pump.fun/coins",
]


class PumpFunBondingScanner(BaseSignalSource):
    """
    扫描 Pump.fun 上即将完成 bonding curve 的代币。
    在毕业前买入，捕捉进入 Raydium 后的流动性冲击收益。
    """

    def __init__(
        self,
        max_signals_per_cycle: int = 2,
        min_market_cap: float = 30_000,
        min_unique_buyers: int = 10,
        max_age_hours: float = 2.0,
        min_progress: float = MIN_PROGRESS_PCT,
        max_progress: float = MAX_PROGRESS_PCT,
    ):
        self.max_signals_per_cycle = max_signals_per_cycle
        self.min_market_cap = min_market_cap
        self.min_unique_buyers = min_unique_buyers
        self.max_age_hours = max_age_hours
        self.min_progress = min_progress
        self.max_progress = max_progress
        # 去重：token_address -> 上次信号发出的时间戳
        self._seen: dict[str, float] = {}

    async def start(self, on_signal: Callable[[TradeSignal], None]) -> None:
        logger.info(
            "PumpFunBondingScanner started progress=%.0f-%.0f%% "
            "min_mc=$%.0f min_buyers=%d max_age=%.1fh",
            self.min_progress, self.max_progress,
            self.min_market_cap, self.min_unique_buyers, self.max_age_hours,
        )
        async with httpx.AsyncClient(
            timeout=15.0,
            verify=False,
            headers={"User-Agent": "MemeBot/2.0"},
        ) as http:
            while True:
                try:
                    triggered = 0
                    coins = await self._fetch_coins(http)

                    for coin in coins:
                        if triggered >= self.max_signals_per_cycle:
                            break
                        sig = self._evaluate_coin(coin)
                        if sig:
                            if asyncio.iscoroutinefunction(on_signal):
                                await on_signal(sig)
                            else:
                                on_signal(sig)
                            triggered += 1

                    if triggered > 0:
                        logger.info("PumpFunBonding 本轮触发 %d 个信号", triggered)

                    # 清理过期的去重记录
                    self._cleanup_seen()

                except Exception as e:
                    logger.error("PumpFunBondingScanner error: %s", e)

                await asyncio.sleep(SCAN_INTERVAL)

    async def _fetch_coins(self, http: httpx.AsyncClient) -> list[dict]:
        """尝试多个 Pump.fun API 端点获取代币列表"""
        for endpoint in PUMP_ENDPOINTS:
            try:
                coins = await self._try_endpoint(http, endpoint)
                if coins:
                    logger.debug(
                        "PumpFunBonding fetched %d coins from %s",
                        len(coins), endpoint,
                    )
                    return coins
            except Exception as e:
                logger.debug("Endpoint %s failed: %s", endpoint, e)
                continue

        # 所有端点都失败时，尝试带排序参数的备用请求
        return await self._fetch_with_sort_params(http)

    async def _try_endpoint(
        self, http: httpx.AsyncClient, endpoint: str,
    ) -> list[dict]:
        """请求单个端点并返回代币列表"""
        params: dict = {}
        if "currently-live" not in endpoint:
            # 非 currently-live 端点尝试按 market cap 降序排列
            params = {
                "sort": "market_cap",
                "order": "DESC",
                "limit": "100",
                "offset": "0",
                "includeNsfw": "false",
            }

        resp = await http.get(endpoint, params=params)
        if resp.status_code != 200:
            return []

        data = resp.json()

        # API 可能返回 list 或 dict 包含 list
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            # 尝试常见的嵌套键
            for key in ("coins", "data", "results", "items"):
                if key in data and isinstance(data[key], list):
                    return data[key]
            # 如果 dict 本身看起来是分页结构
            if "total" in data or "count" in data:
                for key in data:
                    if isinstance(data[key], list):
                        return data[key]
        return []

    async def _fetch_with_sort_params(
        self, http: httpx.AsyncClient,
    ) -> list[dict]:
        """备用方案：尝试不同参数组合"""
        fallback_urls = [
            (
                "https://frontend-api-v3.pump.fun/coins/for-you",
                {"offset": "0", "limit": "50"},
            ),
            (
                "https://frontend-api-v3.pump.fun/coins",
                {"sort": "currently_live", "limit": "50", "offset": "0"},
            ),
        ]
        for url, params in fallback_urls:
            try:
                resp = await http.get(url, params=params)
                if resp.status_code == 200:
                    data = resp.json()
                    coins = data if isinstance(data, list) else []
                    if not coins and isinstance(data, dict):
                        for key in ("coins", "data", "results"):
                            if key in data and isinstance(data[key], list):
                                coins = data[key]
                                break
                    if coins:
                        logger.debug(
                            "PumpFunBonding fallback got %d coins from %s",
                            len(coins), url,
                        )
                        return coins
            except Exception as e:
                logger.debug("Fallback %s failed: %s", url, e)
        return []

    def _evaluate_coin(self, coin: dict) -> TradeSignal | None:
        """评估代币是否即将完成 bonding curve，值得买入"""
        # 提取基本信息 - Pump.fun API 字段名可能不同，兼容多种格式
        ca = (
            coin.get("mint")
            or coin.get("token_address")
            or coin.get("address")
            or coin.get("contract_address")
            or ""
        )
        if not ca:
            return None

        # 去重检查
        now = time.time()
        if ca in self._seen and (now - self._seen[ca]) < DEDUP_WINDOW:
            return None

        name = coin.get("name", "?")
        symbol = coin.get("symbol", "?")

        # === 市值与 bonding curve 进度 ===
        # Pump.fun API 可能直接提供 progress 或 bonding curve 百分比
        progress = self._get_progress(coin)
        market_cap = self._get_market_cap(coin)

        # 如果没有直接的进度字段，从市值推算
        if progress is None and market_cap and market_cap > 0:
            progress = (market_cap / BONDING_CURVE_COMPLETION_MC) * 100.0

        if progress is None:
            return None

        # 进度过滤
        if progress < self.min_progress or progress > self.max_progress:
            return None

        # 市值过滤
        if market_cap is None:
            market_cap = BONDING_CURVE_COMPLETION_MC * (progress / 100.0)
        if market_cap < self.min_market_cap:
            return None

        # === 时间过滤：只关注最近创建的代币 ===
        created_ts = (
            coin.get("created_timestamp")
            or coin.get("createdAt")
            or coin.get("created_at")
            or coin.get("timestamp")
            or 0
        )
        if created_ts:
            # 可能是毫秒或秒级时间戳
            if created_ts > 1e12:
                created_ts = created_ts / 1000.0
            age_hours = (now - created_ts) / 3600.0
            if age_hours > self.max_age_hours:
                return None
            if age_hours < 0:
                # 时间戳异常
                return None
        # 如果没有创建时间信息，仍然放行（API 可能不返回此字段）

        # === 买家数量过滤（防止 dev 自买） ===
        unique_buyers = self._get_unique_buyers(coin)
        if unique_buyers is not None and unique_buyers < self.min_unique_buyers:
            return None

        # === 通过所有过滤 -> 发出信号 ===
        self._seen[ca] = now

        age_str = ""
        if created_ts and created_ts > 0:
            age_min = (now - created_ts) / 60.0
            age_str = f" age={age_min:.0f}m"

        buyers_str = f" buyers={unique_buyers}" if unique_buyers else ""

        logger.info(
            "PumpFunBonding %s(%s) progress=%.1f%% mc=$%.0f%s%s ca=%s",
            name, symbol, progress, market_cap,
            buyers_str, age_str, ca[:12] + "...",
        )

        return TradeSignal(
            chain="sol",
            token_address=ca,
            action="buy",
            source="pumpfun_bonding",
            reason=(
                f"Bonding {progress:.1f}% {name}({symbol}) "
                f"mc=${market_cap:.0f}{buyers_str}{age_str}"
            ),
        )

    def _get_progress(self, coin: dict) -> float | None:
        """从 API 响应中提取 bonding curve 进度百分比"""
        # 尝试多种可能的字段名
        for key in (
            "progress",
            "bonding_curve_progress",
            "bondingCurveProgress",
            "curve_progress",
            "completion",
            "bonding_progress",
        ):
            val = coin.get(key)
            if val is not None:
                try:
                    pct = float(val)
                    # 有些 API 返回 0-1 范围，有些返回 0-100
                    if 0 < pct <= 1.0:
                        return pct * 100.0
                    if 0 < pct <= 100.0:
                        return pct
                except (ValueError, TypeError):
                    continue
        return None

    def _get_market_cap(self, coin: dict) -> float | None:
        """从 API 响应中提取市值"""
        for key in (
            "market_cap",
            "marketCap",
            "usd_market_cap",
            "marketCapUsd",
            "fdv",
            "mcap",
        ):
            val = coin.get(key)
            if val is not None:
                try:
                    mc = float(val)
                    if mc > 0:
                        return mc
                except (ValueError, TypeError):
                    continue
        return None

    def _get_unique_buyers(self, coin: dict) -> int | None:
        """从 API 响应中提取独立买家数量"""
        for key in (
            "unique_buyers",
            "uniqueBuyers",
            "num_holders",
            "holders",
            "holderCount",
            "holder_count",
            "reply_count",  # Pump.fun 的回复数可作为活跃度参考
        ):
            val = coin.get(key)
            if val is not None:
                try:
                    return int(val)
                except (ValueError, TypeError):
                    continue
        return None

    def _cleanup_seen(self) -> None:
        """清理过期的去重记录，避免内存无限增长"""
        now = time.time()
        expired = [
            ca for ca, ts in self._seen.items()
            if (now - ts) > DEDUP_WINDOW
        ]
        for ca in expired:
            del self._seen[ca]
