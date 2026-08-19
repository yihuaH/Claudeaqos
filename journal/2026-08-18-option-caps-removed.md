# 2026-08-18 · 期权轨道: 取消单张风险上限与预算上限 (用户指示)

**授权**: 用户当晚经选项确认「不设上限」= 取消 ③单张风险 $5,000 上限 + ④期权预算净值×50% 上限。
用户在选项中已看到风险说明 (「单笔最大亏损不再封顶」「期权敞口可吃掉全部净值」) 后作出选择。
起因: 弹药保留 4 次零转化的核查暴露期权轨道被上限卡死 (skip 全是 risk_too_large / budget_exceeded)。

## 变更内容

| 项 | 原值 | 新值 |
|---|---|---|
| `contract.max_premium_per_contract_usd` | 5000 | **null (不限)** |
| `budget.max_open_premium_pct_of_portfolio` | 50 | **null (不限)** |

`weekly_calls.py` 三处单张上限检查 (single / vertical / credit_put_spread) 改为支持 null:
`_pcap = cc.get(...)`; `if _pcap is not None and ...`。budget 侧代码本已支持 None (`bud_cap is not None` 守卫), 无需改。

## 仍然生效的约束 (未动)

1. **实时 buying_power 双封顶** —— 红线3 防杠杆闸, 绝不用借来的钱 (`min(BP, cash)`);
2. **轨道熔断** `circuit_breaker.max_cumulative_realized_loss_pct_of_portfolio = 50` (独立配置, 与 budget 无关);
3. **注码规则** `sizing.position_pct_of_portfolio = 20%` (每信号 ≈ 净值×20% 风险);
4. **贵档单仓上限** `sizing.max_single_position_pct_of_portfolio = 50%` ← **见下, 本次未取消**;
5. 点差双档闸 (12%/24%)、DTE 窗口 8-17 日、21 只白名单、财报黑窗。

## ⚠️ 验证发现: 第三个上限接管 (需用户决定是否一并取消)

用今日数据重跑实盘期权引擎, 两个目标上限确已解除 (skip 原因改变), 但**买入仍为 0**:

| 标的 | 取消前 skip | 取消后 skip |
|---|---|---|
| AVGO | `risk_too_large(6055)` | `premium_exceeds_position_cap(per=6055, target=2011)` |
| QQQ | `risk_too_large(6342)` | 同上 (per=6342) |
| DIA | `budget_exceeded(cost=4994)` | `insufficient_buying_power(cost=4994, bp_left=1010)` |
| XLI | 点差闸 62.58% | 点差闸 (不变) |

**新的绑定约束**: `sizing.max_single_position_pct_of_portfolio = 50%` = 净值 10,056 × 50% = **$5,028**
—— AVGO ($6,055) / QQQ ($6,342) 仍超此线。DIA ($4,994) 已过该闸, 改卡在今晚实时 BP $1,010
(股票三单吃完购买力所致, 属临时状态)。

该 50% 是 2026-08-14 用户「都改到50%和5000」时设的**注码层**上限, 与本次取消的两项不同,
本次未动。若用户希望 AVGO/QQQ 这类 $6,000+ 在险额的单也能开, 需一并调整或取消它。

## 风险提示 (记录用户知情状态)

取消后单笔期权理论最大亏损 = 在险额全损, 无美元封顶; 期权总敞口理论上可占满全部可用现金。
当前唯一的规模天花板是**实时 BP** 与**注码规则**。熔断仍在 (累计已实现亏 ≥ 净值×50% → 只出不进)。
**建议后续观察**: 首笔在旧上限下会被拦的期权仓成交后, 单独跟踪其盈亏与对组合的冲击。
