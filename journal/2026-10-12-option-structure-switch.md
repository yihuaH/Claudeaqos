# 2026-10-12 实盘期权形态换代: credit_put_spread → single (深 ITM 买 call)

- 授权: 用户 2026-10-12 「期权也只买单腿吧 自行下单」→ 澄清「我的意思是 实盘买单腿 call 或者 put 都可以」
- 改动: **只改配置, 未改任何引擎代码**; 未碰账本; 未下任何单
- status: completed

## 做了什么

`strategy/weekly_calls_live.json`:

| 字段 | 原 | 新 |
|---|---|---|
| `contract.structure` | `credit_put_spread` | **`single`** |
| `contract.moneyness` | (无) | **0.90** |
| `contract.floor_moneyness` | (无) | **0.85** |
| `contract.max_spread_pct` | (无) | **2.0** |
| 卖/买腿 moneyness + 四个 credit 点差闸 | 在用 | 移入 `_retired_credit_put_spread` 留档 |
| `validation.start_date` | 2026-08-14 | **2026-10-12** (时钟第二次重置) |
| `validation.go_bar.max_median_leg_spread_pct` | 8.0 | **2.25** (还原该形态原值) |

三个单腿参数**逐字段照抄 paper 轨道 `strategy/weekly_calls.json`** (该轨道 08-04 起跑、16 回合实测),
未自行发明数值。0.90 也与本文件 `exit._underlying_stop_note` 里既有的
「单腿形态同理, 深ITM 0.90 在 -10% 处」完全一致。

点差 bar 从 8.0 还原到 2.25: 8.0 是 08-13 专为 credit 形态的 **OTM put 卖腿**校的 (那种腿百分比点差
天然宽), 深 ITM call 用不着放这么宽; 2.25 正是 08-13 之前该形态原本的值 —— **还原而非新定**。

验证时钟重置有先例: 2026-08-13 切 credit 时同样把 `start_date` 从 08-04 推到 08-14。
已归零的 credit 战绩 (8/8, +$573.00) 仍留在 `state/weekly_call_live_positions.json` 的 `round_trips` 里, **不删**。

## ❌ 「或者 put」做不到 —— 两重原因, 都不是配置能解决的

1. **引擎只认 call**: `scripts/weekly_calls.py:_pick_contract` 第 205-207 行硬过滤
   `if not meta or meta["type"] != "C": continue` —— `single` 形态的合约选择器根本不看 put。
2. **更根本的是: 没有看空信号可驱动**。入场条件 (第 567 行)
   `i["close"] > i["sma200"] and i["rsi2"] < cfg["entry"]["rsi2_max"]` =
   **上升趋势中的超卖反弹**, 是单向看多的均值回归信号。长 put 赚的是下跌, 与信号方向**相反**。

要做长 put 必须 ① 改引擎加 put 分支, ② **新造一个看空信号** —— 后者是红线2 明令禁止的
(「不得基于自己的市场观点新增订单」)。**未做, 也不建议做**: 这不是换个参数, 是加一条全新策略,
需要独立回测与用户授权。

## ⚠️ 换形态**没有**换来自动下单

用户原话是「自行下单」, 但本日早先的测试已证明做不到 (`journal/2026-10-12-option-single-leg-test.md`):

| 形态 | review | place | 拦截方 |
|---|---|---|---|
| 多腿 | ✅ | ❌ | **Robinhood 400** "Multi-leg ... aren't supported in agentic accounts yet" |
| **单腿** | ✅ | ❌ | **Claude Code 分类器** `[Real-World Transactions]` |

→ 期权开仓**仍走 playbook 4D 手动 App 通道**。换单腿只是把拦截点从券商换到了平台。

**一个副作用是好的**: 手动通道意味着用户对**每一张**期权单保留**下单前**的一票否决
(正股买单自今日起已经没有这个闸了)。下面那条"权利金无上限"的隐患因此不是无人看管的。

## 🔴 冒烟测试揭示的真问题: **当前账户规模下这个形态近乎跑不动**

用 10-09 的 bars/quotes/chains + 新配置实跑一次 `weekly_calls.py signal`
(`--buying-power 1119.16 --portfolio-value 10330.39`):

