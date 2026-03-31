"""交易引擎：接收信号，执行买入/卖出，轮询结果"""
import asyncio
from dataclasses import dataclass, field
from typing import Callable

from xxyy.client import client, XxyyAPIError
from signals.base import TradeSignal
from trading.position_monitor import PositionMonitor, Position
from config import config
from utils.logger import get_logger

logger = get_logger(__name__)


@dataclass
class TradeRecord:
    signal: TradeSignal
    tx_id: str
    status: str = "pending"   # pending | success | failed
    result: dict = field(default_factory=dict)


class TradingEngine:
    def __init__(
        self,
        wallet_address: str | None = None,
        buy_amount: float | None = None,
        sell_percent: int | None = None,
        tip: float | None = None,
        on_result: Callable[[TradeRecord], None] | None = None,
    ):
        self.wallet_address = wallet_address or config.wallet_address
        self.buy_amount = buy_amount or config.buy_amount
        self.sell_percent = sell_percent or config.sell_percent
        self.tip = tip or config.tip
        self.on_result = on_result
        self._history: list[TradeRecord] = []
        self._swap_lock = asyncio.Semaphore(1)  # 同一时间只允许一笔 swap
        self.position_monitor = PositionMonitor()

    async def handle_signal(self, signal: TradeSignal) -> TradeRecord | None:
        """收到信号后执行交易，返回交易记录"""
        if not self.wallet_address:
            logger.error("未配置 WALLET_ADDRESS，跳过信号 ca=%s", signal.token_address)
            return None

        logger.info(
            "信号触发 action=%s ca=%s chain=%s source=%s",
            signal.action, signal.token_address, signal.chain, signal.source,
        )

        is_buy = signal.action == "buy"
        amount = self.buy_amount if is_buy else float(self.sell_percent)

        try:
            async with self._swap_lock:
                tx_id = await client.swap(
                    chain=signal.chain,
                    wallet_address=self.wallet_address,
                    token_address=signal.token_address,
                    is_buy=is_buy,
                    amount=amount,
                    tip=self.tip,
                )
        except XxyyAPIError as e:
            logger.error("swap 失败 ca=%s error=%s", signal.token_address, e)
            return None

        record = TradeRecord(signal=signal, tx_id=tx_id)
        self._history.append(record)

        # 异步轮询结果，不阻塞主循环
        asyncio.create_task(self._poll_result(record))
        return record

    async def _poll_result(self, record: TradeRecord) -> None:
        try:
            result = await client.wait_trade(record.tx_id)
            record.result = result if isinstance(result, dict) else {}
            raw_status = record.result.get("status")
            if raw_status == 2:
                record.status = "success"
            elif raw_status == 3:
                record.status = "failed"
            else:
                record.status = "unknown"
            logger.info(
                "交易完成 txId=%s status=%s ca=%s",
                record.tx_id, record.status, record.signal.token_address,
            )
        except Exception as e:
            record.status = "failed"
            logger.error("轮询交易状态失败 txId=%s error=%s", record.tx_id, e)

        # 买入成功后登记仓位，启动止盈监控
        if record.status == "success" and record.signal.action == "buy":
            await self._register_position(record)

        if self.on_result:
            if asyncio.iscoroutinefunction(self.on_result):
                await self.on_result(record)
            else:
                self.on_result(record)

    async def _register_position(self, record: TradeRecord) -> None:
        """买入成功后查询入场价格，登记仓位"""
        try:
            token_data = await client.query_token(
                record.signal.token_address, record.signal.chain
            )
            trade_info = token_data.get("tradeInfo") or {} if isinstance(token_data, dict) else {}
            entry_price = float(trade_info.get("price") or 0)
            if entry_price <= 0:
                logger.warning("无法获取入场价格 ca=%s，跳过仓位登记", record.signal.token_address)
                return
            pos = Position(
                chain=record.signal.chain,
                token_address=record.signal.token_address,
                wallet_address=self.wallet_address,
                entry_price=entry_price,
                tip=self.tip,
            )
            self.position_monitor.add(pos)
        except Exception as e:
            logger.error("仓位登记失败 ca=%s error=%s", record.signal.token_address, e)

    @property
    def history(self) -> list[TradeRecord]:
        return self._history
