"""
Twitter/X 名人推文监控

监控策略：
1. 定期轮询名人账号最新推文
2. 推文命中触发关键词 → 提取合约地址 → 直接触发买入信号
3. 推文命中触发关键词但没有 CA → 记录日志，人工关注
4. S 级名人（Musk/Trump）的推文优先处理

数据源优先级：
- XXYY KOL 接口（如有）
- Twitter API v2（需自行配置 TWITTER_BEARER_TOKEN）
- 备用：第三方推文流服务
"""
import asyncio
import re
from typing import Callable

from signals.base import BaseSignalSource, TradeSignal
from signals.celebrity_config import (
    CELEBRITY_ACCOUNTS, CELEBRITY_HANDLES, S_TIER,
    CRYPTO_TRIGGER_WORDS,
)
from xxyy.client import client
from config import config
from utils.logger import get_logger

logger = get_logger(__name__)

# 从推文中提取 Solana/EVM 合约地址的正则
SOL_CA_RE = re.compile(r"\b[1-9A-HJ-NP-Za-km-z]{32,44}\b")
EVM_CA_RE = re.compile(r"\b0x[0-9a-fA-F]{40}\b")

# 触发词正则（预编译）
_trigger_patterns = [
    re.compile(rf"\b{re.escape(w)}\b", re.IGNORECASE)
    for w in CRYPTO_TRIGGER_WORDS
]


def extract_ca(text: str, chain: str) -> str | None:
    """从文本中提取合约地址"""
    if chain == "sol":
        matches = SOL_CA_RE.findall(text)
    else:
        matches = EVM_CA_RE.findall(text)
    return matches[0] if matches else None


def has_trigger_word(text: str) -> tuple[bool, str]:
    """检查推文是否包含触发关键词"""
    for pattern in _trigger_patterns:
        if pattern.search(text):
            return True, pattern.pattern
    return False, ""


def get_tier(handle: str) -> str:
    """获取账号等级"""
    for h, tier, _ in CELEBRITY_ACCOUNTS:
        if h.lower() == handle.lower():
            return tier
    return "C"