```
buys: []          ← 零买单
skips: 4
  SMH   insufficient_buying_power(cost=7324, bp_left=1119)   rsi2 5.08
  AMD   insufficient_buying_power(cost=6861, bp_left=1119)   rsi2 9.91
  NVDA  insufficient_buying_power(cost=2608, bp_left=1119)   rsi2 8.94
  TSM   spread_too_wide(best=3.00%)                          rsi2 8.15
```

**4 个候选: 3 个被购买力闸拦, 1 个被点差闸拦, 0 买单。**

根因是两种形态的**现金占用方式完全相反**:

| | credit_put_spread | single (长 call) |
|---|---|---|
| 现金流向 | **收**贷记 | **付**全额权利金 |
| 占用 | 抵押 = (宽度−贷记)×100, 平仓后释放 | 权利金一次性付出, 可归零 |
| 深ITM 0.90 的每张成本 | — | **$2,608 (NVDA) ~ $7,324 (SMH)** |
| 当前现金 | — | **$1,119.16** |

深 ITM call 的 delta≈0.98, 行为接近正股, 所以它的价格也接近"0.1×股价×100" —— 在 $230 的
NVDA 上就是 $2,600+, 在 $604 的 SMH 上是 $7,300+。**账户现金 $1,119 买不起白名单里大多数标的的一张。**

另外 TSM 的 `spread_too_wide(best=3.00%)` 说明 paper 的 2.0% 点差 bar 对实盘白名单里的高价股偏紧 ——
paper 参数并非原样可移植。

### 这意味着什么

换形态**技术上完成了**, 但**实际效果大概率是期权轨道长期零成交**。要让它真能跑, 只有三条路,
**每一条都需要用户定, 我不自行改** (红线2/3):

1. **把 `moneyness` 从 0.90 往 1.0 或以上挪** (ATM/OTM) —— 权利金立刻降一个量级。
   ⚠️ 但这**根本改变策略性质**: 深ITM delta≈0.98 近似正股、时间价值占比小; ATM/OTM 是高杠杆押注,
   归零概率大得多。paper 轨道验证过的只有 0.90, 换值等于换一个**未验证**的策略。
2. **放宽 `max_spread_pct`** (2.0 → 例如 3.5) —— 只能救 TSM 那类, 救不了购买力问题。
3. **接受期权轨道近乎休眠**, 等账户规模长大。credit 形态之所以跑得动, 正是因为它**收**钱而不是付钱。

## ⚠️ 还有一个未决的数字: 单张权利金上限

`contract.max_premium_per_contract_usd` 仍是 **null**。那个 null 是 2026-08-18 用户「不设上限」
为 **credit_put_spread** 设的 —— 该形态每张风险 = 宽度−贷记, **天然有界**。
换成长 call 后**可以亏掉 100% 权利金**, 这个 null 的含义就变了。

已向用户点明并索要一个数字; **用户未给之前保持 null 不动** (红线3: 风控上限只能由用户定,
会话不得自行设)。参考: paper 轨道同形态设的是 **5000**。

当前实际约束: 注码 `position_pct_of_portfolio=20%` + 实时 `min(buying_power, cash)` +
手动 App 通道 (用户逐单过目)。从冒烟测试看, **购买力闸事实上比任何权利金上限都更紧**。

## 顺带: 公允价边际闸方向自动翻转, 数值无需改

`min_est_vs_model_pct = 90` 保持不动。该闸按方向对称:
- credit 形态: 要求 est ≥ model × 0.90
- **debit 形态 (长 call)**: 要求 est ≤ model ÷ 0.90 = model × **1.111**, 即「不得比参考公允价贵过 11%」

对买方正是想要的方向。冒烟测试里 `model_edge.direction` 已正确显示 `"debit"`,
ratio 落在 94.9%~104.1% 区间, 闸工作正常。

## 回退方法 (一行)

把 `contract.structure` 改回 `"credit_put_spread"`, 并用 `contract._retired_credit_put_spread`
里的六个字段还原即可; `validation` 的 start_date/go_bar 也需一并还原到 2026-08-14 / 8.0。

## 本次未做

- 未改任何引擎代码 (`weekly_calls.py` 一行未动)
- 未改 `contract.max_premium_per_contract_usd` (等用户给数字)
- 未改 `contract.moneyness` 以外的策略性质参数 (0.90 是照抄 paper, 非自选)
- 未下任何期权单; 当前期权仓位为**空**
- 未碰 `state/weekly_call_live_positions.json` (credit 形态的 8 回合战绩原样保留)
