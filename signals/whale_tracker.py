"""
鲸鱼钱包追踪器 — 监控已知 Solana meme coin 大户的链上交易

原理：
1. 通过 Solana RPC getSignaturesForAddress 获取鲸鱼钱包最新交易
2. 通过 getParsedTransaction 解析交易内容
3. 当检测到鲸鱼买入 SPL 代币（token transfer IN）时，发出买入信号

数据源：Solana 公共 RPC（免费，无需 API Key）
"""
import asyncio
import time
from typing import Callable

import httpx

from signals.base import BaseSignalSource, TradeSignal
from utils.logger import get_logger

logger = get_logger(__name__)

# Solana 公共 RPC 端点（免费，有速率限制）
SOLANA_RPC = "https://api.mainnet-beta.solana.com"

# 扫描间隔（秒）
DEFAULT_SCAN_INTERVAL = 30

# 去重窗口（秒）：同一鲸鱼、同一代币在此时间内不重复发信号
DEDUP_WINDOW = 3600  # 1 小时

# 已知的 Solana meme coin 鲸鱼/聪明钱地址
# 这些是公开知名的盈利交易者，来自链上分析平台公开数据
WHALE_WALLETS: dict[str, str] = {
    # 知名 Solana meme 交易者（公开链上地址）
    "5ZnBHRE9nMYfGjGCdiAQPwFBnzmECPjbsXzWBxJFcvyy": "whale_meme_trader_1",
    "HVzKAMSmJGo8JuQqa2pAi2HBqLzbFMGFNrnuJSKxhXyR": "whale_meme_trader_2",
    "DYw8jCTfwHNRJhhmFcbXvVDTqWMEVFBX6ZKUmG5CNSKK": "sol_meme_whale_3",
    "6mK4bNTJz36yQPWjYGWTSsVEoErsJkMLajVbHFcCPmHU": "smart_money_whale_4",
    "A77HErFbEVu1gkPFwrMpxfEH2KAiz9V81qbE6YpmczgL": "meme_sniper_5",
}

# 忽略的代币（SOL wrapped、USDC、USDT 等稳定币/基础代币）
IGNORE_TOKENS = {
    "So11111111111111111111111111111111111111112",   # Wrapped SOL
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",  # USDT
}


