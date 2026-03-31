"""仓位监控：买入成功后持续监控价格，涨到 2x 自动卖出一半"""
import asyncio
from dataclasses import dataclass, field

from xxyy.client import client, XxyyAPIError
from config import config
from utils.logger import get_logger

logger = get_logger(__name__)

TAKE_PROFIT_MULTIPLIER = 2.0   # 翻倍出本
SELL_PERCENT_AT_TP = 50        # 卖出一半
PRICE_CHECK_INTERVAL = 15      # 每 15 秒查一次价格


@dataclass
class Position:
    chain: str
    token_address: str
    wallet_address: str
    entry_price: float          # 买入均价（USD）
    tip: float
    status: str = "open"        # open | tp_triggered | closed


class PositionMonitor:
    def __init__(self):
        self._positions: list[Position] = []
        self._swap_lock = asyncio.Semaphore(1)
        self._running = False

    def add(self, position: Position) -> None:
        self._positions.append(position)
        logger.info(
            "仓位已记录 ca=%s entry_price=$%.8f chain=%s",
            position.token_address, position.entry_price, position.chain,
        )

    async def start(self) -> None:
        self._running = True
        logger.info("PositionMonitor started interval=%ds", PRICE_CHECK_INTERVAL)
        while self._running:
            await self._check_all()
            await asyncio.sleep(PRICE_CHECK_INTERVAL)

    def stop(self) -> None:
        self._running = False

    async def _check_all(self) -> None:
        open_positions = [p for p in self._positions if p.status == "open"]
        for pos in open_positions:
            try:
                await self._check_position(pos)
            except Exception as e:
                logger.error("check position error ca=%s error=%s", pos.token_address, e)

    async def _check_position(self, pos: Position) -> None:
        data = await client.query_token(pos.token_address, pos.chain)
        if not isinstance(data, dict):
            return

        trade_info = data.get("tradeInfo") or {}
        current_price = float(trade_info.get("price") or 0)
        if current_price <= 0 or pos.entry_price <= 0:
            return

        multiplier = current_price / pos.entry_price
        logger.debug(
            "price check ca=%s current=$%.8f entry=$%.8f x=%.2f",
            pos.token_address, current_price, pos.entry_price, multiplier,
        )

        if multiplier >= TAKE_PROFIT_MULTIPLIER:
            logger.info(
                "🎯 触发翻倍出本 ca=%s x=%.2f 卖出%d%%",
                pos.token_address, multiplier, SELL_PERCENT_AT_TP,
            )
            pos.status = "tp_triggered"
            await self._sell_half(pos, multiplier)

    async def _sell_half(self, pos: Position, multiplier: float) -> None:
        try:
            async with self._swap_lock:
                tx_id = await client.swap(
                    chain=pos.chain,
                    wallet_address=pos.wallet_address,
                    token_address=pos.token_address,
                    is_buy=False,
                    amount=SELL_PERCENT_AT_TP,
                    tip=pos.tip,
                )
        except XxyyAPIError as e:
            logger.error("止盈卖出失败 ca=%s error=%s", pos.token_address, e)
            pos.status = "open"  # 回滚，下次继续检测
            return

        logger.info("止盈卖出提交 txId=%s ca=%s", tx_id, pos.token_address)

        result = await client.wait_trade(tx_id)
        raw_status = result.get("status") if isinstance(result, dict) else None
        if raw_status == 2:
            logger.info(
                "✅ 止盈成功 ca=%s x=%.2f 已卖出%d%% txId=%s",
                pos.token_address, multiplier, SELL_PERCENT_AT_TP, tx_id,
            )
            pos.status = "closed"
        else:
            logger.error("止盈卖出链上失败 ca=%s txId=%s status=%s", pos.token_address, tx_id, raw_status)
            pos.status = "open"

    @property
    def positions(self) -> list[Position]:
        return self._positions
