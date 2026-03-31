import os
from dataclasses import dataclass, field
from dotenv import load_dotenv

load_dotenv()


@dataclass
class Config:
    # XXYY API
    api_key: str = field(default_factory=lambda: os.environ["XXYY_API_KEY"])
    api_base_url: str = field(default_factory=lambda: os.getenv("XXYY_API_BASE_URL", "https://www.xxyy.io"))

    # 默认交易链
    default_chain: str = field(default_factory=lambda: os.getenv("DEFAULT_CHAIN", "sol"))

    # 默认钱包地址（买入时需要）
    wallet_address: str = field(default_factory=lambda: os.getenv("WALLET_ADDRESS", ""))

    # 买入金额（原生币，如 0.1 SOL）
    buy_amount: float = field(default_factory=lambda: float(os.getenv("BUY_AMOUNT", "0.1")))

    # 卖出比例（1-100%）
    sell_percent: int = field(default_factory=lambda: int(os.getenv("SELL_PERCENT", "100")))

    # 优先费（SOL 推荐 0.001-0.1）
    tip: float = field(default_factory=lambda: float(os.getenv("TIP", "0.005")))

    # Feed 扫描间隔（秒）
    feed_interval: int = field(default_factory=lambda: int(os.getenv("FEED_INTERVAL", "10")))

    # 买入前分析器最低通过分数（0-100，越高越严格）
    analyzer_min_score: int = field(default_factory=lambda: int(os.getenv("ANALYZER_MIN_SCORE", "50")))

    # Twitter 监控账号列表（逗号分隔）
    twitter_accounts: list[str] = field(default_factory=lambda: [
        a.strip() for a in os.getenv("TWITTER_ACCOUNTS", "").split(",") if a.strip()
    ])


config = Config()
