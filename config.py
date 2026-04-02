import os
from dataclasses import dataclass, field
from dotenv import load_dotenv

load_dotenv()


@dataclass
class Config:
    # XXYY API（多 key 分流，每个 key 独立 1QPS）
    api_key: str = field(default_factory=lambda: os.environ["XXYY_API_KEY"])
    api_key_swap: str = field(default_factory=lambda: os.getenv("XXYY_API_KEY_SWAP", ""))
    api_key_scanner: str = field(default_factory=lambda: os.getenv("XXYY_API_KEY_SCANNER", ""))
    api_key_analyzer: str = field(default_factory=lambda: os.getenv("XXYY_API_KEY_ANALYZER", ""))
    api_key_monitor: str = field(default_factory=lambda: os.getenv("XXYY_API_KEY_MONITOR", ""))
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

    # Twitter API Bearer Token（用于推文监控，可选）
    twitter_bearer_token: str = field(default_factory=lambda: os.getenv("TWITTER_BEARER_TOKEN", ""))

    # TikTok 趋势 API Key（可选，用于 meme 趋势扫描）
    tiktok_api_key: str = field(default_factory=lambda: os.getenv("TIKTOK_API_KEY", ""))

    # MiniMax 大模型（用于代币 AI 智能研判）
    minimax_api_key: str = field(default_factory=lambda: os.getenv("MINIMAX_API_KEY", ""))
    minimax_model: str = field(default_factory=lambda: os.getenv("MINIMAX_MODEL", "MiniMax-M2.7-highspeed"))

    # Twitter 额外监控账号（逗号分隔，会与内置名人列表合并）
    twitter_accounts: list[str] = field(default_factory=lambda: [
        a.strip() for a in os.getenv("TWITTER_ACCOUNTS", "").split(",") if a.strip()
    ])


config = Config()
