# Claudeaqos — 每日自动交易系统

通过 Robinhood MCP 接口每日自主交易的系统。策略为 **RSI(2) 均值回归 + 200日均线趋势过滤**,
候选池 = **8 只低价孪生 ETF** (QQQM / IWM / XLK / XLF / XLE / SOXQ / GLDM / TLT) + **100 只个股**
(`screen.py` 周度筛选, 防御层强制: 财报回避 / 异动过滤 / 行业上限; 2026-07-21 起并入实盘);
同时纳管账户存量持仓 (保护性止损 + 弱势轮动换仓)。

实盘账户 **802095265** (`mode=live`), 唯一允许操作的账户 (CLAUDE.md 红线 1)。

## 架构

```
定时调度 (claude.ai Routine)      决策 (本仓库, 确定性)              执行
每交易日唤醒常驻会话         →   scripts/daily.py 调各引擎      →   会话 + Robinhood MCP
  10:45 / 15:20 / 17:45 ET        signals / weekly_calls /          review → place → 回写 state
  周一另有 13:00 (刷股票池)        momentum / overnight ...          journal git 提交, 全程可审计
```

- **买卖信号 100% 由确定性代码产生**, AI 只负责取数、执行、记录 (CLAUDE.md 红线 2)。
- **拆分式双段跑** (2026-09-10 起): 15:20 ET 收盘前关键路径出信号+出场+买入; 17:45 ET 收尾跑
  纸面轨道+行情核对+journal+战报。收盘前若挂掉, 17:45 自动 fail-safe 退化为完整主跑。

## 每日节奏

| 时刻 (ET) | 窗口 | 干什么 |
|---|---|---|
| 10:45 | 晨间核查 | **条件式**: 残单回收 + T+1 官方收盘核对; 无事静默 |
| 13:00 (周一) | 周度股票池刷新 | 刷 `universe.json` 100 只, **必须在 15:20 前 finalize** |
| **15:20** | **收盘前主跑** | 信号 → 出场卖单即时成交 → **买单即时成交** |
| 17:45 | 收盘后收尾 + 战报 | 纸面轨道 + 行情核对 + journal 回写 + 发当日战报 |

详细唤醒词见 `strategy/routines.md`; 执行契约见 `strategy/playbook.md`。

## 策略规则 (现行值, 以 `strategy/config.json` 为准)

| 环节 | 规则 |
|---|---|
| 入场 | 收盘 > SMA200 且 RSI(2) < 10, 按 RSI 从低到高选 |
| 出场 | 收盘 > SMA5 或 RSI(2) ≥ 65; **止损 −7%** (2026-08-07 由 −5% 上调, 1013 笔扫描依据); 持仓 ≥10 交易日强平 |
| 仓位 | **单仓 = 净值 10%**, 槽数与每日新开数**均不限** (现金即节流阀, 2026-08-06); `min_cash_reserve_usd` $5 |
| 加仓 | 已持仓收盘 ≤ 加权均价 ×(1−3%) → 补一档, **每票最多 2 档 = 单票敞口上限 20%** (2026-08-07) |
| 存量持仓 | 跌破纳管价 −7% 保护性止损; 盈利且反弹到位止盈; 需要资金时按最弱者轮出 / 加速清理 (每日≤3只) |
| 防御层 | 财报黑窗 7 日 · 近20日单日异动 ≥8% 跳过 (财报上涨跳空豁免) · **同行业持仓上限 3 只** · ETF 池豁免 |
| 宏观 | VIX ≥ 30 暂停**新开仓** (止损/出场任何时候照常) |
| 熔断 | 净值从高水位回撤 ≥10% → 全面停止, 等用户人工决定 |

## 下单协议 (CLAUDE.md 红线 9)

**`execution.mode = auto_buys`** (2026-10-12 起, 取代 2026-07-20 起的 `semi_auto`):

| 订单类型 | 无人值守会话 |
|---|---|
| 出场 / 止损 / 兜底卖单 | ✅ 自动 review → place (一贯如此) |
| **正股新买入 + 加仓** | ✅ **自动 review → place** (2026-10-12 用户「以后不用我回复执行了, 你直接执行」) |
| 换仓 / 加速清理卖单 | ✅ 自动 review → place |
| **期权开仓 (buy_to_open)** | ❌ **仍需用户在 Robinhood App 手动下单** (见下) |
| 期权出场 (sell_to_close) | 手动 App 通道 (同上) |

