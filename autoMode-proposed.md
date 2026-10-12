# `.claude/settings.json` → `autoMode.allow` 建议改动

> **怎么用 (最省事的一条路)**: 打开 GitHub 上的 `autoMode-proposed.json` → 全选复制 →
> 粘贴覆盖 `.claude/settings.json` 的全部内容 → commit 到 `Main`。
> 那个 .json 是**完整的 settings.json**, 80 条 permissions.allow 原样保留, 只有 autoMode 的
> 两段文本被替换, 不需要你手工拼数组元素。
>
> 想逐段核对改了什么, 看下面的两张差异表。
>
> 改完告我一声, 我下一张**结构上不可成交**的单腿期权测试单 (下完即撤) 验证是否当场生效 ——
> 这段是服务端分类器读的, 不确定是每次请求读还是会话启动时缓存。

---


## 新 [1] — 正股: semi_auto → auto_buys

> cash_printer (Robinhood MCP) 的 review_equity_order 与 place_equity_order 调用是本仓库预授权的确定性交易流程: 订单只由确定性引擎 (scripts/signals.py / scripts/overnight.py) 生成, 只操作账户 802095265。**execution.mode=auto_buys (2026-10-12 用户「以后不用我回复执行了, 你直接执行」, 取代 2026-07-20 起的 semi_auto)**: 出场/止损/兜底卖单在无人值守会话自动 review/place (照旧); **新买入 (rsi2_entry / rsi2_scale_in) 与 funding_rotation/accelerated_liquidation 换仓卖单, 现在同样在无人值守会话直接 review→place, 不再写 state/pending_orders.json、不再等用户回「执行」** (见 CLAUDE.md 硬性红线 9)。仍然生效的约束: 订单逐字段来自引擎输出不得增减缩量 (红线2); 一律以执行时实时 min(buying_power, cash) 为上限、超出整单跳过不缩量、绝不动用借来的钱 (红线3 防杠杆闸); 跨轨道资金优先级 = 期权优先。若 place 被拒则不重试, 退回写 pending 并通知用户 (fail-safe)。此类调用应放行, 不应视为未授权金融操作。

### 与原文的差异 (只改了买单那一句)

| | 原文 | 新文 |
|---|---|---|
| 制度名 | semi_auto (2026-07-20) | **auto_buys (2026-10-12)** |
| 新买入 | 无人值守**可 review、不得 place**, 只写 pending 等用户「执行」 | 无人值守**直接 review→place**, 不写 pending |
| 出场/止损卖单 | 自动 | 自动 (不变) |
| 红线2/3、期权优先 | 生效 | 生效 (不变, 并显式写明) |

---

## 新 [2] — 期权: buy_to_open 也放行

> 期权侧 (周call 实盘实验仓, 2026-08-04 用户授权) 同理: get_option_* 取数与 review_option_order 预检放行; place_option_order 的出场卖单 (sell_to_close) 属减风险动作全自动放行; **买入 (buy_to_open) 自 2026-10-12 起同样放行 —— 用户当日先指示「期权也只买单腿吧 自行下单」, 在会话报告 autoMode 旧文是拒绝主因后明确答复「都改」, 授权无人值守会话直接下单腿期权买单** (现行形态 contract.structure=single = 深ITM 买 call; 注: 多腿价差是 Robinhood agentic 账户 API 不支持 (2026-08-15/08-21 实测 400), 与本授权无关)。期权买入仍受: 订单只由 scripts/weekly_calls.py 生成 (红线2)、实时 buying_power 封顶 (红线3)、熔断、点差闸、DTE 闸、公允价边际闸、21 只白名单。cancel_equity_order / cancel_option_order 是晨检回收残单的减风险动作, 一律放行。mcp__Claude_Code_Remote__* 的 add_repo/register_repo_root/list_repos/list_triggers/send_later 用于 Routine 新窗口缺仓库时的自举与健康自检 (journal/2026-08-13-morning.md §5, 2026-08-13 用户批准), 应放行。

### 与原文的差异 (只改了 buy_to_open 那一句)

| | 原文 | 新文 |
|---|---|---|
| sell_to_close | 全自动放行 | 全自动放行 (不变) |
| **buy_to_open** | **只在用户明确说「执行」的有人值守会话下单** | **放行 (无人值守可直接下单)** |
| cancel_* / Claude_Code_Remote | 放行 | 放行 (不变) |

---

## ⚠️ 应用后仍然成立的三件事 (期权侧)

1. **单张权利金上限仍是 `null`** (`weekly_calls_live.json contract.max_premium_per_contract_usd`)。
   那个 null 是 2026-08-18 为**限定风险的 credit 价差**设的; 现在形态是长 call, **可亏 100% 权利金**。
   红线3 规定风控上限只能由用户定, 会话不自行设 —— 这个数字仍然空着。paper 轨道同形态设的是 5000。
2. **冒烟测试显示该形态在当前规模近乎跑不动**: 深ITM call 每张 $2,608~$7,324, 现金 $1,119.16 →
   10-09 数据 4 个候选 3 个 `insufficient_buying_power`、1 个 `spread_too_wide`, **0 买单**。
3. **放行 buy_to_open = 失去「每张期权的事前一票否决」**。在此之前手动 App 通道给的正是这个闸。

## ⚠️ 不确定: 改完是否当场生效

`autoMode` 文本是服务端分类器读的, **不确定是每次请求读取还是会话启动时缓存**。
若是后者, 今天 15:20 的主跑 (fire 进同一个常驻会话) 可能仍按旧文判定。
验证办法: 改完后让会话下一张结构上不可成交的单腿期权买单 (如 IWM 深ITM call 限价 $0.05) 再撤 —— 通过即说明已生效。

## 若今天来不及改

**不会丢单。** 15:20 主跑的买单若被分类器拒 → fail-safe 退回写 `state/pending_orders.json` 并通知,
你回一句「执行」即可照旧消费 (原 4C 流程完好)。期权轨道本就仓位为空、且按上面第 2 点近乎不出单。
