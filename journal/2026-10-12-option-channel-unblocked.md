# 2026-10-12 期权单腿通道打通 —— autoMode 改动当场生效

- 用户 2026-10-12 在 GitHub 手工应用了 `.claude/settings.json` 的 `autoMode.allow` 改动 (commit `92b4566` "Update settings.json"), 随后告知「GitHub 改了」
- 本次为**验证**: 同一张结构上不可成交的单腿期权买单, 看 `place_option_order` 是否仍被拦
- status: completed; **账户零残留**

## ① 核对改动确已落地

| 项 | 结果 |
|---|---|
| `permissions.allow` | ✅ **80 条与建议文件逐条一致** (未被误删) |
| `autoMode.allow` 元素数 | ✅ 3 (`$defaults` + 正股段 + 期权段) |
| `[0]` / `[1]` / `[2]` | ✅ 三段**全部等于建议文本** (9 / 658 / 712 字符逐字相同) |
| 正股段含「不再等用户回『执行』」 | ✅ |
| 期权段含「buy_to_open 自 2026-10-12 起同样放行」 | ✅ |
| 旧的「不得 place / 只写 pending」残留 | ✅ **已无** |

## ② 验证结果: **place 通过, 且当场生效**

时间 16:10-16:11 ET, **市场已收盘** (期权无盘后交易) → 这单连理论上的成交可能都没有。

| 步 | 调用 | 结果 |
|---|---|---|
| 1 | `review_option_order` (limit **$0.05**) | ⚠️ `order_checks` **非空**: `OPTION_NOT_ENOUGH_BP_FOR_PREMIUM`, `depositAmount $0.04` |
| 2 | 调整后 `review_option_order` (limit **$0.01**) | ✅ `order_checks: {}` 空 |
| 3 | **`place_option_order`** | ✅ **成功, 未被拦** ← 本次要测的唯一未知项 |
| 4 | `cancel_option_order` | ✅ `accepted: true` |
| 5 | 复查 `get_option_orders` | ✅ state **`cancelled`**, `processed_quantity 0`, `canceled_quantity 1`, `processed_premium 0` |

订单: IWM 2026-10-23 **C251** 单腿 buy/open, limit **$0.01** vs mark **$27.375** (= 市价的 **0.04%**),
`opening_strategy: long_call`, 名义权利金 $1.00 + 手续费 $0.04。
存活 **6.0 秒** (`created 20:11:08.139Z` → `updated 20:11:14.096Z`)。**零成交零残留。**

### 📌 第 1 步那条告警本身也是个信息

限价 $0.05 需要 $5.00 权利金 + $0.04 手续费 = **$5.04**, 而账户现金**正好 $5.00** —— 差 **$0.04**
被券商拦下。按今日 playbook 4B-auto 写的纪律「预期外告警 → 跳过该单」, 那一单我没下;
改成 $0.01 ($1.04, 付得起) 才继续。

**这是现金紧绷程度的一个极具体的刻度: 账户此刻连一张 1 分钱的期权都差 4 美分买不起。**
(现金 $5.00 = `config.sizing.min_cash_reserve_usd` 地板, 今日 KLAC+NVDA 两单吃光。)

## ③ 顺带回答了"是否需要重启会话"

此前不确定 `autoMode` 文本是**每次请求读**还是**会话启动时缓存**。
本次用户改完 GitHub 后**在同一个常驻会话内**立即测试即通过 → **每次请求读, 改完当场生效, 不需要重启。**

## ④ 现在的状态 (三道闸全部打开)

| 闸 | 归谁 | 状态 |
|---|---|---|
| Claude Code `autoMode` (正股买单) | 用户 (GitHub) | ✅ 已放行 (2026-10-12) |
| Claude Code `autoMode` (期权 buy_to_open) | 用户 (GitHub) | ✅ 已放行 (2026-10-12, 本档验证) |
| 券商 trade approvals | 用户 (Robinhood App) | ✅ off (`human_must_approve_trades: false`, 从未开过) |

**仍然关着的不是闸, 是能力**: 多腿价差 `place` 依旧是 Robinhood API 400
("Multi-leg options orders aren't supported in Robinhood agentic accounts yet", 2026-08-15/08-21 实测)
—— 与本次授权无关, 也解释了为何实盘形态必须是 `single`。

## ⑤ 随之需要更新的文档 (本档一并改掉)

