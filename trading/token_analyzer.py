"""
买入前代币分析器：综合评估代币质量，打分决定是否买入。

评估维度：
1. 安全性 — 蜜罐、合约权限、税率
2. 筹码分布 — Dev 持仓、Top10 集中度、持仓人数
3. 流动性 — 池子深度
4. 市值定位 — 早期介入甜区
5. 交易热度 — 成交量、买卖比
6. 社交信号 — 官网、Twitter、Telegram
7. 聪明钱信号 — Smart Money 持仓/买入
8. AI 智能研判 — MiniMax 大模型分析叙事质量
"""
from dataclasses import dataclass, field
from xxyy.client import client
from llm.minimax_client import minimax
from config import config
from utils.logger import get_logger

logger = get_logger(__name__)


@dataclass
class AnalysisResult:
    token_address: str
    chain: str
    score: int = 0              # 总分 0-100
    passed: bool = False        # 是否通过阈值
    reasons: list[str] = field(default_factory=list)   # 加分/扣分原因
    fatal: list[str] = field(default_factory=list)     # 一票否决原因

    def summary(self) -> str:
        status = "PASS" if self.passed else "REJECT"
        lines = [f"[{status}] score={self.score} ca={self.token_address}"]
        if self.fatal:
            lines.append(f"  fatal: {'; '.join(self.fatal)}")
        for r in self.reasons:
            lines.append(f"  {r}")
        return "\n".join(lines)


