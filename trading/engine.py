"""交易引擎：接收信号，执行买入/卖出，轮询结果"""
import asyncio
import os
import time
from dataclasses import dataclass, field
from typing import Callable

from xxyy.client import client, XxyyAPIError
from signals.base import TradeSignal
from trading.position_monitor import PositionMonitor, Position
from trading.token_analyzer import TokenAnalyzer
from trading.safety import SafetyGuard
from config import config
from utils.logger import get_logger

logger = get_logger(__name__)


@dataclass
class TradeRecord:
    signal: TradeSignal
    tx_id: str
    status: str = "pending"   # pending | success | failed
    result: dict = field(default_factory=dict)
    score: int = 0             # 分析评分
    tier: str = ""             # 分档: 顶级/人上人/NPC
    buy_amount: float = 0.0    # 实际买入金额


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
        self._signal_queue: asyncio.Queue = asyncio.Queue()
        self._processing: set[str] = set()  # 正在处理的 CA，防止并发分析同一个币
        self._rejected_cache: set[str] = set()  # 已拒绝的 CA 缓存，避免重复分析
        self.position_monitor = PositionMonitor()
        self.analyzer = TokenAnalyzer()
        self.safety = SafetyGuard()
        self.position_monitor.set_safety(self.safety)
        self.position_monitor._on_signal = self.handle_signal  # 墓地复活信号回调
        self.reporter = None  # 由 main.py 注入
        # 启动信号消费者
        self._consumer_task = None

    def start_consumer(self):
        """启动信号队列消费者（在 main.py 的 run() 中调用）"""
        self._consumer_task = asyncio.create_task(self._consume_signals())

    async def _consume_signals(self):
        """串行消费信号队列，避免 XXYY API 429"""
        while True:
            signal = await self._signal_queue.get()
            try:
                await self._process_signal(signal)
            except Exception as e:
                logger.error("信号处理异常 ca=%s: %s", signal.token_address, e)
            finally:
                self._processing.discard(signal.token_address)
                self._signal_queue.task_done()

    async def handle_signal(self, signal: TradeSignal) -> TradeRecord | None:
        """收到信号后放入队列（非阻塞），由消费者串行处理"""
        if not self.wallet_address:
            logger.error("未配置 WALLET_ADDRESS，跳过信号 ca=%s", signal.token_address)
            return None

        is_buy = signal.action == "buy"

        # 快速去重（不需要等队列）
        if is_buy:
            ca = signal.token_address
            # 已在处理中
            if ca in self._processing:
                return None
            # 已被拒绝过（墓地复活信号除外，给二次机会）
            if ca in self._rejected_cache:
                if signal.source == "graveyard_revive":
                    self._rejected_cache.discard(ca)
                else:
                    return None
            # 已持仓
            held = {p.token_address for p in self.position_monitor.positions if p.status != "closed"}
            if ca in held:
                return None
            self._processing.add(ca)

        logger.info(
            "信号触发 action=%s ca=%s chain=%s source=%s",
            signal.action, signal.token_address, signal.chain, signal.source,
        )

        # 放入队列串行处理
        await self._signal_queue.put(signal)
        return None

    async def _process_signal(self, signal: TradeSignal) -> TradeRecord | None:
        """实际处理信号（由消费者串行调用）"""
        is_buy = signal.action == "buy"

        # 再次检查持仓（可能队列等待期间已买入）
        if is_buy:
            held = {p.token_address for p in self.position_monitor.positions if p.status != "closed"}
            if signal.token_address in held:
                logger.info("已持仓，跳过重复买入 ca=%s", signal.token_address)
                return None

        # 买入前先做全面分析 + 分档投入
        if is_buy:
            analysis = await self.analyzer.analyze(signal.token_address, signal.chain)
            logger.info("分析结果:\n%s", analysis.summary())
            if not analysis.passed:
                logger.info("分析未通过，跳过买入 ca=%s score=%d", signal.token_address, analysis.score)
                self._rejected_cache.add(signal.token_address)
                if self.reporter:
                    self.reporter.record_signal(signal.source, passed=False, score=analysis.score)
                return None
            if self.reporter:
                self.reporter.record_signal(signal.source, passed=True, score=analysis.score)

        if is_buy:
            amount = self._calc_buy_amount(analysis.score)

            # ── 安全护栏检查 ──────────────────────────────
            try:
                wallet_info = await client.wallet_info(self.wallet_address, signal.chain)
                sol_balance = float(
                    (wallet_info or {}).get("balance", 0)
                    or (wallet_info or {}).get("solBalance", 0)
                    or 0
                )
            except Exception:
                sol_balance = 0.0

            open_count = len([p for p in self.position_monitor.positions if p.status != "closed"])
            allowed, reason = self.safety.can_buy(amount, sol_balance, open_count)
            if not allowed:
                logger.warning("安全护栏拦截买入: %s ca=%s amount=%.3f", reason, signal.token_address, amount)
                return None
        else:
            amount = float(self.sell_percent)

        # ── 买入重试机制：最多重试 BUY_RETRY_MAX 次 ──────────────
        max_attempts = self.BUY_RETRY_MAX if is_buy else 1

        for attempt in range(1, max_attempts + 1):
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
                logger.error("swap API 失败 ca=%s attempt=%d/%d error=%s",
                             signal.token_address, attempt, max_attempts, e)
                if is_buy and attempt < max_attempts:
                    logger.info("⏳ %d秒后重试买入 ca=%s", self.BUY_RETRY_DELAY, signal.token_address)
                    await asyncio.sleep(self.BUY_RETRY_DELAY)
                    continue
                if is_buy:
                    self.safety.record_failure()
                return None

            # swap 提交成功，轮询链上结果
            result = await self._wait_trade_result(tx_id)
            status = result.get("status") if isinstance(result, dict) else None

            if status == 2:
                # 链上成功
                record = TradeRecord(signal=signal, tx_id=tx_id, buy_amount=amount)
                if is_buy:
                    record.score = analysis.score
                    record.tier = self._get_tier_name(analysis.score)
                record.status = "success"
                record.result = result
                self._history.append(record)
                self._on_trade_done(record)
                return record

            if status == 3 and is_buy and attempt < max_attempts:
                # 链上失败，重试
                logger.warning("🔄 链上执行失败，%d秒后重试 ca=%s attempt=%d/%d",
                               self.BUY_RETRY_DELAY, signal.token_address, attempt, max_attempts)
                await asyncio.sleep(self.BUY_RETRY_DELAY)
                continue

            # 最终失败或卖出失败
            record = TradeRecord(signal=signal, tx_id=tx_id, buy_amount=amount)
            if is_buy:
                record.score = analysis.score
                record.tier = self._get_tier_name(analysis.score)
            record.status = "failed" if status == 3 else "unknown"
            record.result = result if isinstance(result, dict) else {}
            self._history.append(record)
            self._on_trade_done(record)
            return record

        return None

    # ── 重试参数 ──────────────────────────────────────────────
    BUY_RETRY_MAX = 2       # 买入最多尝试 2 次（1次原始 + 1次重试）
    BUY_RETRY_DELAY = 3     # 重试间隔 3 秒

    async def _wait_trade_result(self, tx_id: str) -> dict:
        """轮询链上交易结果"""
        try:
            result = await client.wait_trade(tx_id)
            return result if isinstance(result, dict) else {}
        except Exception as e:
            logger.error("轮询交易状态失败 txId=%s error=%s", tx_id, e)
            return {}

    def _on_trade_done(self, record: TradeRecord) -> None:
        """交易完成后的统一回调（安全统计 + 仓位登记）"""
        logger.info("交易完成 txId=%s status=%s ca=%s",
                     record.tx_id, record.status, record.signal.token_address)

        # 安全统计
        if record.signal.action == "buy":
            if record.status == "success":
                self.safety.record_buy(record.buy_amount)
                self.safety.record_success()
            elif record.status == "failed":
                self.safety.record_failure()

        # 买入成功后登记仓位，启动止盈监控，写入信号汇总
        if record.status == "success" and record.signal.action == "buy":
            self._log_trade_signal(record)
            asyncio.create_task(self._register_position(record))

        if self.on_result:
            self.on_result(record)

    REGISTER_RETRY_MAX = 3
    REGISTER_RETRY_DELAY = 5

    async def _register_position(self, record: TradeRecord) -> None:
        """买入成功后查询入场价格，登记仓位。失败重试，确保不丢仓位。"""
        for attempt in range(1, self.REGISTER_RETRY_MAX + 1):
            try:
                token_data = await client.query_token(
                    record.signal.token_address, record.signal.chain
                )
                trade_info = token_data.get("tradeInfo") or {} if isinstance(token_data, dict) else {}
                entry_price = float(trade_info.get("price") or 0)
                if entry_price <= 0:
                    if attempt < self.REGISTER_RETRY_MAX:
                        logger.warning(
                            "获取入场价格失败 ca=%s attempt=%d/%d，%ds后重试",
                            record.signal.token_address, attempt, self.REGISTER_RETRY_MAX,
                            self.REGISTER_RETRY_DELAY,
                        )
                        await asyncio.sleep(self.REGISTER_RETRY_DELAY)
                        continue
                    # 最后一次仍失败，用买入金额估算一个价格，宁可不准也不丢仓位
                    logger.error(
                        "⚠️ 无法获取入场价格 ca=%s，使用备用价格登记仓位",
                        record.signal.token_address,
                    )
                    entry_price = 1e-10  # 极小值，确保任何涨幅都能触发止盈
                pos = Position(
                    chain=record.signal.chain,
                    token_address=record.signal.token_address,
                    wallet_address=self.wallet_address,
                    entry_price=entry_price,
                    tip=self.tip,
                )
                self.position_monitor.add(pos)
                return
            except Exception as e:
                if attempt < self.REGISTER_RETRY_MAX:
                    logger.warning(
                        "仓位登记异常 ca=%s attempt=%d/%d: %s，%ds后重试",
                        record.signal.token_address, attempt, self.REGISTER_RETRY_MAX,
                        e, self.REGISTER_RETRY_DELAY,
                    )
                    await asyncio.sleep(self.REGISTER_RETRY_DELAY)
                else:
                    # 最终兜底：用极小价格登记，确保仓位不丢
                    logger.error(
                        "⚠️ 仓位登记最终失败 ca=%s，使用备用价格强制登记: %s",
                        record.signal.token_address, e,
                    )
                    pos = Position(
                        chain=record.signal.chain,
                        token_address=record.signal.token_address,
                        wallet_address=self.wallet_address,
                        entry_price=1e-10,
                        tip=self.tip,
                    )
                    self.position_monitor.add(pos)

    # ── 分档投入（广撒网策略）─────────────────────────────────
    # 小注为主，顶级才加码，拉跨也敢少量试
    # 顶级(≥90): 2x | 人上人(≥75): 1.5x | NPC(≥50): 1x | 探路(≥40): 0.5x

    TIERS = [
        (90, 2.0, "顶级"),
        (75, 1.5, "人上人"),
        (50, 1.0, "NPC"),
        (40, 0.5, "探路"),
    ]

    def _calc_buy_amount(self, score: int) -> float:
        for min_score, multiplier, tier_name in self.TIERS:
            if score >= min_score:
                amount = self.buy_amount * multiplier
                logger.info(
                    "分档投入: score=%d tier=%s multiplier=%.1fx amount=%.3f",
                    score, tier_name, multiplier, amount,
                )
                return amount
        return self.buy_amount

    def _get_tier_name(self, score: int) -> str:
        for min_score, _, tier_name in self.TIERS:
            if score >= min_score:
                return tier_name
        return "拉跨"

    SIGNAL_LOG = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "trade_signals.log")

    def _log_trade_signal(self, record: TradeRecord) -> None:
        """每笔成功买入写入 trade_signals.log，方便查看汇总"""
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        line = (
            f"[{ts}] BUY {record.tier}({record.score}分) "
            f"ca={record.signal.token_address} "
            f"amount={record.buy_amount:.3f}SOL "
            f"source={record.signal.source} "
            f"txId={record.tx_id}\n"
        )
        try:
            with open(self.SIGNAL_LOG, "a") as f:
                f.write(line)
        except Exception as e:
            logger.error("写入信号汇总失败: %s", e)

    @property
    def history(self) -> list[TradeRecord]:
        return self._history
