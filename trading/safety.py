"""
安全护栏 — 保护资金不会一夜亏完

规则：
1. 单日最大亏损限额：超过则暂停所有买入
2. 最大同时持仓数：防止资金分散太多
3. 连续失败冷却：连续N笔失败后暂停一段时间
4. 最低余额保护：SOL余额低于阈值停止买入
5. 单笔最大投入：防止单笔下注过大
"""
import time
from utils.logger import get_logger

logger = get_logger("safety")

# ── 安全参数 ─────────────────────────────────────────────
MAX_DAILY_LOSS_SOL = 1.5          # 单日最大亏损 1.5 SOL（样本多了损耗会增加）
MAX_CONCURRENT_POSITIONS = 20      # 最多同时持仓 20 个（广撒网核心）
MAX_CONSECUTIVE_FAILURES = 5       # 连续 5 笔失败后冷却（样本多，容忍度提高）
FAILURE_COOLDOWN_SECONDS = 120     # 失败冷却 2 分钟（快速恢复）
MIN_SOL_BALANCE = 0.3             # 余额低于 0.3 SOL 停止买入
MAX_SINGLE_BUY_SOL = 0.1          # 单笔最大投入 0.1 SOL（严控单笔风险）

# ── 连续亏损冷却期 ─────────────────────────────────────────
# 连续 N 笔卖出都是亏损（multiplier < 1.0）→ 暂停买入一段时间
# 和 FAILURE_COOLDOWN 不同：failure 是交易执行失败，loss 是卖出亏损
CONSECUTIVE_LOSS_THRESHOLD = 5     # 连续 5 笔亏损卖出触发冷却
LOSS_COOLDOWN_SECONDS = 600        # 冷却 10 分钟（市场可能整体不行，等等再说）


class SafetyGuard:
    """资金安全护栏"""

    def __init__(self):
        self._daily_invested = 0.0     # 今日总投入
        self._daily_loss = 0.0         # 今日总亏损（估算）
        self._consecutive_failures = 0  # 连续失败次数
        self._last_failure_time = 0.0
        self._paused = False
        self._pause_reason = ""
        self._day_start = time.time()
        # 连续亏损冷却
        self._consecutive_losses = 0    # 连续亏损卖出次数
        self._last_loss_time = 0.0
        self._loss_cooldown_active = False

    def can_buy(self, amount: float, sol_balance: float, open_positions: int) -> tuple[bool, str]:
        """
        检查是否允许买入。
        返回 (是否允许, 原因)
        """
        # 每天重置计数器
        if time.time() - self._day_start > 86400:
            self._reset_daily()

        # 1. 手动暂停
        if self._paused:
            return False, f"已暂停: {self._pause_reason}"

        # 2. 单笔金额检查
        if amount > MAX_SINGLE_BUY_SOL:
            return False, f"单笔{amount:.3f}SOL超过上限{MAX_SINGLE_BUY_SOL}SOL"

        # 3. 余额保护
        if sol_balance < MIN_SOL_BALANCE:
            return False, f"余额{sol_balance:.3f}SOL低于安全线{MIN_SOL_BALANCE}SOL"

        # 4. 余额不够这笔交易
        if sol_balance < amount + 0.01:  # 留0.01 SOL给gas
            return False, f"余额{sol_balance:.3f}SOL不足以支付{amount:.3f}SOL+gas"

        # 5. 持仓数量限制
        if open_positions >= MAX_CONCURRENT_POSITIONS:
            return False, f"持仓{open_positions}个已达上限{MAX_CONCURRENT_POSITIONS}"

        # 6. 单日亏损限制
        if self._daily_loss >= MAX_DAILY_LOSS_SOL:
            return False, f"今日亏损{self._daily_loss:.3f}SOL已达上限{MAX_DAILY_LOSS_SOL}SOL"

        # 7. 连续失败冷却
        if self._consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
            elapsed = time.time() - self._last_failure_time
            if elapsed < FAILURE_COOLDOWN_SECONDS:
                remaining = int(FAILURE_COOLDOWN_SECONDS - elapsed)
                return False, f"连续{self._consecutive_failures}笔失败，冷却中({remaining}s)"
            else:
                # 冷却结束，重置
                self._consecutive_failures = 0

        # 8. 连续亏损冷却（市场不行就停手）
        if self._consecutive_losses >= CONSECUTIVE_LOSS_THRESHOLD:
            elapsed = time.time() - self._last_loss_time
            if elapsed < LOSS_COOLDOWN_SECONDS:
                remaining = int(LOSS_COOLDOWN_SECONDS - elapsed)
                if not self._loss_cooldown_active:
                    self._loss_cooldown_active = True
                    logger.warning(
                        "🧊 连续%d笔亏损卖出，进入%d秒冷却期（市场可能整体不行）",
                        self._consecutive_losses, LOSS_COOLDOWN_SECONDS,
                    )
                return False, f"连续{self._consecutive_losses}笔亏损，冷却中({remaining}s)"
            else:
                logger.info("🧊 亏损冷却期结束，恢复买入")
                self._consecutive_losses = 0
                self._loss_cooldown_active = False

        return True, "ok"

    def record_buy(self, amount: float) -> None:
        """记录一笔买入"""
        self._daily_invested += amount
        logger.info("安全统计: 今日投入+%.3f (累计%.3f SOL)", amount, self._daily_invested)

    def record_success(self) -> None:
        """记录一笔成功交易"""
        self._consecutive_failures = 0

    def record_failure(self) -> None:
        """记录一笔失败交易"""
        self._consecutive_failures += 1
        self._last_failure_time = time.time()
        if self._consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
            logger.warning(
                "⚠️ 连续%d笔失败，进入%d秒冷却期",
                self._consecutive_failures, FAILURE_COOLDOWN_SECONDS,
            )

    def record_loss(self, loss_sol: float) -> None:
        """记录亏损金额"""
        self._daily_loss += loss_sol
        self._consecutive_losses += 1
        self._last_loss_time = time.time()
        logger.warning(
            "安全统计: 今日亏损+%.3f (累计%.3f SOL) 连续亏损:%d笔",
            loss_sol, self._daily_loss, self._consecutive_losses,
        )
        if self._daily_loss >= MAX_DAILY_LOSS_SOL:
            logger.warning("🛑 今日亏损已达上限 %.3f SOL，暂停买入", self._daily_loss)

    def record_profit(self) -> None:
        """记录一笔盈利卖出，重置连续亏损计数"""
        if self._consecutive_losses > 0:
            logger.info("连续亏损计数重置（盈利卖出）之前连续亏损:%d笔", self._consecutive_losses)
        self._consecutive_losses = 0
        self._loss_cooldown_active = False

    def pause(self, reason: str) -> None:
        self._paused = True
        self._pause_reason = reason
        logger.warning("🛑 安全暂停: %s", reason)

    def resume(self) -> None:
        self._paused = False
        self._pause_reason = ""
        logger.info("✅ 安全恢复，继续交易")

    def _reset_daily(self) -> None:
        logger.info("日切重置: 昨日投入%.3f 亏损%.3f", self._daily_invested, self._daily_loss)
        self._daily_invested = 0.0
        self._daily_loss = 0.0
        self._day_start = time.time()

    def status(self) -> str:
        return (
            f"今日投入:{self._daily_invested:.3f}SOL "
            f"今日亏损:{self._daily_loss:.3f}SOL "
            f"连续失败:{self._consecutive_failures} "
            f"暂停:{self._paused}"
        )
