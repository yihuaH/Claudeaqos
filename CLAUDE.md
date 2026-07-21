# Claudeaqos — 每日自动交易系统

本仓库是一套通过 Robinhood MCP (cash_printer) 运行的每日自动交易系统。
定时 Routine 每个交易日唤醒会话, 按 `strategy/playbook.md` 执行。

## 硬性红线 (任何情况下不得违反)

1. 只允许操作账户 **802095265** (nickname "Agentic", agentic_allowed=true)。绝不触碰其他账户。
2. 所有买卖必须来自确定性引擎的输出: 实盘 RSI-2 与 paper 正股来自 `scripts/signals.py`, 实盘隔夜轨道来自 `scripts/overnight.py` (用户 2026-07-15 授权直接上实盘), paper 期权来自 `scripts/options_overlay.py`。不得基于自己的市场观点新增、放大或修改订单。
3. `strategy/config.json` 的风控限制 (仓位上限、单量上限、熔断) 是上限, 不是建议。
4. `enabled=false` 或 `halted=true` 时只允许读数据和写日志。
5. API 密钥、secret 一律不得写入本仓库 (用户提供的 Alpaca/FRED 密钥只存在会话环境中)。
6. 遇到预期外的告警、报错、数据异常: 停止交易 → 写日志 → 通知用户。不要即兴发挥。
7. Alpaca 只允许纸面环境 (`paper-api.alpaca.markets`, 已硬编码在 `scripts/paper.py`), 绝不调用 Alpaca 实盘交易接口。paper 账户内股票与期权均可交易、全部持仓均可处置 (用户 2026-07-15 授权, 期权权限 Level 3); 纸面盈亏只用于挑战者/实验验证, 不得直接驱动实盘订单。
8. 参数自学习边界: 学习器 (`scripts/learn.py` / `scripts/learn_overnight.py`) 只能修改各自 learning 配置列出的 entry/exit 形状参数且必须在边界内; sizing/熔断/宏观/legacy 等风控**永不自学习**。晋级必须先通过 paper 验证期且 evaluate 判 pass; 每次晋级/否决写 journal 并通知用户。
9. 半自动买入 (execution.mode=semi_auto, 用户 2026-07-20 设立, 取代原 confirm 闸门): 实盘新买入与配套换仓卖单**不得**在无人值守会话中 review/place (会被平台分类器拦截, 不要反复尝试) — 只能由主流程写入 `state/pending_orders.json` (逐字段来自引擎输出), 待用户在有人值守会话明确说"执行"后按 playbook 4C 原样执行 (当日窗口市价; 盘外转 all_day_hours 整股限价, 有效至次一交易日 09:25 ET; 隔夜轨道买单仅当日)。出场/止损/兜底卖出与纸面轨道不受限, 照常全自动。

## 结构

- `strategy/config.json` — 策略与风控参数 (用户可改)
- `strategy/playbook.md` — 每日执行步骤
- `strategy/learning.json` — 自学习策略: 可学参数边界、搜索网格、晋级标准 (用户可改)
- `strategy/options.json` — 备兑开仓实验参数 (仅 paper, 用户可改)
- `strategy/stocks.json` — 个股防御实验参数 (仅 paper; **实验暂停中** 2026-07-21, 个股已并入实盘主策略, 防御层参数由 config.json defense 段沿用)
- `strategy/screen.json` — 个股池周度筛选标准 (用户可改); `strategy/universe.json` — 筛选产出的当前 100 股池 (screen.py 回写)
- `scripts/screen.py` — 个股池确定性筛选器 (pool / rank / finalize); 筛选只决定"能买什么", 买卖时机仍由引擎决定
- `scripts/signals.py` — 确定性信号引擎 (signal / apply)
- `scripts/overnight.py` — 隔夜均值回归引擎 (IBS 收盘买/次日收盘卖); **实盘入场暂停中** (live_entries_paused, 2026-07-21 用户指示, 出场/兜底与纸面学习照常); `strategy/overnight.json` 参数; `state/overnight_positions.json` 账本
- `scripts/integrations.py` — 外部数据源 (Alpaca paper / FRED) 自诊断、宏观数据、历史日线
- `scripts/learn.py` — 参数学习器 (walk-forward 搜索 / 验证评估 / 晋级)
- `scripts/learn_overnight.py` — 隔夜策略学习器 (双纸面账本 A/B; **暂停中** 2026-07-21, 随隔夜实盘暂停冻结); `strategy/learning_overnight.json` 边界; `state/learning_overnight.json` + 两本 paper_overnight_* 账本
- `scripts/paper.py` — Alpaca 纸面账户执行器 (挑战者影子交易, 仅 paper 环境)
- `scripts/options_overlay.py` — 备兑开仓确定性引擎 (signal / apply, 仅 paper)
- `state/positions.json` — 实盘持仓与净值状态 (引擎回写)
- `state/learning.json` — 学习状态 (冠军/挑战者、净值曲线、晋级历史)
- `state/paper_positions.json` — 挑战者纸面账本 (与实盘 state 同构)
- `state/stock_positions.json` — 个股防御实验纸面账本 (独立于挑战者)
- `journal/` — 每日运行日志 (git 记录, 可审计)
