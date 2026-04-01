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
from llm.minimax_client import minimax
from trading.holding_analyzer import holding_analyzer
from config import config
from utils.logger import get_logger

logger = get_logger(__name__)

PRICE_CHECK_INTERVAL = 15  # 每 15 秒查一轮价格（meme 币波动快，要快速捕捉止盈点）

# ── 止盈阶梯 ─────────────────────────────────────────────
# 核心逻辑：土狗赚钱靠一笔 10x-100x 覆盖所有亏损
# 不要急着跑，让利润奔跑！
# (倍数阈值, 卖出百分比, 描述)
TAKE_PROFIT_LEVELS = [
    (2.0,  30, "2x翻倍减仓30%"),    # 翻倍只减30%，保留大头
    (5.0,  20, "5x锁一点利润"),     # 5倍只卖20%
    (10.0, 20, "10x再锁一点"),      # 10倍再卖20%
    (50.0, 30, "50x大肉落袋"),      # 50倍卖30%
    (100.0, 50, "100x半仓落袋"),    # 百倍卖一半，剩下永远留着
]

# ── 移动止盈 ─────────────────────────────────────────────
TRAILING_STOP_DROP = 0.30       # 从最高点回撤30%才触发（给波动空间）
TRAILING_SELL_PERCENT = 100     # 移动止盈触发后全部卖出

# ── 时间止损 ─────────────────────────────────────────────
TIME_STOP_MINUTES = 45          # 给45分钟发酵期，meme币需要时间
TIME_STOP_MIN_MULTIPLIER = 1.0  # 45分钟不亏就留着（不要求涨）
TIME_STOP_SELL_PERCENT = 100

# ── 动量衰退 ─────────────────────────────────────────────
VOLUME_DROP_THRESHOLD = 0.3     # 成交量降至30%以下才触发（从50%放宽）
HOLDER_DROP_THRESHOLD = 0.8     # 持仓人降至80%以下（从90%放宽）

# ── 破位止损 ─────────────────────────────────────────────
CRASH_STOP_MULTIPLIER = 0.50    # 跌破入场价50%止损（从35%收紧，及早割肉减少损失）
CRASH_STOP_SELL_PERCENT = 100

# ── 死币自动清理 ─────────────────────────────────────────
DEAD_COIN_MC = 1000             # 市值低于 $1k
DEAD_COIN_HOLDERS = 3           # 持仓人低于 3
DEAD_COIN_VOLUME = 30           # 1h 成交量低于 $30

# ── AI 持仓分析 ──────────────────────────────────────────
AI_ANALYSIS_INTERVAL = 1800     # 每 30 分钟做一次 AI 分析（从20min放宽，减少误杀）
AI_SELL_CONFIDENCE = 92         # AI 说 SELL 且 confidence >= 92 才执行（从85提高，减少误杀）
AI_FIRST_ANALYSIS_DELAY = 1800  # 买入后 30 分钟才做第一次 AI 分析（从15min延长，给足发酵时间）

# ── 链上同步 ────────────────────────────────────────────
ONCHAIN_SYNC_INTERVAL = 120     # 每 2 分钟同步一次链上持仓

# ── 墓地复活扫描 ────────────────────────────────────────
GRAVEYARD_CHECK_INTERVAL = 600  # 每 10 分钟扫描一次已卖出的币
GRAVEYARD_REVIVE_MC = 10000     # 市值恢复到 $10k 以上算复活
GRAVEYARD_REVIVE_VOL = 2000     # 1h 成交量恢复到 $2k 以上
GRAVEYARD_REVIVE_HOLDERS = 20   # 持仓人恢复到 20 以上
GRAVEYARD_MAX_AGE = 86400       # 只追踪 24 小时内卖出的币


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
    # 实际买入金额（用于准确计算亏损）
    buy_amount: float = 0.0
    # 卖出记录
    sell_log: list[str] = field(default_factory=list)
    # AI 分析
    last_ai_analysis: float = 0.0   # 上次 AI 分析时间


