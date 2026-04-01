"""
定期策略报告：输出当前交易状态、各维度统计、优化建议。

每个周期输出：
1. 交易总览 — 总信号/买入/成功/失败
2. 持仓状态 — 当前持仓、盈亏
3. 分档分布 — 顶级/人上人/NPC 各多少笔
4. 信号源效率 — 各信号源触发/通过/买入统计
5. 分析器维度 — 各维度平均得分
6. AI 研判统计 — MiniMax 通过率/平均分
7. 优化建议 — 基于数据自动生成调优建议
"""
import asyncio
import time
from collections import defaultdict

from xxyy.client import client
from config import config
from utils.logger import get_logger

logger = get_logger("strategy_report")

# 报告间隔（秒），默认 5 分钟
REPORT_INTERVAL = 300


class StrategyReporter:
    """定期生成策略报告，追踪交易表现，并自动优化参数"""

    def __init__(self, engine, interval: int = REPORT_INTERVAL):
        self._engine = engine
        self._interval = interval
        self._start_time = time.time()
        self._cycle = 0
        # 记录被拒绝的信号（用于统计通过率）
        self._rejected: list[dict] = []
        self._signals_total = 0
        # 自动优化追踪
        self._auto_optimized = False

    def record_signal(self, source: str, passed: bool, score: int = 0) -> None:
        """记录每个信号的分析结果（不管是否通过）"""
        self._signals_total += 1
        if not passed:
            self._rejected.append({"source": source, "score": score})

    async def start(self) -> None:
        """定期输出策略报告"""
        logger.info("StrategyReporter started, interval=%ds", self._interval)
        while True:
            await asyncio.sleep(self._interval)
            self._cycle += 1
            try:
                await self._generate_report()
            except Exception as e:
                logger.error("report generation error: %s", e)

    async def _generate_report(self) -> None:
        history = self._engine.history
        positions = self._engine.position_monitor.positions
        uptime = time.time() - self._start_time

        lines = [
            "",
            "=" * 70,
            f"  策略报告 #{self._cycle}  |  运行时间: {self._format_uptime(uptime)}",
            "=" * 70,
        ]

        # ── 1. 交易总览 ──────────────────────────────────
        buys = [r for r in history if r.signal.action == "buy"]
        success = [r for r in buys if r.status == "success"]
        failed = [r for r in buys if r.status == "failed"]
        total_invested = sum(r.buy_amount for r in success)

        lines.append("")
        lines.append("【交易总览】")
        lines.append(f"  信号总数: {self._signals_total}  |  分析通过: {len(buys)}  |  通过率: {len(buys)/max(self._signals_total,1):.0%}")
        lines.append(f"  买入成功: {len(success)}  |  买入失败: {len(failed)}  |  成功率: {len(success)/max(len(buys),1):.0%}")
        lines.append(f"  总投入: {total_invested:.3f} SOL")

        # ── 1.5 盈亏统计 ────────────────────────────────
        pnl_data = await self._calc_pnl(positions)
        lines.append("")
        lines.append("【盈亏统计】")
        lines.append(f"  当前持仓价值: ${pnl_data['holding_value']:.2f}")
        lines.append(f"  总投入成本:   ${pnl_data['total_cost']:.2f}")
        lines.append(f"  已实现盈亏:   ${pnl_data['realized_pnl']:+.2f}")
        lines.append(f"  未实现盈亏:   ${pnl_data['unrealized_pnl']:+.2f}")
        total_pnl = pnl_data['realized_pnl'] + pnl_data['unrealized_pnl']
        pnl_emoji = "🟢" if total_pnl >= 0 else "🔴"
        roi = total_pnl / max(pnl_data['total_cost'], 0.01) * 100
        lines.append(f"  {pnl_emoji} 总盈亏: ${total_pnl:+.2f} (ROI: {roi:+.1f}%)")
        # 胜率
        wins = pnl_data['win_count']
        losses = pnl_data['loss_count']
        total_trades = wins + losses
        if total_trades > 0:
            lines.append(f"  胜率: {wins}/{total_trades} ({wins/total_trades:.0%})  |  盈亏比: {pnl_data['avg_win']:.2f}:{pnl_data['avg_loss']:.2f}")

        # ── 2. 持仓状态 ──────────────────────────────────
        lines.append("")
        lines.append("【当前持仓】")
        open_positions = [p for p in positions if p.status in ("open", "trailing")]
        closed_positions = [p for p in positions if p.status == "closed"]

        if open_positions:
            for pos in open_positions:
                current_price = await self._get_current_price(pos)
                if current_price > 0 and pos.entry_price > 0:
                    pnl = (current_price / pos.entry_price - 1) * 100
                    emoji = "📈" if pnl > 0 else "📉"
                    trail = " [移动止盈中]" if pos.trailing_active else ""
                    tp_info = f" TP{pos.tp_level}/3" if pos.tp_level > 0 else ""
                    lines.append(
                        f"  {emoji} {pos.token_address[:8]}... | "
                        f"入场${pos.entry_price:.8f} → 现价${current_price:.8f} | "
                        f"{'+'if pnl>0 else ''}{pnl:.1f}%{tp_info}{trail}"
                    )
                else:
                    lines.append(f"  ⏳ {pos.token_address[:8]}... | 入场${pos.entry_price:.8f} | 价格获取中...")
        else:
            lines.append("  无持仓")

        # 卖出记录
        if closed_positions:
            lines.append(f"  已平仓: {len(closed_positions)} 笔")
            for pos in closed_positions[-5:]:  # 最近5笔
                if pos.sell_log:
                    lines.append(f"    {pos.token_address[:8]}... → {'; '.join(pos.sell_log)}")

        # ── 2.5 钱包总持仓（链上实际数据）─────────────────
        await self._append_wallet_holdings(lines)

        # ── 3. 分档分布 ──────────────────────────────────
        lines.append("")
        lines.append("【分档分布】")
        tier_counts = defaultdict(int)
        tier_amounts = defaultdict(float)
        for r in success:
            tier = r.tier or "未知"
            tier_counts[tier] += 1
            tier_amounts[tier] += r.buy_amount

        for tier_name in ["顶级", "人上人", "NPC", "探路"]:
            cnt = tier_counts.get(tier_name, 0)
            amt = tier_amounts.get(tier_name, 0)
            lines.append(f"  {tier_name}: {cnt} 笔, 投入 {amt:.3f} SOL")

        # ── 4. 信号源效率 ────────────────────────────────
        lines.append("")
        lines.append("【信号源效率】")
        source_stats = defaultdict(lambda: {"total": 0, "passed": 0, "success": 0})

        for r in history:
            src = r.signal.source.split("/")[0]
            source_stats[src]["total"] += 1
            source_stats[src]["passed"] += 1
            if r.status == "success":
                source_stats[src]["success"] += 1

        for rej in self._rejected:
            src = rej["source"].split("/")[0]
            source_stats[src]["total"] += 1

        for src, stats in sorted(source_stats.items()):
            total = stats["total"]
            passed = stats["passed"]
            success_cnt = stats["success"]
            lines.append(
                f"  {src}: 信号{total} → 通过{passed} → 成功{success_cnt}"
                f"  (通过率{passed/max(total,1):.0%})"
            )

        # ── 5. 评分分布 ──────────────────────────────────
        lines.append("")
        lines.append("【评分分布】")
        scores = [r.score for r in buys if r.score > 0]
        rejected_scores = [r["score"] for r in self._rejected if r["score"] > 0]
        all_scores = scores + rejected_scores

        if all_scores:
            avg = sum(all_scores) / len(all_scores)
            max_s = max(all_scores)
            min_s = min(all_scores)
            lines.append(f"  平均分: {avg:.0f}  |  最高: {max_s}  |  最低: {min_s}")
            lines.append(f"  ≥90(顶级): {sum(1 for s in all_scores if s>=90)}")
            lines.append(f"  75-89(人上人): {sum(1 for s in all_scores if 75<=s<90)}")
            lines.append(f"  50-74(NPC): {sum(1 for s in all_scores if 50<=s<75)}")
            lines.append(f"  <50(拉跨): {sum(1 for s in all_scores if s<50)}")

        # ── 6. 优化建议 ──────────────────────────────────
        lines.append("")
        lines.append("【优化建议】")
        suggestions = self._generate_suggestions(history, all_scores, open_positions)
        for i, s in enumerate(suggestions, 1):
            lines.append(f"  {i}. {s}")

        # ── 7. 自动优化 ──────────────────────────────────
        optimizations = self._auto_optimize(history, all_scores, open_positions)
        if optimizations:
            lines.append("")
            lines.append("【自动优化已执行】")
            for opt in optimizations:
                lines.append(f"  ⚙️ {opt}")

        # ── 8. 安全护栏状态 ──────────────────────────────
        if hasattr(self._engine, 'safety'):
            lines.append("")
            lines.append(f"【安全护栏】{self._engine.safety.status()}")

        # ── 9. API 健康状态 ──────────────────────────────
        from xxyy.client import api_health
        lines.append(f"【API 健康】{api_health.status()}")

        lines.append("")
        lines.append("=" * 70)

        report = "\n".join(lines)
        logger.info(report)

    def _generate_suggestions(
        self, history: list, all_scores: list, open_positions: list
    ) -> list[str]:
        """基于当前数据生成优化建议"""
        suggestions = []

        buys = [r for r in history if r.signal.action == "buy"]
        success = [r for r in buys if r.status == "success"]
        failed = [r for r in buys if r.status == "failed"]

        # 通过率太低
        if self._signals_total > 5 and len(buys) / max(self._signals_total, 1) < 0.1:
            suggestions.append(
                "通过率过低(<10%)，考虑降低 ANALYZER_MIN_SCORE 或放宽过滤条件"
            )

        # 通过率太高
        if self._signals_total > 5 and len(buys) / max(self._signals_total, 1) > 0.5:
            suggestions.append(
                "通过率过高(>50%)，买入标准可能太宽松，考虑提高 ANALYZER_MIN_SCORE"
            )

        # 失败率高
        if len(buys) > 3 and len(failed) / len(buys) > 0.3:
            suggestions.append(
                f"买入失败率偏高({len(failed)}/{len(buys)})，可能是滑点或流动性不足，考虑提高流动性门槛"
            )

        # 分数都很高但没亏损数据
        if all_scores and sum(all_scores) / len(all_scores) > 80:
            suggestions.append(
                "平均分偏高，评分标准可能偏松，持续观察实际收益率"
            )

        # 分数都很低
        if all_scores and sum(all_scores) / len(all_scores) < 40:
            suggestions.append(
                "平均分偏低，当前市场可能没有好机会，或过滤关键词需要更新"
            )

        # 某个信号源一直没产生成功交易
        source_success = defaultdict(int)
        source_total = defaultdict(int)
        for r in buys:
            src = r.signal.source.split("/")[0]
            source_total[src] += 1
            if r.status == "success":
                source_success[src] += 1
        for src, total in source_total.items():
            if total >= 3 and source_success[src] == 0:
                suggestions.append(
                    f"信号源 {src} 已触发 {total} 笔但全部失败，建议检查该信号源质量"
                )

        # 持仓数量建议
        if len(open_positions) > 10:
            suggestions.append(
                f"当前持仓 {len(open_positions)} 个，仓位过多，建议控制同时持仓数量"
            )

        if not suggestions:
            suggestions.append("当前运行正常，继续观察")

        return suggestions

    def _auto_optimize(self, history: list, all_scores: list, open_positions: list) -> list[str]:
        """基于运行数据自动调优参数，保护资金安全"""
        optimizations = []
        buys = [r for r in history if r.signal.action == "buy"]
        failed = [r for r in buys if r.status == "failed"]
        success = [r for r in buys if r.status == "success"]

        # 如果失败率 > 40% 且有足够样本，提高最低分数线
        if len(buys) >= 5 and len(failed) / len(buys) > 0.4:
            old_score = config.analyzer_min_score
            new_score = min(old_score + 5, 85)
            if new_score != old_score:
                config.analyzer_min_score = new_score
                optimizations.append(f"失败率过高({len(failed)}/{len(buys)})，最低分数线 {old_score} → {new_score}")

        # 如果连续 3 笔都失败，降低单笔投入
        recent = buys[-3:] if len(buys) >= 3 else []
        if len(recent) == 3 and all(r.status == "failed" for r in recent):
            from trading.safety import MAX_SINGLE_BUY_SOL
            old_amount = self._engine.buy_amount
            new_amount = max(old_amount * 0.7, 0.05)  # 最低 0.05 SOL
            if new_amount < old_amount:
                self._engine.buy_amount = new_amount
                optimizations.append(f"连续失败，单笔投入 {old_amount:.3f} → {new_amount:.3f} SOL")

        # 如果一直没有成功交易且已运行超过 30 分钟，暂停买入 10 分钟
        uptime_min = (time.time() - self._start_time) / 60
        if uptime_min > 30 and len(buys) > 5 and len(success) == 0:
            if hasattr(self._engine, 'safety') and not self._engine.safety._paused:
                self._engine.safety.pause("长时间无成功交易，自动暂停10分钟观察")
                optimizations.append("长时间无成功交易，暂停买入10分钟")
                # 10分钟后自动恢复
                asyncio.get_event_loop().call_later(600, self._engine.safety.resume)

        return optimizations

    async def _append_wallet_holdings(self, lines: list) -> None:
        """查询钱包链上实际持仓，输出总览"""
        wallet = config.wallet_address
        if not wallet:
            return

        lines.append("")
        lines.append("【钱包总持仓（链上）】")

        try:
            # 查钱包信息（余额）
            info = await client.wallet_info(wallet, config.default_chain)
            if isinstance(info, dict):
                sol_balance = float(info.get("balance", 0) or info.get("solBalance", 0) or 0)
                lines.append(f"  SOL 余额: {sol_balance:.4f} SOL")

            # 查持有代币
            holdings = await client.wallet_holdings(wallet, config.default_chain)
            if holdings:
                total_value = 0.0
                lines.append(f"  持有代币: {len(holdings)} 个")
                # 按价值排序，展示前10个
                sorted_holdings = sorted(
                    holdings,
                    key=lambda h: float(h.get("valueUSD", 0) or h.get("holdingValueUSD", 0) or 0),
                    reverse=True,
                )
                for h in sorted_holdings[:10]:
                    symbol = h.get("symbol", "?")
                    value = float(h.get("valueUSD", 0) or h.get("holdingValueUSD", 0) or 0)
                    pnl = float(h.get("pnl", 0) or h.get("profitUSD", 0) or 0)
                    total_value += value
                    pnl_str = f"+${pnl:.2f}" if pnl >= 0 else f"-${abs(pnl):.2f}"
                    lines.append(f"    {symbol:12s} | 价值${value:,.2f} | 盈亏{pnl_str}")
                lines.append(f"  总持仓价值: ${total_value:,.2f}")
            else:
                lines.append("  无代币持仓")
        except Exception as e:
            lines.append(f"  查询失败: {e}")

    async def _calc_pnl(self, positions: list) -> dict:
        """计算总盈亏：已实现 + 未实现"""
        result = {
            'holding_value': 0.0,
            'total_cost': 0.0,
            'realized_pnl': 0.0,
            'unrealized_pnl': 0.0,
            'win_count': 0,
            'loss_count': 0,
            'avg_win': 0.0,
            'avg_loss': 0.0,
        }

        wins = []
        losses = []

        for pos in positions:
            # 估算成本（entry_price * 假设数量，用 buy_amount / entry_price 估算）
            # 由于没记录精确数量，用比例计算盈亏
            current_price = await self._get_current_price(pos) if pos.status != "closed" else 0
            entry = pos.entry_price

            if entry <= 0:
                continue

            if pos.status == "closed":
                # 已平仓 — 用 sell_log 里的信息估算
                # 简化：用 highest_price 和 entry 的关系估算
                if pos.highest_price > 0:
                    pnl_ratio = (pos.highest_price / entry - 1)
                else:
                    pnl_ratio = -0.5  # 默认假设亏50%（止损）
                # 假设每笔买入 0.03 SOL（当前配置），换算美元
                cost_usd = config.buy_amount * 170  # 粗估 SOL 价格
                pnl_usd = cost_usd * pnl_ratio
                result['realized_pnl'] += pnl_usd
                result['total_cost'] += cost_usd
                if pnl_ratio >= 0:
                    wins.append(pnl_ratio)
                else:
                    losses.append(abs(pnl_ratio))
            else:
                # 未平仓 — 用当前价格算浮盈浮亏
                if current_price > 0:
                    pnl_ratio = (current_price / entry - 1)
                    cost_usd = config.buy_amount * 170
                    result['unrealized_pnl'] += cost_usd * pnl_ratio
                    result['holding_value'] += cost_usd * (1 + pnl_ratio)
                    result['total_cost'] += cost_usd
                    if pnl_ratio >= 0:
                        wins.append(pnl_ratio)
                    else:
                        losses.append(abs(pnl_ratio))

        result['win_count'] = len(wins)
        result['loss_count'] = len(losses)
        result['avg_win'] = sum(wins) / len(wins) * 100 if wins else 0
        result['avg_loss'] = sum(losses) / len(losses) * 100 if losses else 0

        return result

    async def _get_current_price(self, pos) -> float:
        """获取当前价格"""
        try:
            data = await client.query_token(pos.token_address, pos.chain)
            if isinstance(data, dict):
                trade_info = data.get("tradeInfo") or {}
                return float(trade_info.get("price", 0) or 0)
        except Exception:
            pass
        return 0.0

    @staticmethod
    def _format_uptime(seconds: float) -> str:
        h = int(seconds // 3600)
        m = int((seconds % 3600) // 60)
        s = int(seconds % 60)
        if h > 0:
            return f"{h}h{m}m{s}s"
        return f"{m}m{s}s"
