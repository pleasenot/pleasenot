"""
MiniMax 大模型客户端

用于代币买入前的 AI 智能研判：
- 分析代币是否蹭了真实热点
- 判断项目叙事逻辑是否成立
- 识别垃圾关键词堆砌的空气币
"""
import httpx
from config import config
from utils.logger import get_logger

logger = get_logger(__name__)

MINIMAX_API_URL = "https://api.minimaxi.com/v1/text/chatcompletion_v2"

SYSTEM_PROMPT = """你是一个加密货币 Meme Coin 分析专家，专注于 AI 和量子计算相关的叙事币。

你的任务是评估一个新代币是否值得早期买入。请从以下维度分析：

1. **叙事真实性**（0-30分）：这个币是否蹭了真实存在的 AI/科技热点？还是纯粹瞎编？
   - 例如：蹭 Claude 新模型发布 = 真实热点 = 高分
   - 例如：随便拼凑 AI 关键词 = 垃圾 = 低分

2. **时效性**（0-20分）：是否踩中当前热点时间窗口？
   - 热点刚出来 1-2 天 = 高分
   - 过时的旧闻 = 低分

3. **Meme 传播力**（0-20分）：名字和概念是否容易传播？是否有 meme 潜力？
   - 好记、好传播、有梗 = 高分
   - 无聊、没记忆点 = 低分

4. **项目可信度**（0-30分）：有无官网/Twitter/Telegram？描述是否像正经项目？
   - 有完整社交 + 清晰描述 = 高分
   - 什么都没有 = 低分

请直接输出 JSON 格式：
{"score": 总分0-100, "verdict": "BUY"或"SKIP", "reason": "一句话理由"}

不要输出其他内容，只输出 JSON。"""


class MiniMaxClient:
    def __init__(self):
        self._api_key = config.minimax_api_key
        self._model = config.minimax_model

    @property
    def available(self) -> bool:
        return bool(self._api_key)

    async def analyze_token(
        self,
        name: str,
        symbol: str,
        description: str = "",
        market_cap: float = 0,
        holders: int = 0,
        has_website: bool = False,
        has_twitter: bool = False,
        has_telegram: bool = False,
    ) -> dict:
        """
        用 AI 分析代币是否值得买入。

        返回: {"score": int, "verdict": "BUY"|"SKIP", "reason": str}
        """
        if not self.available:
            return {"score": 0, "verdict": "SKIP", "reason": "MiniMax 未配置"}

        socials = []
        if has_website:
            socials.append("官网")
        if has_twitter:
            socials.append("Twitter")
        if has_telegram:
            socials.append("Telegram")

        user_msg = (
            f"代币名称: {name}\n"
            f"符号: {symbol}\n"
            f"描述: {description or '无'}\n"
            f"市值: ${market_cap:,.0f}\n"
            f"持仓人数: {holders}\n"
            f"社交: {', '.join(socials) if socials else '无'}\n"
            f"\n请分析这个代币是否值得买入。"
        )

        try:
            async with httpx.AsyncClient(timeout=15.0) as http:
                resp = await http.post(
                    MINIMAX_API_URL,
                    headers={
                        "Authorization": f"Bearer {self._api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": self._model,
                        "messages": [
                            {"role": "system", "content": SYSTEM_PROMPT},
                            {"role": "user", "content": user_msg},
                        ],
                        "max_completion_tokens": 200,
                        "temperature": 0.5,
                    },
                )

            if resp.status_code != 200:
                logger.warning("MiniMax API error: %d %s", resp.status_code, resp.text[:100])
                return {"score": 0, "verdict": "SKIP", "reason": f"API error {resp.status_code}"}

            data = resp.json()
            content = (
                data.get("choices", [{}])[0]
                .get("message", {})
                .get("content", "")
            )

            # 解析 JSON 响应
            import json
            result = json.loads(content.strip())
            logger.info(
                "MiniMax 分析 %s(%s): score=%d verdict=%s reason=%s",
                name, symbol, result.get("score", 0),
                result.get("verdict", "?"), result.get("reason", "?"),
            )
            return result

        except Exception as e:
            logger.warning("MiniMax analyze error: %s", e)
            return {"score": 0, "verdict": "SKIP", "reason": str(e)}


minimax = MiniMaxClient()
