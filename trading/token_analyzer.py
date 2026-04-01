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

import httpx

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
        self.min_score = min_score or config.analyzer_min_score  # 默认40，通过 .env ANALYZER_MIN_SCORE 配置

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

        # 提取各层数据（适配 XXYY 实际返回字段名）
        trade_info = data.get("tradeInfo") or {}
        security_info = data.get("securityInfo") or {}
        pair_info = data.get("pairInfo") or {}
        link_info = data.get("linkInfo") or {}
        dev_info = data.get("dev") or {}

        # ── 0. 硬性门槛（不达标直接否决，节省 API 调用）─────
        holders = int(trade_info.get("holder", 0) or 0)
        vol = float(trade_info.get("hourTradeVolume", 0) or 0)
        mc = float(trade_info.get("marketCapUsd", 0) or 0)

        if holders < 5:
            result.fatal.append(f"持仓人不足5({holders})，太早期")
            return result
        if vol < 500:
            result.fatal.append(f"1h成交量不足$500(${vol:,.0f})，无人气")
            return result
        if mc < 2000:
            result.fatal.append(f"市值不足$2k(${mc:,.0f})，太小")
            return result

        # ── 1. 安全性检查（一票否决 + 加分）──────────────
        self._check_security(security_info, trade_info, result)

        # ── 2. 筹码分布 ─────────────────────────────────
        self._check_holders(trade_info, dev_info, data, result)

        # ── 3. 流动性 ───────────────────────────────────
        self._check_liquidity(pair_info, result)

        # ── 4. 市值定位 ─────────────────────────────────
        self._check_market_cap(trade_info, result)

        # ── 5. 交易热度 ─────────────────────────────────
        self._check_volume(trade_info, result)

        # ── 5b. 成交量动量（DexScreener）────────────────
        await self._check_volume_momentum(token_address, result)

        # ── 6. 社交信号 ─────────────────────────────────
        self._check_socials(link_info, data, result)

        # ── 7. 聪明钱信号 ───────────────────────────────
        await self._check_smart_money(token_address, chain, result)

        # ── 8. AI 智能研判（MiniMax）─────────────────────
        await self._check_ai_verdict(data, link_info, result)

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

    def _check_security(self, security_info: dict, trade_info: dict, result: AnalysisResult) -> None:
        """
        securityInfo 实际字段: locked, noMint, noFreeze
        """
        # noMint + noFreeze = 相当于 renounced（无法增发、无法冻结）
        no_mint = security_info.get("noMint", False)
        no_freeze = security_info.get("noFreeze", False)
        locked = security_info.get("locked", False)

        # PumpFun 上几乎所有币都 noMint+noFreeze+locked，降低白送分
        if no_mint and no_freeze:
            result.score += 2
            result.reasons.append("+2 合约安全(noMint+noFreeze)")
        elif no_mint or no_freeze:
            result.score += 1
            result.reasons.append(f"+1 合约部分安全")
        else:
            result.fatal.append("合约未放弃权限，有 rug 风险")

        if locked:
            result.score += 1
            result.reasons.append("+1 流动性已锁定")

    # ── 筹码分布 ────────────────────────────────────────────

    def _check_holders(self, trade_info: dict, dev_info: dict, data: dict, result: AnalysisResult) -> None:
        """
        tradeInfo.holder = 持仓人数
        dev.pct = Dev 持仓百分比
        topHolderPct = Top10 集中度
        """
        holders = int(trade_info.get("holder", 0) or 0)

        if holders >= 500:
            result.score += 10
            result.reasons.append(f"+10 持仓人数多({holders})")
        elif holders >= 200:
            result.score += 8
            result.reasons.append(f"+8 持仓人数不错({holders})")
        elif holders >= 50:
            result.score += 6
            result.reasons.append(f"+6 持仓人数尚可({holders})")
        elif holders >= 15:
            result.score += 4
            result.reasons.append(f"+4 持仓人数偏少但可接受({holders})")
        else:
            result.score += 2
            result.reasons.append(f"+2 极早期({holders}人)")

        # Dev 持仓
        dev_hp = float(dev_info.get("pct", 0) or 0)
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
        top10_hp = float(data.get("topHolderPct", 0) or 0)
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

    def _check_liquidity(self, pair_info: dict, result: AnalysisResult) -> None:
        liquidity = float(pair_info.get("liquidateUsd", 0) or pair_info.get("liquidity", 0) or 0)

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
        mc = float(trade_info.get("marketCapUsd", 0) or trade_info.get("marketCapUSD", 0) or 0)

        # 早期介入甜区：$3k - $100k（土狗就是要早）
        if 3_000 <= mc <= 50_000:
            result.score += 12
            result.reasons.append(f"+12 市值极早期甜区(${mc:,.0f})")
        elif 50_000 < mc <= 200_000:
            result.score += 10
            result.reasons.append(f"+10 市值甜区(${mc:,.0f})")
        elif 200_000 < mc <= 1_000_000:
            result.score += 7
            result.reasons.append(f"+7 市值适中(${mc:,.0f})")
        elif 1_000_000 < mc <= 5_000_000:
            result.score += 4
            result.reasons.append(f"+4 市值偏高(${mc:,.0f})")
        elif mc > 5_000_000:
            result.score += 2
            result.reasons.append(f"+2 市值高，上行有限(${mc:,.0f})")
        else:
            result.score += 0
            result.reasons.append(f"+0 市值过低(${mc:,.0f})")

    # ── 交易热度 ────────────────────────────────────────────

    def _check_volume(self, trade_info: dict, result: AnalysisResult) -> None:
        """tradeInfo: hourTradeVolume(1h成交额), hourTradeNum(1h交易笔数)"""
        vol = float(trade_info.get("hourTradeVolume", 0) or trade_info.get("volume24h", 0) or 0)
        trade_num = int(trade_info.get("hourTradeNum", 0) or 0)

        if vol >= 100000:
            result.score += 10
            result.reasons.append(f"+10 成交量火爆(${vol:,.0f}, {trade_num}笔)")
        elif vol >= 50000:
            result.score += 8
            result.reasons.append(f"+8 成交量活跃(${vol:,.0f}, {trade_num}笔)")
        elif vol >= 10000:
            result.score += 6
            result.reasons.append(f"+6 成交量不错(${vol:,.0f}, {trade_num}笔)")
        elif vol >= 2000:
            result.score += 4
            result.reasons.append(f"+4 成交量尚可(${vol:,.0f}, {trade_num}笔)")
        elif vol >= 500:
            result.score += 2
            result.reasons.append(f"+2 成交量偏低但有人气(${vol:,.0f}, {trade_num}笔)")
        else:
            result.score += 0
            result.reasons.append(f"+0 成交量冷清(${vol:,.0f}, {trade_num}笔)")

    # ── 成交量动量（DexScreener 免费 API）───────────────────

    async def _check_volume_momentum(self, token_address: str, result: AnalysisResult) -> None:
        """
        用 DexScreener 免费 API 获取多时间框架成交量，计算动量和买压。
        - momentum = volume_5m / (volume_1h / 12)  — >1.5 加速, <0.5 衰退
        - buy_pressure = buys_5m / (buys_5m + sells_5m) — >0.6 看多
        DexScreener 查询失败不影响整体分析。
        """
        try:
            async with httpx.AsyncClient(verify=False, timeout=10.0) as http:
                resp = await http.get(
                    f"https://api.dexscreener.com/latest/dex/tokens/{token_address}"
                )
                if resp.status_code != 200:
                    logger.debug("DexScreener volume query failed status=%d ca=%s", resp.status_code, token_address)
                    result.reasons.append("+0 DexScreener成交量查询失败")
                    return

                data = resp.json()
                pairs = data.get("pairs")
                if not pairs:
                    result.reasons.append("+0 DexScreener无交易对数据")
                    return

                # 取流动性最大的交易对
                pair = max(pairs, key=lambda p: float((p.get("liquidity") or {}).get("usd", 0) or 0))

                volume = pair.get("volume") or {}
                txns = pair.get("txns") or {}

                vol_5m = float(volume.get("m5", 0) or 0)
                vol_1h = float(volume.get("h1", 0) or 0)

                txns_5m = txns.get("m5") or {}
                buys_5m = int(txns_5m.get("buys", 0) or 0)
                sells_5m = int(txns_5m.get("sells", 0) or 0)

                # 计算动量: 5分钟成交量 vs 1小时均值的5分钟切片
                if vol_1h > 0:
                    momentum = vol_5m / (vol_1h / 12)
                else:
                    momentum = 0.0

                # 计算买压
                total_txns_5m = buys_5m + sells_5m
                if total_txns_5m > 0:
                    buy_pressure = buys_5m / total_txns_5m
                else:
                    buy_pressure = 0.5  # 无数据时中性

                # 动量评分
                if momentum > 1.5:
                    result.score += 10
                    result.reasons.append(f"+10 成交量加速(动量{momentum:.1f}x)")
                elif momentum < 0.5:
                    result.score -= 10
                    result.reasons.append(f"-10 成交量衰退(动量{momentum:.1f}x)")
                else:
                    result.reasons.append(f"+0 成交量动量平稳({momentum:.1f}x)")

                # 买压评分
                if buy_pressure > 0.6:
                    result.score += 8
                    result.reasons.append(f"+8 买压强劲({buy_pressure:.0%})")
                elif buy_pressure < 0.4:
                    result.score -= 8
                    result.reasons.append(f"-8 卖压沉重({buy_pressure:.0%})")
                else:
                    result.reasons.append(f"+0 买卖压平衡({buy_pressure:.0%})")

                logger.debug(
                    "volume momentum ca=%s momentum=%.2f buy_pressure=%.2f vol_5m=%.0f vol_1h=%.0f",
                    token_address, momentum, buy_pressure, vol_5m, vol_1h,
                )

        except Exception as e:
            logger.debug("DexScreener volume momentum failed ca=%s: %s", token_address, e)
            result.reasons.append("+0 DexScreener动量检测跳过")

    # ── 社交信号 ────────────────────────────────────────────

    def _check_socials(self, link_info: dict, data: dict, result: AnalysisResult) -> None:
        """linkInfo 实际字段: web, x, tg"""
        social_count = 0

        has_website = bool(link_info.get("web"))
        has_twitter = bool(link_info.get("x"))
        has_telegram = bool(link_info.get("tg"))

        if has_website:
            social_count += 1
        if has_twitter:
            social_count += 1
        if has_telegram:
            social_count += 1

        if social_count >= 3:
            result.score += 5
            result.reasons.append("+5 社交齐全(官网+Twitter+TG)")
        elif social_count >= 2:
            result.score += 3
            result.reasons.append(f"+3 有{social_count}个社交渠道")
        elif social_count == 1:
            result.score += 1
            result.reasons.append("+1 仅有1个社交渠道")
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

    async def _check_ai_verdict(self, data: dict, link_info: dict, result: AnalysisResult) -> None:
        """
        用 MiniMax 大模型分析代币叙事质量。
        AI 判断这个币是蹭了真实热点还是纯垃圾。
        """
        if not minimax.available:
            result.reasons.append("+0 AI研判未启用(未配置MINIMAX_API_KEY)")
            return

        trade_info = data.get("tradeInfo") or {}
        name = data.get("baseSymbol") or data.get("name", "")
        symbol = data.get("baseSymbol") or data.get("symbol", "")
        description = data.get("description", "")
        mc = float(trade_info.get("marketCapUsd", 0) or 0)
        holders = int(trade_info.get("holder", 0) or 0)

        # 补充 launch 平台信息
        launch = data.get("launchPlatform") or {}
        if launch.get("name"):
            description += f" | 发射平台: {launch['name']} 进度: {launch.get('progress', '?')}%"

        ai_result = await minimax.analyze_token(
            name=name,
            symbol=symbol,
            description=description,
            market_cap=mc,
            holders=holders,
            has_website=bool(link_info.get("web")),
            has_twitter=bool(link_info.get("x")),
            has_telegram=bool(link_info.get("tg")),
        )

        ai_score = ai_result.get("score", 0)
        verdict = ai_result.get("verdict", "SKIP")
        reason = ai_result.get("reason", "无")

        # AI 评分权重大幅提升（满分100 → 最高加 30 分）
        # AI 是区分真热点 vs 垃圾的核心能力
        bonus = int(ai_score * 0.3)
        result.score += bonus
        result.reasons.append(
            f"+{bonus} AI研判: {verdict} (AI评分{ai_score}/100) — {reason}"
        )

        # AI 说 SKIP 就要认真对待
        if verdict == "SKIP" and ai_score < 30:
            # AI 明确否定 → 一票否决
            result.fatal.append(f"AI否决: 叙事不过关(AI评分{ai_score}/100) — {reason}")
        elif verdict == "SKIP" and ai_score < 50:
            # AI 不看好但不至于否决 → 大幅扣分
            result.score -= 15
            result.reasons.append(f"-15 AI不推荐(SKIP且评分{ai_score})")
        elif verdict == "BUY" and ai_score >= 70:
            # AI 强烈推荐 → 额外加分
            result.score += 10
            result.reasons.append(f"+10 AI强烈推荐")
