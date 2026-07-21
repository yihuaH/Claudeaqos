# 每日交易操作手册 (playbook)

每个交易日 15:30 ET(19:30 UTC 夏令时)由 Routine 唤醒后, **严格按顺序**执行以下步骤。
执行者(Claude 会话)只做数据搬运和按引擎输出下单, **不得加入自己的市场判断**。

## 0. 前置检查 (任何一条不满足 → 只写日志, 不交易)

0. **时段校验** (防调度器误触发, 2026-07-16 实证必要): 主流程实盘下单仅允许在 15:20–15:55 ET 执行;
   时段外触发 → 只读核查 (分支/状态/闸门), 不交易, 布置正点后备唤醒后结束。

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

全部卖单确认后, 检查确认闸门 (`strategy/config.json` execution.mode=confirm 时):
`state/approval.json` 必须 `approved=true` 且 `trade_date=今天`; 买单标的必须在 `buy_candidates` 内,
换仓与加速清理卖单 (reason=funding_rotation / accelerated_liquidation) 标的必须在 `funding_sell_order` 内。
不满足 → 跳过该买单及其换仓卖单 (加速清理卖单同样跳过), journal 记"未获确认, 只出不进"。
**出场/止损类卖单永不受此闸门限制** (风险只减不增)。闸门通过后, 对 `buys` 里每一单:
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

## 5B. 隔夜轨道 (实盘, ETF + 个股, strategy/overnight.json enabled=true 时)

第 5 节完成后执行 (RSI-2 策略优先用资金)。独立账本 `state/overnight_positions.json`。
用户校准 (2026-07-15): 目标 ~10笔/天 (晨间 ~5 卖 + 收盘 ~5 买), 换仓卖出不设 2 只/日限制, 持仓数不设上限。

**晨间窗口 (9:35 ET, 独立 Routine, exit.window=next_open 时)**:
0. **时段校验** (防调度器误触发): 当前 ET 时间必须在 09:30–10:15 之间才允许执行卖出;
   时段外触发 → 只做只读核查 (分支同步/持仓状态), 不交易, 异常才通知。
1. `git pull` → `integrations.py status`: Alpaca 时钟 `is_open` 必须为 true, 否则写日志结束。
2. `state/overnight_positions.json` 无隔夜持仓 → 直接结束。
3. 持仓符号拉快照 → `overnight.py signal --window open --config strategy/overnight.json --state state/overnight_positions.json --main-state state/positions.json --snapshots <snaps> --positions <券商映射> --date <今天> --portfolio-value <pv> --buying-power 0`
4. 按第 4 节规则执行卖单 → fills 回写 `signals.py apply --state state/overnight_positions.json`。
5. journal 附记 + push。晨间窗口失败不影响主窗口 (15:30 会 close_backstop_exit 兜底清仓)。
6. 学习账本开盘卖出: 凡学习账本对应配置的 exit.window=next_open (当前: 冠军孪生), 对该账本同样执行
   `overnight.py signal --window open` → `paper.py run` (Alpaca 纸面) → `signals.py apply` 回写该账本。
7. 结算合规 (账户 802095265 为现金账户, T+1 结算, GFV 规则适用):
   - review 返回 GFV/结算类警告 → 该卖单跳过, 留给 15:30 兜底窗口, 记 journal 并通知用户;
   - 引擎只花 get_portfolio 报告的 buying_power (券商已扣除未结算部分), 不得自行放大;
   - journal 每日记录 buying_power 与 cash 差额, 用于观察实际资金周转节奏。

**主窗口 (15:30 ET, 第 5 节完成后执行)**:

1. 数据 (Alpaca): `integrations.py bars` 拉 [ETF池 + universe.json 100股 + 存量持仓] 约 300 天日线;
   `integrations.py snapshots --symbols-file <同一批符号>` 拉当日实时 OHLC (IBS 用)。
2. 财报日: 与第 7 节共用同一份 earnings.json (每天只拉一次); 取不到则不传 → 财报日未知的个股仍可候选 (allow_unknown_earnings=true), 仅失去财报回避保护, 引擎会告警注明。
3. 重新取 `get_portfolio` 的最新 buying_power (RSI-2 执行后剩余的), 然后:
   `python3 scripts/overnight.py signal --config strategy/overnight.json --state state/overnight_positions.json --main-state state/positions.json --bars <bars> --snapshots <snaps> --earnings <earnings> --macro <macro> --positions <券商持仓映射> --date <今天> --portfolio-value <total_value> --buying-power <剩余bp> --out <scratchpad>/overnight_orders.json`
