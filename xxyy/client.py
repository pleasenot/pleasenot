"""XXYY Open API 客户端封装"""
import asyncio
import httpx
from typing import Any

from config import config
from utils.logger import get_logger

logger = get_logger(__name__)

BASE = config.api_base_url
PREFIX = "/api/trade/open/api"


class XxyyAPIError(Exception):
    def __init__(self, code: int, msg: str):
        self.code = code
        self.msg = msg
        super().__init__(f"[{code}] {msg}")


class XxyyClient:
    def __init__(self):
        self._client = httpx.AsyncClient(
            base_url=BASE,
            headers={"Authorization": f"Bearer {config.api_key}"},
            timeout=30.0,
        )

    async def close(self):
        await self._client.aclose()

    async def _get(self, path: str, **params) -> Any:
        resp = await self._client.get(f"{PREFIX}{path}", params=params)
        return self._parse(resp)

    async def _post(self, path: str, body: dict) -> Any:
        resp = await self._client.post(f"{PREFIX}{path}", json=body)
        return self._parse(resp)

    def _parse(self, resp: httpx.Response) -> Any:
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") != 200:
            raise XxyyAPIError(data.get("code", -1), data.get("msg", "unknown error"))
        return data.get("data")

    # ── 基础 ──────────────────────────────────────────────

    async def ping(self) -> str:
        return await self._get("/ping")

    # ── 钱包 ──────────────────────────────────────────────

    async def list_wallets(self, chain: str, page: int = 1, size: int = 20) -> list[dict]:
        return await self._get("/wallets", chain=chain, pageNum=page, pageSize=size)

    async def wallet_info(self, wallet_address: str, chain: str) -> dict:
        return await self._get("/wallet/info", walletAddress=wallet_address, chain=chain)

    # ── 代币查询 ──────────────────────────────────────────

    async def query_token(self, ca: str, chain: str) -> dict:
        return await self._get("/query", ca=ca, chain=chain)

    # ── 交易 ──────────────────────────────────────────────

    async def swap(
        self,
        chain: str,
        wallet_address: str,
        token_address: str,
        is_buy: bool,
        amount: float,
        tip: float | None = None,
    ) -> str:
        """发起买入或卖出，返回 txId。卖出时 amount 为百分比(1-100)。"""
        body = {
            "chain": chain,
            "walletAddress": wallet_address,
            "tokenAddress": token_address,
            "isBuy": is_buy,
            "amount": amount,
            "tip": tip if tip is not None else config.tip,
        }
        result = await self._post("/swap", body)
        tx_id = result.get("txId") if isinstance(result, dict) else result
        logger.info("swap submitted txId=%s buy=%s ca=%s", tx_id, is_buy, token_address)
        return tx_id

    async def get_trade(self, tx_id: str) -> dict:
        return await self._get("/trade", txId=tx_id)

    async def wait_trade(self, tx_id: str, retries: int = 3, interval: int = 5) -> dict:
        """轮询交易状态，最多重试 retries 次，每次间隔 interval 秒。"""
        for i in range(retries):
            result = await self.get_trade(tx_id)
            status = result.get("status") if isinstance(result, dict) else None
            logger.info("trade status txId=%s status=%s attempt=%d", tx_id, status, i + 1)
            if status in ("success", "failed"):
                return result
            if i < retries - 1:
                await asyncio.sleep(interval)
        return result

    # ── Feed 扫描 ─────────────────────────────────────────

    async def feed(self, feed_type: str = "NEW", chain: str = "sol", filters: dict | None = None) -> list[dict]:
        """
        feed_type: NEW | ALMOST | COMPLETED
        chain: sol | bsc
        filters: 可选过滤条件（市值、流动性、持仓人数等）
        """
        body = {"chain": chain, **(filters or {})}
        result = await self._post(f"/feed/{feed_type}", body)
        return result if isinstance(result, list) else []


# 全局单例
client = XxyyClient()
