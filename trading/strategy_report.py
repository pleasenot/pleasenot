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
    """定期生成策略报告，追踪交易表现"""

    def __init__(self, engine, interval: int = REPORT_INTERVAL):
        self._engine = engine
        self._interval = interval
        self._start_time = time.time()
        self._cycle = 0
        # 记录被拒绝的信号（用于统计通过率）
        self._rejected: list[dict] = []
        self._signals_total = 0

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

        # ── 2. 持仓状态 ──────────────────────────────────
        lines.append("")
        lines.append("【当前持仓】")
        open_positions = [p for p in positions if p.status == "open"]
        tp_positions = [p for p in positions if p.status in ("tp_triggered", "closed")]

        if open_positions:
            for pos in open_positions:
                current_price = await self._get_current_price(pos)
                if current_price > 0 and pos.entry_price > 0:
                    pnl = (current_price / pos.entry_price - 1) * 100
                    emoji = "📈" if pnl > 0 else "📉"
                    lines.append(
                        f"  {emoji} {pos.token_address[:8]}... | "
                        f"入场${pos.entry_price:.8f} → 现价${current_price:.8f} | "
                        f"{'+'if pnl>0 else ''}{pnl:.1f}%"
                    )
                else:
                    lines.append(f"  ⏳ {pos.token_address[:8]}... | 入场${pos.entry_price:.8f} | 价格获取中...")
        else:
            lines.append("  无持仓")

        if tp_positions:
            lines.append(f"  已止盈: {len(tp_positions)} 笔")

        # ── 3. 分档分布 ──────────────────────────────────
        lines.append("")
        lines.append("【分档分布】")
        tier_counts = defaultdict(int)
        tier_amounts = defaultdict(float)
        for r in success:
            tier = r.tier or "未知"
            tier_counts[tier] += 1
            tier_amounts[tier] += r.buy_amount

        for tier_name in ["顶级", "人上人", "NPC"]:
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
