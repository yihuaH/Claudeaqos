# Claudeaqos — 每日自动交易系统

通过 Robinhood MCP 接口每日自主交易的系统。策略为 **RSI(2) 均值回归 + 200日均线趋势过滤**,
标的池为 10 只高流动性 ETF; 同时纳管账户存量持仓(保护性止损 + 弱势轮动换仓)。

## 架构

```
定时调度                决策 (本仓库)                     执行
GitHub Routine   →   scripts/signals.py (确定性)   →   Claude 会话 + Robinhood MCP
每交易日 19:30 UTC     输入: 行情/持仓/状态/配置            review → place → 回写 state
                      输出: 当日订单清单                   日志 git 提交, 全程可审计
```

- 买卖信号 100% 由确定性代码产生, AI 只负责取数、执行、记录。
- 每天一次, 收盘前 30 分钟运行 (15:30 ET)。

## 策略规则

| 环节 | 规则 |
|---|---|
| 入场 | 收盘 > SMA200 且 RSI(2) < 10, 按 RSI 从低到高选 |
| 出场 | 收盘 > SMA5 或 RSI(2) > 65; 止损 -5%; 持仓 >10 交易日强平 |
| 仓位 | 单仓 = 组合净值 15%, 最多 4 仓, 每日最多新开 2 仓 |
| 存量持仓 | 跌破纳管价 -7% 保护性卖出; 需要资金时按 价格/SMA20 最弱者轮出 (每日最多 2 只) |
| 熔断 | 组合净值从高水位回撤 ≥10% → 全面停止, 等用户人工决定 |

## 用户控制

- **急停**: 把 `strategy/config.json` 的 `enabled` 改成 `false`, 或在 claude.ai 暂停 Routine, 或直接对 agent 说停。
- **调参**: 改 `strategy/config.json` 即可, 次日生效。
- **审计**: 每天的信号、订单、成交、净值都在 `journal/` 里, git 历史即完整交易记录。

## 数据源集成状态

| 数据源 | 用途 | 状态 |
|---|---|---|
| Robinhood MCP (cash_printer) | 行情 + 历史 + 下单 | ✅ 使用中 |
| Alpaca paper API | 模拟盘影子交易 / 市场日历 | ⛔ 被环境出站网络策略拦截 (2026-07-15), 需在环境设置放行 `*.alpaca.markets` |
| FRED API | 宏观风控 (VIX/利率 regime 过滤) | ⛔ 同上, 需放行 `api.stlouisfed.org` |

## 密钥管理

- 任何 key/secret 一律不写入仓库 (.gitignore 已拦截常见密钥文件)。
- 代码统一从环境变量读取, 约定名称:
  - `ALPACA_API_KEY_ID` / `ALPACA_API_SECRET_KEY` — Alpaca paper API
  - `FRED_API_KEY` — FRED API
- 注入位置按运行环境选择 (互不相通):
  - **Claude Code 云会话 (本系统运行处)**: claude.ai/code → 该环境的设置 → Environment variables
  - GitHub Codespaces: repo Settings → Secrets and variables → **Codespaces** (只注入 Codespaces)
  - GitHub Actions: repo Settings → Secrets and variables → **Actions** (只注入 Actions workflow)

## 风险声明

历史规律不保证未来收益, 本系统可能亏损。这是用户自有账户上的自动化工具, 不构成投资建议。
