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

## 报告与通知

- **每日战报**由绑定用户"报告窗口"会话的 Routine 发送 (`Claudeaqos 每日战报 → 报告窗口`), 每交易日两次:
  - **10:45 ET 晨间核查** — 仅当晨间窗口 (9:35 ET) 有成交或异常时发简报, 否则静默不打扰;
  - **买单就绪推送 (~15:45 ET)** — 主流程生成待执行买单 (`state/pending_orders.json`) 时直接推送通知, 用户**次日开盘前任意时间**到报告窗口回复「执行」: 当日 15:55 ET 前为市价单, 之后自动转次日开盘限价单 (信号价 ±0.5% 容差, 跳空不追; 隔夜轨道买单仅限当日);
  - **16:45 ET 收盘战报** — 必发: 当日 journal 摘要 (净值/回撤/信号/成交/告警)、券商订单核对、待执行订单状态 (executed/expired); 主流程未产出时先复查 20 分钟再诊断上报。
- **买入半自动** (2026-07-20 起): 无人值守会话的买单会被平台分类器拦截, 故新买入与换仓卖单由主流程写入待执行清单, 用户在报告窗口回复「执行」后原样执行 (playbook 4C); 出场/止损卖单不受影响, 照常全自动。原 approval.json 每日预审确认制随之退役。
- 战报窗口平时只读 (不主动下单、不改交易分支); 唯一例外是用户当场下达「执行」指令时按 playbook 4C 执行待执行清单。
- 交易执行本身的推送通知仍由主流程 (15:30 ET) 与晨间窗口 (9:35 ET) 的独立会话 Routine 发出。

## 数据源集成状态

| 数据源 | 用途 | 状态 |
|---|---|---|
| Robinhood MCP (cash_printer) | 行情 + 历史 + 下单 | ✅ 使用中 |
| Alpaca paper API | 市场时钟/假期日历 (休市判定) | ✅ 网络已放行, 已接入 playbook; 等环境变量注入 (容器重启后生效) |
| FRED API | 宏观风控: VIX ≥ 30 暂停新开仓 | ✅ 网络已放行, 过滤逻辑已进引擎 (三态验证通过); 等环境变量注入 |

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
