"""信号基类：所有信号源都实现此接口"""
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
