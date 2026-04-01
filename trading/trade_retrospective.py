"""
交易复盘系统 — 从实际交易数据中学习，优化策略参数

核心功能：
1. 解析 trade_signals.log，配对 BUY/SELL 生成完整交易记录
2. 按信号源、评分档位、退出策略分维度统计
3. 交给 MiniMax AI 深度分析，生成可执行的优化建议
4. 定期运行，持续优化参数
"""
import asyncio
import json
import os
import re
import time
from collections import defaultdict
from dataclasses import dataclass, field

from config import config
from llm.minimax_client import minimax
from utils.logger import get_logger

logger = get_logger("retrospective")

# 复盘间隔（秒），默认 1 小时
RETROSPECTIVE_INTERVAL = 3600

# trade_signals.log 路径
SIGNAL_LOG = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "trade_signals.log",
)

# 解析正则
BUY_RE = re.compile(
    r"\[(?P<ts>[^\]]+)\] BUY (?P<tier>\S+)\((?P<score>\d+)分\) "
    r"ca=(?P<ca>\S+) amount=(?P<amount>[\d.]+)SOL source=(?P<source>\S+) txId=(?P<txid>\S+)"
)
SELL_RE = re.compile(
    r"\[(?P<ts>[^\]]+)\] SELL \[(?P<reason>[^\]]+)\] (?P<pct>\d+)% "
    r"ca=(?P<ca>\S+) entry=\$(?P<entry>[\d.eE+-]+) txId=(?P<txid>\S+)"
)


@dataclass
class CompleteTrade:
    """一笔完整交易（买入 + 所有卖出）"""
    ca: str
    buy_time: str
    tier: str
    score: int
    amount: float
    source: str
    entry_price: float = 0.0
    sells: list[dict] = field(default_factory=list)  # [{reason, pct, time}]
    fully_closed: bool = False

    @property
    def total_sold_pct(self) -> int:
        return sum(s["pct"] for s in self.sells)

    @property
    def exit_reason(self) -> str:
        if not self.sells:
            return "持仓中"
        return self.sells[-1]["reason"]

    @property
    def hold_minutes(self) -> float:
        if not self.sells:
            return 0
        try:
            from datetime import datetime
            buy_dt = datetime.strptime(self.buy_time, "%Y-%m-%d %H:%M:%S")
            sell_dt = datetime.strptime(self.sells[-1]["time"], "%Y-%m-%d %H:%M:%S")
            return (sell_dt - buy_dt).total_seconds() / 60
        except Exception:
            return 0


@dataclass
class RetrospectiveReport:
    """复盘报告"""
    total_trades: int = 0
    total_invested: float = 0.0
    closed_trades: int = 0
    open_trades: int = 0
    # 退出原因统计
    exit_stats: dict = field(default_factory=dict)
    # 信号源统计
    source_stats: dict = field(default_factory=dict)
    # 评分档位统计
    tier_stats: dict = field(default_factory=dict)
    # 持仓时间统计
    avg_hold_minutes: float = 0.0
    # 问题发现
    issues: list[str] = field(default_factory=list)
    # 优化建议
    recommendations: list[str] = field(default_factory=list)