4. 执行: 与第 4 节完全相同的规则 (先卖后买、review→place、ref_id 幂等、15:55 截止)。
5. 回写 (按 bucket 分两次):
   - strategy 桶成交 → `signals.py apply --state state/overnight_positions.json --fills <strategy fills> --date <今天>`
   - legacy 桶成交 (换仓卖出) → `signals.py apply --state state/positions.json --fills <legacy fills> --date <今天>`
6. journal 加"隔夜轨道"小节: 每笔进出、IBS 值、顺延/止损标注。**上线首周 (至 2026-07-23) 每笔交易单独列明盈亏。**

## 6. 影子验证运行 (Alpaca paper — 挑战者)

条件: `strategy/learning.json` 的 `enabled=true` 且 `state/learning.json` 有 `status=validating` 的挑战者。
实盘步骤 1-5 全部完成后才执行; **影子环节任何失败只记日志, 不得影响实盘结果**。

0. 一次性重置 (仅当 `state/paper_positions.json` 有 `pending_reset` 键):
   a. `python3 scripts/paper.py liquidate --all` — 清算 paper 账户全部存量持仓 (含期权), 撤销挂单。
   b. `python3 scripts/paper.py account` 取清算后 `equity` →
      `python3 scripts/learn.py restart-validation --state-learn state/learning.json --paper-ledger state/paper_positions.json --date <今天> --start-capital <equity>`
      (账本以全账户资金重开, 挑战者参数不变, 验证期重新起算; 该命令会自动清除 pending_reset)。
   c. 清算结果与新起始资金记入 journal, 通知用户。然后继续下面步骤 1-7。

1. 生成挑战者配置:
   `python3 scripts/learn.py challenger-config --config strategy/config.json --state-learn state/learning.json --out <scratchpad>/challenger_config.json`
2. 算挑战者净值: `python3 scripts/paper.py equity --ledger state/paper_positions.json --quotes <报价映射文件>` → 记下 equity/cash。
3. 用**当天同一批数据**算挑战者信号 (换 config/state/资金三项, 其余与实盘完全一致):
   `python3 scripts/signals.py signal --config <challenger_config> --state state/paper_positions.json --historicals <同实盘> --quotes <同实盘> --macro <同实盘> --date <今天> --portfolio-value <paper equity> --buying-power <paper cash> --out <scratchpad>/paper_orders.json`
4. 备兑信号 (strategy/options.json `enabled=true` 时; 仅当账本有期权持仓或有 ≥100 股整手持仓, 否则跳到步骤 5):
   a. 拉链: `python3 scripts/integrations.py chains --underlyings <相关正股逗号分隔> --date <今天> --dte-max 35 --out <scratchpad>/chains.json`
   b. `python3 scripts/options_overlay.py signal --config strategy/options.json --ledger state/paper_positions.json --quotes <报价映射> --chains <chains> --orders <paper_orders> --date <今天> --out-closes <scratchpad>/opt_closes.json --out-opens <scratchpad>/opt_opens.json`
5. 纸面执行 (顺序不可乱: 先平期权 → 正股 → 再开备兑):
   a. `paper.py run --orders <opt_closes> --date <今天> --fills-out <scratchpad>/opt_close_fills.json` (期权平仓单被拒 = 可能已被指派 → 记异常、通知用户)
   b. `paper.py run --orders <paper_orders> --date <今天> --fills-out <scratchpad>/paper_fills.json`
   c. `paper.py run --orders <opt_opens> --date <今天> --fills-out <scratchpad>/opt_open_fills.json`
6. 回写账本:
   a. 正股: `python3 scripts/signals.py apply --state state/paper_positions.json --fills <paper_fills> --date <今天> --portfolio-value <paper equity>`
   b. 期权: 对 opt_close_fills 和 opt_open_fills 分别 `python3 scripts/options_overlay.py apply --ledger state/paper_positions.json --fills <文件> --date <今天>`
7. 记录净值 (成交后重算: `paper.py equity --ledger ... --quotes ... --chains <chains>`):
   `python3 scripts/learn.py record --state-learn state/learning.json --date <今天> --live-equity <实盘 total_value> --paper-equity <equity_ex_options>`
   **注意: record 必须用 `equity_ex_options`** (剔除期权轨道, 保持参数 A/B 对比干净); `overlay_pnl` 单独记入 journal 的备兑一栏。
8. 评估: `python3 scripts/learn.py evaluate --learning strategy/learning.json --state-learn state/learning.json --paper-ledger state/paper_positions.json --date <今天>`, 结果记入 journal。
   - `pass` 且 `auto_promote=true` → `learn.py promote ...`, 在 journal 显著标注**参数晋级**并**通知用户** (新旧参数、验证期表现)。
   - `fail` → `learn.py reject --reason <evaluate给出的原因>`, 记日志并通知用户, 然后按第 7 节搜索新挑战者。
   - 其余 (`insufficient_data`/`extend`) → 继续验证, 无需通知。

