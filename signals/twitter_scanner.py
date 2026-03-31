"""
Twitter/X 信号监控（占位模块）

当前实现：通过 XXYY 的 tweet-scan 能力（若 API 支持）或
外部 Twitter API 获取推文，解析合约地址后触发信号。

TODO: 接入具体的 Twitter 数据源后完善此模块。
目前支持手动传入合约地址触发信号，用于测试。
"""
import asyncio
import re
from typing import Callable

from signals.base import BaseSignalSource, TradeSignal
from utils.logger import get_logger

logger = get_logger(__name__)

# 从推文中提取 Solana/EVM 合约地址的正则
SOL_CA_RE = re.compile(r"\b[1-9A-HJ-NP-Za-km-z]{32,44}\b")
EVM_CA_RE = re.compile(r"\b0x[0-9a-fA-F]{40}\b")


def extract_ca(text: str, chain: str) -> str | None:
    """从文本中提取合约地址"""
    if chain == "sol":
        matches = SOL_CA_RE.findall(text)
    else:
        matches = EVM_CA_RE.findall(text)
    return matches[0] if matches else None


class TwitterScanner(BaseSignalSource):
    """
    监控指定 Twitter 账号的推文，解析出合约地址后触发买入信号。
    需要外部提供推文数据（通过 feed_tweet 方法）。
    """

    def __init__(self, accounts: list[str], chain: str = "sol"):
        self.accounts = accounts
        self.chain = chain

    async def start(self, on_signal: Callable[[TradeSignal], None]) -> None:
        logger.info("TwitterScanner started, watching accounts: %s", self.accounts)
        logger.warning("TwitterScanner: 需要接入 Twitter API，当前为占位实现")
        # TODO: 接入 Twitter/X API 或第三方推文流服务
        await asyncio.sleep(0)

    async def process_tweet(self, tweet_text: str, author: str, on_signal: Callable) -> None:
        """处理单条推文，提取 CA 并触发信号"""
        ca = extract_ca(tweet_text, self.chain)
        if not ca:
            return
        logger.info("tweet signal from @%s ca=%s", author, ca)
        signal = TradeSignal(
            chain=self.chain,
            token_address=ca,
            action="buy",
            source=f"twitter/@{author}",
            reason=tweet_text[:100],
        )
        if asyncio.iscoroutinefunction(on_signal):
            await on_signal(signal)
        else:
            on_signal(signal)
