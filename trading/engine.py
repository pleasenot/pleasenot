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


# ── 信号去重缓存 ────────────────────────────────────────────
# 同一个 CA 在 DEDUP_TTL 秒内不重复处理（跨信号源去重）
DEDUP_TTL = 300  # 5 分钟

# ── 动态仓位：固定比例 + 信号强度 ──────────────────────────
POSITION_RATIO = 0.01         # 钱包余额的 1%
POSITION_MIN_SOL = 0.01       # 最小买入 0.01 SOL
POSITION_MAX_SOL = 0.1        # 最大买入 0.1 SOL
# 信号强度加码倍数 {命中信号源数量: 倍数}
SIGNAL_STRENGTH_MULTIPLIER = {
    1: 1.0,   # 单信号源：标准仓位
    2: 1.5,   # 双信号源交叉验证：1.5x
    3: 2.0,   # 三信号源：2x
}
SIGNAL_STRENGTH_MAX_MULTI = 3.0   # 4个以上信号源最高 3x
# 信号强度窗口：同一个 CA 在多少秒内被多个信号源命中算交叉验证
SIGNAL_STRENGTH_WINDOW = 300  # 5 分钟


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
    # ── 并发分析参数 ─────────────────────────────────────────
    # ── 重试参数（8054 多数是临时性错误，重试可成交）──────────
    BUY_RETRY_MAX = 2
    BUY_RETRY_DELAY = 5
    CONCURRENT_ANALYSES = 3     # 最多 3 个信号同时分析

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
        self._swap_lock = asyncio.Semaphore(1)  # 同一时间只允许一笔 swap（买入+卖出共享）
        self._analysis_sem = asyncio.Semaphore(self.CONCURRENT_ANALYSES)  # 并发分析限制
        self._signal_queue: asyncio.Queue = asyncio.Queue()
        self._processing: set[str] = set()  # 正在处理的 CA，防止并发分析同一个币
        self._rejected_cache: set[str] = set()  # 已拒绝的 CA 缓存，避免重复分析
        self._signal_seen: dict[str, float] = {}  # 跨信号源去重 {ca: timestamp}
        # 信号强度追踪：{ca: {source1, source2, ...}}，记录每个 CA 被哪些信号源命中
        self._signal_hits: dict[str, dict] = {}  # {ca: {"sources": set, "first_seen": float}}
        self.position_monitor = PositionMonitor(swap_lock=self._swap_lock)
        self.analyzer = TokenAnalyzer()
        self.safety = SafetyGuard()
        self.position_monitor.set_safety(self.safety)
        self.position_monitor._on_signal = self.handle_signal  # 墓地复活信号回调
        self.reporter = None  # 由 main.py 注入
        self._consumer_tasks: list[asyncio.Task] = []

    def start_consumer(self):
        """启动多个信号消费者（并发分析，swap 仍串行）"""
        for i in range(self.CONCURRENT_ANALYSES):
            task = asyncio.create_task(self._consume_signals())
            self._consumer_tasks.append(task)
        logger.info("信号消费者启动: %d 个并发分析器", self.CONCURRENT_ANALYSES)

    async def _consume_signals(self):
        """消费信号队列，分析可并发，swap 串行"""
        while True:
            signal = await self._signal_queue.get()
            try:
                await self._process_signal(signal)
            except Exception as e:
                logger.error("信号处理异常 ca=%s: %s", signal.token_address, e)
                # 处理失败，清除去重缓存让信号可以重试
                self._signal_seen.pop(signal.token_address, None)
            finally:
                self._processing.discard(signal.token_address)
                self._signal_queue.task_done()

    def _cleanup_dedup(self) -> None:
        """清理过期的去重缓存、信号强度追踪、拒绝缓存、历史记录"""
        now = time.time()
        expired = [ca for ca, ts in self._signal_seen.items() if now - ts > DEDUP_TTL]
        for ca in expired:
            del self._signal_seen[ca]
        # 清理过期的信号强度记录
        expired_hits = [ca for ca, h in self._signal_hits.items() if now - h["first_seen"] > SIGNAL_STRENGTH_WINDOW * 2]
        for ca in expired_hits:
            del self._signal_hits[ca]
        # 拒绝缓存：超过 1000 个时清掉一半（LRU 近似）
        if len(self._rejected_cache) > 1000:
            # set 无序，随机删一半
            to_remove = list(self._rejected_cache)[:500]
            for ca in to_remove:
                self._rejected_cache.discard(ca)
        # 历史记录：只保留最近 500 条
        if len(self._history) > 500:
            self._history = self._history[-500:]

    async def handle_signal(self, signal: TradeSignal) -> TradeRecord | None:
        """收到信号后放入队列（非阻塞），由消费者并发处理"""
        if not self.wallet_address:
            logger.error("未配置 WALLET_ADDRESS，跳过信号 ca=%s", signal.token_address)
            return None

        is_buy = signal.action == "buy"

        # 快速去重 + 信号强度追踪
        if is_buy:
            ca = signal.token_address
            now = time.time()

            # 记录信号强度（即使去重也要记，用于后续加码）
            if ca not in self._signal_hits:
                self._signal_hits[ca] = {"sources": set(), "first_seen": now}
            hit = self._signal_hits[ca]
            # 窗口内的信号才算交叉验证
            if now - hit["first_seen"] <= SIGNAL_STRENGTH_WINDOW:
                hit["sources"].add(signal.source)
            else:
                # 窗口过期，重置
                self._signal_hits[ca] = {"sources": {signal.source}, "first_seen": now}

            # 已在处理中
            if ca in self._processing:
                return None
            # 跨信号源去重：同一个 CA 5 分钟内只处理一次
            if ca in self._signal_seen and now - self._signal_seen[ca] < DEDUP_TTL:
                if signal.source != "graveyard_revive":
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
            self._signal_seen[ca] = now

        logger.info(
            "信号触发 action=%s ca=%s chain=%s source=%s",
            signal.action, signal.token_address, signal.chain, signal.source,
        )

        # 放入队列
        await self._signal_queue.put(signal)

        # 定期清理各类缓存
        if len(self._signal_seen) > 200 or len(self._rejected_cache) > 500:
            self._cleanup_dedup()

        return None

    async def _process_signal(self, signal: TradeSignal) -> TradeRecord | None:
        """实际处理信号（分析可并发，swap 串行）"""
        is_buy = signal.action == "buy"

        # 再次检查持仓（可能队列等待期间已买入）
        if is_buy:
            held = {p.token_address for p in self.position_monitor.positions if p.status != "closed"}
            if signal.token_address in held:
                logger.info("已持仓，跳过重复买入 ca=%s", signal.token_address)
                return None

        # 买入前先做全面分析（可并发，受 _analysis_sem 限制）
        if is_buy:
            async with self._analysis_sem:
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
            # ── 查询钱包余额（用于动态仓位计算 + 安全检查）───
            try:
                wallet_info = await client.wallet_info(self.wallet_address, signal.chain)
                sol_balance = float(
                    (wallet_info or {}).get("balance", 0)
                    or (wallet_info or {}).get("solBalance", 0)
                    or 0
                )
            except Exception as e:
                logger.warning("查询钱包余额失败，使用缓存余额: %s", e)
                sol_balance = getattr(self, '_cached_balance', 0.0)

            if sol_balance > 0:
                self._cached_balance = sol_balance

            # 计算信号强度
            hit_info = self._signal_hits.get(signal.token_address)
            signal_count = len(hit_info["sources"]) if hit_info else 1

            amount = self._calc_buy_amount(analysis.score, sol_balance, signal_count)

            # ── 安全护栏检查 ──────────────────────────────
            open_count = len([p for p in self.position_monitor.positions if p.status != "closed"])
            allowed, reason = self.safety.can_buy(amount, sol_balance, open_count)
            if not allowed:
                logger.warning("安全护栏拦截买入: %s ca=%s amount=%.3f", reason, signal.token_address, amount)
                return None
        else:
            amount = float(self.sell_percent)

        # ── 执行 swap（8054 多数是临时性错误，重试一次）──────────
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
                logger.error("swap 失败 ca=%s attempt=%d/%d error=%s",
                             signal.token_address, attempt, max_attempts, e)
                if is_buy and attempt < max_attempts:
                    logger.info("⏳ %d秒后重试 ca=%s", self.BUY_RETRY_DELAY, signal.token_address)
                    await asyncio.sleep(self.BUY_RETRY_DELAY)
                    continue
                if is_buy:
                    self.safety.record_failure()
                return None

            # swap 提交成功，轮询链上结果
            result = await self._wait_trade_result(tx_id)
            status = result.get("status") if isinstance(result, dict) else None

            # 兼容字符串和数字格式的 status
            if status == 2 or status == "success":
                record = TradeRecord(signal=signal, tx_id=tx_id, buy_amount=amount)
                if is_buy:
                    record.score = analysis.score
                    record.tier = self._get_tier_name(analysis.score)
                record.status = "success"
                record.result = result
                self._history.append(record)
                self._on_trade_done(record)
                return record

            if (status == 3 or status == "failed") and is_buy and attempt < max_attempts:
                logger.warning("🔄 链上失败，%d秒后重试 ca=%s", self.BUY_RETRY_DELAY, signal.token_address)
                await asyncio.sleep(self.BUY_RETRY_DELAY)
                continue

            # 最终失败
            record = TradeRecord(signal=signal, tx_id=tx_id, buy_amount=amount)
            if is_buy:
                record.score = analysis.score
                record.tier = self._get_tier_name(analysis.score)
            record.status = "failed" if (status == 3 or status == "failed") else "unknown"
            record.result = result if isinstance(result, dict) else {}
            self._history.append(record)
            self._on_trade_done(record)
            return record

        return None

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
        """买入成功后计算 SOL 计价入场价，登记仓位。"""
        for attempt in range(1, self.REGISTER_RETRY_MAX + 1):
            try:
                # 优先从交易结果算入场价（SOL计价）：买入SOL / 获得代币数
                entry_price = 0.0
                base_amount = float(record.result.get("baseAmount", 0) or 0)  # SOL 花费
                quote_amount = float(record.result.get("quoteAmount", 0) or 0)  # 代币数量
                if base_amount > 0 and quote_amount > 0:
                    entry_price = base_amount / quote_amount  # SOL/token
                    logger.info("入场价(SOL计): %.12f SOL/token (花费%.4f SOL 获得%.0f个)",
                                entry_price, base_amount, quote_amount)

                # fallback: 用 DexScreener SOL 计价
                if entry_price <= 0:
                    try:
                        import httpx
                        async with httpx.AsyncClient(timeout=8.0, verify=False) as http:
                            resp = await http.get(
                                f"https://api.dexscreener.com/latest/dex/tokens/{record.signal.token_address}"
                            )
                            if resp.status_code == 200:
                                pairs = resp.json().get("pairs") or []
                                if pairs:
                                    entry_price = float(pairs[0].get("priceNative", 0) or 0)
                    except Exception:
                        pass

                # fallback: XXYY query_token
                if entry_price <= 0:
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
                    logger.error(
                        "⚠️ 无法获取入场价格 ca=%s，标记待定，等 monitor 补价",
                        record.signal.token_address,
                    )
                    entry_price = -1.0
                pos = Position(
                    chain=record.signal.chain,
                    token_address=record.signal.token_address,
                    wallet_address=self.wallet_address,
                    entry_price=entry_price,
                    tip=self.tip,
                    buy_amount=record.buy_amount,
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
                    logger.error(
                        "⚠️ 仓位登记最终失败 ca=%s，标记待定强制登记: %s",
                        record.signal.token_address, e,
                    )
                    pos = Position(
                        chain=record.signal.chain,
                        token_address=record.signal.token_address,
                        wallet_address=self.wallet_address,
                        entry_price=-1.0,
                        tip=self.tip,
                        buy_amount=record.buy_amount,
                    )
                    self.position_monitor.add(pos)

    # ── 动态仓位管理 ─────────────────────────────────────────
    # 根据持仓数量、近期胜率动态调整买入金额
    TIERS = [
        (90, 2.0, "顶级"),
        (75, 1.5, "人上人"),
        (50, 1.0, "NPC"),
        (40, 0.5, "探路"),
    ]

    def _calc_buy_amount(self, score: int, sol_balance: float = 0.0, signal_count: int = 1) -> float:
        # ── 第1层：固定比例基础仓位 ─────────────────────────
        if sol_balance > 0:
            base = sol_balance * POSITION_RATIO
            base = max(POSITION_MIN_SOL, min(POSITION_MAX_SOL, base))
        else:
            base = self.buy_amount  # fallback

        # ── 第2层：评分分档倍数 ─────────────────────────────
        tier_multi = 1.0
        for min_score, multiplier, tier_name in self.TIERS:
            if score >= min_score:
                tier_multi = multiplier
                break

        # ── 第3层：信号强度加码 ─────────────────────────────
        signal_multi = SIGNAL_STRENGTH_MULTIPLIER.get(
            signal_count,
            SIGNAL_STRENGTH_MAX_MULTI if signal_count > 3 else 1.0,
        )

        amount = base * tier_multi * signal_multi

        # clamp 到安全范围（不能让 safety 拒绝好交易，应该截断到上限）
        from trading.safety import MAX_SINGLE_BUY_SOL
        amount = max(POSITION_MIN_SOL, min(MAX_SINGLE_BUY_SOL, amount))

        logger.info(
            "动态仓位: balance=%.3f base=%.4f × tier=%.1f(score=%d) × signal=%.1f(%d源) → %.4f SOL",
            sol_balance, base, tier_multi, score, signal_multi, signal_count, amount,
        )
        return amount

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
