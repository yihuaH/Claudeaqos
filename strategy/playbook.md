# 每日交易操作手册 (playbook)

每个交易日 15:30 ET(19:30 UTC 夏令时)由 Routine 唤醒后, **严格按顺序**执行以下步骤。
执行者(Claude 会话)只做数据搬运和按引擎输出下单, **不得加入自己的市场判断**。

## 0. 前置检查 (任何一条不满足 → 只写日志, 不交易)

1. `git pull` 拉取本分支最新代码与状态。
2. `strategy/config.json` 里 `enabled` 必须为 `true`; `state/positions.json` 里 `halted` 必须为 `false`。
3. 幂等: 如果 `journal/<今天>.md` 已存在且标记 `status: completed`, 说明今天已跑过, 直接结束。
4. 数据源自诊断: `python3 scripts/integrations.py status`, 输出记入当天 journal 的"系统事件"。
   (Alpaca/FRED 集成已于 2026-07-15 经用户确认转正; 任一数据源不可用只降级, 不阻断交易。)

5. 开市检查:
   - 首选: 上一步 status 里 alpaca `ok=true` 时, 以 Alpaca 时钟为准 — `market_is_open=false` 视为休市 → 写日志"休市"并结束。
   - 回退 (alpaca 不可用): `get_equity_quotes(["SPY"])`, 若 `venue_last_trade_time` 的日期不是今天(UTC), 视为休市日 → 写日志"休市"并结束。

## 1. 取数

1. `get_portfolio(802095265)` → 记下 `total_value` 和 `buying_power`。
2. `get_equity_positions(802095265)` → 与 `state/positions.json` 核对; 数量不一致以券商为准, 先修正 state。把结果整理成简单映射存到 scratchpad: `{"SYM": {"qty": x, "available": y, "intraday": z}}`。
3. `get_equity_quotes(ETF池 + 全部持仓符号)` → 整理成 `{"SYM": price}` 存到 scratchpad (只收录 state=active 的)。
4. `get_equity_historicals(ETF池10只, start=今天-450天, interval=day)` 和 `get_equity_historicals(持仓符号, start=今天-140天, interval=day)` → 原始输出会自动存到 tool-results 文件, 记下路径。
5. 宏观数据 (标准步骤): `python3 scripts/integrations.py macro --out <scratchpad>/macro.json`。
   成功则在算信号时加 `--macro <macro.json>`; 失败则省略该参数, 交易照常, 在日志注明。
   (VIX ≥ 配置阈值时引擎只停新开仓, 卖出/止损照常; VIX 数据过旧时引擎自动跳过过滤并告警。)

## 2. 算信号

```bash
python3 scripts/signals.py signal \
  --config strategy/config.json \
  --state state/positions.json \
  --historicals <ETF历史文件> <持仓历史文件...> \
  --quotes <报价映射文件> \
  --positions <持仓映射文件> \
  --date <YYYY-MM-DD> \
  --portfolio-value <total_value> \
  --buying-power <buying_power> \
  --out <scratchpad>/orders.json
```

## 3. 熔断处理

如果输出里 `circuit_breaker_triggered: true`:
把 `state/positions.json` 的 `halted` 改为 `true`, 写日志, **通知用户**, 结束。不执行任何订单。

## 4. 执行订单 (先卖后买, 逐单执行)

对 `sells` 里每一单:
1. `review_equity_order(account=802095265, symbol, side=sell, type=market, quantity, market_hours=regular_hours)`。
2. 无异常告警 → `place_equity_order(同参数, ref_id=新UUID)`; 出现**预期外**告警(停牌、限制等) → 跳过该单并记入日志的 anomalies。
3. `get_equity_orders` 确认成交, 记录实际 qty/price。

全部卖单确认后, 对 `buys` 里每一单:
1. `review_equity_order(..., side=buy, type=market, dollar_amount)`。
2. 若告警显示购买力不足 → 按告警金额下调 `dollar_amount`(不低于 min_order_usd, 否则跳过)。
3. `place_equity_order(..., ref_id=新UUID)`, 确认成交。

