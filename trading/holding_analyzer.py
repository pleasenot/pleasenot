"""
持仓实时动态分析器

不是简单的"当前数据丢给 AI"，而是：
1. 历史趋势追踪：每次检查记录快照，构建趋势曲线
2. 链上行为分析：聪明钱进出、Dev 抛售、大户动态
3. 同类对比：和当前热门币对比指标
4. 社交热度：Reddit 提及、Twitter 活跃度
5. 综合研判：把所有维度打包给 MiniMax 做最终决策
"""
import asyncio
import time
from collections import defaultdict
from dataclasses import dataclass, field

from xxyy.client import client
from llm.minimax_client import minimax
from config import config
from utils.logger import get_logger

logger = get_logger(__name__)

# 快照保留数量（每5分钟一次 × 12 = 1小时趋势）
MAX_SNAPSHOTS = 12


@dataclass
class Snapshot:
    """某一时刻的代币状态快照"""
    timestamp: float
    price: float
    holders: int
    volume_1h: float
    market_cap: float
    liquidity: float
    dev_pct: float
    top10_pct: float
    trade_count: int  # 1h 交易笔数


@dataclass
class SmartMoneyInfo:
    """聪明钱分析结果"""
    wallet_count: int = 0
    total_value_usd: float = 0.0
    buying_count: int = 0
    selling_count: int = 0
    net_action: str = "无数据"  # "净买入" / "净卖出" / "观望" / "无数据"


@dataclass
class PeerComparison:
    """同类币对比"""
    avg_holder_count: int = 0
    avg_volume: float = 0.0
    avg_market_cap: float = 0.0
    trending_count: int = 0  # 当前热门币总数
    rank_holders: str = "未知"  # "高于平均" / "低于平均"
    rank_volume: str = "未知"
    rank_mc: str = "未知"


@dataclass
class SocialSignal:
    """社交热度信号"""
    reddit_mentions: int = 0
    has_active_twitter: bool = False
    has_active_telegram: bool = False
    has_website: bool = False
    social_score: str = "冷门"  # "火热" / "活跃" / "一般" / "冷门"


@dataclass
class HoldingDiagnosis:
    """完整的持仓诊断报告"""
    token_address: str
    name: str
    symbol: str

    # 趋势
    snapshots: list[Snapshot] = field(default_factory=list)
    holder_trend: str = "无数据"       # "持续增长" / "高位震荡" / "持续下降" / "稳定"
    volume_trend: str = "无数据"       # "放量" / "缩量" / "稳定" / "无量"
    price_trend: str = "无数据"        # "上涨" / "下跌" / "横盘"

    # 链上
    smart_money: SmartMoneyInfo = field(default_factory=SmartMoneyInfo)
    dev_selling: bool = False          # Dev 是否在抛售
    top10_concentrating: bool = False  # Top10 是否在集中

    # 同类对比
    peers: PeerComparison = field(default_factory=PeerComparison)

    # 社交
    social: SocialSignal = field(default_factory=SocialSignal)

    # 当前状态
    current_price: float = 0.0
    multiplier: float = 1.0
    hold_minutes: float = 0.0


