"""AI 相关关键词过滤，用于筛选 AI 概念 meme coin"""
import re

# ── AI 关键词列表 ─────────────────────────────────────────
# 分类整理，方便维护和扩展
# 匹配逻辑：token 的 name / symbol / description 中包含任意关键词即命中

AI_KEYWORDS = [
    # 模型 & 产品
    "gpt", "chatgpt", "openai", "claude", "anthropic", "gemini", "bard",
    "llama", "mistral", "deepseek", "qwen", "copilot", "midjourney",
    "sora", "dall-e", "dalle", "stable diffusion", "flux",
    "grok", "perplexity", "cursor", "devin", "manus",

    # AI 核心概念
    "ai", "artificial intelligence", "machine learning",
    "neural", "transformer", "diffusion", "llm",
    "agi", "asi", "superintelligence",

    # Agent & 工具链
    "agent", "agentic", "mcp", "skill", "tool use",
    "rag", "vector", "embedding", "fine-tune", "finetune",
    "prompt", "chain of thought", "cot",
    "distill", "distillation", "蒸馏",

    # 基础设施 & 芯片
    "nvidia", "cuda", "gpu", "tpu", "tensor",
    "h100", "h200", "b100", "b200", "blackwell",

    # AI 公司 & 实验室
    "deepmind", "meta ai", "xai", "inflection",
    "cohere", "hugging face", "huggingface", "replicate",
    "stability ai", "stabilityai",

    # 加密 x AI
    "ai16z", "virtuals", "fetch.ai", "fetchai",
    "singularity", "ocean protocol", "render",
    "bittensor", "tao", "worldcoin",

    # 热点事件相关（可随时添加）
    "claude code", "myths", "opus", "sonnet", "haiku",
]

# 预编译正则：每个关键词作为独立 pattern，忽略大小写
# 对于 2 字符以下的关键词（如 "ai"）要求词边界匹配，避免误命中
_patterns: list[tuple[re.Pattern, str]] = []
for kw in AI_KEYWORDS:
    if len(kw) <= 2:
        pattern = re.compile(rf"\b{re.escape(kw)}\b", re.IGNORECASE)
    else:
        pattern = re.compile(re.escape(kw), re.IGNORECASE)
    _patterns.append((pattern, kw))


def is_ai_related(name: str, symbol: str, description: str = "") -> tuple[bool, str]:
    """
    检查 token 是否与 AI 相关。

    返回 (是否命中, 命中的关键词)
    """
    text = f"{name} {symbol} {description}"
    for pattern, kw in _patterns:
        if pattern.search(text):
            return True, kw
    return False, ""
