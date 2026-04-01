"""信号基类：所有信号源都实现此接口"""
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class TradeSignal:
    chain: str          # sol / eth / bsc / base
    token_address: str  # 合约地址
    action: str         # buy | sell
    source: str         # 信号来源描述
    reason: str = ""    # 触发原因（如推文内容）


class BaseSignalSource(ABC):
    @abstractmethod
    async def start(self, on_signal) -> None:
        """启动监控，收到信号时调用 on_signal(TradeSignal)"""
        ...


class TTLSet:
    """带过期时间的去重集合，替代永久 set() 防止内存泄漏"""

    def __init__(self, ttl: float = 3600):
        self._data: dict[str, float] = {}
        self._ttl = ttl

    def __contains__(self, key: str) -> bool:
        ts = self._data.get(key)
        if ts is None:
            return False
        if time.time() - ts > self._ttl:
            del self._data[key]
            return False
        return True

    def add(self, key: str) -> None:
        self._data[key] = time.time()

    def cleanup(self) -> None:
        now = time.time()
        expired = [k for k, ts in self._data.items() if now - ts > self._ttl]
        for k in expired:
            del self._data[k]

    def __len__(self) -> int:
        return len(self._data)
