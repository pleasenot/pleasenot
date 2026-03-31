# ⚠️ 最重要：API 密钥安全规则

**XXYY_API_KEY 绝对不能提交到 GitHub 或任何版本控制系统。**

- 用户会在对话中提供 `XXYY_API_KEY`，配置到本地环境变量即可
- 每次 `git add` / `git commit` / `git push` 前，必须确认以下文件未被包含：
  - `.env`
  - 任何包含 `XXYY_API_KEY` 或 `xxyy_ak_` 的文件
- `.env` 必须在 `.gitignore` 中

---

# 项目说明

这是一个加密货币自动化交易机器人，主要用于土狗（Meme Coin）交易。

## 技术栈
- 交易执行：XXYY Open API（支持 Solana、ETH、BSC、Base）
- 信号来源：待定（基于 X/Twitter 等社交信号）

## 环境变量
```bash
export XXYY_API_KEY=xxyy_ak_xxxx   # 从 https://www.xxyy.io/apikey 获取，绝对不提交到 git
export XXYY_API_BASE_URL=https://www.xxyy.io  # 可选
```