## 6B. 隔夜参数学习 (Alpaca paper 双账本 A/B)

条件: `strategy/learning_overnight.json` enabled=true 且 `state/learning_overnight.json` 有 validating 挑战者。
第 6 节完成后执行, 失败只记日志。数据复用第 5B 节的 bars/snapshots/earnings/macro。

1. 配置: `learn_overnight.py challenger-config --config strategy/overnight.json --state-learn state/learning_overnight.json --out <scratchpad>/ch_overnight.json` 和 `learn_overnight.py twin-config --config strategy/overnight.json --out <scratchpad>/twin_overnight.json` (两者均自动禁换仓)。
2. 对两本账分别执行 (冠军孪生: twin 配置 + `state/paper_overnight_champion.json`; 挑战者: ch 配置 + `state/paper_overnight_positions.json`):
   a. `paper.py equity --ledger <账本> --quotes <收盘价映射>` → equity/cash
   b. `overnight.py signal --config <各自配置> --state <各自账本> --main-state state/positions.json --bars <同5B> --snapshots <同5B> --earnings <同> --macro <同> --date <今天> --portfolio-value <equity> --buying-power <cash>` (不传 --positions, 账本即真相)
   c. `paper.py run` (Alpaca 纸面, 幂等) → `signals.py apply --state <各自账本>`
3. `learn_overnight.py record --state-learn state/learning_overnight.json --date <今天> --live-equity <冠军孪生 equity> --paper-equity <挑战者 equity>` (live 字段=冠军孪生, 构成干净 A/B)。
4. `learn_overnight.py evaluate ... --paper-ledger state/paper_overnight_positions.json`:
   - `pass` 且 auto_promote → `learn_overnight.py promote` (写 strategy/overnight.json, **实盘隔夜参数随之切换**), journal 显著标注并**通知用户**;
   - `fail` → reject + 通知, 然后重新 search (`integrations.py bars --start 今天-3.5年` + universe);
   - 其余继续验证。

## 7. 个股防御实验 (仅 paper, strategy/stocks.json enabled=true 时)

第 6 节完成后执行, 独立账本 `state/stock_positions.json`, 任何失败只记日志。

0. universe 周度刷新 (每周一, 或 `strategy/stocks.json` 的 `universe_generated` 距今超过 `screen.json` 的 `refresh_days`):
   a. `python3 scripts/integrations.py assets --out <scratchpad>/assets.json` (全市场正股, 名称启发式剔除基金)
   b. `integrations.py bars --symbols-file <assets> --start <今天-40天> --out <scratchpad>/p1.json` → `python3 scripts/screen.py pool --config strategy/screen.json --bars <p1> --out <scratchpad>/pool.json` (流动性前1000)
   c. `python3 -c` 把 pool 转成 {"symbols": [...]} → `integrations.py bars --symbols-file <pool_syms> --start <今天-470天> --out <scratchpad>/p2.json`
   d. Robinhood 热门榜: `get_popular_watchlists` 找 "100 most popular" → `get_watchlist_items` → 存 `{"symbols": [...]}` 到 popular.json
   e. `screen.py rank --config strategy/screen.json --pool <pool> --bars <p2> --popular <popular> --date <今天> --out <scratchpad>/ranked.json`
   f. 行业: 对 ranked 候选按每批 10 只调 `get_equity_fundamentals`, 整理 `{"SYM": {"sector":..., "name":...}|null}` 存 sectors.json
   g. `screen.py finalize --config strategy/screen.json --ranked <ranked> --sectors <sectors> --date <今天> --out strategy/universe.json --apply-stocks strategy/stocks.json`
   h. universe 变动 (新增/剔除的符号) 记入 journal。**已持仓但被剔除出 universe 的股票不强制卖出, 按正常出场规则走完。**

1. 数据 (全部走 Alpaca): `python3 scripts/integrations.py bars --symbols <stocks universe> --start <今天-450天> --out <scratchpad>/stock_hist.json` 和 `integrations.py quotes --symbols <同> --out <scratchpad>/stock_quotes.json`。
2. 财报日: 用 cash_printer 的 `get_earnings_calendar` / `get_earnings_results` 查 universe 内个股, 整理成 `{"SYM": "YYYY-MM-DD"}` (确认近期无财报的记 `null`, 查不到的**不要写入**) 存 earnings.json。**整体取不到就不传 --earnings** — 财报日未知的个股仍可候选 (allow_unknown_earnings=true, 用户指示), 仅失去财报回避保护; 引擎会告警注明。
3. 净值: `python3 scripts/paper.py equity --ledger state/stock_positions.json --quotes <stock_quotes>` → equity/cash。
4. 信号: `python3 scripts/signals.py signal --config strategy/stocks.json --state state/stock_positions.json --historicals <stock_hist> --quotes <stock_quotes> --macro <同实盘> --earnings <earnings> --date <今天> --portfolio-value <equity> --buying-power <cash> --out <scratchpad>/stock_orders.json`
5. 执行与回写 (同第 6 节模式): `paper.py run` → `signals.py apply --state state/stock_positions.json`。熔断触发则该账本自行 halted, 通知用户, 不影响其他轨道。
6. journal 记录: 当日净值、持仓、信号、防御过滤触发明细 (warnings)。实验累计 ≥20 交易日后与用户复盘决定去留。

