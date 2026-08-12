<div align="center">

# 🤖 OKX Trading MCP Server

**让 AI 帮你盯盘、开仓、平仓、自动挂止损止盈——全在一个聊天框里完成**

*Give your AI assistant direct OKX crypto futures trading powers — open, close, manage SL/TP, all from chat.*

![Version](https://img.shields.io/badge/version-1.0.0-blue) ![License](https://img.shields.io/badge/license-MIT-green) ![Protocol](https://img.shields.io/badge/MCP-✓-green)

</div>

---

## 🚀 What is this?

An MCP (Model Context Protocol) server that connects any AI client — Claude Desktop, Cursor, Cherry Studio, Hermes, Windsurf — to your OKX futures account. Say it in plain language, the AI executes:

- 📊 查余额 / 持仓 / 行情
- ⚡ 一句话市价开仓、平仓（reduceOnly 保护）
- 🛡 自动挂止损止盈（条件单，交易所服务器执行）
- 📋 条件单管理、一键清理
- ⚙️ 杠杆设置（1–125x，双向持仓适配）

**10 个工具，覆盖完整交易闭环。** 所有 OKX API 的坑（双向持仓模式、张数取整、条件单残留、追踪止损兼容）都已踩平。

## 🎁 免费使用 · Free & Open Source

本项目**完全免费**，MIT 协议开源，无需购买、无需注册：

- 全部 10 个交易工具 + 安装向导 + 中英文文档
- **`pro/` 增强脚本**（同仓库免费获取）：
  - 📡 `market_scan_wide.py` — 全市场 RSI 信号扫描器
  - 📈 `backtest_rsi.py` — 策略回测（含手续费模型）
  - 🔧 `okx_utils.py` — 共享工具库（统一凭证/指标计算）

**🚀 快速开始：**

```bash
 git clone https://github.com/wl18295600077-a11y/okx-trading-mcp.git
 cd okx-trading-mcp
 python setup_env.py    # 配置 OKX API Key（只读本地 .env，安全）
```

然后按 `examples/mcp-config.json` 把服务器接入任意 MCP 客户端即可（Claude Desktop / Cursor / Cherry Studio / Hermes / Windsurf）。

**📄 产品介绍页 → [https://cdn.jsdelivr.net/gh/wl18295600077-a11y/okx-trading-mcp@main/index.html](https://cdn.jsdelivr.net/gh/wl18295600077-a11y/okx-trading-mcp@main/index.html)**

> 💡 喜欢这个项目？点个 ⭐ Star 就是最大的支持！

## ⚠️ Disclaimer

加密货币合约交易风险极高，可能导致全部本金损失。本工具仅提供交易自动化能力，不构成投资建议。*Cryptocurrency futures trading carries extreme risk. This tool provides trading automation only, NOT investment advice.*

---

<div align="center">© 2026 OKX Trading MCP · 非 OKX 官方产品 · Not an official OKX product</div>