class TwitterScanner(BaseSignalSource):
    """
    监控名人 Twitter 账号推文，提取合约地址触发买入信号。

    支持两种数据源：
    1. XXYY KOL 买入列表（通过 API 获取 KOL 钱包动态）
    2. Twitter API（需配置 TWITTER_BEARER_TOKEN）

    当前实现：轮询 XXYY KOL 接口 + 预留 Twitter API 接入点
    """

    def __init__(
        self,
        accounts: list[str] | None = None,
        chain: str = "sol",
        interval: int = 15,
    ):
        self.accounts = accounts or CELEBRITY_HANDLES
        self.chain = chain
        self.interval = interval
        self._seen_tweets: set[str] = set()
        self._seen_kol_txs: set[str] = set()

    async def start(self, on_signal: Callable[[TradeSignal], None]) -> None:
        logger.info(
            "TwitterScanner started, watching %d accounts, chain=%s",
            len(self.accounts), self.chain,
        )
        logger.info("S-tier accounts: %s", S_TIER)

        # 并行运行 KOL 监控和 Twitter API 监控
        tasks = [
            asyncio.create_task(self._poll_kol_buys(on_signal)),
            asyncio.create_task(self._poll_twitter_api(on_signal)),
        ]
        await asyncio.gather(*tasks)

    # ── XXYY KOL 买入跟单 ──────────────────────────────────

    async def _poll_kol_buys(self, on_signal: Callable) -> None:
        """轮询 XXYY KOL 买入列表，跟单名人钱包"""
        logger.info("KOL buy tracker started")
        while True:
            try:
                buys = await client.kol_buys(chain=self.chain)
                for item in buys:
                    tx_id = item.get("txId") or item.get("signature", "")
                    if not tx_id or tx_id in self._seen_kol_txs:
                        continue
                    self._seen_kol_txs.add(tx_id)

                    ca = item.get("tokenAddress") or item.get("ca", "")
                    kol_name = item.get("kolName") or item.get("walletName", "unknown")
                    symbol = item.get("symbol", "?")

                    if not ca:
                        continue

                    logger.info(
                        "KOL buy detected kol=%s ca=%s symbol=%s",
                        kol_name, ca, symbol,
                    )
                    signal = TradeSignal(
                        chain=self.chain,
                        token_address=ca,
                        action="buy",
                        source=f"kol/{kol_name}",
                        reason=f"KOL {kol_name} bought {symbol}",
                    )
                    if asyncio.iscoroutinefunction(on_signal):
                        await on_signal(signal)
                    else:
                        on_signal(signal)
            except Exception as e:
                logger.error("KOL buy poll error: %s", e)
            await asyncio.sleep(self.interval)

    # ── Twitter API 推文监控 ────────────────────────────────

    async def _poll_twitter_api(self, on_signal: Callable) -> None:
        """
        通过 Twitter API 监控名人推文。
        需要配置 TWITTER_BEARER_TOKEN 环境变量。
        未配置时仅打印提示，不影响其他功能。
        """
        bearer_token = config.twitter_bearer_token
        if not bearer_token:
            logger.warning(
                "未配置 TWITTER_BEARER_TOKEN，Twitter 推文监控未启用。"
                "KOL 跟单仍正常运行。"
            )
            return

        logger.info("Twitter API monitor started for %d accounts", len(self.accounts))

        try:
            import httpx
        except ImportError:
            logger.error("需要 httpx 库来使用 Twitter API")
            return

        async with httpx.AsyncClient(
            headers={"Authorization": f"Bearer {bearer_token}"},
            timeout=30.0,
        ) as http:
            while True:
                for handle in self.accounts:
                    try:
                        await self._check_account_tweets(http, handle, on_signal)
                    except Exception as e:
                        logger.error("twitter poll error @%s: %s", handle, e)
                await asyncio.sleep(self.interval)

    async def _check_account_tweets(
        self, http, handle: str, on_signal: Callable
    ) -> None:
        """检查单个账号的最新推文"""
        # Twitter API v2: 先获取 user_id，再获取最新推文
        # 简化实现：用 search/recent 搜索 from:handle
        resp = await http.get(
            "https://api.twitter.com/2/tweets/search/recent",
            params={
                "query": f"from:{handle}",
                "max_results": 10,
                "tweet.fields": "created_at,text",
            },
        )
        if resp.status_code != 200:
            return

        data = resp.json()
        tweets = data.get("data", [])

        for tweet in tweets:
            tweet_id = tweet.get("id", "")
            if tweet_id in self._seen_tweets:
                continue
            self._seen_tweets.add(tweet_id)

            text = tweet.get("text", "")
            tier = get_tier(handle)

            # 检查是否包含触发词
            triggered, trigger = has_trigger_word(text)
            if not triggered and tier != "S":
                # 非 S 级且没有触发词，跳过
                continue

            # S 级名人的所有推文都记录
            if tier == "S":
                logger.info("S-TIER tweet @%s: %s", handle, text[:120])

            # 尝试提取合约地址
            ca = extract_ca(text, self.chain)
            if ca:
                logger.info(
                    "tweet signal @%s [%s] ca=%s trigger=%s",
                    handle, tier, ca, trigger,
                )
                signal = TradeSignal(
                    chain=self.chain,
                    token_address=ca,
                    action="buy",
                    source=f"twitter/@{handle}",
                    reason=f"[@{handle}][{tier}] {text[:100]}",
                )
                if asyncio.iscoroutinefunction(on_signal):
                    await on_signal(signal)
                else:
                    on_signal(signal)
            else:
                # 有触发词但没 CA，记录供人工关注
                logger.info(
                    "tweet mention (no CA) @%s [%s]: %s",
                    handle, tier, text[:120],
                )

    async def process_tweet(self, tweet_text: str, author: str, on_signal: Callable) -> None:
        """手动喂入推文，提取 CA 并触发信号（用于测试或外部数据源）"""
        ca = extract_ca(tweet_text, self.chain)
        if not ca:
            return
        tier = get_tier(author)
        logger.info("manual tweet signal from @%s [%s] ca=%s", author, tier, ca)
        signal = TradeSignal(
            chain=self.chain,
            token_address=ca,
            action="buy",
            source=f"twitter/@{author}",
            reason=f"[@{author}][{tier}] {tweet_text[:100]}",
        )
        if asyncio.iscoroutinefunction(on_signal):
            await on_signal(signal)
        else:
            on_signal(signal)
