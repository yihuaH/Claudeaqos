# 轨道规范与手工回退 (tracks.md)

> **本文件是 `scripts/daily.py` 驱动器所实现步骤的权威规范**, 平时不需要逐条执行 —
> 主跑走 playbook §1 的快捷路径即可。**只在两种情况下按本文件手工逐步执行**:
> ① 驱动器报错或输出可疑 (plan.json 有 fatal/anomalies 且无法定位);
> ② 需要单独重跑某一条轨道。手工执行后须在 journal 注明"驱动器回退, 手工执行 §X"。
>
> 拆分自 playbook.md (2026-08-06 用户「拆」): 原文档 431 行中约 140 行属本类内容,
> 与每日必读的执行契约 (§4) 混在一起稀释重点, 故独立成册。**内容一字未改**, 仅迁移。
> 日常执行契约 (§0/§4/§5/§8B/§9) 仍在 `strategy/playbook.md`; 已停用轨道见 `strategy/archive/paused-tracks.md`。

## 数据与信号 (驱动器 phase_data / phase_stock_signal)

### 1. 取数 (完整手工步骤)


1. `get_portfolio(802095265)` → 记下 `total_value` 和 `buying_power`。
2. `get_equity_positions(802095265)` → 与 `state/positions.json` 核对; 数量不一致以券商为准, 先修正 state。把结果整理成简单映射存到 scratchpad: `{"SYM": {"qty": x, "available": y, "intraday": z}}`。
3. 报价 (候选池 = ETF池9只 + universe.json 100只, 2026-07-21 起):
   `get_equity_quotes(ETF池 + 全部持仓符号)` (Robinhood, 只收录 state=active) +
   `integrations.py quotes --symbols <universe 100只>` (Alpaca IEX) → 合并成一个 `{"SYM": price}` 映射存 scratchpad
   (同名以 Robinhood 为准; universe 中某符号无报价 → 该符号自然落选, 引擎会告警)。
4. 历史:
   - `integrations.py bars --symbols <ETF池 + universe 100只> --start 今天-660天` → 一个文件 (SMA200 需 ≥200 交易日);
   - `get_equity_historicals(持仓符号, start=今天-140天, interval=day)` (Robinhood) → 持仓文件。
4B. 财报日: 生成/复用当日 `earnings.json` (供 5B 隔夜轨道复用同一份, 每天只拉一次) — 个股防御层需要;
   取不到则不传 `--earnings`, 引擎按 allow_unknown_earnings 继续 (仅失去财报回避, 会告警)。
5. 宏观数据 (标准步骤): `python3 scripts/integrations.py macro --out <scratchpad>/macro.json`。
   成功则在算信号时加 `--macro <macro.json>`; 失败则省略该参数, 交易照常, 在日志注明。
   (VIX ≥ 配置阈值时引擎只停新开仓, 卖出/止损照常; VIX 数据过旧时引擎自动跳过过滤并告警。)
   macro.json 含 `context` 段 (FRED 收益率曲线 T10Y2Y / 高收益+投资级信用利差, 2026-07-31 用户加) —
   **仅报告级"宏观环境", 引擎只读 `vix` 门控交易, `context` 不参与任何交易决策**; 战报 8B 展示 (见 §8B)。

### 2. 算信号 (完整命令)

```bash
python3 scripts/signals.py signal \
  --config strategy/config.json \
  --state state/positions.json \
  --historicals <Alpaca bars文件(ETF池+universe)> <持仓历史文件...> \
  --quotes <合并报价映射文件> \
  --positions <持仓映射文件> \
  --earnings <earnings.json (有则传)> \
  --date <YYYY-MM-DD> \
  --portfolio-value <total_value> \
  --buying-power <buying_power> \
  --out <scratchpad>/orders.json
```

候选池 (2026-07-21 起) = config `etf_universe` 9 只低价孪生 ETF + `stock_universe_file` (universe.json) 100 只个股,
同一引擎按 RSI2 从低到高竞争 4 个仓位槽 (个股不设槽位上限, 用户 2026-07-21 指示)。
防御层对个股强制生效 (财报回避/单日异动过滤/行业上限≤3), ETF 在 exempt_symbols 中豁免。