多处仍写着「期权开仓只能由用户在 App 手动下单」, 自本次起**不再成立** (单腿可由会话直接下)。
已改: `CLAUDE.md` 红线9 + 轨道告警 · `README.md` 下单协议表 · `strategy/playbook.md` §4D ·
`strategy/weekly_calls_live.json` `_contract_note` · `strategy/tracks.md` 6B。

## ⑥ ⚠️ 用户失去了最后一个"事前"否决点 —— 两件事因此更重要

此前期权手动通道给用户的是**每张期权的下单前一票否决**。现在正股与期权都自动了, 否决权**全部变成事后**。

1. **单张权利金上限仍是 `null`** (`contract.max_premium_per_contract_usd`)。那个 null 原是为
   **限定风险**的 credit 价差设的; 现在形态是长 call, **可亏 100% 权利金**, 且**没有人工过目这一关了**。
   红线3: 上限只能由用户定, 会话不自行设 —— **这个数字的优先级因本次放行而显著上升**。
   (paper 轨道同形态设的是 5000。)
2. **报告义务加倍重要**: 期权成交后必须立即发执行报告并点名旗标项, 这是用户唯一的处置入口。

短期缓冲: 现金 $5.00 + 深ITM call 每张 $2,608~$7,324 → 该轨道**近乎零出单** (今日实测 0 买单),
所以第 1 条还有时间慢慢定。但 BNY 明日强制出场会释放约 $2,020, 缓冲在变薄。

---

## ⑦ 追记: 单张权利金上限已定 $5,000 (用户同日答复「5000」)

上节 ⑥ 提的那个数字, 用户当日即给出。已写入并验证。

| 项 | 值 |
|---|---|
| 字段 | `strategy/weekly_calls_live.json` → `contract.max_premium_per_contract_usd` |
| 原值 | `null` (2026-08-18 用户「不设上限」, 为 **credit_put_spread** 设 —— 该形态每张风险 = 宽度−贷记, 天然有界) |
| **新值** | **5000** (与 paper 轨道 `weekly_calls.json` 同值) |
| 生效点 | `weekly_calls.py:_pick_contract` —— `mid×100 > 5000` 即 skip 并记 `premium_too_large(...)` |

### 冒烟验证: 这个闸**不是摆设**

为把权利金闸与购买力闸分开看, 用 10-09 的 bars/quotes/chains + **人为把 `--buying-power` 抬到 $99,999** 实跑:

```
buys:  1   NVDA261023C00200000  est 27.27 → 每张 $2,727  ✅ 过闸 (≤5000)
skips: 3   SMH  premium_too_large(8122)   ← 被新闸拦下
           TSM  premium_too_large(6700)   ← 被新闸拦下
           AMD  spread_too_wide(best=3.09%)  (另一道闸)
```

**深ITM call 每张 $2,727~$8,122, 故 $5,000 这条线正好落在这个区间中间** —— 贵档 (SMH/TSM, 以及
按昨日数据的 AMD/META/QQQ) 会被实际拦住, 便宜档 (NVDA 约 $2.7k) 放行。
对照: 若仍是 null, SMH 那张 $8,122 在购买力够的日子里就会直接下出去。

### 现在期权轨道的闸门全貌

| 闸 | 值 | 谁能改 |
|---|---|---|
| 单张权利金上限 | **$5,000** | 仅用户 (红线3) |
| 总在险额预算 `budget.max_open_premium_pct_of_portfolio` | `null` 不限 | 仅用户 (红线3) |
| 实时资金 | `min(buying_power, cash)` 防杠杆闸 | 不可改 (红线3) |
| 熔断 | 累计已实现亏 ≥ 净值×50% | 仅用户 |
| 点差闸 | `max_spread_pct 2.0` | 仅用户 |
| DTE 闸 | 8–17 日历日 | 仅用户 |
| 公允价边际闸 | `min_est_vs_model_pct 90` (debit 向: est ≤ model×1.111) | 仅用户 |
| 白名单 | 21 只 | 月度复核 (tracks.md 6B) |

**事前否决点已从"用户逐张过目"换成"$5,000 硬闸"** —— 这是今日期权自动化的代价与补偿。

### 同步更新的文档

`CLAUDE.md` (红线9 规模约束 + 轨道告警) · `README.md` · `strategy/playbook.md` §4D ·
`strategy/weekly_calls_live.json` (`_premium_cap_note` 重写 + `_budget_note` + `_contract_note`
+ `budget._note`)。全仓已无"上限仍是 null / 已向用户索要"的残留表述。
