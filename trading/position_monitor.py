"""
仓位监控 — "会卖的才是师傅"

卖出策略体系：
1. 分批止盈：2x 卖30% → 5x 卖30% → 10x 清仓（锁利润、留飞天机会）
2. 移动止盈：首次TP后启动，从最高点回撤20%自动卖出（保护利润）
3. 时间止损：买入超过30分钟没涨过1.5x，清仓（不恋战）
4. 动量衰退：成交量骤降50%或持仓人减少，清仓（聪明人已走）
5. 破位止损：跌破入场价50%，清仓（保残值，不归零）
"""
import asyncio
import time
from dataclasses import dataclass, field

from xxyy.client import client, XxyyAPIError
from config import config
from utils.logger import get_logger

logger = get_logger(__name__)

PRICE_CHECK_INTERVAL = 10  # 每 10 秒查一次价格

# ── 止盈阶梯 ─────────────────────────────────────────────
# (倍数阈值, 卖出百分比, 描述)
TAKE_PROFIT_LEVELS = [
    (2.0,  30, "2x翻倍出本"),
    (5.0,  30, "5x锁大利"),
    (10.0, 100, "10x清仓"),
]

# ── 移动止盈 ─────────────────────────────────────────────
TRAILING_STOP_DROP = 0.20       # 从最高点回撤20%触发卖出
TRAILING_SELL_PERCENT = 100     # 移动止盈触发后全部卖出

# ── 时间止损 ─────────────────────────────────────────────
TIME_STOP_MINUTES = 30          # 30分钟没动静
TIME_STOP_MIN_MULTIPLIER = 1.5  # 这段时间内至少涨到1.5x才保留
TIME_STOP_SELL_PERCENT = 100

# ── 动量衰退 ─────────────────────────────────────────────
VOLUME_DROP_THRESHOLD = 0.5     # 成交量降至首次记录的50%以下
HOLDER_DROP_THRESHOLD = 0.9     # 持仓人数降至首次记录的90%以下

# ── 破位止损 ─────────────────────────────────────────────
CRASH_STOP_MULTIPLIER = 0.5     # 跌破入场价50%
CRASH_STOP_SELL_PERCENT = 100


@dataclass
class Position:
    chain: str
    token_address: str
    wallet_address: str
    entry_price: float
    tip: float
    status: str = "open"            # open | trailing | closed
    # 止盈进度
    tp_level: int = 0               # 已触发的止盈阶梯（0=未触发）
    # 移动止盈
    highest_price: float = 0.0      # 历史最高价
    trailing_active: bool = False   # 是否启动移动止盈
    # 动量追踪
    initial_volume: float = 0.0     # 首次记录的成交量
    initial_holders: int = 0        # 首次记录的持仓人数
    volume_recorded: bool = False
    # 时间追踪
    entry_time: float = field(default_factory=time.time)
    # 卖出记录
    sell_log: list[str] = field(default_factory=list)