## 纸面轨道 (驱动器 phase_paper / phase_options)

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
   d. **收盘后运行 (market closed, 收盘后主跑常态)**: 上述每个 `paper.py run` 追加
      `--allow-queue --queued-out state/paper_queued_challenger.json` — Alpaca-paper 收盘后拒 market 单,
      故改用 limit/day (限价 est×(1±3%)) 挂至**次一开盘**, 不等成交 (今日 fills 空), 排队清单入库并提交。
      **回收放在次一交易日主跑 §6 开头** (下方步骤 0, 主跑上下文写账本不受分类器限制; 报告窗口晨检为只读不做 paper 写回)。
      开盘时段运行则无需 --allow-queue (照旧即时成交)。
0. **回收昨日排队单 (§6 最先做)**: 若 `state/paper_queued_challenger.json` 存在, 先
   `paper.py sync --queued state/paper_queued_challenger.json --fills-out <scratchpad>/ch_sync_fills.json --prune`
   → `signals.py apply --state state/paper_positions.json --fills <同> --date <今天>` 把昨日排队单的开盘成交回写账本
   (sync 只读订单状态+写本地账本, 无需开盘), 再往下算当日挑战者信号。momentum/期权轨道同理各自 sync 其 queued 文件。
6. 回写账本:
   a. 正股: `python3 scripts/signals.py apply --state state/paper_positions.json --fills <paper_fills> --date <今天> --portfolio-value <paper equity>`
   b. 期权: 对 opt_close_fills 和 opt_open_fills 分别 `python3 scripts/options_overlay.py apply --ledger state/paper_positions.json --fills <文件> --date <今天>`
7. 记录净值 (成交后重算: `paper.py equity --ledger ... --quotes ... --chains <chains>`):
   `python3 scripts/learn.py record --state-learn state/learning.json --date <今天> --live-equity <实盘 total_value> --paper-equity <equity_ex_options>`
   ⚠️ **当日若有入金/出金必须加 `--live-deposit <净流入, 出金为负>`** (2026-08-07 修正): 净值曲线改用
   时间加权收益 (TWR), 逐段剔除外部现金流。漏填会把入金当成策略收益, 令 edge 指标失效 —
   08-04 的 $4,000 入金曾让 live_return 虚高到 +218% (真实 +12.2%), 挑战者 A/B 空转两天。
   判定方法: `get_portfolio` 的 total_value 日间跳变远大于持仓涨跌, 或用户明确说过充值。
   **注意: record 必须用 `equity_ex_options`** (剔除期权轨道, 保持参数 A/B 对比干净); `overlay_pnl` 单独记入 journal 的备兑一栏。
8. 评估: `python3 scripts/learn.py evaluate --learning strategy/learning.json --state-learn state/learning.json --paper-ledger state/paper_positions.json --date <今天>`, 结果记入 journal。
   - `pass` 且 `auto_promote=true` → `learn.py promote ...`, 在 journal 显著标注**参数晋级**并**通知用户** (新旧参数、验证期表现)。
   - `fail` → `learn.py reject --reason <evaluate给出的原因>`, 记日志并通知用户, 然后按第 8 节搜索新挑战者。
   - 其余 (`insufficient_data`/`extend`) → 继续验证, 无需通知。

## 7B. 动量轮动实验 (仅 paper, strategy/momentum.json enabled=true 时)

第 7 节完成后执行, 独立账本 `state/momentum_positions.json`, 任何失败只记日志。
周度节奏: 每周一为调仓日 (账本从未调仓过时立即调仓; 节假日错过由 max_days_between=8 兜底补调);
非调仓日只查硬止损, 通常无单。

1. 数据 (Alpaca): `python3 scripts/integrations.py bars --symbols <momentum universe 逗号分隔> --start <今天-550天> --out <scratchpad>/mom_bars.json` 和 `integrations.py quotes --symbols <同> --out <scratchpad>/mom_quotes.json`。
2. 净值: `python3 scripts/paper.py equity --ledger state/momentum_positions.json --quotes <mom_quotes>` → equity/cash。
3. 信号: `python3 scripts/momentum.py signal --config strategy/momentum.json --state state/momentum_positions.json --bars <mom_bars> --quotes <mom_quotes> --date <今天> --portfolio-value <equity> --buying-power <cash> --out <scratchpad>/mom_orders.json`
4. 执行与回写: `paper.py run --orders <mom_orders> --date <今天> --coid-prefix cqm --fills-out <mom_fills>` → `signals.py apply --state state/momentum_positions.json --fills <mom_fills> --date <今天> --portfolio-value <equity>`。
   (`--coid-prefix cqm` 隔离幂等ID, 防止与其他 paper 轨道同日同标的订单冲突。)
   **收盘后运行**: `run` 追加 `--allow-queue --queued-out state/paper_queued_momentum.json` 挂至次开;
   **次一交易日主跑 §7B 开头**先 `paper.py sync --queued state/paper_queued_momentum.json --fills-out <f> --prune`
   回收 → `signals.py apply --state state/momentum_positions.json ...` 回写, 再算当日动量信号。
   (无调仓交易时 run 无单、不生成排队文件, 照旧只更新 last_rebalance。)
