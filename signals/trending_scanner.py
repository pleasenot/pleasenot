"""基于 XXYY Trending List 的信号源 — 捕捉当前最热代币"""
import asyncio
from typing import Callable

from signals.base import BaseSignalSource, TradeSignal, TTLSet
from xxyy.client import scanner_client as client
from config import config
from utils.logger import get_logger

logger = get_logger(__name__)


class TrendingScanner(BaseSignalSource):
    """
    轮询 XXYY /trending-list 接口，获取当前最热门代币。
    使用短周期（5M）捕捉正在爆发的币，带安全过滤。
    """

    def __init__(
        self,
        chain: str | None = None,
        interval: int = 60,
        period: str = "5M",
        max_signals_per_cycle: int = 2,
        min_market_cap: float = 3000,
        max_market_cap: float = 500000,   # 太大的币不是 meme 早期
        min_holders: int = 10,
        max_top_holder_pct: float = 50.0, # top10 持仓 > 50% 不买
    ):
        self.chain = chain or config.default_chain
        self.interval = interval
        self.period = period
        self.max_signals_per_cycle = max_signals_per_cycle
        self.min_market_cap = min_market_cap
        self.max_market_cap = max_market_cap
        self.min_holders = min_holders
        self.max_top_holder_pct = max_top_holder_pct
        self._seen = TTLSet(ttl=1800)

    def _is_safe(self, token: dict) -> tuple[bool, str]:
        """安全过滤"""
        mc = float(token.get("marketCapUSD", 0) or 0)
        if mc < self.min_market_cap:
            return False, f"市值太低(${mc:.0f})"
        if mc > self.max_market_cap:
            return False, f"市值太高(${mc:.0f})"

        holders = token.get("holders", 0)
        if holders < self.min_holders:
            return False, f"持仓人太少({holders})"

        # 安全检查
        security = token.get("security") or {}
        mint_auth = security.get("mintAuthority") or {}
        if mint_auth.get("value") is True:
            return False, "mint权限未撤销"

        freeze_auth = security.get("freezeAuthority") or {}
        if freeze_auth.get("value") is True:
            return False, "freeze权限未撤销"

        top_holder = security.get("topHolder") or {}
        top_pct = float(top_holder.get("value", 0) or 0)
        if top_pct > self.max_top_holder_pct:
            return False, f"top10持仓过高({top_pct:.1f}%)"

        dev_hp = float(token.get("devHoldPercent", 0) or 0)
        if dev_hp > 30:
            return False, f"dev持仓过高({dev_hp:.1f}%)"

        return True, "ok"

    async def start(self, on_signal: Callable[[TradeSignal], None]) -> None:
        logger.info(
            "TrendingScanner started chain=%s period=%s interval=%ds",
            self.chain, self.period, self.interval,
        )
        while True:
            try:
                tokens = await client.trending_list(chain=self.chain, period=self.period)

                triggered = 0
                for token in tokens:
                    if triggered >= self.max_signals_per_cycle:
                        break

                    ca = token.get("tokenAddress", "")
                    if not ca or ca in self._seen:
                        continue

                    safe, reason = self._is_safe(token)
                    if not safe:
                        logger.debug("trending skip ca=%s reason=%s", ca[:12], reason)
                        continue

                    self._seen.add(ca)

                    symbol = token.get("symbol", "?")
                    mc = float(token.get("marketCapUSD", 0) or 0)
                    holders = token.get("holders", 0)
                    volume = float(token.get("volume", 0) or 0)

                    # Skill 新增数据：smartWallets + auditInfo
                    smart_wallets = token.get("smartWallets") or {}
                    smart_count = smart_wallets.get("total", 0)
                    audit = token.get("auditInfo") or {}
                    bundle_hp = audit.get("bundleHp", 0)
                    sniper_count = audit.get("snipers", 0)

                    # 额外过滤：bundleHp 过高说明批量操控
                    if bundle_hp > 30:
                        logger.debug("trending skip ca=%s bundleHp=%d", ca[:12], bundle_hp)
                        continue
                    buy_count = token.get("buyCount", 0)
                    sell_count = token.get("sellCount", 0)

                    # 买卖比 > 1 说明买压更强
                    bs_ratio = buy_count / max(sell_count, 1)

                    logger.info(
                        "Trending signal ca=%s symbol=%s mc=$%.0f holders=%d vol=$%.0f B/S=%.1f",
                        ca, symbol, mc, holders, volume, bs_ratio,
                    )
                    signal = TradeSignal(
                        chain=self.chain,
                        token_address=ca,
                        action="buy",
                        source="trending",
                        reason=f"热门{self.period} {symbol} mc=${mc:.0f} vol=${volume:.0f} B/S={bs_ratio:.1f} SM={smart_count}",
                    )
                    if asyncio.iscoroutinefunction(on_signal):
                        await on_signal(signal)
                    else:
                        on_signal(signal)
                    triggered += 1

            except Exception as e:
                logger.error("TrendingScanner error: %s", e)
            await asyncio.sleep(self.interval)
