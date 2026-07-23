# 2026-07-22 半自动执行 (4C, 报告窗口会话)

用户 2026-07-22 16:31 ET (收盘战报后) 逐笔确认「执行」→ 3 笔 accelerated_liquidation
换仓卖单 (pending_orders.json, trade_date=2026-07-22)。

## 时段与模式

- 触发 16:31 ET (盘后) → 盘外模式; 分数股无法限价 → 按用户显式指示走 market + regular_hours,
  盘后提交自动排队至 2026-07-23 09:30 开盘市价成交 (开盘价, 无价格保护; 清仓弱势存量可接受)。

## 逐笔 (review 全部 order_checks={} 无告警, place 全部成功 queued)

| 卖出 | order_id | ref_id | review价 | avg_cost |
|---|---|---|---|---|
| ETSY 2.073344 | 6a617d11… | cdd90af2… | ~$80.48 | $84.07 |
| SE 0.908207 | 6a617d14… | 32dd5b5a… | ~$104.28 | $105.01 |
| SNOW 0.181326 | 6a617d16… | b4437e81… | ~$268.02 | $260.97 |

- 成交在次日开盘, 盈亏以实际成交价为准 (ETSY/SE 预计小亏, SNOW 预计小盈)。

## 回写安排

- 本会话未回写 state (未成交)。次日 (2026-07-23) 10:45 ET 晨检 get_equity_orders 捕获成交 →
  signals.py apply 回写 legacy (移除 ETSY/SE/SNOW) → 存量将仅剩 NET 一只。

## 分支说明

- 交易分支已由 claude/new-session-ty4g79 重组为 claude/focused-euler-9iakky (默认分支 Main);
  本记录提交至现行交易分支。Routine 唤醒词仍指向旧分支, 待用户决定是否重建。

status: executed (4C 盘外; 3笔排队开盘; 回写留次日晨检)