## 7B. 动量轮动实验 (仅 paper, strategy/momentum.json enabled=true 时)

第 7 节完成后执行, 独立账本 `state/momentum_positions.json`, 任何失败只记日志。
周度节奏: 每周一为调仓日 (账本从未调仓过时立即调仓; 节假日错过由 max_days_between=8 兜底补调);
非调仓日只查硬止损, 通常无单。

1. 数据 (Alpaca): `python3 scripts/integrations.py bars --symbols <momentum universe 逗号分隔> --start <今天-550天> --out <scratchpad>/mom_bars.json` 和 `integrations.py quotes --symbols <同> --out <scratchpad>/mom_quotes.json`。
2. 净值: `python3 scripts/paper.py equity --ledger state/momentum_positions.json --quotes <mom_quotes>` → equity/cash。
3. 信号: `python3 scripts/momentum.py signal --config strategy/momentum.json --state state/momentum_positions.json --bars <mom_bars> --quotes <mom_quotes> --date <今天> --portfolio-value <equity> --buying-power <cash> --out <scratchpad>/mom_orders.json`
4. 执行与回写: `paper.py run --orders <mom_orders> --date <今天> --coid-prefix cqm --fills-out <mom_fills>` → `signals.py apply --state state/momentum_positions.json --fills <mom_fills> --date <今天> --portfolio-value <equity>`。
   (`--coid-prefix cqm` 隔离幂等ID, 防止与其他 paper 轨道同日同标的订单冲突。)
5. journal 记录: 是否调仓日、动量分数榜前5、目标持仓 vs 实际、净值。熔断触发则该账本自行 halted, 通知用户, 不影响其他轨道。
6. 评估: 实验累计 ≥60 交易日 (约12次周度调仓) 后与用户复盘决定去留; 期间不并入任何实盘决策。

## 8. 参数搜索 (无活跃挑战者时执行)

1. 拉长历史 (约 5 年): `python3 scripts/integrations.py bars --symbols <ETF池逗号分隔> --start <今天减5年> --out <scratchpad>/bars_5y.json`
2. `python3 scripts/learn.py search --config strategy/config.json --learning strategy/learning.json --state-learn state/learning.json --historicals <bars_5y> --date <今天> --start-capital <实盘 total_value> --paper-ledger state/paper_positions.json`
3. 输出记 journal: `new_challenger` → 新挑战者次日起影子运行; `champion_optimal` → 冠军仍最优, 本轮不设挑战者。

## 8B. 次日预审报告 (execution.mode=confirm 时, 每日收盘后)

0. **先查资金**: `get_portfolio(802095265)` 取实时 buying_power 与 cash。报告必须以资金段开头,
   并按资金分两层出计划: ①立即可执行层 (仅用现有购买力能买什么) ②依赖换仓层 (需卖存量腾资金的部分,
   注明依赖"当日卖出款即时可用"这一结算假设)。计划总额不得超过 现有BP + 计划换仓卖出估值。
1. 用当日收盘数据对两个实盘引擎做次日 dry-run (隔夜引擎用 max_new_entries 放大到 10 取扩展候选)。
2. 生成 `state/approval.json`: trade_date=次一交易日, approved=false, buy_candidates=[ETF池10只 + 隔夜扩展候选], funding_sell_order=[存量按弱势排序], preview_top5。
   funding_sell_order 兼作次日**加速清理授权** (config.json legacy.accelerated_liquidation): 引擎将额外卖出其中最弱的最多3只, 与买入需求无关; 报告需向用户列明这一层。
3. 生成用户报告文档 `<scratchpad>/daily_report_<日期>.md` (当日成交/五轨状态/系统事件/次日预审), 用 SendUserFile 发送给用户。
4. 把预审报告发给用户 (候选表 + 换仓顺序 + 风险注记), 提示"回复确认即生效"。
5. 用户确认后: approved=true + approved_at 时间戳, 提交推送。次日闸门按此放行。

## 9. 异常总原则

任何预期外情况(API 报错、成交与订单不符、数据缺失过半) → **立即停止交易**, 把已发生的事写进日志, 通知用户。宁可错过一天, 不做没把握的操作。
