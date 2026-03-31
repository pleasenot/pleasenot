"""
Meme 热点表情包/文化关键词

历史规律：社交平台爆款 meme → 同名 meme coin 出现 → 早期介入有巨大收益
例：Pepe 表情包 → $PEPE, Doge meme → $DOGE, Bonk 狗 → $BONK

监控 TikTok / Instagram / Reddit 上的病毒式传播 meme，
匹配链上同名代币，抢在大众 FOMO 之前介入。
"""
import re

# ── 经典 meme 角色 & IP（已证明能带动 meme coin）─────────
CLASSIC_MEMES = [
    # 动物类 — meme coin 最大类别
    "pepe", "pepe frog", "doge", "dogecoin", "shiba", "shib",
    "bonk", "floki", "neiro", "cat", "popcat", "mog", "brett",
    "wif", "dogwifhat", "cheems", "kabosu",
    "monke", "monkey", "ape", "gorilla",
    "penguin", "pudgy", "pingu",

    # 人物/表情类
    "wojak", "chad", "gigachad", "npc", "doomer", "zoomer", "boomer",
    "troll face", "trollface", "rage comic",
    "stonks", "not stonks",
    "this is fine", "distracted boyfriend",
    "drake", "surprised pikachu",
    "sigma", "sigma male", "skibidi",

    # 病毒式口号/梗
    "gm", "wagmi", "ngmi", "lfg", "hodl", "fomo",
    "to the moon", "wen moon", "wen lambo",
    "cope", "seethe", "based", "cringe",
    "rizz", "gyatt", "ohio", "skibidi toilet",
    "hawk tuah", "mewing", "looksmaxxing",
    "demure", "very demure", "brat", "brat summer",
]

# ── TikTok / Instagram / Reddit 近期高频 meme 趋势 ──────
# 这些需要定期更新，跟热点走
TRENDING_MEMES = [
    # 2024-2025 TikTok 爆款
    "moo deng", "baby hippo",          # 泰国小河马 → $MOODENG 暴涨
    "peanut", "pnut",                  # 松鼠 Peanut → $PNUT
    "just a chill guy",                # chill guy meme → $CHILLGUY
    "goatseus maximus", "goat",        # AI 生成 meme → $GOAT
    "fartcoin", "fart",                # fartcoin 从 meme 到数亿市值
    "act i", "act",                    # AI meme 叙事
    "retardio",                        # SOL meme

    # Reddit / 4chan 文化
    "tendies", "wife's boyfriend",
    "rug pull",                        # 讽刺性 meme coin
    "gme", "gamestop", "roaring kitty", "dfv",
    "wallstreetbets", "wsb",
    "diamond hands", "paper hands",

    # Instagram / 文化现象
    "nft", "metaverse",
    "vibe coding", "vibe",             # AI coding 热潮
    "slop", "ai slop",                 # AI 生成内容梗
]

# ── Reddit 重点监控子版块 ────────────────────────────────
REDDIT_SUBREDDITS = [
    "memes",                 # 最大 meme 子版
    "dankmemes",             # 高质量 meme
    "cryptocurrency",        # 加密讨论
    "CryptoMoonShots",       # 新币推荐
    "wallstreetbets",        # WSB 文化
    "SolanaMemecoin",        # SOL meme 专区
    "memecoin",              # meme coin 讨论
    "TikTokCringe",          # TikTok 热点搬运
    "shitposting",           # 梗文化
]

# ── 合并所有关键词 ───────────────────────────────────────
ALL_MEME_KEYWORDS = CLASSIC_MEMES + TRENDING_MEMES

# 预编译正则
_meme_patterns: list[tuple[re.Pattern, str]] = []
for kw in ALL_MEME_KEYWORDS:
    if len(kw) <= 3:
        pattern = re.compile(rf"\b{re.escape(kw)}\b", re.IGNORECASE)
    else:
        pattern = re.compile(re.escape(kw), re.IGNORECASE)
    _meme_patterns.append((pattern, kw))


def is_meme_related(name: str, symbol: str, description: str = "") -> tuple[bool, str]:
    """
    检查 token 是否与热门 meme 相关。
    返回 (是否命中, 命中的关键词)
    """
    text = f"{name} {symbol} {description}"
    for pattern, kw in _meme_patterns:
        if pattern.search(text):
            return True, kw
    return False, ""
