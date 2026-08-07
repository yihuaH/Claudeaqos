# 已停用轨道存档 (archive/paused-tracks.md)

> 以下轨道当前**未运行** (配置开关已关或 paused)。留档以便将来恢复;
> 恢复时把对应小节移回 `strategy/tracks.md` 并在 CLAUDE.md 轨道表更新状态。
> 拆分自 playbook.md (2026-08-06 用户「拆」), 内容一字未改。

## 5B. 隔夜轨道 (实盘, ETF + 个股, strategy/overnight.json enabled=true 时)

第 5 节完成后执行 (RSI-2 策略优先用资金)。独立账本 `state/overnight_positions.json`。

**晨间窗口 (9:35 ET, 独立 Routine)** — **已停用** (2026-07-21): 隔夜实盘入场暂停后本窗口无剩余职责,
4C 盘外残单撤销已移交战报窗口 10:45 ET 晨检。完整步骤 (时段校验/开盘卖出/学习账本开盘卖/结算合规/残单撤销)
见 git d55493a。恢复隔夜实盘时需重新启用本 Routine 并还原步骤。

**主窗口 (15:30 ET, 第 5 节完成后执行)**:

> **实盘入场暂停中** (overnight.json `live_entries_paused=true`, 2026-07-21): 引擎强制 slots=0 只出不进 —
> 本窗口照常跑信号与出场/兜底, 不产生实盘新入场。恢复: 删除该标志。

1. 数据 (Alpaca): `integrations.py bars` 拉 [ETF池 + universe.json 100股 + 存量持仓] 约 300 天日线;
   `integrations.py snapshots --symbols-file <同一批符号>` 拉当日实时 OHLC (IBS 用)。
2. 财报日: 复用第 1 节 4B 生成的 earnings.json (每天只拉一次); 取不到则不传 → 财报日未知的个股仍可候选 (allow_unknown_earnings=true), 仅失去财报回避保护, 引擎会告警注明。
3. 重新取 `get_portfolio` 的最新 buying_power (RSI-2 执行后剩余的), 然后:
   `python3 scripts/overnight.py signal --config strategy/overnight.json --state state/overnight_positions.json --main-state state/positions.json --bars <bars> --snapshots <snaps> --earnings <earnings> --macro <macro> --positions <券商持仓映射> --date <今天> --portfolio-value <total_value> --buying-power <剩余bp> --out <scratchpad>/overnight_orders.json`
4. 执行 (semi_auto 拆分): 出场/兜底类卖单按 **4A** 直接执行; 入场买单与 funding_rotation 换仓卖单
   并入 **4B** 的 `state/pending_orders.json` (state_file=state/overnight_positions.json, 与 RSI-2 订单同一文件同一次通知), 由用户按 **4C** 触发执行。
5. 回写 (仅对实际成交, 按 bucket 分两次):
   - strategy 桶成交 → `signals.py apply --state state/overnight_positions.json --fills <strategy fills> --date <今天>`
   - legacy 桶成交 (换仓卖出) → `signals.py apply --state state/positions.json --fills <legacy fills> --date <今天>`
6. journal 加"隔夜轨道"小节: 每笔进出、IBS 值、顺延/止损标注。

## 6B. 隔夜参数学习 (Alpaca paper 双账本 A/B)

> **暂停中** (learning_overnight.json `paused=true`, 2026-07-21): 隔夜实盘入场暂停, 学习无出口。
> 两本纸面账本与冠军/挑战者状态冻结保留, 删除 paused 标志即恢复。
> 完整 A/B runbook (challenger/twin 配置 → 双账本 signal → paper.py run → record → evaluate/promote,
> 数据复用第 5B 节) 见 git d55493a。随隔夜实盘一并恢复。

## 7. 个股防御实验 (仅 paper, strategy/stocks.json enabled=true 时)

> **暂停中** (stocks.json `enabled=false`, 2026-07-21): 个股已并入实盘主候选池 (见第 2 节),
> 防御层参数由 config.json defense 段沿用; 独立纸面账本 state/stock_positions.json 冻结保留。
> 完整 runbook (universe 周度刷新 assets→pool→rank→finalize + 数据/信号/回写/复盘) 见 git d55493a。恢复: enabled=true。
