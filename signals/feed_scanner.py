"""基于 XXYY feed 接口扫描新币信号"""
import asyncio
from typing import Callable

from signals.base import BaseSignalSource, TradeSignal, TTLSet
from signals.ai_keywords import is_ai_related
from xxyy.client import scanner_client as client
from config import config
from utils.logger import get_logger

logger = get_logger(__name__)

# ── Skill 策略：三层过滤，精准打新 ──────────────────────────

# Tier A: 新币（1-70分钟内创建，最核心的打新策略）
TIER_A_FILTERS = {
    "topHp": "22,40",       # top10 持仓 22-40%（太低=无人关注，太高=庄控）
    "snipers": ",6",        # 狙击者 < 6（少狙击者=更健康）
    "insiderHp": ",8",      # 内部人持仓 < 8%（严格）
    "holder": "10,",        # 持仓人 ≥ 10
    "mc": "8000,",          # 市值 ≥ $8k（太小的不稳定）
    "oneLink": 1,           # 至少一个社交链接（有运营意愿）
    "createTime": "1,70",   # 创建 1-70 分钟内（关键！只买新币）
    "bundleHp": ",30",      # Bundle 持仓 < 30%
    "newWalletHp": ",30",   # 新钱包持仓 < 30%
}

# Tier B: 即将毕业币（有 DexScreener 付费推广，创建 1-120 分钟）
TIER_B_FILTERS = {
    "createTime": "1,120",  # 创建 1-120 分钟内
    "dexPay": 1,            # DexScreener 付费推广
    "mc": "13000,",         # 市值 ≥ $13k
}

# Tier C: 毕业币（质量更高，门槛更高）
TIER_C_FILTERS = {
    "createTime": "1,240",  # 创建 4 小时内
    "topHp": "18,",         # top10 持仓 ≥ 18%（有大户关注）
    "holder": "300,",       # 持仓人 ≥ 300（社区基础）
    "mc": "20000,160000",   # 市值 $20k-$160k（毕业后的甜区）
}

# KOL 买入的新币（条件放宽，KOL 背书）
SMART_FILTERS = {
    "holder": "3,",
    "mc": "1000,",
    "kol": "1,",            # 至少 1 个 KOL 买入
    "insiderHp": ",25",
    "snipers": ",15",
    "createTime": "1,120",  # 2 小时内
}

# 兼容旧代码
DEFAULT_FILTERS = TIER_A_FILTERS
DEXPAID_FILTERS = TIER_B_FILTERS


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
        ai_only: bool = False,
    ):
        self.chain = chain or config.default_chain
        self.feed_type = feed_type
        self.filters = {**DEFAULT_FILTERS, **(filters or {})}
        self.interval = interval or config.feed_interval
        self.max_signals_per_cycle = max_signals_per_cycle
        self.ai_only = ai_only
        self._seen = TTLSet(ttl=1800)  # 30分钟过期

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
