"""
社交平台 Meme 趋势扫描器

监控 Reddit / TikTok / Instagram 上的病毒式 meme，
当链上出现同名代币时及时介入。

工作流程：
1. 定期抓取各平台热门内容
2. 提取高频关键词/话题
3. 用关键词去链上搜索匹配的新代币
4. 匹配成功 → 触发买入信号

数据源：
- Reddit: 通过 JSON API（无需 OAuth）抓取热帖标题
- TikTok: 预留（需接入第三方趋势 API）
- Instagram: 预留（需接入第三方趋势 API）
"""
import asyncio
import re
from typing import Callable

from signals.base import BaseSignalSource, TradeSignal
from signals.meme_keywords import is_meme_related, REDDIT_SUBREDDITS
from xxyy.client import client
from config import config
from utils.logger import get_logger

logger = get_logger(__name__)


class SocialTrendScanner(BaseSignalSource):
    """
    扫描社交平台热点 meme，交叉匹配链上新代币。

    双重机制：
    1. Reddit 热帖 → 提取 meme 关键词 → 匹配链上代币
    2. XXYY Feed 新币 → meme 关键词过滤 → 命中热点就买
    """

    def __init__(
        self,
        chain: str | None = None,
        interval: int = 60,
        feed_interval: int | None = None,
    ):
        self.chain = chain or config.default_chain
        self.interval = interval          # 社交平台扫描间隔（秒）
        self.feed_interval = feed_interval or config.feed_interval
        self._seen_tokens: set[str] = set()
        self._trending_words: set[str] = set()  # 当前热点词

    async def start(self, on_signal: Callable[[TradeSignal], None]) -> None:
        logger.info(
            "SocialTrendScanner started chain=%s social_interval=%ds feed_interval=%ds",
            self.chain, self.interval, self.feed_interval,
        )
        tasks = [
            asyncio.create_task(self._poll_reddit()),
            asyncio.create_task(self._poll_tiktok()),
            asyncio.create_task(self._match_feed_tokens(on_signal)),
        ]
        await asyncio.gather(*tasks, return_exceptions=True)

    # ── Reddit 热帖扫描 ─────────────────────────────────────

    async def _poll_reddit(self) -> None:
        """轮询 Reddit 热帖，提取 meme 关键词"""
        try:
            import httpx
        except ImportError:
            logger.error("需要 httpx 库来使用 Reddit 扫描")
            return

        logger.info("Reddit scanner started, watching %d subreddits", len(REDDIT_SUBREDDITS))

        async with httpx.AsyncClient(
            headers={"User-Agent": "MemeBot/1.0"},
            timeout=15.0,
        ) as http:
            while True:
                new_words: set[str] = set()
                for sub in REDDIT_SUBREDDITS:
                    try:
                        words = await self._scan_subreddit(http, sub)
                        new_words.update(words)
                    except Exception as e:
                        logger.debug("reddit scan r/%s error: %s", sub, e)

                if new_words:
                    added = new_words - self._trending_words
                    if added:
                        logger.info("Reddit 新热点词: %s", added)
                    self._trending_words.update(new_words)

                await asyncio.sleep(self.interval)

    async def _scan_subreddit(self, http, subreddit: str) -> set[str]:
        """扫描单个子版块的热帖，返回命中的 meme 关键词"""
        resp = await http.get(
            f"https://www.reddit.com/r/{subreddit}/hot.json",
            params={"limit": 25},
        )
        if resp.status_code != 200:
            return set()

        data = resp.json()
        posts = data.get("data", {}).get("children", [])
        found: set[str] = set()

        for post in posts:
            post_data = post.get("data", {})
            title = post_data.get("title", "")
            flair = post_data.get("link_flair_text", "")
            text = f"{title} {flair}"

            matched, keyword = is_meme_related(text, "", "")
            if matched:
                found.add(keyword.lower())

            # 也检查高互动帖子的自定义词（高赞 = 可能成为趋势）
            score = post_data.get("score", 0)
            if score >= 5000:
                # 提取标题中的潜在 meme 名词（大写词、特殊词）
                words = re.findall(r'\b[A-Z][a-z]+(?:\s[A-Z][a-z]+)*\b', title)
                for w in words:
                    if len(w) >= 4:
                        found.add(w.lower())

        return found

    # ── TikTok 趋势扫描（预留）──────────────────────────────

    async def _poll_tiktok(self) -> None:
        """
        TikTok 趋势监控。

        接入方案（按优先级）：
        1. 第三方 TikTok 趋势 API（如 TokAPI、Ensembl）
        2. TikTok Research API（需申请）
        3. 爬虫抓取 trending hashtags

        需要配置 TIKTOK_API_KEY 环境变量后启用。
        """
        tiktok_key = config.tiktok_api_key
        if not tiktok_key:
            logger.info(
                "TikTok 趋势扫描未启用（未配置 TIKTOK_API_KEY）。"
                "Reddit + 链上匹配仍正常运行。"
            )
            return

        logger.info("TikTok trend scanner started")

        try:
            import httpx
        except ImportError:
            logger.error("需要 httpx 库")
            return

        async with httpx.AsyncClient(timeout=15.0) as http:
            while True:
                try:
                    # 通用第三方 API 接口（按实际服务调整）
                    resp = await http.get(
                        "https://api.tokapi.online/v1/trending/hashtags",
                        headers={"X-API-Key": tiktok_key},
                        params={"count": 30},
                    )
                    if resp.status_code == 200:
                        hashtags = resp.json().get("data", [])
                        for tag in hashtags:
                            name = tag.get("name", "") if isinstance(tag, dict) else str(tag)
                            matched, keyword = is_meme_related(name, "", "")
                            if matched:
                                self._trending_words.add(keyword.lower())
                                logger.info("TikTok 热点 meme: #%s → %s", name, keyword)
                except Exception as e:
                    logger.debug("TikTok poll error: %s", e)

                await asyncio.sleep(self.interval)

    # ── 链上代币匹配 ────────────────────────────────────────

    async def _match_feed_tokens(self, on_signal: Callable) -> None:
        """
        轮询链上新代币，用当前热点 meme 关键词匹配。
        双重匹配：内置 meme 关键词库 + 实时从社交平台抓取的热点词。
        """
        logger.info("Meme-token matcher started")

        while True:
            try:
                tokens = await client.feed("NEW", self.chain)
                for token in tokens:
                    ca = token.get("tokenAddress") or token.get("ca")
                    if not ca or ca in self._seen_tokens:
                        continue
                    self._seen_tokens.add(ca)

                    name = token.get("name", "")
                    symbol = token.get("symbol", "")
                    desc = token.get("description", "")

                    # 方式1：内置 meme 关键词库匹配
                    matched, keyword = is_meme_related(name, symbol, desc)

                    # 方式2：实时热点词匹配
                    if not matched and self._trending_words:
                        text = f"{name} {symbol} {desc}".lower()
                        for tw in self._trending_words:
                            if tw in text:
                                matched = True
                                keyword = f"trending:{tw}"
                                break

                    if not matched:
                        continue

                    mc = float(token.get("marketCapUSD", 0) or 0)
                    holders = token.get("holders", 0)

                    logger.info(
                        "Meme match! ca=%s name=%s symbol=%s keyword=%s mc=$%.0f",
                        ca, name, symbol, keyword, mc,
                    )
                    signal = TradeSignal(
                        chain=self.chain,
                        token_address=ca,
                        action="buy",
                        source=f"meme_trend/{keyword}",
                        reason=f"Meme趋势: {name}({symbol}) keyword={keyword} mc=${mc:.0f}",
                    )
                    if asyncio.iscoroutinefunction(on_signal):
                        await on_signal(signal)
                    else:
                        on_signal(signal)

            except Exception as e:
                logger.error("meme-token match error: %s", e)
            await asyncio.sleep(self.feed_interval)
