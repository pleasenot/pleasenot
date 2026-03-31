"""
名人推特监控配置

历史影响力分析：
- Elon Musk: DOGE 因其推文多次暴涨 100%+，推"D.O.G.E."概念直接带飞相关币，
  发火箭/改头像都能拉盘，2021年一条"Bitcoin"推文让BTC涨5%
- Donald Trump: 推出 $TRUMP 官方 meme coin，发布 DeFi 项目 World Liberty Financial，
  竞选言论频繁提及 crypto，政策风向直接影响大盘
- Vitalik Buterin: ETH 生态风向标，提到哪个项目哪个就涨，卖币也能砸盘
- CZ (赵长鹏): 币安上币预告、项目点评直接影响价格
- Michael Saylor: MicroStrategy 持续买入 BTC 的信号源，每次发推都影响 BTC 情绪
- Cathie Wood: ARK Invest 对 AI+Crypto 交叉领域的观点影响机构资金
- Sam Altman: OpenAI CEO，AI 相关言论影响 AI 概念币，参与 Worldcoin
- Jensen Huang: NVIDIA CEO，AI 芯片/算力相关言论影响 AI+DePIN 板块
- Marc Andreessen: a16z 掌门人，投资动向影响 AI+Crypto 赛道
- Brian Armstrong: Coinbase CEO，上币/政策言论影响市场
- Arthur Hayes: BitMEX 联创，宏观分析影响交易情绪，喊单影响力大
- Ansem: Solana 生态顶级 KOL，喊单 meme coin 影响力极强
"""

# ── 重点监控账号 ─────────────────────────────────────────
# 格式：(twitter_handle, 影响力等级, 关注领域)
# 等级：S=一条推文能直接拉盘, A=强影响力, B=值得关注

CELEBRITY_ACCOUNTS = [
    # S 级 — 一条推文就能拉盘
    ("elonmusk",        "S", ["meme", "doge", "ai", "tech"]),
    ("realDonaldTrump", "S", ["politics", "meme", "defi"]),

    # A 级 — 强影响力
    ("VitalikButerin",  "A", ["eth", "defi", "zk"]),
    ("caboringduck",    "A", ["sol", "meme"]),          # Ansem
    ("cz_binance",      "A", ["exchange", "altcoin"]),
    ("saborai",         "A", ["sol", "meme"]),           # Murad
    ("blknoiz06",       "A", ["sol", "meme"]),           # Blknoiz06
    ("HsakaTrades",     "A", ["macro", "trading"]),

    # A 级 — AI + Crypto 交叉
    ("sama",            "A", ["ai", "worldcoin"]),       # Sam Altman
    ("JensenHuang",     "A", ["ai", "gpu", "nvidia"]),   # Jensen Huang (非官方)
    ("pmarca",          "A", ["ai", "crypto", "vc"]),    # Marc Andreessen

    # B 级 — 值得关注
    ("saborai",         "B", ["sol", "meme"]),
    ("michael_saylor",  "B", ["btc"]),
    ("CathieDWood",     "B", ["ai", "btc", "innovation"]),
    ("brian_armstrong",  "B", ["exchange", "policy"]),
    ("CryptoHayes",     "B", ["macro", "trading"]),      # Arthur Hayes
    ("aaboringduck",    "B", ["sol", "meme"]),
]

# 去重，提取纯账号列表
CELEBRITY_HANDLES: list[str] = list(dict.fromkeys(
    handle for handle, _, _ in CELEBRITY_ACCOUNTS
))

# 按等级分组
S_TIER = [h for h, tier, _ in CELEBRITY_ACCOUNTS if tier == "S"]
A_TIER = [h for h, tier, _ in CELEBRITY_ACCOUNTS if tier == "A"]
B_TIER = [h for h, tier, _ in CELEBRITY_ACCOUNTS if tier == "B"]

# ── 名人推文中的触发关键词 ───────────────────────────────
# 推文中出现这些词才触发信号（避免无关推文干扰）
CRYPTO_TRIGGER_WORDS = [
    # 直接提币
    "bitcoin", "btc", "ethereum", "eth", "solana", "sol",
    "doge", "dogecoin", "shib", "pepe",
    "crypto", "coin", "token", "blockchain", "web3", "defi",
    "nft", "mint", "airdrop",

    # 买入/看好信号
    "bullish", "moon", "pump", "send it", "lfg", "wagmi",
    "buy", "accumulate", "hodl", "diamond hands",
    "to the moon", "going up",

    # AI + Crypto 交叉
    "ai", "artificial intelligence", "gpt", "claude", "agent",
    "quantum", "pqc",

    # Meme 文化
    "meme", "degen", "ape", "fomo",

    # 合约地址模式（有人直接贴 CA）
    # 这个在 twitter_scanner.py 里通过正则处理
]