class TokenAnalyzer:
    """买入前全面分析，返回评分和建议"""

    def __init__(self, min_score: int | None = None):
        self.min_score = min_score or config.analyzer_min_score

    async def analyze(self, token_address: str, chain: str) -> AnalysisResult:
        result = AnalysisResult(token_address=token_address, chain=chain)

        try:
            data = await client.query_token(token_address, chain)
        except Exception as e:
            result.fatal.append(f"查询失败: {e}")
            logger.error("analyzer query failed ca=%s error=%s", token_address, e)
            return result

        if not isinstance(data, dict):
            result.fatal.append("返回数据无效")
            return result

        # 提取各层数据
        trade_info = data.get("tradeInfo") or {}
        security = data.get("security") or {}
        holder_info = data.get("holderInfo") or {}
        socials = data.get("socials") or {}

        # ── 1. 安全性检查（一票否决 + 扣分）──────────────
        self._check_security(security, trade_info, result)

        # ── 2. 筹码分布 ─────────────────────────────────
        self._check_holders(holder_info, security, result)

        # ── 3. 流动性 ───────────────────────────────────
        self._check_liquidity(trade_info, result)

        # ── 4. 市值定位 ─────────────────────────────────
        self._check_market_cap(trade_info, result)

        # ── 5. 交易热度 ─────────────────────────────────
        self._check_volume(trade_info, result)

        # ── 6. 社交信号 ─────────────────────────────────
        self._check_socials(socials, data, result)

        # ── 7. 聪明钱信号 ───────────────────────────────
        await self._check_smart_money(token_address, chain, result)

        # ── 8. AI 智能研判（MiniMax）─────────────────────
        await self._check_ai_verdict(data, socials, result)

        # 有一票否决项直接不过
        if result.fatal:
            result.passed = False
        else:
            result.passed = result.score >= self.min_score

        logger.info(
            "analysis done ca=%s score=%d passed=%s",
            token_address, result.score, result.passed,
        )
        return result

    # ── 安全性 ──────────────────────────────────────────────

    def _check_security(self, security: dict, trade_info: dict, result: AnalysisResult) -> None:
        # 蜜罐检测
        is_honeypot = security.get("isHoneypot")
        if is_honeypot:
            result.fatal.append("蜜罐代币")
            return

        # 合约权限是否放弃（renounced）
        is_renounced = security.get("renounced")
        if is_renounced:
            result.score += 15
            result.reasons.append("+15 合约已放弃权限(renounced)")
        else:
            result.score += 0
            result.reasons.append("+0 合约未放弃权限，有 rug 风险")

        # 买卖税率
        buy_tax = float(security.get("buyTax", 0) or 0)
        sell_tax = float(security.get("sellTax", 0) or 0)
        if buy_tax > 10 or sell_tax > 10:
            result.fatal.append(f"税率过高 buy={buy_tax}% sell={sell_tax}%")
            return
        elif buy_tax <= 2 and sell_tax <= 2:
            result.score += 10
            result.reasons.append(f"+10 税率低 buy={buy_tax}% sell={sell_tax}%")
        else:
            result.score += 5
            result.reasons.append(f"+5 税率一般 buy={buy_tax}% sell={sell_tax}%")

    # ── 筹码分布 ────────────────────────────────────────────

    def _check_holders(self, holder_info: dict, security: dict, result: AnalysisResult) -> None:
        holders = int(holder_info.get("holders", 0) or security.get("holders", 0) or 0)

        if holders >= 200:
            result.score += 15
            result.reasons.append(f"+15 持仓人数多({holders})")
        elif holders >= 50:
            result.score += 10
            result.reasons.append(f"+10 持仓人数尚可({holders})")
        elif holders >= 10:
            result.score += 5
            result.reasons.append(f"+5 持仓人数偏少({holders})")
        else:
            result.score -= 5
            result.reasons.append(f"-5 持仓人太少({holders})")

        # Dev 持仓
        dev_hp = float(security.get("devHoldPercent", 0) or 0)
        if dev_hp > 30:
            result.fatal.append(f"Dev 持仓过高({dev_hp:.1f}%)")
        elif dev_hp > 15:
            result.score += 0
            result.reasons.append(f"+0 Dev 持仓偏高({dev_hp:.1f}%)")
        elif dev_hp > 5:
            result.score += 5
            result.reasons.append(f"+5 Dev 持仓适中({dev_hp:.1f}%)")
        else:
            result.score += 10
            result.reasons.append(f"+10 Dev 持仓低({dev_hp:.1f}%)")

        # Top10 集中度
        top10_hp = float(security.get("top10HoldPercent", 0) or holder_info.get("top10Percent", 0) or 0)
        if top10_hp > 60:
            result.score -= 5
            result.reasons.append(f"-5 Top10 集中度过高({top10_hp:.1f}%)")
        elif top10_hp > 40:
            result.score += 5
            result.reasons.append(f"+5 Top10 集中度一般({top10_hp:.1f}%)")
        else:
            result.score += 10
            result.reasons.append(f"+10 Top10 集中度健康({top10_hp:.1f}%)")

    # ── 流动性 ──────────────────────────────────────────────

    def _check_liquidity(self, trade_info: dict, result: AnalysisResult) -> None:
        liquidity = float(trade_info.get("liquidity", 0) or trade_info.get("liquidityUSD", 0) or 0)

        if liquidity >= 50000:
            result.score += 15
            result.reasons.append(f"+15 流动性充足(${liquidity:,.0f})")
        elif liquidity >= 10000:
            result.score += 10
            result.reasons.append(f"+10 流动性尚可(${liquidity:,.0f})")
        elif liquidity >= 3000:
            result.score += 5
            result.reasons.append(f"+5 流动性偏低(${liquidity:,.0f})")
        else:
            result.score -= 5
            result.reasons.append(f"-5 流动性不足(${liquidity:,.0f})")

    # ── 市值定位 ────────────────────────────────────────────

    def _check_market_cap(self, trade_info: dict, result: AnalysisResult) -> None:
        mc = float(trade_info.get("marketCapUSD", 0) or trade_info.get("mc", 0) or 0)

        # 早期介入甜区：$10k - $500k
        if 10_000 <= mc <= 100_000:
            result.score += 20
            result.reasons.append(f"+20 市值处于早期甜区(${mc:,.0f})")
        elif 100_000 < mc <= 500_000:
            result.score += 15
            result.reasons.append(f"+15 市值适中(${mc:,.0f})")
        elif 500_000 < mc <= 2_000_000:
            result.score += 10
            result.reasons.append(f"+10 市值偏高但仍有空间(${mc:,.0f})")
        elif mc > 2_000_000:
            result.score += 5
            result.reasons.append(f"+5 市值较高，上行空间有限(${mc:,.0f})")
        else:
            result.score += 0
            result.reasons.append(f"+0 市值过低，风险较大(${mc:,.0f})")

    # ── 交易热度 ────────────────────────────────────────────

    def _check_volume(self, trade_info: dict, result: AnalysisResult) -> None:
        vol_24h = float(trade_info.get("volume24h", 0) or trade_info.get("vol24h", 0) or 0)
        buys = int(trade_info.get("buys24h", 0) or trade_info.get("buys", 0) or 0)
        sells = int(trade_info.get("sells24h", 0) or trade_info.get("sells", 0) or 0)

        if vol_24h >= 50000:
            result.score += 10
            result.reasons.append(f"+10 24h成交量活跃(${vol_24h:,.0f})")
        elif vol_24h >= 10000:
            result.score += 5
            result.reasons.append(f"+5 24h成交量一般(${vol_24h:,.0f})")
        else:
            result.score += 0
            result.reasons.append(f"+0 24h成交量低(${vol_24h:,.0f})")

        # 买卖比：买多于卖是积极信号
        total_txs = buys + sells
        if total_txs > 0:
            buy_ratio = buys / total_txs
            if buy_ratio >= 0.6:
                result.score += 5
                result.reasons.append(f"+5 买盘强势(买{buys}/卖{sells}={buy_ratio:.0%})")
            elif buy_ratio <= 0.35:
                result.score -= 5
                result.reasons.append(f"-5 卖盘压力大(买{buys}/卖{sells}={buy_ratio:.0%})")

    # ── 社交信号 ────────────────────────────────────────────

    def _check_socials(self, socials: dict, data: dict, result: AnalysisResult) -> None:
        social_count = 0

        has_website = bool(socials.get("website") or data.get("website"))
        has_twitter = bool(socials.get("twitter") or data.get("twitter"))
        has_telegram = bool(socials.get("telegram") or data.get("telegram"))

        if has_website:
            social_count += 1
        if has_twitter:
            social_count += 1
        if has_telegram:
            social_count += 1

        if social_count >= 3:
            result.score += 10
            result.reasons.append(f"+10 社交齐全(官网+Twitter+TG)")
        elif social_count >= 2:
            result.score += 5
            result.reasons.append(f"+5 有{social_count}个社交渠道")
        elif social_count == 1:
            result.score += 2
            result.reasons.append(f"+2 仅有1个社交渠道")
        else:
            result.score += 0
            result.reasons.append("+0 无社交信息")

    # ── 聪明钱信号 ──────────────────────────────────────────

    async def _check_smart_money(self, token_address: str, chain: str, result: AnalysisResult) -> None:
        """
        查询聪明钱（Smart Money）是否持仓/买入该代币。
        聪明钱 = 历史胜率高的钱包（鲸鱼、KOL、专业交易员）。
        有聪明钱介入是强烈的看涨信号。
        """
        try:
            wallets = await client.smart_wallets(token_address, chain)
        except Exception as e:
            logger.debug("smart wallet query failed ca=%s: %s", token_address, e)
            result.reasons.append("+0 聪明钱数据查询失败")
            return

        if not wallets:
            result.reasons.append("+0 无聪明钱数据")
            return

        sm_count = len(wallets)

        # 统计聪明钱总持仓金额
        total_value = 0.0
        buy_count = 0
        for w in wallets:
            val = float(w.get("holdingValueUSD", 0) or w.get("valueUSD", 0) or 0)
            total_value += val
            action = w.get("action", "").lower()
            if action in ("buy", "bought"):
                buy_count += 1

        if sm_count >= 5:
            result.score += 20
            result.reasons.append(
                f"+20 聪明钱强信号({sm_count}个钱包, 持仓${total_value:,.0f})"
            )
        elif sm_count >= 3:
            result.score += 15
            result.reasons.append(
                f"+15 聪明钱关注中({sm_count}个钱包, 持仓${total_value:,.0f})"
            )
        elif sm_count >= 1:
            result.score += 10
            result.reasons.append(
                f"+10 有聪明钱介入({sm_count}个钱包, 持仓${total_value:,.0f})"
            )

        if buy_count > 0:
            result.score += 5
            result.reasons.append(f"+5 聪明钱近期有{buy_count}笔买入")

    # ── AI 智能研判（MiniMax）───────────────────────────────

    async def _check_ai_verdict(self, data: dict, socials: dict, result: AnalysisResult) -> None:
        """
        用 MiniMax 大模型分析代币叙事质量。
        AI 判断这个币是蹭了真实热点还是纯垃圾。
        """
        if not minimax.available:
            result.reasons.append("+0 AI研判未启用(未配置MINIMAX_API_KEY)")
            return

        trade_info = data.get("tradeInfo") or {}
        name = data.get("name") or data.get("tokenName", "")
        symbol = data.get("symbol", "")
        description = data.get("description", "")
        mc = float(trade_info.get("marketCapUSD", 0) or 0)
        holders = int(data.get("holders", 0) or 0)

        ai_result = await minimax.analyze_token(
            name=name,
            symbol=symbol,
            description=description,
            market_cap=mc,
            holders=holders,
            has_website=bool(socials.get("website") or data.get("website")),
            has_twitter=bool(socials.get("twitter") or data.get("twitter")),
            has_telegram=bool(socials.get("telegram") or data.get("telegram")),
        )

        ai_score = ai_result.get("score", 0)
        verdict = ai_result.get("verdict", "SKIP")
        reason = ai_result.get("reason", "无")

        # AI 评分映射到分析器加分（AI满分100 → 最高加20分）
        bonus = int(ai_score * 0.2)
        result.score += bonus
        result.reasons.append(
            f"+{bonus} AI研判: {verdict} (AI评分{ai_score}/100) — {reason}"
        )

        # AI 强烈不推荐时扣分
        if ai_score < 20 and verdict == "SKIP":
            result.score -= 10
            result.reasons.append(f"-10 AI强烈不推荐")
