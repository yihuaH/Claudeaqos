# 2026-07-21 半自动执行 (4C, 报告窗口会话)

用户 2026-07-21 21:54 ET 在报告窗口逐笔确认「执行」→ 按 playbook 4C 盘外模式执行
`state/pending_orders.json` (trade_date=2026-07-21, 4笔)。

## 执行时段与模式

- 触发时刻 21:54 ET (盘后) → 盘外模式。
- 买单 (NEE): all_day_hours 整股限价, 今晚 24h 时段即可成交。
- 加速清仓卖单 (FTNT/NVO/ABBV): 分数股无法限价 → 按用户明确指示改走 **market + regular_hours**,
  盘后提交自动排队至次一交易日 (2026-07-22) 09:30 开盘市价成交 (开盘价, 无价格保护; 清仓弱势股可接受)。
  注: 这是对 4C "盘外换仓/加速清仓卖单一律跳过" 默认规则的用户显式覆盖 (本批用户要求"买卖都做")。

## 逐笔 (review 全部 order_checks={} 无告警, place 全部成功)

| # | 订单 | 类型 | order_id | ref_id | 状态 |
|---|---|---|---|---|---|
| 1 | 卖 FTNT 1.419734 | market/reg | 6a602759-e44f-4291-a223-7a8bb1c945fc | 5aaee881… | queued→明开盘 |
| 2 | 卖 NVO 1.812025 | market/reg | 6a60275a-58de-4ab6-a105-eb7eea6023ab | af47d73a… | queued→明开盘 |
| 3 | 卖 ABBV 0.942873 | market/reg | 6a60275d-6eb1-4634-a219-b37cf281d8d8 | 2afd6a4d… | queued→明开盘 |
| 4 | 买 NEE 3股 @$88.63 | limit/24h | 6a60275e-1e71-4fcf-8a8b-68b43a48dd06 | 4f416805… | unconfirmed |

- NEE 整股: floor(289.17 / 88.63) = 3 股, ≈$265.89 (信号价 87.75×1.01=88.63 封顶; 购买力 $847.21 充足, 不依赖卖单)。
- 引擎原单 CAT $290.77 (rsi2 5.49) 不在此清单 (est_price $864/股, 15% 仓位不足一整股, 当日市价窗口已过 → 自然落选)。

## 回写安排

- 本会话未回写 state/positions.json (卖单未成交, NEE 未确认)。
- 次日 (2026-07-22) 10:45 ET 晨检: get_equity_orders(802095265) 捕获 4 笔实际成交 →
  signals.py apply 回写 legacy (3笔卖出) + strategy (NEE 买入) → 更新 journal。

## 同日附带改动 (代码, 见随附提交)

- **存量止盈** (signals.py): 用户 2026-07-21 设立。存量仓现价 ≥ 纳管价 (breakeven, config
  legacy.take_profit_min_pct=0) 且触发反弹信号 (收盘>SMA5 或 RSI2>65) → 自动卖 (reason=legacy_take_profit),
  归 4A 自动执行。与 -7% 止损对称, 只卖赢家 (亏损仓由止损兜底, 不被误卖)。合成数据三情形验证通过
  (WIN 止盈 / LOSS 止损 / HOLD 持有), 与加速清仓组合无重复卖。次日主流程起生效。

status: executed (4C 盘外; 3卖排队开盘 + NEE 24h限价; 回写留待次日晨检)