5. journal 记录: 是否调仓日、动量分数榜前5、目标持仓 vs 实际、净值。熔断触发则该账本自行 halted, 通知用户, 不影响其他轨道。
6. 评估: 实验累计 ≥60 交易日 (约12次周度调仓) 后与用户复盘决定去留; 期间不并入任何实盘决策。

## 7C. 周 call 轨道 (步骤 1-6 = paper 摩擦实测 `weekly_calls.json`; 步骤 7 = 实盘实验仓 `weekly_calls_live.json`)

第 7B 节完成后执行, 独立账本 `state/weekly_call_positions.json`, 任何失败只记日志, 不影响其他轨道。
**目的 (2026-08-04 用户授权设立)**: 实测深ITM周call的真实点差/成交价 vs 回测模型 — 回测结论: RSI-2 × 深ITM(0.90)
× 无期权止损 × 跟正股出场, 模型摩擦 ±2% 时 +1.6~2.6%/笔、±3% 时 ≈0, 唯一悬而未决就是真实摩擦
(全部回测记录见 `journal/2026-08-04-weekly-calls.md`)。**纸面盈亏绝不驱动实盘 (红线7);
达 validation.go_bar 才与用户讨论实盘, 不达自动否决关停。**

0. **回收昨日排队单 (最先做, 在覆盖 last_orders 之前)**: 若 `state/paper_queued_weekly_calls.json` 存在:
   `paper.py sync --queued state/paper_queued_weekly_calls.json --fills-out <scratchpad>/wc_sync_fills.json --prune`
   → `python3 scripts/weekly_calls.py apply --ledger state/weekly_call_positions.json --fills <同> --context state/weekly_call_last_orders.json --date <今天>`
   (context = 昨日 signal 输出的入库副本, 提供入场/出场报价快照; skip 记录按日期去重, 重复 apply 无害。)
1. 数据 (Alpaca):
   - bars: `integrations.py bars --symbols <weekly_calls.json 与 weekly_calls_live.json universe 并集 (当前20只) 逗号分隔> --start <今天-450天> --out <scratchpad>/wc_bars.json` (SMA200 需 ≥200 交易日);
   - quotes: `integrations.py quotes --symbols <同一并集> --out <scratchpad>/wc_quotes.json`;
   - chains: `integrations.py chains --underlyings <同一并集 + 两账本持仓底层> --date <今天> --dte-max 17 --out <scratchpad>/wc_chains.json`;
   - earnings: 复用第 1 节 4B 的 earnings.json (缺则不传, allow_unknown_earnings=true 照常)。
2. 信号: `python3 scripts/weekly_calls.py signal --config strategy/weekly_calls.json --ledger state/weekly_call_positions.json --bars <wc_bars> --quotes <wc_quotes> --chains <wc_chains> --earnings <earnings.json> --date <今天> --out state/weekly_call_last_orders.json`
   (输出**入库** — 次日跨会话回收排队单时需要它做 --context; 本步 commit 时一并提交。)
3. 执行: `paper.py run --orders state/weekly_call_last_orders.json --date <今天> --coid-prefix cqw --fills-out <scratchpad>/wc_fills.json`;
   **收盘后运行 (主跑常态)** 追加 `--allow-queue --queued-out state/paper_queued_weekly_calls.json`
   (limit/day 挂次开: 买 mid×1.03 / 卖 mid×0.97; OCC 单整张)。无单则不生成排队文件。
4. 回写: `weekly_calls.py apply --ledger state/weekly_call_positions.json --fills <wc_fills> --context state/weekly_call_last_orders.json --date <今天>`
   (排队日 fills 为空, apply 仍要跑 — 它负责把当日 skip 记录进 skip_log; 成交在次日步骤 0 回收)。
