"""XXYY Open API 客户端封装"""
import asyncio
import time
import httpx
from typing import Any

from config import config
from utils.logger import get_logger

logger = get_logger(__name__)

BASE = config.api_base_url
PREFIX = "/api/trade/open/api"

# 查询缓存 TTL（秒）
CACHE_TTL = 15  # 同一个代币 15 秒内不重复查（配合 position monitor 15s 检查间隔）


class XxyyAPIError(Exception):
    def __init__(self, code: int, msg: str):
        self.code = code
        self.msg = msg
        super().__init__(f"[{code}] {msg}")


class APIHealthMonitor:
    """API 健康监测：追踪成功/失败，连续失败告警"""

    ALERT_THRESHOLD = 5          # 连续失败 5 次触发告警
    RECOVERY_LOG_INTERVAL = 1    # 恢复后立即通知
    STATUS_FILE = "api_health.json"

    def __init__(self):
        self._consecutive_failures = 0
        self._total_success = 0
        self._total_failures = 0
        self._last_success_time = time.time()
        self._last_failure_time = 0.0
        self._alerted = False        # 已发过告警（避免重复刷日志）
        self._downtime_start = 0.0   # 本次宕机开始时间

    def record_success(self) -> None:
        self._total_success += 1
        was_down = self._consecutive_failures >= self.ALERT_THRESHOLD
        self._consecutive_failures = 0
        self._last_success_time = time.time()

        if was_down and self._alerted:
            downtime = time.time() - self._downtime_start
            logger.info(
                "🟢 API 恢复正常！宕机时长: %.0f秒 (%.1f分钟)",
                downtime, downtime / 60,
            )
            self._alerted = False
            self._downtime_start = 0.0
            self._save_status()

    def record_failure(self, error: str = "") -> None:
        self._consecutive_failures += 1
        self._total_failures += 1
        self._last_failure_time = time.time()

        if self._consecutive_failures == self.ALERT_THRESHOLD:
            self._downtime_start = time.time()
            self._alerted = True
            logger.error(
                "🔴🔴🔴 API 连续失败 %d 次！止盈/止损可能失效！最近错误: %s",
                self._consecutive_failures, error,
            )
            self._save_status()
        elif self._alerted and self._consecutive_failures % 10 == 0:
            # 每 10 次失败再提醒一次
            downtime = time.time() - self._downtime_start
            logger.error(
                "🔴 API 持续异常，已连续失败 %d 次 (%.0f秒)，止盈/止损失效中！",
                self._consecutive_failures, downtime,
            )
            self._save_status()

    @property
    def is_healthy(self) -> bool:
        return self._consecutive_failures < self.ALERT_THRESHOLD

    @property
    def consecutive_failures(self) -> int:
        return self._consecutive_failures

    def status(self) -> str:
        if self.is_healthy:
            return f"🟢 健康 (成功:{self._total_success} 失败:{self._total_failures})"
        downtime = time.time() - self._downtime_start if self._downtime_start else 0
        return (
            f"🔴 异常 连续失败:{self._consecutive_failures} "
            f"宕机:{downtime:.0f}秒 "
            f"(总成功:{self._total_success} 总失败:{self._total_failures})"
        )

    def _save_status(self) -> None:
        """写入状态文件，方便外部监控"""
        import json, os
        status = {
            "healthy": self.is_healthy,
            "consecutive_failures": self._consecutive_failures,
            "total_success": self._total_success,
            "total_failures": self._total_failures,
            "last_success": self._last_success_time,
            "last_failure": self._last_failure_time,
            "downtime_start": self._downtime_start,
        }
        try:
            path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), self.STATUS_FILE)
            with open(path, "w") as f:
                json.dump(status, f, indent=2)
        except Exception:
            pass


# 全局健康监测实例
api_health = APIHealthMonitor()


