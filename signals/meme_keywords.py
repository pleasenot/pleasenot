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

# ── 中文 meme / 华语圈热点 ──────────────────────────────
CHINESE_MEMES = [
    # 动物 / 神兽
    "龙", "dragon", "熊猫", "panda", "猴", "猴子",
    "仙鹤", "锦鲤", "麒麟", "凤凰", "蛇",

    # 中国文化 IP
    "悟空", "wukong", "孙悟空", "大圣",
    "哪吒", "nezha",
    "三体", "三体人", "dark forest",
    "功夫", "kungfu", "kung fu",
    "太极", "taichi",
    "财神", "god of wealth",
    "红包", "hongbao",
    "嫦娥", "chang'e",

    # 中文网络梗 / 流行语
    "绝绝子", "yyds", "永远的神",
    "内卷", "involution",
    "躺平", "lying flat",
    "摆烂", "let it rot",
    "社死", "social death",
    "二次元", "waifu", "husbando",
    "打工人", "worker", "996",
    "韭菜", "leek",
    "梭哈", "all in",
    "暴富", "暴涨",
    "割韭菜",
    "冲", "冲冲冲",
    "上车",
    "钻石手", "纸手",

    # 华人名人/热点
    "习近平", "xi jinping",
    "马斯克", "musk",
    "cz", "赵长鹏",
    "孙宇晨", "justin sun",
    "李嘉诚",

    # 中国节日/事件
    "春节", "chinese new year", "cny",
    "中秋", "moon cake", "mooncake",
    "国庆",

    # 中文谐音梗
    "六六六", "666", "八八八", "888",
    "发财", "facai",
    "牛逼", "niubi",

    # 亚洲 meme 文化
    "草泥马", "alpaca",
    "doge", "柴犬",
    "奥特曼", "ultraman",
    "高达", "gundam",

    # 方言 / 谐音梗
    "6", "8", "nb", "niu bi", "niubility",
    "diss", "giao", "奥利给", "aoligei",
    "芜湖", "wuhu",                        # 芜湖起飞
    "绝了", "麻了", "破防",
    "xswl", "笑死我了",
    "awsl", "啊我死了",
    "u1s1",                                # 有一说一
    "zqsg",                                # 真情实感
    "nsdd",                                # 你说得对
    "dbq",                                 # 对不起
    "yygq",                                # 阴阳怪气
    "duck不必",                            # 大可不必谐音
    "蚌埠住了", "bengbu",                   # 绷不住了谐音
    "寄", "寄了",                           # 完蛋了
    "蛤蟆", "toad",                        # 膜蛤文化
    "蛙", "frog",
    "鸡你太美", "ikun", "坤坤", "kun",      # 蔡徐坤梗
    "蒸鹅心", "zheng'exin",                # 真恶心谐音
    "累觉不爱",
    "我太难了",
    "打工人", "dagong",

    # TikTok / 抖音 2024-2025 热词
    "city不city", "city walk",
    "特种兵旅游",
    "多巴胺", "dopamine",
    "美拉德", "maillard",
    "i人", "e人", "mbti",
    "松弛感",
    "搭子", "饭搭子",
    "显眼包",
    "遥遥领先",
    "泼天富贵",
    "命运的齿轮",
    "听我说谢谢你",
    "挖呀挖",
    "科目三", "kemusan",
    "公主请上车",
    "你个老六", "老六", "lao liu",
    "纯爱战士",
    "完美",
    "真的会谢",
    "栓q", "shuan q",
    "退退退",
    "孤勇者",
    "本草纲目",
    "刘畊宏",
    "王心凌",
    "黑神话",
    "temu", "拼多多", "pinduoduo",
    "shein",

    # 加密圈中文黑话
    "土狗", "金狗", "百倍", "千倍", "万倍",
    "起飞", "暴拉", "插针", "砸盘",
    "貔貅", "pixiu",                       # 貔貅盘 = 只进不出
    "老鼠仓",
    "庄家", "主力",
    "抄底", "逃顶",
    "fud", "利好", "利空",
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
    "China_irl",             # 中文社区
    "chonglangTV",           # 华人梗文化
]

# ── 合并所有关键词 ───────────────────────────────────────
ALL_MEME_KEYWORDS = CLASSIC_MEMES + TRENDING_MEMES + CHINESE_MEMES

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