class TradeRetrospective:
    """交易复盘引擎"""

    def __init__(self, engine=None):
        self._engine = engine
        self._last_report_time = 0
        self._trades: list[CompleteTrade] = []

    def parse_log(self) -> list[CompleteTrade]:
        """解析 trade_signals.log，配对 BUY/SELL"""
        if not os.path.exists(SIGNAL_LOG):
            logger.warning("trade_signals.log not found")
            return []

        buys: dict[str, CompleteTrade] = {}  # ca -> CompleteTrade
        # 用列表保持顺序，一个 CA 可能多次交易
        trades: list[CompleteTrade] = []

        with open(SIGNAL_LOG) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue

                # 匹配 BUY
                m = BUY_RE.match(line)
                if m:
                    ca = m.group("ca")
                    trade = CompleteTrade(
                        ca=ca,
                        buy_time=m.group("ts"),
                        tier=m.group("tier"),
                        score=int(m.group("score")),
                        amount=float(m.group("amount")),
                        source=m.group("source"),
                    )
                    # 如果同一个 CA 之前的交易已关闭，开新交易
                    if ca in buys and buys[ca].fully_closed:
                        del buys[ca]
                    buys[ca] = trade
                    trades.append(trade)
                    continue

                # 匹配 SELL
                m = SELL_RE.match(line)
                if m:
                    ca = m.group("ca")
                    entry = float(m.group("entry"))
                    pct = int(m.group("pct"))
                    reason = m.group("reason")
                    sell_time = m.group("ts")

                    if ca in buys:
                        buys[ca].entry_price = entry
                        buys[ca].sells.append({
                            "reason": reason,
                            "pct": pct,
                            "time": sell_time,
                        })
                        if buys[ca].total_sold_pct >= 100:
                            buys[ca].fully_closed = True

        self._trades = trades
        return trades

    def analyze(self) -> RetrospectiveReport:
        """分析所有交易，生成复盘报告"""
        trades = self.parse_log()
        if not trades:
            return RetrospectiveReport()

        report = RetrospectiveReport()
        report.total_trades = len(trades)
        report.total_invested = sum(t.amount for t in trades)

        closed = [t for t in trades if t.fully_closed]
        open_trades = [t for t in trades if not t.fully_closed and t.sells]
        still_holding = [t for t in trades if not t.sells]

        report.closed_trades = len(closed)
        report.open_trades = len(still_holding)

        # ── 退出原因统计 ──────────────────────────────
        exit_reasons = defaultdict(lambda: {"count": 0, "total_amount": 0.0})
        for t in closed:
            reason = self._normalize_exit_reason(t.exit_reason)
            exit_reasons[reason]["count"] += 1
            exit_reasons[reason]["total_amount"] += t.amount
        report.exit_stats = dict(exit_reasons)

        # ── 信号源统计 ────────────────────────────────
        source_stats = defaultdict(lambda: {
            "total": 0, "closed": 0, "amount": 0.0,
            "exit_reasons": defaultdict(int),
            "scores": [],
        })
        for t in trades:
            src = t.source
            source_stats[src]["total"] += 1
            source_stats[src]["amount"] += t.amount
            source_stats[src]["scores"].append(t.score)
            if t.fully_closed:
                source_stats[src]["closed"] += 1
                reason = self._normalize_exit_reason(t.exit_reason)
                source_stats[src]["exit_reasons"][reason] += 1
        report.source_stats = dict(source_stats)

        # ── 评分档位统计 ──────────────────────────────
        tier_stats = defaultdict(lambda: {
            "count": 0, "amount": 0.0,
            "exit_reasons": defaultdict(int),
            "hold_minutes": [],
        })
        for t in trades:
            tier_stats[t.tier]["count"] += 1
            tier_stats[t.tier]["amount"] += t.amount
            if t.fully_closed:
                reason = self._normalize_exit_reason(t.exit_reason)
                tier_stats[t.tier]["exit_reasons"][reason] += 1
                if t.hold_minutes > 0:
                    tier_stats[t.tier]["hold_minutes"].append(t.hold_minutes)
        report.tier_stats = dict(tier_stats)

        # ── 平均持仓时间 ──────────────────────────────
        hold_times = [t.hold_minutes for t in closed if t.hold_minutes > 0]
        report.avg_hold_minutes = sum(hold_times) / len(hold_times) if hold_times else 0

        # ── 问题发现 ──────────────────────────────────
        report.issues = self._find_issues(trades, closed)

        # ── 优化建议 ──────────────────────────────────
        report.recommendations = self._generate_recommendations(trades, closed, report)

        return report

    def _normalize_exit_reason(self, reason: str) -> str:
        """归一化退出原因"""
        if "破位止损" in reason:
            return "破位止损"
        if "时间止损" in reason:
            return "时间止损"
        if "移动止盈" in reason:
            return "移动止盈"
        if "动量衰退" in reason:
            return "动量衰退"
        if "死币清理" in reason:
            return "死币清理"
        if "AI" in reason:
            return "AI卖出"
        if "止盈" in reason:
            return "止盈"
        return reason

    def _find_issues(self, trades: list[CompleteTrade], closed: list[CompleteTrade]) -> list[str]:
        """从交易数据中发现问题"""
        issues = []

        # 1. 重复卖出检测
        sell_counts = defaultdict(int)
        for t in trades:
            for s in t.sells:
                sell_counts[(t.ca, s["reason"])] += 1
        for (ca, reason), count in sell_counts.items():
            if count > 2:
                issues.append(
                    f"重复卖出: {ca[:12]}... 被 [{reason}] 触发了 {count} 次"
                )

        # 2. 止损占比过高
        if closed:
            stop_loss_count = sum(
                1 for t in closed
                if "止损" in t.exit_reason or "破位" in t.exit_reason
            )
            ratio = stop_loss_count / len(closed)
            if ratio > 0.5:
                issues.append(
                    f"止损率过高: {stop_loss_count}/{len(closed)} ({ratio:.0%}) 的交易以止损结束"
                )

        # 3. 高分也亏钱
        high_score_losses = [
            t for t in closed
            if t.score >= 75 and ("止损" in t.exit_reason or "破位" in t.exit_reason)
        ]
        if high_score_losses:
            issues.append(
                f"高分(>=75)也亏损: {len(high_score_losses)} 笔 — 评分系统可能失准"
            )

        # 4. 持仓时间过短
        short_trades = [t for t in closed if 0 < t.hold_minutes < 10]
        if len(short_trades) >= 3:
            issues.append(
                f"频繁快进快出: {len(short_trades)} 笔持仓不到10分钟就被卖出"
            )

        # 5. 单一信号源
        sources = set(t.source for t in trades)
        if len(sources) == 1:
            issues.append(
                f"信号来源单一: 所有 {len(trades)} 笔交易都来自 {sources.pop()}，其他信号源未产出"
            )

        # 6. 同一个币被反复买卖
        ca_trade_counts = defaultdict(int)
        for t in trades:
            ca_trade_counts[t.ca] += 1
        for ca, count in ca_trade_counts.items():
            if count >= 3:
                issues.append(f"反复交易: {ca[:12]}... 被买入了 {count} 次")

        return issues

    def _generate_recommendations(
        self, trades: list[CompleteTrade], closed: list[CompleteTrade],
        report: RetrospectiveReport,
    ) -> list[str]:
        """基于数据生成可执行的优化建议"""
        recs = []

        if not closed:
            recs.append("交易样本不足，暂无优化建议，继续积累数据")
            return recs

        # 1. 止损率分析 → 调整买入标准
        stop_loss_trades = [t for t in closed if "止损" in t.exit_reason or "破位" in t.exit_reason]
        profit_trades = [t for t in closed if "止盈" in t.exit_reason or "移动止盈" in t.exit_reason]

        stop_loss_ratio = len(stop_loss_trades) / len(closed) if closed else 0
        if stop_loss_ratio > 0.6:
            avg_stop_score = sum(t.score for t in stop_loss_trades) / len(stop_loss_trades)
            recs.append(
                f"止损率 {stop_loss_ratio:.0%}（目标<40%）— "
                f"止损交易平均分 {avg_stop_score:.0f}，建议提高 ANALYZER_MIN_SCORE 到 {int(avg_stop_score) + 10}"
            )

        # 2. 评分档位分析 → 调整分档策略
        for tier, stats in report.tier_stats.items():
            exits = stats.get("exit_reasons", {})
            total_exits = sum(exits.values())
            if total_exits < 2:
                continue
            loss_exits = exits.get("破位止损", 0) + exits.get("时间止损", 0)
            if total_exits > 0 and loss_exits / total_exits > 0.7:
                recs.append(
                    f"[{tier}] 档位亏损率 {loss_exits}/{total_exits} ({loss_exits/total_exits:.0%})，"
                    f"考虑降低该档位的投入金额或提高准入门槛"
                )

        # 3. 持仓时间分析 → 调整时间止损
        if report.avg_hold_minutes > 0:
            time_stop_trades = [t for t in closed if "时间止损" in t.exit_reason]
            if time_stop_trades:
                avg_time_stop_min = sum(t.hold_minutes for t in time_stop_trades) / len(time_stop_trades)
                # 如果大部分时间止损的币后来涨了（从日志看不到，但可以给建议）
                recs.append(
                    f"时间止损平均持仓 {avg_time_stop_min:.0f} 分钟 — "
                    f"如果这些币后续有反弹，考虑延长 TIME_STOP_MINUTES"
                )

        # 4. 信号源分析 → 关闭低效信号源
        for src, stats in report.source_stats.items():
            exits = stats.get("exit_reasons", {})
            total_exits = sum(exits.values())
            if total_exits < 3:
                continue
            profit_exits = exits.get("止盈", 0) + exits.get("移动止盈", 0)
            if profit_exits == 0:
                recs.append(
                    f"信号源 [{src}] 已完成 {total_exits} 笔交易但 0 笔盈利 — "
                    f"考虑暂停该信号源或加强过滤"
                )

        # 5. 破位止损深度分析
        if stop_loss_trades:
            low_score_stops = [t for t in stop_loss_trades if t.score < 60]
            high_score_stops = [t for t in stop_loss_trades if t.score >= 60]
            if low_score_stops and len(low_score_stops) > len(high_score_stops):
                recs.append(
                    f"低分(<60)止损 {len(low_score_stops)} 笔 > 高分(>=60)止损 {len(high_score_stops)} 笔 — "
                    f"提高最低分数线能减少亏损"
                )

        # 6. 投入金额分析
        if stop_loss_trades and profit_trades:
            avg_loss_amount = sum(t.amount for t in stop_loss_trades) / len(stop_loss_trades)
            avg_win_amount = sum(t.amount for t in profit_trades) / len(profit_trades)
            if avg_loss_amount >= avg_win_amount:
                recs.append(
                    f"亏损交易平均投入 {avg_loss_amount:.3f} SOL >= 盈利交易 {avg_win_amount:.3f} SOL — "
                    f"低分交易应该用更小的仓位"
                )

        if not recs:
            recs.append("当前策略表现正常，继续观察")

        return recs

    def generate_report_text(self) -> str:
        """生成完整的复盘报告文本"""
        report = self.analyze()
        lines = [
            "",
            "=" * 70,
            "  交易复盘报告",
            "=" * 70,
            "",
            "【交易总览】",
            f"  总交易: {report.total_trades} 笔 | "
            f"已平仓: {report.closed_trades} | 持仓中: {report.open_trades}",
            f"  总投入: {report.total_invested:.3f} SOL",
            f"  平均持仓: {report.avg_hold_minutes:.0f} 分钟",
        ]

        # 退出原因
        if report.exit_stats:
            lines.append("")
            lines.append("【退出原因分布】")
            total_exits = sum(s["count"] for s in report.exit_stats.values())
            for reason, stats in sorted(report.exit_stats.items(), key=lambda x: -x[1]["count"]):
                pct = stats["count"] / total_exits * 100
                lines.append(
                    f"  {reason}: {stats['count']} 笔 ({pct:.0f}%) "
                    f"投入 {stats['total_amount']:.3f} SOL"
                )

        # 信号源
        if report.source_stats:
            lines.append("")
            lines.append("【信号源表现】")
            for src, stats in report.source_stats.items():
                avg_score = sum(stats["scores"]) / len(stats["scores"]) if stats["scores"] else 0
                exits = stats.get("exit_reasons", {})
                profit_cnt = exits.get("止盈", 0) + exits.get("移动止盈", 0)
                loss_cnt = exits.get("破位止损", 0) + exits.get("时间止损", 0)
                lines.append(
                    f"  {src}: {stats['total']}笔 (平均{avg_score:.0f}分) "
                    f"盈利{profit_cnt}笔 亏损{loss_cnt}笔 投入{stats['amount']:.3f}SOL"
                )

        # 评分档位
        if report.tier_stats:
            lines.append("")
            lines.append("【评分档位表现】")
            for tier, stats in sorted(report.tier_stats.items(), key=lambda x: -x[1]["count"]):
                exits = stats.get("exit_reasons", {})
                exit_summary = ", ".join(f"{k}:{v}" for k, v in exits.items()) if exits else "无"
                avg_hold = (
                    sum(stats["hold_minutes"]) / len(stats["hold_minutes"])
                    if stats.get("hold_minutes") else 0
                )
                lines.append(
                    f"  {tier}: {stats['count']}笔 {stats['amount']:.3f}SOL "
                    f"| 退出: {exit_summary}"
                    f"{f' | 平均持仓{avg_hold:.0f}min' if avg_hold > 0 else ''}"
                )

        # 问题
        if report.issues:
            lines.append("")
            lines.append("【发现问题】")
            for i, issue in enumerate(report.issues, 1):
                lines.append(f"  {i}. {issue}")

        # 建议
        if report.recommendations:
            lines.append("")
            lines.append("【优化建议】")
            for i, rec in enumerate(report.recommendations, 1):
                lines.append(f"  {i}. {rec}")

        lines.append("")
        lines.append("=" * 70)
        return "\n".join(lines)

    async def auto_tune(self) -> list[str]:
        """基于复盘数据自动调整策略参数，返回调整说明"""
        report = self.analyze()
        adjustments = []

        if report.closed_trades < 5:
            return ["样本不足(<5笔)，暂不自动调参"]

        # 1. 止损率>60% → 提高最低分数线
        stop_count = sum(
            s["count"] for reason, s in report.exit_stats.items()
            if "止损" in reason or "破位" in reason
        )
        stop_ratio = stop_count / report.closed_trades if report.closed_trades > 0 else 0

        if stop_ratio > 0.6 and self._engine:
            old_min = self._engine.analyzer.min_score
            new_min = min(old_min + 5, 80)
            if new_min > old_min:
                self._engine.analyzer.min_score = new_min
                adjustments.append(f"止损率 {stop_ratio:.0%} > 60% → ANALYZER_MIN_SCORE {old_min} → {new_min}")

        # 2. 低分(<60)止损多 → 提高低分档投入惩罚
        low_score_stops = 0
        for t in self._trades:
            if t.fully_closed and t.score < 60 and ("止损" in t.exit_reason or "破位" in t.exit_reason):
                low_score_stops += 1

        if low_score_stops >= 3 and self._engine:
            # 如果"探路"档(score 40-49)亏太多，取消"探路"档
            old_tiers = self._engine.TIERS
            if old_tiers and old_tiers[-1][0] < 50:
                self._engine.TIERS = [t for t in old_tiers if t[0] >= 50]
                adjustments.append(f"低分止损 {low_score_stops} 笔 → 取消'探路'档(score<50不再买入)")

        # 3. 时间止损频繁 → 如果亏损不多，可以延长
        time_stop_count = sum(
            s["count"] for reason, s in report.exit_stats.items()
            if "时间止损" in reason
        )
        if time_stop_count >= 3:
            import trading.position_monitor as pm
            old_time = pm.TIME_STOP_MINUTES
            if old_time < 60:
                pm.TIME_STOP_MINUTES = 60
                adjustments.append(f"时间止损触发 {time_stop_count} 次 → TIME_STOP_MINUTES {old_time} → 60")

        return adjustments

    async def ai_retrospective(self) -> str | None:
        """让 MiniMax AI 对交易数据做深度复盘分析"""
        if not minimax.available:
            logger.info("MiniMax 未配置，跳过 AI 复盘")
            return None

        report = self.analyze()
        if report.total_trades < 3:
            return None

        # 构建给 AI 的数据摘要
        data_summary = self.generate_report_text()

        # 加上当前策略参数供 AI 参考
        import trading.position_monitor as pm
        params = (
            f"\n\n【当前策略参数】\n"
            f"  ANALYZER_MIN_SCORE = {config.analyzer_min_score}\n"
            f"  BUY_AMOUNT = {config.buy_amount} SOL\n"
            f"  止盈阶梯: {[(m, p) for m, p, _ in pm.TAKE_PROFIT_LEVELS]}\n"
            f"  移动止盈回撤: {pm.TRAILING_STOP_DROP*100:.0f}%\n"
            f"  时间止损: {pm.TIME_STOP_MINUTES}分钟\n"
            f"  破位止损: 跌至{pm.CRASH_STOP_MULTIPLIER*100:.0f}%\n"
            f"  并发分析: {self._engine.CONCURRENT_ANALYSES if self._engine else '?'}\n"
        )

        system_prompt = """你是一个顶级加密货币量化交易策略分析师。你会收到一个 Solana meme coin 交易机器人的完整交易复盘数据。

你的任务：
1. **诊断问题** — 从数据中发现亏损的根本原因（不是表面的"止损率高"，而是深层原因）
2. **策略评估** — 当前止盈/止损参数是否合理？分数线是否恰当？
3. **可执行建议** — 给出具体的参数调整建议（精确到数值），说明预期效果
4. **优先级排序** — 哪些调整最紧急、ROI最高

关键经验法则：
- Meme coin 交易的核心是"赢一笔覆盖所有亏损"，止盈不能太激进
- 如果大部分交易都在破位止损，说明买入质量太差，而不是止损线太紧
- 如果时间止损太多，可能需要延长，meme coin 需要发酵时间
- 信号源单一是大问题，需要多元化
- 高分交易也亏损说明评分系统有误差，需要校准

请输出 JSON 格式（每个字段控制在100字以内，param_changes最多5条）：
{
  "diagnosis": "100字以内的核心问题",
  "param_changes": [
    {"param": "参数名", "current": "当前值", "suggested": "建议值", "reason": "20字理由"}
  ],
  "strategy_advice": "100字以内的整体建议",
  "risk_level": "low/medium/high",
  "confidence": 0-100
}

只输出 JSON，不要其他内容。"""

        try:
            import httpx
            async with httpx.AsyncClient(timeout=30.0, verify=False) as http:
                resp = await http.post(
                    "https://api.minimaxi.com/v1/text/chatcompletion_v2",
                    headers={
                        "Authorization": f"Bearer {config.minimax_api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": config.minimax_model,
                        "messages": [
                            {"role": "system", "content": system_prompt},
                            {"role": "user", "content": data_summary + params},
                        ],
                        "max_completion_tokens": 2000,
                        "temperature": 0.3,
                    },
                )

            if resp.status_code != 200:
                logger.warning("AI 复盘请求失败: %d", resp.status_code)
                return None

            data = resp.json()
            content = (
                data.get("choices", [{}])[0]
                .get("message", {})
                .get("content", "")
            )

            # 解析 AI 回复（兼容 ```json 代码块包裹）
            raw = content.strip()
            if raw.startswith("```"):
                # 去掉 ```json 和 ```
                raw = re.sub(r"^```(?:json)?\s*", "", raw)
                raw = re.sub(r"\s*```\s*$", "", raw)
            ai_result = json.loads(raw)

            # 格式化输出
            lines = [
                "",
                "=" * 70,
                "  🤖 MiniMax AI 深度复盘",
                "=" * 70,
                "",
                f"【诊断】{ai_result.get('diagnosis', '无')}",
                "",
                f"【风险等级】{ai_result.get('risk_level', '?')} | 置信度: {ai_result.get('confidence', 0)}%",
                "",
                "【参数调整建议】",
            ]
            for change in ai_result.get("param_changes", []):
                lines.append(
                    f"  {change['param']}: {change['current']} → {change['suggested']} ({change['reason']})"
                )
            lines.append("")
            lines.append(f"【策略建议】{ai_result.get('strategy_advice', '无')}")
            lines.append("=" * 70)

            ai_report = "\n".join(lines)
            logger.info(ai_report)

            # 自动应用 AI 建议的参数调整（只在置信度 >= 80 时）
            if ai_result.get("confidence", 0) >= 80 and self._engine:
                await self._apply_ai_suggestions(ai_result.get("param_changes", []))

            return ai_report

        except json.JSONDecodeError as e:
            logger.warning("AI 复盘 JSON 解析失败: %s, raw: %s", e, content[:200] if 'content' in dir() else '?')
            return None
        except Exception as e:
            logger.error("AI 复盘异常: %s", e)
            return None

    async def _apply_ai_suggestions(self, param_changes: list[dict]) -> None:
        """自动应用 AI 建议的参数调整"""
        import trading.position_monitor as pm

        for change in param_changes:
            param = change.get("param", "")
            suggested = change.get("suggested", "")

            try:
                if "MIN_SCORE" in param.upper() and self._engine:
                    val = int(suggested)
                    if 30 <= val <= 85:
                        old = self._engine.analyzer.min_score
                        self._engine.analyzer.min_score = val
                        logger.info("🔧 AI调参: ANALYZER_MIN_SCORE %d → %d", old, val)

                elif "TIME_STOP" in param.upper():
                    val = int(suggested)
                    if 20 <= val <= 120:
                        old = pm.TIME_STOP_MINUTES
                        pm.TIME_STOP_MINUTES = val
                        logger.info("🔧 AI调参: TIME_STOP_MINUTES %d → %d", old, val)

                elif "CRASH_STOP" in param.upper():
                    val = float(suggested)
                    if 0.2 <= val <= 0.5:
                        old = pm.CRASH_STOP_MULTIPLIER
                        pm.CRASH_STOP_MULTIPLIER = val
                        logger.info("🔧 AI调参: CRASH_STOP_MULTIPLIER %.2f → %.2f", old, val)

                elif "TRAILING" in param.upper() and "DROP" in param.upper():
                    val = float(suggested)
                    if 0.15 <= val <= 0.5:
                        old = pm.TRAILING_STOP_DROP
                        pm.TRAILING_STOP_DROP = val
                        logger.info("🔧 AI调参: TRAILING_STOP_DROP %.2f → %.2f", old, val)

                elif "BUY_AMOUNT" in param.upper():
                    val = float(suggested)
                    if 0.01 <= val <= 0.1:
                        old = config.buy_amount
                        config.buy_amount = val
                        if self._engine:
                            self._engine.buy_amount = val
                        logger.info("🔧 AI调参: BUY_AMOUNT %.3f → %.3f", old, val)

            except (ValueError, TypeError) as e:
                logger.debug("AI调参跳过 %s: %s", param, e)

    async def start(self, interval: int = RETROSPECTIVE_INTERVAL) -> None:
        """定期运行复盘"""
        logger.info("TradeRetrospective started, interval=%ds", interval)
        # 首次等 5 分钟再运行（等积累一些数据）
        await asyncio.sleep(300)

        ai_cycle = 0
        while True:
            try:
                # 每次都输出基础统计报告
                report_text = self.generate_report_text()
                logger.info(report_text)

                # 基础自动调参（规则驱动，每次都跑）
                if self._engine:
                    adjustments = await self.auto_tune()
                    if adjustments:
                        for adj in adjustments:
                            logger.info("🔧 自动调参: %s", adj)

                # AI 深度复盘（每 3 个周期跑一次，节省 API 调用）
                ai_cycle += 1
                if ai_cycle % 3 == 0:
                    await self.ai_retrospective()

            except Exception as e:
                logger.error("retrospective error: %s", e)

            await asyncio.sleep(interval)
