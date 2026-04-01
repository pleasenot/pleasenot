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
import json
import os
import time
from dataclasses import dataclass, field

from xxyy.client import client, XxyyAPIError
from config import config
from utils.logger import get_logger

logger = get_logger(__name__)

PRICE_CHECK_INTERVAL = 60  # 每 60 秒查一轮价格（避免 429）

# ── 止盈阶梯 ─────────────────────────────────────────────
# 核心策略：快速回本，利润奔跑
# (倍数阈值, 卖出百分比, 描述)
TAKE_PROFIT_LEVELS = [
    (1.3,  70, "1.3x回本出局"),     # 涨30%就卖70%回本金
    (2.0,  50, "2x翻倍锁利"),       # 翻倍卖剩余的50%
    (5.0,  30, "5x大肉锁一点"),     # 5倍只卖30%，留大头
    (20.0, 30, "20x再锁一点"),      # 20倍再锁30%
    (100.0, 50, "100x半仓落袋"),    # 百倍卖一半，剩下的永远留着
]

# ── 移动止盈 ─────────────────────────────────────────────
TRAILING_STOP_DROP = 0.15       # 从最高点回撤15%触发卖出（收紧，保利润）
TRAILING_SELL_PERCENT = 100     # 移动止盈触发后全部卖出

# ── 时间止损 ─────────────────────────────────────────────
TIME_STOP_MINUTES = 15          # 缩短到15分钟，不涨就跑
TIME_STOP_MIN_MULTIPLIER = 1.1  # 15分钟至少涨10%才留
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
        self.save_positions()

    async def recover_from_wallet(self, wallet_address: str, chain: str) -> None:
        """启动时从链上查询钱包持仓，恢复未关闭的仓位"""
        try:
            holdings = await client.wallet_holdings(wallet_address, chain)
            if not holdings:
                logger.info("钱包无代币持仓，无需恢复")
                return
            known_cas = {p.token_address for p in self._positions}
            recovered = 0
            for h in holdings:
                ca = h.get("tokenAddress") or h.get("address") or ""
                if not ca or ca in known_cas:
                    continue
                # 查询当前价格作为"入场价"（保守估计，用于止损判断）
                try:
                    data = await client.query_token(ca, chain)
                    trade_info = (data or {}).get("tradeInfo") or {} if isinstance(data, dict) else {}
                    price = float(trade_info.get("price", 0) or 0)
                except Exception:
                    price = 0.0
                if price <= 0:
                    continue
                pos = Position(
                    chain=chain,
                    token_address=ca,
                    wallet_address=wallet_address,
                    entry_price=price,
                    tip=config.tip,
                )
                self._positions.append(pos)
                recovered += 1
                logger.info("恢复持仓 ca=%s price=$%.8f", ca, price)
            logger.info("从钱包恢复了 %d 个持仓", recovered)
        except Exception as e:
            logger.error("恢复持仓失败: %s", e)

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
            # 每个持仓查完后等 3 秒，避免连续打 API 触发 429
            await asyncio.sleep(3)

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
                self._safety.record_loss(0.05)
            self.save_positions()
            # 写入交易信号汇总
            self._log_sell_signal(pos, sell_percent, reason, tx_id)
            return True
        else:
            logger.error(
                "❌ 卖出链上失败[%s] ca=%s txId=%s status=%s",
                reason, pos.token_address, tx_id, raw_status,
            )
            return False

    SIGNAL_LOG = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "trade_signals.log")

    def _log_sell_signal(self, pos: Position, sell_pct: int, reason: str, tx_id: str) -> None:
        """卖出信号写入 trade_signals.log"""
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        line = (
            f"[{ts}] SELL [{reason}] {sell_pct}% "
            f"ca={pos.token_address} "
            f"entry=${pos.entry_price:.8f} "
            f"txId={tx_id}\n"
        )
        try:
            with open(self.SIGNAL_LOG, "a") as f:
                f.write(line)
        except Exception as e:
            logger.error("写入卖出信号失败: %s", e)

    # ── 持仓持久化 ─────────────────────────────────────

    SAVE_FILE = "positions.json"

    def save_positions(self) -> None:
        """保存持仓到文件，重启后可恢复"""
        data = []
        for p in self._positions:
            if p.status == "closed":
                continue
            data.append({
                "chain": p.chain,
                "token_address": p.token_address,
                "wallet_address": p.wallet_address,
                "entry_price": p.entry_price,
                "tip": p.tip,
                "status": p.status,
                "tp_level": p.tp_level,
                "highest_price": p.highest_price,
                "trailing_active": p.trailing_active,
                "entry_time": p.entry_time,
            })
        with open(self.SAVE_FILE, "w") as f:
            json.dump(data, f, indent=2)
        logger.debug("持仓已保存到 %s (%d 个)", self.SAVE_FILE, len(data))

    def load_positions(self) -> int:
        """从文件恢复持仓，返回恢复数量"""
        if not os.path.exists(self.SAVE_FILE):
            return 0
        try:
            with open(self.SAVE_FILE) as f:
                data = json.load(f)
            known = {p.token_address for p in self._positions}
            count = 0
            for d in data:
                if d["token_address"] in known:
                    continue
                # 恢复持仓时重置 entry_time 为当前时间，避免时间止损误触发
                pos = Position(
                    chain=d["chain"],
                    token_address=d["token_address"],
                    wallet_address=d["wallet_address"],
                    entry_price=d["entry_price"],
                    tip=d.get("tip", config.tip),
                    status=d.get("status", "open"),
                    tp_level=d.get("tp_level", 0),
                    highest_price=d.get("highest_price", 0.0),
                    trailing_active=d.get("trailing_active", False),
                    entry_time=time.time(),
                )
                self._positions.append(pos)
                count += 1
                logger.info("恢复持仓 ca=%s entry=$%.8f status=%s", pos.token_address, pos.entry_price, pos.status)
            return count
        except Exception as e:
            logger.error("加载持仓文件失败: %s", e)
            return 0

    @property
    def positions(self) -> list[Position]:
        return self._positions