5. 摩擦报告: `weekly_calls.py report --config strategy/weekly_calls.json --ledger state/weekly_call_positions.json --chains <wc_chains> --date <今天>`
   → journal 加"周call摩擦实测"小节: 当日进出/skip 及原因、round_trips 数、中位单边点差、
   中位每笔盈亏、成交vs前收mid、mid vs 模型价偏差、validation verdict。
6. 判定纪律: report 的 verdict 变为 `GO_candidate` 或 `NO_GO` 时**通知用户**并写显著标注;
   `NO_GO` → 建议用户关停 (enabled=false), 绝不擅自转实盘。合约已过期未平 (被自动行权) 的告警
   → 按红线 6 停该仓、通知用户人工对账。轨道熔断 (累计已实现亏损 ≥$2000) → 引擎自动只出不进, 通知用户。
6B. **期权池月度复核 (每月首个交易周一执行, 周一休市顺延至该周首个交易日; 2026-08-05 用户批准「加进去吧」; 首跑 2026-08-10)**:
   白名单是静态的, 本步是它唯一的更新通道。两部分, 全确定性:
   - **① 在册通过率盘点**: 统计两账本 skip_log 近 30 天各标的 spread gate 拦截/通过次数;
     30 天内被拦 ≥3 次且 0 通过 → journal 标注「休眠」(不移除名单, 仅供用户参考)。
   - **② 候选实测收编**: 候选 = strategy/universe.json 内 [现价 ≤ 当前期权预算÷0.105 (深ITM权利金≈10.5%现价)
     且不在白名单] 的个股 + 主流行业/资产 ETF 板凳 (XLI/XLV/XLU/SLV/GDX 等, 限回测家族同类);
     逐个 `integrations.py chains --dte-max 17` 实测深ITM (0.85-0.92×spot) 点差 →
     **收编标准 (与 2026-08-04 TLT/BAC 同口径): ≥1 档同时满足 spread ≤2% 且权利金 ≤ 当前预算** →
     达标者加入双配置 universe (paper+live 对齐), 未达标记录淘汰原因进 journal; commit。
   - 红线合规: 只改"能买什么"不改"何时买/买多少" (同 screen.py 定位, 红线2); 收编标准确定性, 不得凭观点加减名单。
   `python3 scripts/weekly_calls.py signal --config strategy/weekly_calls_live.json --ledger state/weekly_call_live_positions.json --bars <wc_bars> --quotes <wc_quotes> --chains <wc_chains> --earnings <earnings.json> --buying-power <get_portfolio 实时BP> --portfolio-value <get_portfolio total_value> --date <今天> --out state/weekly_call_live_last_orders.json`
   (输出入库供次日 context) → **sells 按 §4D 自动执行并 apply 回写; buys 写
   `state/pending_option_orders.json` 待用户「执行」(§4D)**; `report --config strategy/weekly_calls_live.json
   --ledger state/weekly_call_live_positions.json` 一并跑, journal 记"实盘周call"小节 (含 skip 原因 —
   budget_exceeded 属预期, 表示待用户追加投资/上调 budget)。回收昨日排队/成交: 晨检或本步开头
   `get_option_orders` 核对后 `apply --context state/weekly_call_live_last_orders.json`。
   **near_signals (期权预警) 处理 (2026-08-05 用户批准)**: signal 输出的 `near_signals` 非空 →
   按 §4B option_alert 格式写进当日 state/pending_orders.json (无股票买单时也生成仅含 option_alert
   的 pending 供 4C 参考) + PushNotification 提及 + journal 记预警; 空则无动作。

## 参数搜索 (低频, 手工触发)

## 8. 参数搜索 (无活跃挑战者时执行)

1. 拉长历史 (约 5 年): `python3 scripts/integrations.py bars --symbols <ETF池逗号分隔> --start <今天减5年> --out <scratchpad>/bars_5y.json`
2. `python3 scripts/learn.py search --config strategy/config.json --learning strategy/learning.json --state-learn state/learning.json --historicals <bars_5y> --date <今天> --start-capital <实盘 total_value> --paper-ledger state/paper_positions.json`
3. 输出记 journal: `new_challenger` → 新挑战者次日起影子运行; `champion_optimal` → 冠军仍最优, 本轮不设挑战者。