规则:
- ref_id 每个逻辑订单生成一次, 网络重试必须复用同一个 ref_id, 防止重复下单。
- 引擎没输出的单**绝不下**; 引擎输出的金额/数量**绝不放大**。
- 收盘前 5 分钟(15:55 ET)后不再提交新单, 未执行的记入日志顺延。

## 5. 回写与日志

1. 把实际成交写成 `{"fills": [{symbol, side, qty, price, bucket, reason}]}`, 运行:
   `python3 scripts/signals.py apply --state state/positions.json --fills <fills.json> --date <今天> --portfolio-value <最新total_value>`
2. 写 `journal/<今天>.md`: 状态(completed/halted/closed/error)、组合净值、回撤、信号表、订单与成交、告警/异常。
3. `git add -A && git commit -m "journal: <今天> trading run" && git push -u origin <当前工作分支>` (用 `git rev-parse --abbrev-ref HEAD` 获取, 不得推到其他分支)。

## 6. 影子验证运行 (Alpaca paper — 挑战者)

条件: `strategy/learning.json` 的 `enabled=true` 且 `state/learning.json` 有 `status=validating` 的挑战者。
实盘步骤 1-5 全部完成后才执行; **影子环节任何失败只记日志, 不得影响实盘结果**。

1. 生成挑战者配置:
   `python3 scripts/learn.py challenger-config --config strategy/config.json --state-learn state/learning.json --out <scratchpad>/challenger_config.json`
2. 算挑战者净值: `python3 scripts/paper.py equity --ledger state/paper_positions.json --quotes <报价映射文件>` → 记下 equity/cash。
3. 用**当天同一批数据**算挑战者信号 (换 config/state/资金三项, 其余与实盘完全一致):
   `python3 scripts/signals.py signal --config <challenger_config> --state state/paper_positions.json --historicals <同实盘> --quotes <同实盘> --macro <同实盘> --date <今天> --portfolio-value <paper equity> --buying-power <paper cash> --out <scratchpad>/paper_orders.json`
4. 纸面下单: `python3 scripts/paper.py run --orders <paper_orders> --date <今天> --fills-out <scratchpad>/paper_fills.json`
5. 回写账本: `python3 scripts/signals.py apply --state state/paper_positions.json --fills <paper_fills> --date <今天> --portfolio-value <paper equity>`
6. 记录净值 (成交后重算一次 equity):
   `python3 scripts/learn.py record --state-learn state/learning.json --date <今天> --live-equity <实盘 total_value> --paper-equity <重算 equity>`
7. 评估: `python3 scripts/learn.py evaluate --learning strategy/learning.json --state-learn state/learning.json --paper-ledger state/paper_positions.json --date <今天>`, 结果记入 journal。
   - `pass` 且 `auto_promote=true` → `learn.py promote ...`, 在 journal 显著标注**参数晋级**并**通知用户** (新旧参数、验证期表现)。
   - `fail` → `learn.py reject --reason <evaluate给出的原因>`, 记日志并通知用户, 然后按第 7 节搜索新挑战者。
   - 其余 (`insufficient_data`/`extend`) → 继续验证, 无需通知。

## 7. 参数搜索 (无活跃挑战者时执行)

1. 拉长历史 (约 5 年): `python3 scripts/integrations.py bars --symbols <ETF池逗号分隔> --start <今天减5年> --out <scratchpad>/bars_5y.json`
2. `python3 scripts/learn.py search --config strategy/config.json --learning strategy/learning.json --state-learn state/learning.json --historicals <bars_5y> --date <今天> --start-capital <实盘 total_value> --paper-ledger state/paper_positions.json`
3. 输出记 journal: `new_challenger` → 新挑战者次日起影子运行; `champion_optimal` → 冠军仍最优, 本轮不设挑战者。

## 8. 异常总原则

任何预期外情况(API 报错、成交与订单不符、数据缺失过半) → **立即停止交易**, 把已发生的事写进日志, 通知用户。宁可错过一天, 不做没把握的操作。