class PositionMonitor:
    def __init__(self, swap_lock: asyncio.Semaphore | None = None):
        self._positions: list[Position] = []
        self._swap_lock = swap_lock or asyncio.Semaphore(1)
        self._running = False
        self._safety = None  # 由 engine 注入
        # 墓地：已卖出的币 {ca: {"sell_time": float, "sell_price": float, "chain": str}}
        self._graveyard: dict[str, dict] = {}
        self._last_graveyard_check: float = 0.0
        self._on_signal = None  # 由 engine 注入，用于复活信号

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
            "止盈阶梯: %s | 移动止盈回撤: %d%% | 时间止损: %dmin | 破位止损: %d%% | AI分析: %ds",
            [f"{m}x→{p}%" for m, p, _ in TAKE_PROFIT_LEVELS],
            int(TRAILING_STOP_DROP * 100),
            TIME_STOP_MINUTES,
            int(CRASH_STOP_MULTIPLIER * 100),
            AI_ANALYSIS_INTERVAL,
        )

        # 启动时同步链上持仓
        await self._sync_onchain_holdings()
        self._last_sync_time = time.time()

        while self._running:
            await self._check_all()

            # 定期同步链上持仓（清理已卖出的）
            if time.time() - self._last_sync_time >= ONCHAIN_SYNC_INTERVAL:
                await self._sync_onchain_holdings()
                self._last_sync_time = time.time()

            # 定期扫描墓地（已卖出的币有没有复活）
            if time.time() - self._last_graveyard_check >= GRAVEYARD_CHECK_INTERVAL:
                await self._check_graveyard()
                self._last_graveyard_check = time.time()

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
            # DexScreener 无 rate limit，1秒间隔够了
            await asyncio.sleep(1)

    async def _fetch_real_pnl(self, pos: Position) -> dict | None:
        """尝试获取真实 PNL 数据，失败返回 None"""
        try:
            pnl_data = await client.pnl(
                wallet_address=pos.wallet_address,
                token_address=pos.token_address,
                chain=pos.chain,
            )
            if pnl_data and isinstance(pnl_data, dict) and pnl_data.get("buy"):
                return pnl_data
        except Exception as e:
            logger.debug("PNL查询失败 ca=%s: %s", pos.token_address[:12], e)
        return None

    async def _get_dexscreener_price_sol(self, token_address: str) -> float:
        """从 DexScreener 获取 SOL 计价的实时价格（meme 币用 SOL 计价才准）"""
        try:
            import httpx
            async with httpx.AsyncClient(timeout=8.0, verify=False) as http:
                resp = await http.get(
                    f"https://api.dexscreener.com/latest/dex/tokens/{token_address}"
                )
                if resp.status_code == 200:
                    pairs = resp.json().get("pairs") or []
                    if pairs:
                        # 优先用 SOL 计价（priceNative），和 XXYY P&L 一致
                        native = float(pairs[0].get("priceNative", 0) or 0)
                        if native > 0:
                            return native
                        # fallback: 用 USD 价格 / SOL 价格 算出 SOL 计价
                        usd = float(pairs[0].get("priceUsd", 0) or 0)
                        if usd > 0:
                            return usd  # 退化为 USD（不理想但总比没有好）
        except Exception:
            pass
        return 0.0

    async def _check_position(self, pos: Position) -> None:
        # 用 DexScreener SOL 计价价格（和 XXYY P&L 一致）
        current_price = await self._get_dexscreener_price_sol(pos.token_address)

        data = await client.query_token(pos.token_address, pos.chain)
        if not isinstance(data, dict):
            data = {}
        trade_info = data.get("tradeInfo") or {}

        if current_price <= 0:
            current_price = float(trade_info.get("price", 0) or 0)
        if current_price <= 0:
            return

        # 待定价格补偿
        if pos.entry_price < 0:
            pos.entry_price = current_price
            logger.info("🔧 补偿入场价格 ca=%s entry=%.10f", pos.token_address, current_price)
            self.save_positions()
            return

        multiplier = current_price / pos.entry_price

        # 记录初始动量数据
        if not pos.volume_recorded:
            vol = float(trade_info.get("hourTradeVolume", 0) or 0)
            holders = int(trade_info.get("holder", 0) or 0)
            # API 返回 0 时不标记已记录，下次重试
            if vol > 0 or holders > 0:
                pos.initial_volume = vol
                pos.initial_holders = holders
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

        if pos.status == "closed":
            return

        # ── 策略6: 死币自动清理 ──────────────────────────
        await self._check_dead_coin(pos, trade_info, multiplier)

        if pos.status == "closed":
            return

        # ── 策略7: AI 持续分析（MiniMax M2.7）─────────────
        await self._check_ai_holding(pos, data, trade_info, multiplier)

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
                success = await self._sell(pos, sell_pct, f"止盈[{desc}]", multiplier)
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

        # 移动止盈只在盈利 1.5x 以上才生效，防止刚 TP 就被小波动触发清仓
        if multiplier < 1.5:
            return

        drop_from_high = 1 - (current_price / pos.highest_price)

        if drop_from_high >= TRAILING_STOP_DROP:
            logger.info(
                "📉 移动止盈触发 ca=%s 最高$%.8f → 现价$%.8f 回撤%.1f%% x=%.2f",
                pos.token_address, pos.highest_price, current_price,
                drop_from_high * 100, multiplier,
            )
            success = await self._sell(pos, TRAILING_SELL_PERCENT, "移动止盈", multiplier)
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
            success = await self._sell(pos, TIME_STOP_SELL_PERCENT, "时间止损", multiplier)
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
            success = await self._sell(pos, 100, "动量衰退-成交量", multiplier)
            if success:
                pos.sell_log.append(f"动量衰退 vol={volume_ratio*100:.0f}% x={multiplier:.2f}")
                pos.status = "closed"
            return

        if holder_ratio < HOLDER_DROP_THRESHOLD and pos.initial_holders >= 20:
            logger.info(
                "📊 动量衰退(持仓人) ca=%s holders从%d降至%d x=%.2f 清仓",
                pos.token_address, pos.initial_holders, current_holders, multiplier,
            )
            success = await self._sell(pos, 100, "动量衰退-持仓人", multiplier)
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
            success = await self._sell(pos, CRASH_STOP_SELL_PERCENT, "破位止损", multiplier)
            if success:
                pos.sell_log.append(f"破位止损 x={multiplier:.2f}")
                pos.status = "closed"

    # ── 策略6: 死币自动清理 ────────────────────────────────

    async def _check_dead_coin(self, pos: Position, trade_info: dict, multiplier: float) -> None:
        """市值/持仓人/成交量全部低于阈值 → 死币，自动清仓回收残值"""
        mc = float(trade_info.get("marketCapUsd", 0) or trade_info.get("marketCapUSD", 0) or 0)
        holders = int(trade_info.get("holder", 0) or 0)
        vol = float(trade_info.get("hourTradeVolume", 0) or 0)

        is_dead = mc < DEAD_COIN_MC and holders < DEAD_COIN_HOLDERS and vol < DEAD_COIN_VOLUME

        if not is_dead:
            return

        logger.info(
            "💀 死币清理 ca=%s mc=$%.0f holders=%d vol=$%.0f x=%.2f",
            pos.token_address, mc, holders, vol, multiplier,
        )
        success = await self._sell(pos, 100, "死币清理", multiplier)
        if success:
            pos.sell_log.append(f"死币清理 mc=${mc:.0f} holders={holders} x={multiplier:.2f}")
            pos.status = "closed"

    # ── 策略7: AI 深度持仓分析（MiniMax M2.7）───────────────

    async def _check_ai_holding(
        self, pos: Position, data: dict, trade_info: dict, multiplier: float
    ) -> None:
        """
        定期用 MiniMax M2.7 深度分析持仓。
        收集多维数据（趋势、链上行为、同类对比、社交热度），交给 AI 做最终研判。
        """
        if not minimax.available:
            return

        # 盈利中的币不需要 AI 判断卖出（让止盈阶梯和移动止盈来管）
        # AI 只负责判断亏损/横盘的币是否还有希望
        if multiplier >= 1.5:
            return

        now = time.time()

        # 买入后一段时间内不做 AI 分析，给币发酵时间
        age = now - pos.entry_time
        if age < AI_FIRST_ANALYSIS_DELAY:
            return

        if now - pos.last_ai_analysis < AI_ANALYSIS_INTERVAL:
            return

        pos.last_ai_analysis = now

        try:
            # 用 HoldingAnalyzer 收集完整诊断数据
            diag = await holding_analyzer.analyze(
                token_address=pos.token_address,
                chain=pos.chain,
                entry_price=pos.entry_price,
                entry_time=pos.entry_time,
                initial_holders=pos.initial_holders,
                initial_volume=pos.initial_volume,
            )

            # 交给 MiniMax 深度分析
            ai_result = await holding_analyzer.get_ai_verdict(diag)
        except Exception as e:
            logger.error("AI 深度分析异常 ca=%s: %s", pos.token_address, e)
            return

        action = ai_result.get("action", "HOLD")
        confidence = ai_result.get("confidence", 0)
        reason = ai_result.get("reason", "无")

        logger.info(
            "🤖 AI深度分析 ca=%s %s action=%s confidence=%d x=%.2f\n"
            "   趋势: 持仓人=%s 成交量=%s 价格=%s\n"
            "   聪明钱: %s | 社交: %s\n"
            "   理由: %s",
            pos.token_address, diag.name, action, confidence, multiplier,
            diag.holder_trend, diag.volume_trend, diag.price_trend,
            diag.smart_money.net_action, diag.social.social_score,
            reason,
        )

        if action == "SELL" and confidence >= AI_SELL_CONFIDENCE:
            logger.info(
                "🤖 AI建议卖出 ca=%s confidence=%d%% → 执行清仓",
                pos.token_address, confidence,
            )
            success = await self._sell(pos, 100, f"AI深度分析-{reason[:20]}", multiplier)
            if success:
                pos.sell_log.append(f"AI深度卖出 confidence={confidence}% reason={reason}")
                pos.status = "closed"

    # ── 卖出执行 ─────────────────────────────────────────

    SELL_RETRY_MAX = 3
    SELL_RETRY_DELAY = 3

    async def _sell(self, pos: Position, sell_percent: int, reason: str, multiplier: float = 1.0) -> bool:
        """执行卖出，失败自动重试，返回是否成功"""
        for attempt in range(1, self.SELL_RETRY_MAX + 1):
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
                # API 调用失败 = 交易还没提交到链上，可以安全重试
                logger.error("卖出API失败[%s] ca=%s attempt=%d/%d error=%s",
                             reason, pos.token_address, attempt, self.SELL_RETRY_MAX, e)
                if attempt < self.SELL_RETRY_MAX:
                    await asyncio.sleep(self.SELL_RETRY_DELAY)
                    continue
                return False

            logger.info("卖出提交[%s] txId=%s ca=%s %d%%", reason, tx_id, pos.token_address, sell_percent)

            # swap 已提交到链上，查询结果
            try:
                result = await client.wait_trade(tx_id)
            except Exception as e:
                # 网络异常无法确认链上状态，不能重试（可能已成功），保守处理
                logger.warning(
                    "⚠️ 卖出状态查询异常[%s] ca=%s txId=%s: %s，不重试避免重复卖出",
                    reason, pos.token_address, tx_id, e,
                )
                return False
            raw_status = result.get("status") if isinstance(result, dict) else None

            if raw_status == 2:
                logger.info(
                    "✅ 卖出成功[%s] ca=%s %d%% txId=%s",
                    reason, pos.token_address, sell_percent, tx_id,
                )
                # 记录盈亏到安全护栏
                if self._safety:
                    if multiplier < 1.0:
                        # 亏损：尝试用真实 PNL API 获取准确亏损
                        real_loss = None
                        try:
                            pnl_data = await self._fetch_real_pnl(pos)
                            if pnl_data and pnl_data.get("pnl") is not None:
                                pnl_val = float(pnl_data["pnl"])
                                if pnl_val < 0:
                                    real_loss = abs(pnl_val) * (sell_percent / 100.0)
                                    logger.info("PNL真实亏损 ca=%s pnl=%.4f SOL", pos.token_address[:12], real_loss)
                        except Exception:
                            pass
                        actual_buy = pos.buy_amount if pos.buy_amount > 0 else config.buy_amount
                        loss = real_loss if real_loss is not None else actual_buy * (1.0 - multiplier) * (sell_percent / 100.0)
                        self._safety.record_loss(loss)
                    else:
                        # 盈利：重置连续亏损计数
                        self._safety.record_profit()
                if sell_percent == 100:
                    self._graveyard[pos.token_address] = {
                        "sell_time": time.time(),
                        "sell_price": pos.highest_price or pos.entry_price,
                        "chain": pos.chain,
                        "reason": reason,
                    }
                    logger.info("🪦 加入墓地监控 ca=%s reason=%s", pos.token_address[:16], reason)
                self.save_positions()
                self._log_sell_signal(pos, sell_percent, reason, tx_id)
                return True
            elif raw_status == 3:
                # 链上明确失败（滑点等），可以安全重试
                logger.error(
                    "❌ 卖出链上失败[%s] ca=%s attempt=%d/%d txId=%s",
                    reason, pos.token_address, attempt, self.SELL_RETRY_MAX, tx_id,
                )
                if attempt < self.SELL_RETRY_MAX:
                    await asyncio.sleep(self.SELL_RETRY_DELAY)
                    continue
                return False
            else:
                # 状态未知（None/pending）= 交易可能已在链上，不能重试，避免重复卖出
                logger.warning(
                    "⚠️ 卖出状态未知[%s] ca=%s txId=%s status=%s，不重试避免重复卖出",
                    reason, pos.token_address, tx_id, raw_status,
                )
                return False

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
        """保存持仓到文件，重启后可恢复。先写临时文件再原子替换，防止损坏。"""
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
                "buy_amount": p.buy_amount,
            })
        # 先写临时文件，成功后再原子替换，避免写一半崩溃导致 JSON 损坏
        tmp_file = self.SAVE_FILE + ".tmp"
        try:
            with open(tmp_file, "w") as f:
                json.dump(data, f, indent=2)
            os.replace(tmp_file, self.SAVE_FILE)  # 原子操作
        except Exception as e:
            logger.error("保存持仓失败: %s", e)
            return
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
                # 恢复持仓时保留原始 entry_time（文件里有就用文件的，没有就用当前时间）
                saved_entry_time = d.get("entry_time", 0)
                # 合法性检查：entry_time 不能在未来，也不能太古老（>7天视为异常）
                now = time.time()
                if saved_entry_time <= 0 or saved_entry_time > now or (now - saved_entry_time) > 7 * 86400:
                    saved_entry_time = now
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
                    entry_time=saved_entry_time,
                    buy_amount=d.get("buy_amount", 0.0),
                )
                self._positions.append(pos)
                count += 1
                logger.info("恢复持仓 ca=%s entry=$%.8f status=%s", pos.token_address, pos.entry_price, pos.status)
            return count
        except Exception as e:
            logger.error("加载持仓文件失败: %s", e)
            return 0

    # ── 链上持仓同步 ─────────────────────────────────────

    async def _check_graveyard(self) -> None:
        """扫描墓地里的币，如果复活了就触发重新买入信号"""
        if not self._graveyard:
            return

        now = time.time()
        revived = []
        expired = []

        for ca, info in list(self._graveyard.items()):
            # 超过 24 小时的不再追踪
            if now - info["sell_time"] > GRAVEYARD_MAX_AGE:
                expired.append(ca)
                continue

            # 已经重新持仓的不查
            if any(p.token_address == ca and p.status != "closed" for p in self._positions):
                expired.append(ca)
                continue

            try:
                data = await client.query_token(ca, info["chain"])
                if not isinstance(data, dict):
                    continue

                ti = data.get("tradeInfo") or {}
                mc = float(ti.get("marketCapUsd", 0) or 0)
                vol = float(ti.get("hourTradeVolume", 0) or 0)
                holders = int(ti.get("holder", 0) or 0)

                if mc >= GRAVEYARD_REVIVE_MC and vol >= GRAVEYARD_REVIVE_VOL and holders >= GRAVEYARD_REVIVE_HOLDERS:
                    symbol = data.get("baseSymbol") or "?"
                    logger.info(
                        "🧟 墓地复活！ %s ca=%s mc=$%.0f vol=$%.0f holders=%d (卖出原因: %s)",
                        symbol, ca[:16], mc, vol, holders, info.get("reason", "?"),
                    )
                    revived.append(ca)

                    # 触发买入信号
                    if self._on_signal:
                        from signals.base import TradeSignal
                        signal = TradeSignal(
                            chain=info["chain"],
                            token_address=ca,
                            action="buy",
                            source="graveyard_revive",
                            reason=f"墓地复活 {symbol} mc=${mc:.0f} vol=${vol:.0f} holders={holders}",
                        )
                        if asyncio.iscoroutinefunction(self._on_signal):
                            await self._on_signal(signal)
                        else:
                            self._on_signal(signal)
            except Exception as e:
                logger.debug("graveyard check ca=%s error: %s", ca[:12], e)

            await asyncio.sleep(3)  # 避免 429

        # 清理
        for ca in expired + revived:
            self._graveyard.pop(ca, None)

        if expired:
            logger.debug("墓地清理 %d 个过期条目", len(expired))

    async def _sync_onchain_holdings(self) -> None:
        """
        链上持仓同步（双重机制）：
        1. 用 XXYY wallet/info 逐个检查本地持仓是否还存在（清理已卖出）
        2. 用 Solana RPC 扫描钱包全量代币（发现未跟踪的新持仓）
        """
        wallet = config.wallet_address
        chain = config.default_chain
        if not wallet:
            return

        try:
            # ── 第1步：清理已卖出的持仓（XXYY API，精确）──
            stale = []
            for pos in self._positions:
                if pos.status == "closed":
                    continue
                try:
                    info = await client.wallet_info(wallet, chain, token_address=pos.token_address)
                    token_bal = (info or {}).get("tokenBalance") or {}
                    ui_amount = float(token_bal.get("uiAmount", 0) or 0)
                    if ui_amount <= 0:
                        stale.append(pos)
                except Exception as e:
                    logger.debug("sync check ca=%s error: %s", pos.token_address[:12], e)
                await asyncio.sleep(3)

            if stale:
                for p in stale:
                    logger.info("清理已卖出持仓 ca=%s (链上已无余额)", p.token_address[:16])
                    p.status = "closed"
                    # 记录到墓地（方便复活扫描）
                    self._graveyard[p.token_address] = {
                        "sell_time": time.time(),
                        "sell_price": p.highest_price or p.entry_price,
                        "chain": p.chain,
                        "reason": "链上同步清理",
                    }
                    # 记录盈亏到安全护栏
                    if self._safety and p.entry_price > 0 and p.buy_amount > 0:
                        self._safety.record_loss(p.buy_amount)
                self._positions = [p for p in self._positions if p.status != "closed"]
                logger.info("清理完成，移除 %d 个已卖出持仓", len(stale))

            # ── 第2步：发现新持仓（Solana RPC 扫描全量代币）──
            new_count = 0
            known_cas = {p.token_address for p in self._positions}
            try:
                import httpx
                rpc_url = "https://api.mainnet-beta.solana.com"
                onchain_mints: set[str] = set()

                for program_id in [
                    "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb",
                    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
                ]:
                    async with httpx.AsyncClient(timeout=15.0, verify=False) as http:
                        resp = await http.post(rpc_url, json={
                            "jsonrpc": "2.0", "id": 1,
                            "method": "getTokenAccountsByOwner",
                            "params": [
                                wallet,
                                {"programId": program_id},
                                {"encoding": "jsonParsed"},
                            ],
                        })
                        data = resp.json()
                        accounts = data.get("result", {}).get("value", [])
                        for acc in accounts:
                            info = acc.get("account", {}).get("data", {}).get("parsed", {}).get("info", {})
                            mint = info.get("mint", "")
                            ui_amount = info.get("tokenAmount", {}).get("uiAmount", 0)
                            if mint and ui_amount and ui_amount > 0:
                                onchain_mints.add(mint)

                for mint in onchain_mints:
                    if mint in known_cas:
                        continue
                    try:
                        # 用 DexScreener SOL 计价（和止盈逻辑一致）
                        price = await self._get_dexscreener_price_sol(mint)
                        if price <= 0:
                            # fallback: XXYY query_token
                            token_data = await client.query_token(mint, chain)
                            ti = (token_data or {}).get("tradeInfo") or {} if isinstance(token_data, dict) else {}
                            price = float(ti.get("price", 0) or 0)
                        if price <= 0:
                            continue
                        name = "?"
                        try:
                            td = await client.query_token(mint, chain)
                            name = (td or {}).get("baseSymbol") or "?"
                        except Exception:
                            pass
                        pos = Position(
                            chain=chain,
                            token_address=mint,
                            wallet_address=wallet,
                            entry_price=price,
                            tip=config.tip,
                            entry_time=time.time(),
                        )
                        self._positions.append(pos)
                        known_cas.add(mint)
                        new_count += 1
                        logger.info("链上同步新持仓 %s ca=%s price=%.10f(SOL)", name, mint[:12], price)
                    except Exception as e:
                        logger.debug("sync token query error ca=%s: %s", mint[:12], e)
                    await asyncio.sleep(3)
            except Exception as e:
                logger.debug("RPC 扫描新持仓失败（不影响清理）: %s", e)

            changed = len(stale) > 0 or new_count > 0
            if changed:
                self.save_positions()
                logger.info("链上同步完成，移除 %d / 新增 %d", len(stale), new_count)
            else:
                logger.info("链上同步完成，无变动")

        except Exception as e:
            logger.error("链上持仓同步失败: %s", e)

    @property
    def positions(self) -> list[Position]:
        return self._positions
