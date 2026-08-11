<div align="center">

# 🤖 OKX Trading MCP Server

**让 AI 帮你盯盘、开仓、平仓、自动挂止损止盈——全在一个聊天框里完成**

*Give your AI assistant direct OKX crypto futures trading powers — open, close, manage SL/TP, all from chat.*

![Version](https://img.shields.io/badge/version-1.0.0-blue) ![License](https://img.shields.io/badge/license-Commercial-orange) ![Protocol](https://img.shields.io/badge/MCP-✓-green)

**Works with:** Claude Desktop · Cursor · Cherry Studio · Hermes · Windsurf · 任何支持 MCP 的客户端

</div>

---

## ✨ 为什么买它 / Why this product?

| 问题 | 解决方案 |
|------|---------|
| 盯盘累、手速慢，错过最佳点位 | AI 实时查行情，你说一句就下单 |
| 忘了挂止损，爆仓一夜回到解放前 | 一句话自动挂好 SL/TP，条件单在交易所服务器执行 |
| 多个客户端各写各的交易脚本 | 一个 MCP 服务，所有 AI 客户端通用 |
| 交易工具乱、API 坑多 | 开箱即用：ccxt + FastMCP，10 个工具全封装好 |

> 💡 **内置实战经验**：双向持仓模式（long_short_mode）适配、张数取整、旧条件单自动清理、追踪止损兼容——所有 OKX API 的坑都已踩平。

---

## 🛠 功能总览 / Tools (10)

| 工具 | 说明 | 类型 |
|------|------|------|
| `okx_balance` | 账户余额 / 总权益 | 📊 查询 |
| `okx_positions` | 全部持仓明细（含强平价/保证金） | 📊 查询 |
| `okx_market_data` | 行情：最新价/买卖盘/24h涨跌 | 📊 查询 |
| `okx_account_config` | 账户配置（杠杆/持仓模式） | 📊 查询 |
| `okx_open_position` | **市价开仓**（多/空，自动设杠杆） | ⚡ 交易 |
| `okx_close_position` | **市价平仓**（reduceOnly 保护） | ⚡ 交易 |
| `okx_set_sl_tp` | **一键挂止损止盈**（百分比距离开仓价） | 🛡 风控 |
| `okx_pending_algos` | 查看挂单中的条件单 | 📋 管理 |
| `okx_cancel_algos` | 撤销条件单（支持 ALL 一键清） | 📋 管理 |
| `okx_set_leverage` | 设置杠杆（1–125x，逐仓/全仓） | ⚙️ 设置 |

---

## 🚀 快速开始 / Quick Start

### 0. 准备 API Key（1 分钟）

OKX App 或 [官网](https://www.okx.com/account/my-api) 创建 API V5：
- ✅ 勾选 **「交易」权限**（必须）
- ❌ **不要**勾选「提现」权限（安全红线）
- 建议开启 **IP 白名单**
- 记录下 API Key / Secret / Passphrase 三个值

### 1. 安装（Windows）

```
双击 install.bat
```

脚本自动：检查 Python → 安装依赖 → 引导填写 API Key → 完成。

> Mac / Linux:
> ```bash
> python3 -m pip install -r requirements.txt
> python3 setup_env.py
> ```

### 2. 配置到你的 MCP 客户端

**Claude Desktop** — `claude_desktop_config.json`:
```json
{
  "mcpServers": {
    "okx-trader": {
      "command": "python",
      "args": ["C:/path/to/okx-trader-mcp/server.py"]
    }
  }
}
```

**Cursor** — `.cursor/mcp.json`:
```json
{
  "mcpServers": {
    "okx-trader": {
      "command": "python",
      "args": ["C:/path/to/okx-trader-mcp/server.py"]
    }
  }
}
```

**Cherry Studio / Hermes / 其他**：添加 MCP 服务器，命令 `python`，参数同上。

### 3. 开始使用 🎉

直接用自然语言指挥 AI：

```
帮我看看账户还有多少钱
→ okx_balance

PEPE 现在什么价？24小时涨跌？
→ okx_market_data

用 10 倍杠杆开 5 张 PEPE 多单
→ okx_open_position(symbol="PEPE/USDT:USDT", side="buy", amount=5, leverage=10)

给我刚开的仓挂上止损 3%、止盈 6%
→ okx_set_sl_tp(symbol="PEPE/USDT:USDT", sl_percent=3, tp_percent=6)

把所有仓位平掉
→ okx_close_position(...)

看看有没有挂着的止损单
→ okx_pending_algos("ALL")
```

---

## 🔒 安全设计 / Security

- API Key 只存本地 `.env`，**绝不上传任何服务器**
- 所有下单指令在你自己的 MCP 客户端执行，走 OKX 官方 API
- `okx_close_position` 强制 `reduceOnly`，**不可能反向开仓**
- 建议开启 IP 白名单 + 交易专用 API Key
- 代码开源透明，你可以逐行审查再使用

---

## ⚠️ 免责声明 / Disclaimer

**加密货币合约交易风险极高，可能导致全部本金损失。** 本工具仅提供交易自动化能力，不构成任何投资建议。使用者需自行承担全部交易风险。过往业绩不代表未来收益。请只在风险承受范围内交易。

*Cryptocurrency futures trading carries extreme risk and can result in total loss of capital. This tool provides trading automation only and does NOT constitute investment advice. Trade at your own risk.*

---

## 📦 购买 / Purchase

| 版本 | 价格 | 内容 |
|------|------|------|
| Standard | **$49** / ¥299 | 全部 10 个工具 + 安装向导 + 1 年更新 |
| Pro | **$99** / ¥599 | Standard + 交易信号扫描器（RSI 策略）+ 回测脚本 |

**支付方式**：Gumroad（信用卡/PayPal）· 微信支付 · USDT

**交付方式**：购买后立即获得下载链接 + 安装文档 + 邮件/微信支持

---

## 📮 联系 / Contact

- 微信 / WeChat: *(购买后提供)*
- Telegram: *(购买后提供)*
- 问题反馈 / Issues: GitHub Issues 或邮件

---

<div align="center">

**© 2026 OKX Trading MCP. All rights reserved.**

*这不是 OKX 官方产品。This is not an official OKX product.*

</div>