class PositionMonitor:
    def __init__(self):
        self._positions: list[Position] = []
        self._swap_lock = asyncio.Semaphore(1)
        self._running = False
        self._safety = None  # 由 engine 注入

    def set_safety(self, safety) -> None:
        self._safety = safety

    def add(self, position: Position) -> None:
        self._positions.append(position)
        logger.info(
            "仓位已记录 ca=%s entry_price=$%.8f chain=%s",
            position.token_address, position.entry_price, position.chain,
        )

    async def start(self) -> None:
        self._running = True
        logger.info("PositionMonitor(师傅级) started interval=%ds", PRICE_CHECK_INTERVAL)
        logger.info(
            "止盈阶梯: %s | 移动止盈回撤: %d%% | 时间止损: %dmin | 破位止损: %d%%",
            [f"{m}x→{p}%" for m, p, _ in TAKE_PROFIT_LEVELS],
            int(TRAILING_STOP_DROP * 100),
            TIME_STOP_MINUTES,
            int(CRASH_STOP_MULTIPLIER * 100),
        )
        while self._running:
            await self._check_all()
            await asyncio.sleep(PRICE_CHECK_INTERVAL)

    def stop(self) -> None:
        self._running = False

    async def _check_all(self) -> None:
        open_positions = [p for p in self._positions if p.status != "closed"]
        for pos in open_positions:
            try:
                await self._check_position(pos)
            except Exception as e:
                logger.error("check position error ca=%s: %s", pos.token_address, e)

    async def _check_position(self, pos: Position) -> None:
        data = await client.query_token(pos.token_address, pos.chain)
        if not isinstance(data, dict):
            return

        trade_info = data.get("tradeInfo") or {}
        current_price = float(trade_info.get("price", 0) or 0)
        if current_price <= 0 or pos.entry_price <= 0:
            return

        multiplier = current_price / pos.entry_price

        # 记录初始动量数据
        if not pos.volume_recorded:
            pos.initial_volume = float(trade_info.get("hourTradeVolume", 0) or 0)
            pos.initial_holders = int(trade_info.get("holder", 0) or 0)
            pos.volume_recorded = True

        # 更新历史最高价
        if current_price > pos.highest_price:
            pos.highest_price = current_price

        logger.debug(
            "price check ca=%s current=$%.8f entry=$%.8f x=%.2f highest=$%.8f",
            pos.token_address, current_price, pos.entry_price,
            multiplier, pos.highest_price,
        )

        # ── 策略1: 分批止盈 ──────────────────────────────
        await self._check_take_profit(pos, multiplier)

        if pos.status == "closed":
            return

        # ── 策略2: 移动止盈（首次TP后启动）────────────────
        await self._check_trailing_stop(pos, current_price, multiplier)

        if pos.status == "closed":
            return

        # ── 策略3: 时间止损 ──────────────────────────────
        await self._check_time_stop(pos, multiplier)

        if pos.status == "closed":
            return

        # ── 策略4: 动量衰退 ──────────────────────────────
        current_volume = float(trade_info.get("hourTradeVolume", 0) or 0)
        current_holders = int(trade_info.get("holder", 0) or 0)
        await self._check_momentum(pos, current_volume, current_holders, multiplier)

        if pos.status == "closed":
            return

        # ── 策略5: 破位止损 ──────────────────────────────
        await self._check_crash_stop(pos, multiplier)

    # ── 策略1: 分批止盈 ──────────────────────────────────

    async def _check_take_profit(self, pos: Position, multiplier: float) -> None:
        """阶梯止盈：2x→30%, 5x→30%, 10x→清仓"""
        for i, (target_multi, sell_pct, desc) in enumerate(TAKE_PROFIT_LEVELS):
            # 只检查未触发的阶梯
            if pos.tp_level > i:
                continue
            if multiplier >= target_multi:
                logger.info(
                    "🎯 触发止盈[%s] ca=%s x=%.2f 卖出%d%%",
                    desc, pos.token_address, multiplier, sell_pct,
                )
                success = await self._sell(pos, sell_pct, f"止盈[{desc}]")
                if success:
                    pos.tp_level = i + 1
                    pos.sell_log.append(f"{desc} x={multiplier:.2f}")
                    # 首次止盈后启动移动止盈
                    if not pos.trailing_active:
                        pos.trailing_active = True
                        pos.status = "trailing"
                        logger.info(
                            "📊 移动止盈已启动 ca=%s 从当前最高价回撤%d%%触发",
                            pos.token_address, int(TRAILING_STOP_DROP * 100),
                        )
                    if sell_pct == 100:
                        pos.status = "closed"
                break  # 每次检查只触发一个阶梯

    # ── 策略2: 移动止盈 ──────────────────────────────────

    async def _check_trailing_stop(self, pos: Position, current_price: float, multiplier: float) -> None:
        """从最高点回撤一定比例 → 全部卖出锁利"""
        if not pos.trailing_active or pos.highest_price <= 0:
            return

        drop_from_high = 1 - (current_price / pos.highest_price)

        if drop_from_high >= TRAILING_STOP_DROP:
            logger.info(
                "📉 移动止盈触发 ca=%s 最高$%.8f → 现价$%.8f 回撤%.1f%% x=%.2f",
                pos.token_address, pos.highest_price, current_price,
                drop_from_high * 100, multiplier,
            )
            success = await self._sell(pos, TRAILING_SELL_PERCENT, "移动止盈")
            if success:
                pos.sell_log.append(f"移动止盈 回撤{drop_from_high*100:.1f}% x={multiplier:.2f}")
                pos.status = "closed"

    # ── 策略3: 时间止损 ──────────────────────────────────

    async def _check_time_stop(self, pos: Position, multiplier: float) -> None:
        """买入超过一定时间没达到目标 → 清仓不恋战"""
        elapsed_min = (time.time() - pos.entry_time) / 60

        if elapsed_min >= TIME_STOP_MINUTES and multiplier < TIME_STOP_MIN_MULTIPLIER:
            logger.info(
                "⏰ 时间止损 ca=%s 已持仓%.0f分钟 仅%.2fx 未达%.1fx 清仓",
                pos.token_address, elapsed_min, multiplier, TIME_STOP_MIN_MULTIPLIER,
            )
            success = await self._sell(pos, TIME_STOP_SELL_PERCENT, "时间止损")
            if success:
                pos.sell_log.append(f"时间止损 {elapsed_min:.0f}min x={multiplier:.2f}")
                pos.status = "closed"

    # ── 策略4: 动量衰退 ──────────────────────────────────

    async def _check_momentum(
        self, pos: Position,
        current_volume: float, current_holders: int,
        multiplier: float,
    ) -> None:
        """成交量骤降或持仓人减少 → 聪明人已走，跟着走"""
        if not pos.volume_recorded or pos.initial_volume <= 0:
            return

        # 只有还在盈利时才用动量退出（亏损的用破位止损）
        if multiplier < 1.0:
            return

        volume_ratio = current_volume / pos.initial_volume if pos.initial_volume > 0 else 1.0
        holder_ratio = current_holders / pos.initial_holders if pos.initial_holders > 0 else 1.0

        if volume_ratio < VOLUME_DROP_THRESHOLD:
            logger.info(
                "📊 动量衰退(成交量) ca=%s volume降至%.0f%% x=%.2f 清仓",
                pos.token_address, volume_ratio * 100, multiplier,
            )
            success = await self._sell(pos, 100, "动量衰退-成交量")
            if success:
                pos.sell_log.append(f"动量衰退 vol={volume_ratio*100:.0f}% x={multiplier:.2f}")
                pos.status = "closed"
            return

        if holder_ratio < HOLDER_DROP_THRESHOLD and pos.initial_holders >= 20:
            logger.info(
                "📊 动量衰退(持仓人) ca=%s holders从%d降至%d x=%.2f 清仓",
                pos.token_address, pos.initial_holders, current_holders, multiplier,
            )
            success = await self._sell(pos, 100, "动量衰退-持仓人")
            if success:
                pos.sell_log.append(f"动量衰退 holders={current_holders}/{pos.initial_holders} x={multiplier:.2f}")
                pos.status = "closed"

    # ── 策略5: 破位止损 ──────────────────────────────────

    async def _check_crash_stop(self, pos: Position, multiplier: float) -> None:
        """跌破入场价一定比例 → 保残值，不等归零"""
        if multiplier <= CRASH_STOP_MULTIPLIER:
            logger.info(
                "💀 破位止损 ca=%s 跌至%.2fx（入场价的%d%%）清仓保残",
                pos.token_address, multiplier, int(multiplier * 100),
            )
            success = await self._sell(pos, CRASH_STOP_SELL_PERCENT, "破位止损")
            if success:
                pos.sell_log.append(f"破位止损 x={multiplier:.2f}")
                pos.status = "closed"

    # ── 卖出执行 ─────────────────────────────────────────

    async def _sell(self, pos: Position, sell_percent: int, reason: str) -> bool:
        """执行卖出，返回是否成功"""
        try:
            async with self._swap_lock:
                tx_id = await client.swap(
                    chain=pos.chain,
                    wallet_address=pos.wallet_address,
                    token_address=pos.token_address,
                    is_buy=False,
                    amount=sell_percent,
                    tip=pos.tip,
                )
        except XxyyAPIError as e:
            logger.error("卖出失败[%s] ca=%s error=%s", reason, pos.token_address, e)
            return False

        logger.info("卖出提交[%s] txId=%s ca=%s %d%%", reason, tx_id, pos.token_address, sell_percent)

        result = await client.wait_trade(tx_id)
        raw_status = result.get("status") if isinstance(result, dict) else None
        if raw_status == 2:
            logger.info(
                "✅ 卖出成功[%s] ca=%s %d%% txId=%s",
                reason, pos.token_address, sell_percent, tx_id,
            )
            # 记录亏损到安全护栏（仅在亏损时）
            if self._safety and "止损" in reason:
                # 粗估亏损：入场价对应的投入（按比例）
                self._safety.record_loss(0.05)  # 保守估算每笔亏损
            return True
        else:
            logger.error(
                "❌ 卖出链上失败[%s] ca=%s txId=%s status=%s",
                reason, pos.token_address, tx_id, raw_status,
            )
            return False

    @property
    def positions(self) -> list[Position]:
        return self._positions