class HoldingAnalyzer:
    """持仓动态分析器 — 收集多维数据，构建趋势，交给 AI 研判"""

    def __init__(self):
        # token_address -> list[Snapshot]，滚动保留最近 MAX_SNAPSHOTS 条
        self._history: dict[str, list[Snapshot]] = defaultdict(list)
        # 缓存同类对比数据（每10分钟刷新一次）
        self._peer_cache: PeerComparison | None = None
        self._peer_cache_time: float = 0.0
        self._peer_cache_ttl: float = 600  # 10 分钟

    def record_snapshot(self, token_address: str, snapshot: Snapshot) -> None:
        """记录一个快照"""
        history = self._history[token_address]
        history.append(snapshot)
        if len(history) > MAX_SNAPSHOTS:
            history.pop(0)

    async def analyze(
        self,
        token_address: str,
        chain: str,
        entry_price: float,
        entry_time: float,
        initial_holders: int,
        initial_volume: float,
    ) -> HoldingDiagnosis:
        """
        全面分析一个持仓代币，返回诊断报告。
        并行查询多个数据源，最大化效率。
        """
        # 并行查询：代币数据 + 聪明钱 + 同类对比
        token_task = client.query_token(token_address, chain)
        smart_task = self._query_smart_money(token_address, chain)
        peer_task = self._query_peers(chain)

        results = await asyncio.gather(
            token_task, smart_task, peer_task,
            return_exceptions=True,
        )

        token_data = results[0] if not isinstance(results[0], Exception) else {}
        smart_money = results[1] if not isinstance(results[1], Exception) else SmartMoneyInfo()
        peers = results[2] if not isinstance(results[2], Exception) else PeerComparison()

        if not isinstance(token_data, dict):
            token_data = {}

        trade_info = token_data.get("tradeInfo") or {}
        pair_info = token_data.get("pairInfo") or {}
        link_info = token_data.get("linkInfo") or {}
        dev_info = token_data.get("dev") or {}

        name = token_data.get("baseSymbol") or token_data.get("name", "?")
        symbol = token_data.get("baseSymbol") or token_data.get("symbol", "?")

        current_price = float(trade_info.get("price", 0) or 0)
        holders = int(trade_info.get("holder", 0) or 0)
        volume = float(trade_info.get("hourTradeVolume", 0) or 0)
        mc = float(trade_info.get("marketCapUsd", 0) or trade_info.get("marketCapUSD", 0) or 0)
        liquidity = float(pair_info.get("liquidateUsd", 0) or pair_info.get("liquidity", 0) or 0)
        dev_pct = float(dev_info.get("pct", 0) or 0)
        top10_pct = float(token_data.get("topHolderPct", 0) or 0)
        trade_count = int(trade_info.get("hourTradeNum", 0) or 0)

        # 记录快照
        snap = Snapshot(
            timestamp=time.time(),
            price=current_price,
            holders=holders,
            volume_1h=volume,
            market_cap=mc,
            liquidity=liquidity,
            dev_pct=dev_pct,
            top10_pct=top10_pct,
            trade_count=trade_count,
        )
        self.record_snapshot(token_address, snap)

        history = self._history[token_address]

        # 计算趋势
        holder_trend = self._calc_trend([s.holders for s in history], "holders")
        volume_trend = self._calc_volume_trend([s.volume_1h for s in history])
        price_trend = self._calc_trend([s.price for s in history], "price")

        # Dev 抛售检测
        dev_selling = False
        if len(history) >= 2:
            dev_selling = history[-1].dev_pct < history[0].dev_pct - 2  # Dev 减仓超过2%

        # Top10 集中度变化
        top10_concentrating = False
        if len(history) >= 2:
            top10_concentrating = history[-1].top10_pct > history[0].top10_pct + 5

        # 社交信号
        social = SocialSignal(
            has_active_twitter=bool(link_info.get("x")),
            has_active_telegram=bool(link_info.get("tg")),
            has_website=bool(link_info.get("web")),
        )
        social_count = sum([social.has_website, social.has_active_twitter, social.has_active_telegram])
        if social_count >= 3:
            social.social_score = "活跃"
        elif social_count >= 2:
            social.social_score = "一般"
        else:
            social.social_score = "冷门"

        # Reddit 提及（异步检查）
        social.reddit_mentions = await self._check_reddit_mentions(name, symbol)
        if social.reddit_mentions > 0:
            social.social_score = "火热" if social.reddit_mentions >= 3 else "活跃"

        # 同类对比排名
        if peers.trending_count > 0:
            peers.rank_holders = "高于平均" if holders > peers.avg_holder_count else "低于平均"
            peers.rank_volume = "高于平均" if volume > peers.avg_volume else "低于平均"
            peers.rank_mc = "高于平均" if mc > peers.avg_market_cap else "低于平均"

        multiplier = current_price / entry_price if entry_price > 0 else 0
        hold_minutes = (time.time() - entry_time) / 60

        return HoldingDiagnosis(
            token_address=token_address,
            name=name,
            symbol=symbol,
            snapshots=history,
            holder_trend=holder_trend,
            volume_trend=volume_trend,
            price_trend=price_trend,
            smart_money=smart_money,
            dev_selling=dev_selling,
            top10_concentrating=top10_concentrating,
            peers=peers,
            social=social,
            current_price=current_price,
            multiplier=multiplier,
            hold_minutes=hold_minutes,
        )

    async def get_ai_verdict(self, diag: HoldingDiagnosis) -> dict:
        """
        把完整诊断报告交给 MiniMax M2.7 做最终研判。
        返回: {"action": "HOLD"|"SELL", "confidence": 0-100, "reason": str}
        """
        if not minimax.available:
            return {"action": "HOLD", "confidence": 0, "reason": "MiniMax 未配置"}

        # 构建趋势文本
        trend_text = self._format_trend(diag)
        smart_text = self._format_smart_money(diag.smart_money)
        peer_text = self._format_peers(diag.peers)
        social_text = self._format_social(diag.social)
        risk_text = self._format_risks(diag)

        context = (
            f"=== 代币: {diag.name} ({diag.symbol}) ===\n"
            f"合约: {diag.token_address}\n"
            f"当前盈亏: {diag.multiplier:.2f}x | 已持有: {diag.hold_minutes:.0f}分钟\n\n"
            f"【趋势分析（最近{len(diag.snapshots)}个数据点）】\n{trend_text}\n\n"
            f"【链上行为】\n{smart_text}\n{risk_text}\n\n"
            f"【同类对比】\n{peer_text}\n\n"
            f"【社交热度】\n{social_text}\n\n"
            f"请综合所有维度，判断是继续持有还是卖出。"
        )

        return await minimax.analyze_holding_deep(context)

    # ── 数据查询 ─────────────────────────────────────────

    async def _query_smart_money(self, token_address: str, chain: str) -> SmartMoneyInfo:
        """查询聪明钱动态"""
        try:
            wallets = await client.smart_wallets(token_address, chain)
        except Exception as e:
            logger.debug("smart money query error: %s", e)
            return SmartMoneyInfo()

        if not wallets:
            return SmartMoneyInfo()

        info = SmartMoneyInfo(wallet_count=len(wallets))

        for w in wallets:
            val = float(w.get("holdingValueUSD", 0) or w.get("valueUSD", 0) or 0)
            info.total_value_usd += val
            action = (w.get("action", "") or "").lower()
            if action in ("buy", "bought", "buying"):
                info.buying_count += 1
            elif action in ("sell", "sold", "selling"):
                info.selling_count += 1

        if info.buying_count > info.selling_count:
            info.net_action = "净买入"
        elif info.selling_count > info.buying_count:
            info.net_action = "净卖出"
        elif info.wallet_count > 0:
            info.net_action = "观望"

        return info

    async def _query_peers(self, chain: str) -> PeerComparison:
        """查询同类热门币数据做对比（带缓存）"""
        now = time.time()
        if self._peer_cache and now - self._peer_cache_time < self._peer_cache_ttl:
            return self._peer_cache

        try:
            trending = await client.ai_trending(chain)
        except Exception as e:
            logger.debug("peer query error: %s", e)
            return PeerComparison()

        if not trending:
            return PeerComparison()

        total_holders = 0
        total_volume = 0.0
        total_mc = 0.0
        count = 0

        for t in trending:
            h = int(t.get("holders", 0) or t.get("holder", 0) or 0)
            v = float(t.get("hourTradeVolume", 0) or t.get("volume", 0) or 0)
            m = float(t.get("marketCapUSD", 0) or t.get("marketCapUsd", 0) or 0)
            if h > 0 or v > 0:
                total_holders += h
                total_volume += v
                total_mc += m
                count += 1

        if count == 0:
            return PeerComparison()

        result = PeerComparison(
            avg_holder_count=total_holders // count,
            avg_volume=total_volume / count,
            avg_market_cap=total_mc / count,
            trending_count=count,
        )

        self._peer_cache = result
        self._peer_cache_time = now
        return result

    async def _check_reddit_mentions(self, name: str, symbol: str) -> int:
        """快速检查 Reddit 是否有提及（复用已有的 subreddit 数据）"""
        try:
            import httpx

            keywords = [name.lower(), symbol.lower()]
            mentions = 0
            subreddits = ["CryptoMoonShots", "SolanaMemecoin", "memecoin"]

            async with httpx.AsyncClient(timeout=8.0, verify=False) as http:
                for sub in subreddits:
                    try:
                        resp = await http.get(
                            f"https://www.reddit.com/r/{sub}/new.json",
                            params={"limit": 25},
                            headers={"User-Agent": "MemeBot/1.0"},
                        )
                        if resp.status_code != 200:
                            continue
                        posts = resp.json().get("data", {}).get("children", [])
                        for post in posts:
                            title = (post.get("data", {}).get("title", "") or "").lower()
                            if any(kw in title for kw in keywords):
                                mentions += 1
                    except Exception:
                        continue

            return mentions
        except Exception:
            return 0

    # ── 趋势计算 ─────────────────────────────────────────

    def _calc_trend(self, values: list, label: str) -> str:
        """从数值序列计算趋势方向"""
        if len(values) < 2:
            return "数据不足"

        first_half = values[: len(values) // 2]
        second_half = values[len(values) // 2:]

        avg_first = sum(first_half) / len(first_half) if first_half else 0
        avg_second = sum(second_half) / len(second_half) if second_half else 0

        if avg_first == 0:
            return "无数据"

        change_pct = (avg_second - avg_first) / avg_first * 100

        if change_pct > 10:
            return "持续增长" if label == "holders" else "上涨"
        elif change_pct < -10:
            return "持续下降" if label == "holders" else "下跌"
        else:
            return "稳定" if label == "holders" else "横盘"

    def _calc_volume_trend(self, volumes: list[float]) -> str:
        """成交量趋势"""
        if len(volumes) < 2:
            return "数据不足"

        if all(v < 100 for v in volumes):
            return "无量"

        first_half = volumes[: len(volumes) // 2]
        second_half = volumes[len(volumes) // 2:]

        avg_first = sum(first_half) / len(first_half) if first_half else 0
        avg_second = sum(second_half) / len(second_half) if second_half else 0

        if avg_first == 0:
            return "无量" if avg_second < 100 else "放量"

        change_pct = (avg_second - avg_first) / avg_first * 100

        if change_pct > 30:
            return "放量"
        elif change_pct < -30:
            return "缩量"
        else:
            return "稳定"

    # ── 格式化输出（给 AI 的文本）────────────────────────

    def _format_trend(self, diag: HoldingDiagnosis) -> str:
        """格式化趋势数据"""
        lines = []
        history = diag.snapshots

        if len(history) >= 2:
            first = history[0]
            last = history[-1]
            mins = (last.timestamp - first.timestamp) / 60

            holder_delta = last.holders - first.holders
            holder_sign = "+" if holder_delta >= 0 else ""

            lines.append(f"观测时长: {mins:.0f}分钟 ({len(history)}个数据点)")
            lines.append(f"持仓人趋势: {diag.holder_trend} ({first.holders}→{last.holders}, {holder_sign}{holder_delta})")
            lines.append(f"成交量趋势: {diag.volume_trend} (${first.volume_1h:,.0f}→${last.volume_1h:,.0f})")
            lines.append(f"价格趋势: {diag.price_trend} (${first.price:.10f}→${last.price:.10f})")
            lines.append(f"市值变化: ${first.market_cap:,.0f}→${last.market_cap:,.0f}")
            lines.append(f"流动性: ${last.liquidity:,.0f}")
            lines.append(f"1h交易笔数: {last.trade_count}")
        else:
            snap = history[-1] if history else None
            if snap:
                lines.append(f"（首次分析，无历史趋势）")
                lines.append(f"持仓人: {snap.holders} | 成交量: ${snap.volume_1h:,.0f}")
                lines.append(f"市值: ${snap.market_cap:,.0f} | 流动性: ${snap.liquidity:,.0f}")

        return "\n".join(lines)

    def _format_smart_money(self, sm: SmartMoneyInfo) -> str:
        """格式化聪明钱数据"""
        if sm.wallet_count == 0:
            return "聪明钱: 无数据（没有聪明钱关注）"

        return (
            f"聪明钱: {sm.wallet_count}个钱包关注 | 总持仓${sm.total_value_usd:,.0f}\n"
            f"  买入: {sm.buying_count}个 | 卖出: {sm.selling_count}个 | 动向: {sm.net_action}"
        )

    def _format_peers(self, peers: PeerComparison) -> str:
        """格式化同类对比"""
        if peers.trending_count == 0:
            return "同类对比: 无数据"

        return (
            f"当前热门币共{peers.trending_count}个\n"
            f"  平均持仓人: {peers.avg_holder_count} (本币: {peers.rank_holders})\n"
            f"  平均成交量: ${peers.avg_volume:,.0f} (本币: {peers.rank_volume})\n"
            f"  平均市值: ${peers.avg_market_cap:,.0f} (本币: {peers.rank_mc})"
        )

    def _format_social(self, social: SocialSignal) -> str:
        """格式化社交信号"""
        channels = []
        if social.has_website:
            channels.append("官网")
        if social.has_active_twitter:
            channels.append("Twitter")
        if social.has_active_telegram:
            channels.append("Telegram")

        lines = [f"社交渠道: {', '.join(channels) if channels else '无'}"]
        lines.append(f"Reddit 近期提及: {social.reddit_mentions}次")
        lines.append(f"综合热度: {social.social_score}")
        return "\n".join(lines)

    def _format_risks(self, diag: HoldingDiagnosis) -> str:
        """格式化风险信号"""
        risks = []
        if diag.dev_selling:
            risks.append("⚠️ Dev 正在减仓（持仓比例下降超2%）")
        if diag.top10_concentrating:
            risks.append("⚠️ Top10 集中度上升（增加超5%），大户在吸筹或准备砸盘")

        sm = diag.smart_money
        if sm.selling_count > sm.buying_count and sm.wallet_count >= 2:
            risks.append("⚠️ 聪明钱净卖出，专业玩家在撤退")

        if not risks:
            return "风险信号: 无明显异常"
        return "风险信号:\n" + "\n".join(risks)


# 全局单例
holding_analyzer = HoldingAnalyzer()