**不因 auto_buys 而改变的约束**: 订单逐字段来自引擎输出, 不得增减缩量 (红线2) ·
所有上限仍是上限 (红线3) · **防杠杆闸**: 买单以实时 `min(buying_power, cash)` 为上限,
超出整单跳过不缩量, **绝不动用借来的钱** · 跨轨道资金优先级 = 期权优先。

🛟 **fail-safe**: 买单 `place` 若被平台分类器拦截 → **不重试**, 退回写 `state/pending_orders.json`
并通知用户, 由用户回「执行」按 playbook 4C 消费。**绝不因被拦而丢单。**

📢 **报告义务**: 取消「执行」= 用户失去下单前的一票否决点, 故成交后**必须立即发执行报告**,
并点名所有旗标项 (新闻红旗 / 同因子敞口 / 财报黑窗失效 / 防御层拦单), 让用户能**事后**处置。

### 期权为什么还要手动

两个**不同**的拦截点, 都已实测:

| 形态 | review | place | 拦截方 |
|---|---|---|---|
| 多腿价差 | ✅ | ❌ | **Robinhood API 400** — "Multi-leg options orders aren't supported in Robinhood agentic accounts yet" (2026-08-15 / 08-21 两次实测) |
| 单腿 | ✅ | ❌ | **Claude Code 分类器** `[Real-World Transactions]` (2026-10-12 实测) |

## 用户控制

- **急停**: 把 `strategy/config.json` 的 `enabled` 改成 `false`, 或把 `state/positions.json` 的
  `halted` 改成 `true`, 或在 claude.ai 暂停 Routine, 或直接对 agent 说停。
- **调参**: 改 `strategy/config.json` 即可, 次日生效。风控上限 (仓位/单量/熔断) **只能由用户改**,
  会话不得自行调整 (红线3)。
- **一票否决**: auto_buys 下买单当场成交, 否决权从"事前"变为"事后" —— 看完执行报告告知卖出即可。
  期权开仓仍保留**事前**否决 (因为要你在 App 里按)。
- **审计**: 每天的信号、订单、成交、净值都在 `journal/` 里, git 历史即完整交易记录。

## 轨道一览

| 轨道 | 环境 | 状态 |
|---|---|---|
| RSI-2 均值回归 (主策略) | 实盘 | ✅ active |
| 隔夜均值回归 | 实盘 | ⏸ 入场暂停 (2026-07-21), 出场/兜底照常 |
| 周度期权实盘实验仓 | 实盘 | ✅ active·机会主义 (2026-10-12 换 `single` 深ITM 买call) |
| 挑战者影子验证 | paper | ⚠️ 验证中但**口径污染未解**, 指标暂不可用于晋级 |
| 周call 摩擦实测 | paper | ✅ active |
| 周度动量轮动 | paper | ✅ active |
| 备兑开仓 overlay / 个股防御实验 / 隔夜A/B学习 | paper | ⏸ 暂停 |

各轨道开关与完整注记见 CLAUDE.md「轨道状态总览」。

## 数据源集成状态

| 数据源 | 用途 | 状态 |
|---|---|---|
| Robinhood MCP (cash_printer) | **当日实时报价** + 下单 + 官方收盘 | ✅ 使用中 |
| Alpaca paper API | 市场时钟/假期日历 + 历史日线 (SIP) + 纸面执行 | ✅ 使用中 (**仅 paper 环境**, 红线7) |
| FRED API | 宏观: VIX + 收益率曲线/信用利差 (后两者仅展示) | ✅ 使用中 |

⚠️ **行情口径**: 历史日线走 Alpaca `EQUITY_FEED=sip`; **当日决策价必须走券商
`get_equity_quotes`** —— `integrations.py quotes` 是 `delayed_sip` 延迟 15 分钟, 盘中不可用。
期权链仍是 `feed=indicative` (OPRA 需签协议), 点差闸读的不是交易所真实 NBBO。

## 密钥管理

- 任何 key/secret 一律不写入仓库 (`.gitignore` 已拦截常见密钥文件; CLAUDE.md 红线5)。
- 代码统一从环境变量读取, 约定名称:
  - `ALPACA_API_KEY_ID` / `ALPACA_API_SECRET_KEY` — Alpaca paper API
  - `FRED_API_KEY` — FRED API
- 注入位置: **Claude Code 云会话** (本系统运行处) → claude.ai/code → 该环境的设置 → Environment variables。

## 风险声明

历史规律不保证未来收益, 本系统可能亏损。这是用户自有账户上的自动化工具, 不构成投资建议。