class XxyyClient:
    def __init__(self):
        self._client = httpx.AsyncClient(
            base_url=BASE,
            headers={"Authorization": f"Bearer {config.api_key}"},
            timeout=30.0,
        )
        # 全局请求节流：避免并发请求触发 429
        self._throttle = asyncio.Semaphore(1)
        self._min_interval = 1.0  # 最小请求间隔（秒），官方限制 1 QPS
        self._last_request = 0.0
        # 查询结果缓存：{cache_key: (timestamp, data)}
        self._cache: dict[str, tuple[float, Any]] = {}
        self._max_cache = 200  # 最多缓存 200 条

    async def close(self):
        await self._client.aclose()

    async def _wait_throttle(self) -> None:
        """全局请求节流"""
        async with self._throttle:
            import time
            now = time.monotonic()
            wait = self._min_interval - (now - self._last_request)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_request = time.monotonic()

    def _cache_get(self, key: str) -> Any | None:
        """查缓存，未命中或过期返回 None"""
        entry = self._cache.get(key)
        if entry and (time.monotonic() - entry[0]) < CACHE_TTL:
            return entry[1]
        return None

    def _cache_set(self, key: str, data: Any) -> None:
        """写缓存，超限时清理最旧条目"""
        if len(self._cache) >= self._max_cache:
            # 删掉最旧的 1/4
            sorted_keys = sorted(self._cache, key=lambda k: self._cache[k][0])
            for k in sorted_keys[:self._max_cache // 4]:
                del self._cache[k]
        self._cache[key] = (time.monotonic(), data)

    async def _request_with_retry(self, method: str, url: str, **kwargs) -> httpx.Response:
        """带 429 重试的请求，最多重试 3 次，指数退避"""
        for attempt in range(4):
            await self._wait_throttle()
            if method == "GET":
                resp = await self._client.get(url, **kwargs)
            else:
                resp = await self._client.post(url, **kwargs)

            if resp.status_code != 429:
                return resp

            if attempt < 3:
                wait = 3 * (attempt + 1)  # 3s, 6s, 9s
                logger.debug("429 限流，%ds 后重试 (attempt %d)", wait, attempt + 1)
                await asyncio.sleep(wait)

        return resp  # 最后一次的响应

    async def _get(self, path: str, **params) -> Any:
        try:
            resp = await self._request_with_retry("GET", f"{PREFIX}{path}", params=params)
        except (httpx.ConnectError, httpx.ReadTimeout, httpx.WriteTimeout, httpx.PoolTimeout) as e:
            api_health.record_failure(f"网络异常: {e}")
            raise
        return self._parse(resp)

    async def _post(self, path: str, body: dict) -> Any:
        try:
            resp = await self._request_with_retry("POST", f"{PREFIX}{path}", json=body)
        except (httpx.ConnectError, httpx.ReadTimeout, httpx.WriteTimeout, httpx.PoolTimeout) as e:
            api_health.record_failure(f"网络异常: {e}")
            raise
        return self._parse(resp)

    def _parse(self, resp: httpx.Response) -> Any:
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") != 200:
            error = XxyyAPIError(data.get("code", -1), data.get("msg", "unknown error"))
            api_health.record_failure(str(error))
            raise error
        api_health.record_success()
        return data.get("data")

    # ── 基础 ──────────────────────────────────────────────

    async def ping(self) -> str:
        return await self._get("/ping")

    # ── 钱包 ──────────────────────────────────────────────

    async def list_wallets(self, chain: str, page: int = 1, size: int = 20,
                           token_address: str = "") -> list[dict]:
        params = dict(chain=chain, pageNum=page, pageSize=size)
        if token_address:
            params["tokenAddress"] = token_address
        return await self._get("/wallets", **params)

    async def wallet_info(self, wallet_address: str, chain: str,
                          token_address: str = "") -> dict:
        params = dict(walletAddress=wallet_address, chain=chain)
        if token_address:
            params["tokenAddress"] = token_address
        return await self._get("/wallet/info", **params)

    async def wallet_holdings(self, wallet_address: str, chain: str) -> list[dict]:
        """查询钱包持有的所有代币"""
        try:
            result = await self._get("/wallet/holdings", walletAddress=wallet_address, chain=chain)
        except (XxyyAPIError, httpx.HTTPStatusError) as e:
            if "404" in str(e) or (isinstance(e, XxyyAPIError) and e.code == 404):
                logger.debug("wallet/holdings endpoint not available")
                return []
            raise
        if isinstance(result, list):
            return result
        if isinstance(result, dict):
            return result.get("items", [])
        return []

    # ── 代币查询 ──────────────────────────────────────────

    async def query_token(self, ca: str, chain: str) -> dict:
        cache_key = f"query:{chain}:{ca}"
        cached = self._cache_get(cache_key)
        if cached is not None:
            return cached
        result = await self._get("/query", ca=ca, chain=chain)
        if isinstance(result, dict):
            self._cache_set(cache_key, result)
        return result

    # ── 交易 ──────────────────────────────────────────────

    async def swap(
        self,
        chain: str,
        wallet_address: str,
        token_address: str,
        is_buy: bool,
        amount: float,
        tip: float | None = None,
        slippage: int = 20,
        model: int = 1,
        priority_fee: float | None = None,
    ) -> str:
        """
        发起买入或卖出，返回 txId。
        卖出时 amount 为百分比(1-100)。
        model: 1=防夹子模式(默认), 2=快速模式
        slippage: 滑点容差百分比(0-100, 默认20)
        priority_fee: Solana 额外优先费(SOL)，网络拥堵时提高成交率
        """
        body = {
            "chain": chain,
            "walletAddress": wallet_address,
            "tokenAddress": token_address,
            "isBuy": is_buy,
            "amount": amount,
            "tip": tip if tip is not None else config.tip,
            "model": model,
            "slippage": slippage,
        }
        if priority_fee is not None and chain == "sol":
            body["priorityFee"] = priority_fee
        result = await self._post("/swap", body)
        if isinstance(result, dict):
            tx_id = result.get("signature") or result.get("txId")
        else:
            tx_id = result
        logger.info("swap submitted txId=%s buy=%s ca=%s", tx_id, is_buy, token_address)
        return tx_id

    async def get_trade(self, tx_id: str) -> dict:
        return await self._get("/trade", txId=tx_id)

    async def wait_trade(self, tx_id: str, retries: int = 12, interval: int = 5) -> dict:
        """轮询交易状态，最多重试 retries 次（默认60秒），每次间隔 interval 秒。"""
        for i in range(retries):
            result = await self.get_trade(tx_id)
            status = result.get("status") if isinstance(result, dict) else None
            logger.info("trade status txId=%s status=%s attempt=%d", tx_id, status, i + 1)
            # status: 1=pending, 2=success, 3=failed
            if status in (2, 3):
                return result
            if i < retries - 1:
                await asyncio.sleep(interval)
        return result

    # ── KOL 跟单 ──────────────────────────────────────────

    async def kol_buys(self, chain: str = "sol") -> list[dict]:
        """获取 KOL 买入列表（兼容旧接口，内部转发到 kol_buy_list）"""
        return await self.kol_buy_list(chain=chain)

    async def smart_wallets(self, token_address: str, chain: str = "sol") -> list[dict]:
        """查询某代币的聪明钱持仓信息。API 路径可能需要调整。"""
        try:
            result = await self._get("/smart-wallet", ca=token_address, chain=chain)
        except (XxyyAPIError, httpx.HTTPStatusError) as e:
            if "404" in str(e) or (isinstance(e, XxyyAPIError) and e.code == 404):
                logger.debug("smart-wallet endpoint not available for ca=%s", token_address)
                return []
            raise
        if isinstance(result, list):
            return result
        if isinstance(result, dict):
            return result.get("items", [])
        return []

    # ── PNL 查询 ──────────────────────────────────────────

    async def pnl(self, wallet_address: str, token_address: str, chain: str = "sol") -> dict:
        """查询钱包-代币对的真实盈亏数据（最近30天）"""
        cache_key = f"pnl:{chain}:{wallet_address}:{token_address}"
        cached = self._cache_get(cache_key)
        if cached is not None:
            return cached
        result = await self._get("/pnl", walletAddress=wallet_address, tokenAddress=token_address, chain=chain)
        if isinstance(result, dict):
            self._cache_set(cache_key, result)
        return result or {}

    # ── 交易历史 ──────────────────────────────────────────

    async def trades(self, wallet_address: str, chain: str = "sol",
                     token_address: str = "", page: int = 1, size: int = 20) -> dict:
        """分页查询成功交易记录，返回 {pageNum, pageSize, total, list}"""
        params = dict(walletAddress=wallet_address, chain=chain, pageNum=page, pageSize=size)
        if token_address:
            params["tokenAddress"] = token_address
        return await self._get("/trades", **params) or {}

    # ── KOL / Smart Money 信号 ────────────────────────────

    async def kol_buy_list(self, chain: str = "sol") -> list[dict]:
        """获取 KOL 最近买入列表"""
        result = await self._get("/kol-buy-list", chain=chain)
        return result if isinstance(result, list) else []

    async def tag_holder_buy_list(self, chain: str = "sol") -> list[dict]:
        """获取聪明钱/大户最近买入列表"""
        result = await self._get("/tag-holder-buy-list", chain=chain)
        return result if isinstance(result, list) else []

    # ── AI 信号 ───────────────────────────────────────────

    async def signal_list(self, chain: str = "sol", signal_type: str = "open-ai-trending") -> list[dict]:
        """获取 AI 趋势信号列表"""
        result = await self._post(f"/signal-list?type={signal_type}&chain={chain}", {})
        return result if isinstance(result, list) else []

    async def ai_trending(self, chain: str = "sol") -> list[dict]:
        """获取 AI 热点代币列表。API 路径可能需要调整。"""
        try:
            result = await self._get("/open-ai-trending", chain=chain)
        except (XxyyAPIError, httpx.HTTPStatusError) as e:
            if "404" in str(e) or (isinstance(e, XxyyAPIError) and e.code == 404):
                logger.debug("ai-trending endpoint not available")
                return []
            raise
        if isinstance(result, list):
            return result
        if isinstance(result, dict):
            return result.get("items", [])
        return []

    # ── 热门代币 ──────────────────────────────────────────

    async def trending_list(self, chain: str = "sol", period: str = "5M") -> list[dict]:
        """获取热门代币列表，period: 1M/5M/30M/1H/6H/24H"""
        result = await self._post(f"/trending-list?chain={chain}", {"period": period})
        return result if isinstance(result, list) else []

    # ── Feed 扫描 ─────────────────────────────────────────

    async def feed(
        self, feed_type: str = "NEW", chain: str = "sol",
        filters: dict | None = None, skip_cache: bool = False,
    ) -> list[dict]:
        """
        feed_type: NEW | ALMOST | COMPLETED
        chain: sol | bsc
        filters: 可选过滤条件（市值、流动性、持仓人数等）
        skip_cache: 高频扫描器设为 True 跳过缓存
        """
        cache_key = f"feed:{feed_type}:{chain}"
        if not skip_cache:
            cached = self._cache_get(cache_key)
            if cached is not None:
                return cached
        body = {"chain": chain, **(filters or {})}
        result = await self._post(f"/feed/{feed_type}", body)
        if isinstance(result, list):
            self._cache_set(cache_key, result)
            return result
        if isinstance(result, dict):
            items = result.get("items", [])
            self._cache_set(cache_key, items)
            return items
        return []


# 全局单例
client = XxyyClient()
