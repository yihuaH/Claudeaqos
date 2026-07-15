# Claudeaqos — 每日自动交易系统

本仓库是一套通过 Robinhood MCP (cash_printer) 运行的每日自动交易系统。
定时 Routine 每个交易日唤醒会话, 按 `strategy/playbook.md` 执行。

## 硬性红线 (任何情况下不得违反)

1. 只允许操作账户 **802095265** (nickname "Agentic", agentic_allowed=true)。绝不触碰其他账户。
2. 所有买卖必须来自 `scripts/signals.py` 的输出。不得基于自己的市场观点新增、放大或修改订单。
3. `strategy/config.json` 的风控限制 (仓位上限、单量上限、熔断) 是上限, 不是建议。
4. `enabled=false` 或 `halted=true` 时只允许读数据和写日志。
5. API 密钥、secret 一律不得写入本仓库 (用户提供的 Alpaca/FRED 密钥只存在会话环境中)。
6. 遇到预期外的告警、报错、数据异常: 停止交易 → 写日志 → 通知用户。不要即兴发挥。
7. Alpaca 只允许纸面环境 (`paper-api.alpaca.markets`, 已硬编码在 `scripts/paper.py`), 只用于挑战者影子验证; 绝不调用 Alpaca 实盘交易接口, 绝不动 paper 账户里账本之外的持仓。
8. 参数自学习边界: 学习器 (`scripts/learn.py`) 只能修改 `strategy/learning.json` `learnable_bounds` 列出的 entry/exit 参数且必须在边界内; sizing/熔断/宏观/legacy 等风控**永不自学习**。晋级必须先通过 paper 验证期且 evaluate 判 pass; 每次晋级/否决写 journal 并通知用户。

## 结构

- `strategy/config.json` — 策略与风控参数 (用户可改)
- `strategy/playbook.md` — 每日执行步骤
- `strategy/learning.json` — 自学习策略: 可学参数边界、搜索网格、晋级标准 (用户可改)
- `scripts/signals.py` — 确定性信号引擎 (signal / apply)
- `scripts/integrations.py` — 外部数据源 (Alpaca paper / FRED) 自诊断、宏观数据、历史日线
- `scripts/learn.py` — 参数学习器 (walk-forward 搜索 / 验证评估 / 晋级)
- `scripts/paper.py` — Alpaca 纸面账户执行器 (挑战者影子交易, 仅 paper 环境)
- `state/positions.json` — 实盘持仓与净值状态 (引擎回写)
- `state/learning.json` — 学习状态 (冠军/挑战者、净值曲线、晋级历史)
- `state/paper_positions.json` — 挑战者纸面账本 (与实盘 state 同构)
- `journal/` — 每日运行日志 (git 记录, 可审计)