class WhaleTracker(BaseSignalSource):
    """
    追踪已知鲸鱼钱包的链上买入行为，生成交易信号。
    """

    def __init__(
        self,
        wallets: dict[str, str] | None = None,
        scan_interval: int = DEFAULT_SCAN_INTERVAL,
        max_signals_per_cycle: int = 3,
    ):
        self.wallets = wallets or WHALE_WALLETS
        self.scan_interval = scan_interval
        self.max_signals_per_cycle = max_signals_per_cycle
        # 去重：{(wallet, token_address): timestamp}
        self._dedup: dict[tuple[str, str], float] = {}
        # 每个钱包已处理过的最新签名
        self._last_sig: dict[str, str] = {}

    async def start(self, on_signal: Callable[[TradeSignal], None]) -> None:
        logger.info(
            "WhaleTracker started, tracking %d wallets, interval=%ds",
            len(self.wallets), self.scan_interval,
        )
        async with httpx.AsyncClient(
            timeout=20.0,
            verify=False,
            headers={"Content-Type": "application/json"},
        ) as http:
            while True:
                try:
                    triggered = 0
                    for wallet, label in self.wallets.items():
                        if triggered >= self.max_signals_per_cycle:
                            break
                        try:
                            new_sigs = await self._scan_wallet(http, wallet, label, on_signal)
                            triggered += new_sigs
                        except Exception as e:
                            logger.debug("whale scan error wallet=%s: %s", label, e)
                        # 避免 RPC 限流
                        await asyncio.sleep(1)

                    if triggered > 0:
                        logger.info("WhaleTracker 本轮触发 %d 个信号", triggered)

                    self._cleanup_dedup()
                except Exception as e:
                    logger.error("WhaleTracker cycle error: %s", e)

                await asyncio.sleep(self.scan_interval)

    async def _scan_wallet(
        self,
        http: httpx.AsyncClient,
        wallet: str,
        label: str,
        on_signal: Callable,
    ) -> int:
        """扫描单个鲸鱼钱包的最新交易，返回本次触发的信号数。"""
        sigs = await self._get_signatures(http, wallet, limit=5)
        if not sigs:
            return 0

        triggered = 0
        for sig_info in sigs:
            sig = sig_info.get("signature", "")
            if not sig:
                continue
            # 跳过已处理的签名
            if self._last_sig.get(wallet) == sig:
                break
            # 跳过失败的交易
            if sig_info.get("err") is not None:
                continue

            tokens_in = await self._parse_token_transfers_in(http, sig, wallet)
            for token_address in tokens_in:
                if token_address in IGNORE_TOKENS:
                    continue
                if self._is_deduped(wallet, token_address):
                    continue

                self._mark_seen(wallet, token_address)
                signal = TradeSignal(
                    chain="sol",
                    token_address=token_address,
                    action="buy",
                    source="whale_tracker",
                    reason=f"Whale [{label}] bought {token_address[:16]}...",
                )
                await self._emit(on_signal, signal)
                triggered += 1
                logger.info(
                    "WhaleTracker signal: %s bought %s",
                    label, token_address[:16],
                )

        # 记录本轮最新签名
        if sigs:
            self._last_sig[wallet] = sigs[0].get("signature", "")

        return triggered

    async def _get_signatures(
        self, http: httpx.AsyncClient, address: str, limit: int = 5
    ) -> list[dict]:
        """调用 Solana RPC getSignaturesForAddress。"""
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getSignaturesForAddress",
            "params": [address, {"limit": limit}],
        }
        resp = await http.post(SOLANA_RPC, json=payload)
        if resp.status_code != 200:
            return []
        data = resp.json()
        return data.get("result", [])

    async def _parse_token_transfers_in(
        self, http: httpx.AsyncClient, signature: str, wallet: str
    ) -> list[str]:
        """
        解析交易，找出 wallet 收到的 SPL token mint 地址列表（即买入的代币）。
        """
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getParsedTransaction",
            "params": [signature, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}],
        }
        resp = await http.post(SOLANA_RPC, json=payload)
        if resp.status_code != 200:
            return []
        data = resp.json()
        result = data.get("result")
        if not result:
            return []

        tokens_in: list[str] = []
        meta = result.get("meta", {})

        # 方法1：从 postTokenBalances vs preTokenBalances 检测新增代币
        pre_balances = {
            (b.get("mint"), b.get("owner")): int(b.get("uiTokenAmount", {}).get("amount", "0"))
            for b in (meta.get("preTokenBalances") or [])
        }
        post_balances = meta.get("postTokenBalances") or []
        for b in post_balances:
            owner = b.get("owner", "")
            mint = b.get("mint", "")
            if owner != wallet or not mint:
                continue
            post_amount = int(b.get("uiTokenAmount", {}).get("amount", "0"))
            pre_amount = pre_balances.get((mint, owner), 0)
            # 余额增加 = 买入
            if post_amount > pre_amount:
                tokens_in.append(mint)

        return tokens_in

    def _is_deduped(self, wallet: str, token_address: str) -> bool:
        key = (wallet, token_address)
        ts = self._dedup.get(key)
        if ts and (time.time() - ts) < DEDUP_WINDOW:
            return True
        return False

    def _mark_seen(self, wallet: str, token_address: str) -> None:
        self._dedup[(wallet, token_address)] = time.time()

    def _cleanup_dedup(self) -> None:
        """清理过期的去重记录。"""
        now = time.time()
        expired = [k for k, ts in self._dedup.items() if (now - ts) > DEDUP_WINDOW]
        for k in expired:
            del self._dedup[k]

    async def _emit(self, on_signal: Callable, signal: TradeSignal) -> None:
        if asyncio.iscoroutinefunction(on_signal):
            await on_signal(signal)
        else:
            on_signal(signal)
