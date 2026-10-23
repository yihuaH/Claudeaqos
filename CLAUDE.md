# Claudeaqos — 每日自动交易系统

本仓库是一套通过 Robinhood MCP (cash_printer) 运行的每日自动交易系统。
定时 Routine 每个交易日唤醒会话, 按 `strategy/playbook.md` 执行。

## 硬性红线 (任何情况下不得违反)

1. 只允许操作账户 **802095265** (nickname "Agentic", agentic_allowed=true)。绝不触碰其他账户。
2. 所有买卖必须来自确定性引擎的输出: 实盘 RSI-2 与 paper 正股来自 `scripts/signals.py`, 实盘隔夜轨道来自 `scripts/overnight.py` (用户 2026-07-15 授权直接上实盘), paper 备兑来自 `scripts/options_overlay.py`, 周call 双轨 (paper 摩擦实测 `weekly_calls.json` + 实盘实验仓 `weekly_calls_live.json`, 后者用户 2026-08-04 授权「接受全赔、后续追加投资」) 均来自 `scripts/weekly_calls.py`, paper 动量轮动来自 `scripts/momentum.py`。不得基于自己的市场观点新增、放大或修改订单。
3. `strategy/config.json` 的风控限制 (仓位上限、单量上限、熔断) 是上限, 不是建议。
4. `enabled=false` 或 `halted=true` 时只允许读数据和写日志。
5. API 密钥、secret 一律不得写入本仓库 (用户提供的 Alpaca/FRED 密钥只存在会话环境中)。
6. 遇到预期外的告警、报错、数据异常: 停止交易 → 写日志 → 通知用户。不要即兴发挥。
7. Alpaca 只允许纸面环境 (`paper-api.alpaca.markets`, 已硬编码在 `scripts/paper.py`), 绝不调用 Alpaca 实盘交易接口。paper 账户内股票与期权均可交易、全部持仓均可处置 (用户 2026-07-15 授权, 期权权限 Level 3); 纸面盈亏只用于挑战者/实验验证, 不得直接驱动实盘订单。
8. 参数自学习边界: 学习器 (`scripts/learn.py` / `scripts/learn_overnight.py`) 只能修改各自 learning 配置列出的 entry/exit 形状参数且必须在边界内; sizing/熔断/宏观/legacy 等风控**永不自学习**。晋级必须先通过 paper 验证期且 evaluate 判 pass; 每次晋级/否决写 journal 并通知用户。
9. **买入自动执行 (execution.mode=auto_buys, 用户 2026-10-12 指示「以后不用我回复执行了, 你直接执行」, 取代 2026-07-20 起的 semi_auto)**: 实盘正股买单 (`rsi2_entry` / `rsi2_scale_in`) 与配套换仓/加速清理卖单 (funding_rotation / accelerated_liquidation) 由主流程在无人值守窗口**直接 review → place**, 不再写 pending、不再等用户回「执行」。前置事实: 2026-10-12 10:48 ET 无人值守平台层测试 place 未被拦 (`journal/2026-10-12-morning.md`)。
   - ⚠️ **本次授权只取消了「等用户回执行」这一步, 以下一律照旧**: ① **红线2** — 订单逐字段来自引擎输出, 不得新增/放大/**缩量**/剔除/重排; ② **红线3** — 所有仓位与单量上限、熔断仍是上限不是建议; ③ **防杠杆闸** — 买单一律以执行时实时 `min(buying_power, cash)` 为上限, 超出**整单跳过不缩量**, **绝不动用借来的钱** (动用杠杆仍须用户单独明确授权); ④ **跨轨道资金优先级 = 期权优先**; ⑤ 出场/止损/兜底卖单与纸面轨道照旧全自动; ⑥ **期权开仓 (buy_to_open) 自 2026-10-12 起同样自动** —— 用户当日在 GitHub 应用 `autoMode` 改动后实测 `place_option_order` 放行 (`journal/2026-10-12-option-channel-unblocked.md`); **仅限单腿**, 多腿价差仍被 Robinhood API 400 拒 (平台能力限制, 与授权无关), 这也是实盘形态必须是 `single` 的原因。
   - 🛟 **fail-safe (必须保留)**: 若 `place` 被平台分类器拦截 → **不重试**, 退回写 `state/pending_orders.json` 并通知用户, 走下方原 4C 协议 (其时效与双模式形态此时仍然适用)。**绝不因为被拦而丢单。** 依据: 2026-10-12 测试 **n=1** 且分类器非确定性 (09-11 LLY 出场卖单首次 place 被拦; 10-10 组合 Bash 被判 `Auto-Mode Bypass`), 且那是一张 $20 远离市价的限价单, **证明不了 $1,039 美元额市价单同样通过**。
   - 📢 **报告义务**: 取消「执行」= 用户失去了下单前的一票否决点, 故主流程**必须在成交后立即发执行报告**, 并点名所有旗标项 (新闻红旗 / 同因子敞口 / 财报黑窗失效 / 防御层拦单等), 让用户能**事后**处置。
   - 📐 **fail-safe 退回 pending 后的执行形态 (两种, 选哪种只看时间)**: **(a) 盘中 09:45–15:55 ET = 单笔 `market`+`regular_hours`+`dollar_amount`** —— 首选, 原生支持分数股, 零额外成本; **(b) 盘外 15:55 → 次一交易日 09:45 (含周末) = 整股限价①腿** —— `limit = 引擎 est_price × 1.010`, `all_day_hours→extended_hours→regular_hours` 逐级降级, `whole==0` 该单整单跳过留次日, **零头不另下单**, 随清单既有顺延机制走次日盘中窗。⚠️ 盘外①腿实测**多付 0.55%** + 盘后点差中位约 2%, 是**有成本的退路, 不是首选**; 且走盘外**必须重做 4C-1B 逐批确认** (形态由美元额市价变为整股限价, 数量不同且可能有单落空)。历史沿革与退役理由见 `strategy/archive/retired-offhours-two-leg.md`。
   - **结算与杠杆规则 (2026-08-07 用户升级 limited margin 后重写; 原 07-31「现金账户 T+1 铁律」作废)**: 账户 802095265 现为 **`type=margin` (limited margin)** — 卖出款**即时可用**、`buying_power` 已含未结算款、**GFV (善意违规) 风险消失**。⚠️ **防杠杆闸**: 该账户目前**无借贷额度** (`unleveraged_buying_power == buying_power`); 4C/4D 执行买单**一律以实时 `min(buying_power, cash)` 为上限**, 超出**整单跳过不缩量** — 若某日 `buying_power > cash` 说明券商开放了借贷, 系统**绝不自动使用借来的钱**, 动用杠杆须用户明确授权 (红线3)。
   - **新闻旗标 + 宏观环境 (报告级, 2026-07-31 用户加)**: `integrations.py news` 对买单标的做确定性红旗分类、`macro` 的 FRED `context` 段, **均仅提示/展示, 绝不改引擎选股或金额** (红线2); 红旗只在 pending/战报点名, 由用户 4C 一票否决。
   - **跨轨道资金优先级 (2026-10-19 用户「优先级最高」把 QQQ LEAP 插到最前)**: 现行次序 = **① QQQ LEAP (`qqq_leap.json funding_priority:1`) → ② 实盘周期权 → ③ RSI-2 正股**。LEAP **自 2026-10-23 起是三档梯子 (148/85/330 DTE 各 1 张)**, 一次信号合计约占净值 **53%** (当日实测 $5,228/$9,816), 且无单笔规模闸, 故它一次出单即可让当日其余两轨全部零成交 (信号 8.4 次/年)。⚠️ 三档是**三张独立订单**, 故 cap 不足时是「能买几张买几张」(按 `contract.ladder` 列表顺序 148→85→330 逐档判), 不是三张一起归零 —— 这正是不把它做成一张 3 contracts 单的原因。`daily.py` 里 `phase_qqq_leap` 排在 `phase_stock_signal` 与 `phase_options` 之前, 其 `reserve_usd` 是后两者 cap 的扣减项。以下原「期权优先」规则在 ②③ 之间照旧: 同日实盘股票买单与实盘周期权买单合计超过实时 `min(buying_power, cash)` 时, 先按期权单的**最大在险额** (credit_put_spread = 价差宽度×100×张数, 券商实扣) 留足抵押, 股票 cap = 可用资金 − 期权抵押, 超 cap 的股票单**整单跳过不缩量**; 期权单自 2026-10-12 起亦由会话自动下单 (playbook 4D, 仅单腿)。用户可单日一票推翻, 但**不再逐日询问**。
   - **实盘周call实验仓 (2026-08-04 用户授权)**: 期权买入 (buy_to_open) **自 2026-10-12 起由会话自动下单** (单腿限价; 用户当日应用 autoMode 改动后实测放行)。多腿仍被 Robinhood API 400 拒, 故形态必须 `single`。⚠️ **用户因此失去了每张期权的事前一票否决**, 而单张权利金上限仍是 null —— 该数字优先级显著上升 (红线3) (时效**照抄 plan.json 的 `option_pending_template`**, 两形态: **收盘前产出 = 当日 15:30–15:45 ET** [2026-09-11 用户「期权也可以当日马上执行」; 比股票的 15:55 早 10 分钟收口, 因 15:45 后是 4D 明令避开的收盘前极端点差区]; 收盘后产出 = 次一交易日 10:30 ET, 推荐 09:45–10:30 等开盘点差收窄, 2026-08-04 用户批准); 期权出场卖单 (sell_to_close) 属出场类, 照常全自动。**规模约束 = 没有单笔规模闸** (用户 2026-10-19「取消5000上限 不设上线」): `contract.max_premium_per_contract_usd` 与 `budget.max_open_premium_pct_of_portfolio` 与 `sizing.max_single_position_pct_of_portfolio` **三者全为 null**。沿革: 10-12 曾定 $5,000 (换 single 后长 call 可亏 100% 权利金), 实测确实拦住贵档 (SMH/TSM 的 `premium_too_large`), 10-19 用户取消。⚠️ **后果**: per_cost > 净值×20% 的贵档走 tier_cap=inf 例外**强制给 1 张**, 故单张规模上限退化为**仅剩实时 `min(buying_power, cash)`** —— 一张深ITM call 可吃掉当日几乎全部可用现金, 且期权优先于股票拿资金, 同日 RSI-2 买单会被整单跳过。仍并行生效: 实时 `min(buying_power, cash)` 防杠杆闸 (红线3) + 熔断 (累计已实现亏 ≥ 净值×50%) + 点差/DTE/公允价边际闸。改这个数只能由用户定。

## 分支约定 (系统级, 优先于 Routine 唤醒词)

**当前交易分支 = `Main`** (2026-07-22 仓库重组归一, 默认分支)。任何会话:
- 取数/回写/提交一律基于 `origin/Main` (`git fetch origin Main`); 若 Main 受保护不能直接 push,
  则 push 到工作分支再开 PR 合并回 Main。
- Routine 唤醒词中若仍写死已废弃分支名 (`claude/new-session-ty4g79` 等), **一律改用 Main**。
  过渡期 `claude/new-session-ty4g79` 保留为 Main 的镜像 (内容对齐), 待主流程 Routine 经界面重建指向
  Main 后即可删除。

## 报告格式约定 (对话输出, 2026-08-21 用户「每次报告开始要给我画明确的分界线」)

会话在对话里发给用户的**任何报告**——主跑结果、晨间核查、收盘战报、待执行清单 (pending)、
异常通知、周度刷新、研究结论——**必须以分界线开头**, 让用户一眼找到报告从哪里开始。

固定格式 (三行头, 一行尾):

```
════════════════════════════════════════
📊 <报告类型> · <日期> <窗口/主题>
════════════════════════════════════════

<正文>

════════════════════════════════════════
```

规则:
- 头部分界线是**第一件事**, 前面不写任何铺垫或过程说明; 过程叙述放在报告之前或之后, 不夹在头部与正文之间。
- 一条消息里若含多份报告 (如战报 + 次日预览), 每份各自加自己的头部分界线。
- 标题行写清"是什么报告 + 哪一天", 例: `📊 收盘战报 · 2026-08-21 (周五)`、
  `⚠️ 异常通知 · 2026-08-21 持仓一致性闸`、`📋 待执行清单 · 2026-08-21 (回复「执行」消费)`。
- 用 `SendUserFile` 发的报告文件不受此约束 (文件本身有标题); 但对话里配发的说明仍照此加分界线。
- 纯粹的一句话确认 (如「已 push」「已撤单」) 不算报告, 不必加。
- **待执行清单一律放在消息的最后** (2026-10-09 用户「以后执行清单放在最后, 这样我打开消息就能看到」):
  一条消息里同时有战报/主跑结果与**需要用户动作的清单** (pending 股票清单、期权 App 参数、
  等待「执行」或等待一票否决的任何东西) 时, **叙述性报告在前, 待执行清单在最后一份**。
  理由: 用户打开消息看到的是末尾, 要动作的东西必须落在那里。
  · 只有一份报告时不受影响 (它本来就是最后一份)。
  · 清单内部的逐笔明细、资金闸、点名提示仍写在清单自己的分界线之内, 不要为了"放最后"而把
    清单拆散到正文各处。
  · ⚠️ **与"先发期权参数再发股票清单"的关系** (Routine 唤醒词原文, 理由是期权窗 15:30-15:45 更窄):
    两条同时成立的做法 = **期权参数单独一条消息先发**, 主报告那条消息以股票清单结尾。
    若硬要塞进同一条消息, 则期权参数排在股票清单**之后** (最靠近用户视线), 因为期权窗更紧 ——
    此时"先期权后股票"按**可见性**而非字面顺序理解。

## 每日节奏 (2026-09-11 重排; 全部唤醒到常驻对话 `session_01NYPNGq5j25cGqpoLrWiA7t`)

| 时刻 (ET) | 窗口 | 干什么 | Routine |
|---|---|---|---|
| 10:45 | 晨间核查 | 残单回收 + **T+1 官方收盘核对** (券商 T 日收盘次日才发布); 只有昨日走了 4B-failsafe 留下 pending 才需用户「执行」, 正常日无事静默 | `trig_01CtgM6KvCBKywWzEtAEkNia` |
| **13:00 周一** | 周度股票池刷新 | 刷 `universe.json` 100 只 | `trig_01PvM5Mj89pokXgPwkMECZrd` |
| **15:20** | **收盘前主跑** | 关键路径: 信号 + 出场即时成交 + 当日 pending | `trig_01PqeuvMEsyXbTKJQ7njyVcR` |
| 17:45 | 收盘后收尾 wrapup **+ 战报** | 纸面轨道 + 行情核对 + journal + 账本回写, **并发当日战报** | `trig_01W1rzTiiZBaRc2taYzV6tKX` |

🕐 **定时任务已锚定时区 (2026-10-23 用户「加」授权, 勿再改回 UTC)**: 四个任务的 cron 原先是
**写死 UTC 无时区前缀**, 于是 **2026-11-01 夏令时结束**后全部会提前一小时 ——
收盘前主跑会跑到 **14:20 ET**, 落在 15:30–15:55 执行窗之外, 且信号会用离收盘更远的价格算。
现已全部加上 `CRON_TZ=America/New_York` 前缀, **时刻值一个没动**:

| 任务 | 现行 cron |
|---|---|
| 晨间核查 | `CRON_TZ=America/New_York 45 10 * * 1-5` |
| 周度股票池刷新 | `CRON_TZ=America/New_York 0 13 * * 1` |
| 收盘前主跑 | `CRON_TZ=America/New_York 20 15 * * 1-5` |
| 收盘后 wrapup | `CRON_TZ=America/New_York 45 17 * * 1-5` |

从此 ET 时刻全年固定, 春秋两次切换都不再漂。
⚠️ 以后**新建或修改**任何定时任务一律带该前缀; 看到不带前缀的就是漏了。
📌 周度刷池的 `next_run_at` 实际落在 **13:06~13:10 ET** (服务端对整点任务自加偏移, 避开 :00 流量),
不影响 15:20 前 finalize 的 2h13m 余量。

⚠️ **周一硬约束 (股票池刷新器必读)**: 刷新 13:00 起跑, **必须在 15:20 主跑前 finalize** (主跑要读
`universe.json`), 余量仅 2h20m。**跑到 15:10 仍未 finalize 就停手, 不要提交半成品** —— 写 journal
通知用户、次日补跑; 旧池继续有效, 迟一天远好过池被写坏。

## 轨道状态总览

状态以各 config 的开关字段为准; **沿革与回测依据见 `strategy/archive/track-rationale.md`**。

| 轨道 | 环境 | 状态 | 开关 |
|---|---|---|---|
| **RSI-2 均值回归** (主策略, ETF+个股) | 实盘 | ✅ active | `config.json enabled` + `scale_in.enabled` |
| └ 拆分式双段跑: 15:20 `--phase preclose` / 17:45 `--phase wrapup` | 实盘 | ✅ active | Routine `trig_01PqeuvMEsyXbTKJQ7njyVcR` |
| 隔夜均值回归 — **入场** | 实盘 | ⏸ 暂停 (2026-07-21) | `overnight.json live_entries_paused` |
| 隔夜均值回归 — 出场/兜底 | 实盘 | ✅ active (暂停只停入场) | 同上 |
| **周度期权实盘实验仓** | 实盘 | ✅ active·机会主义 | `weekly_calls_live.json enabled` |
| **QQQ LEAP call 梯子** (跌≥2% → **148/85/330 DTE 各买 1 张** delta 0.30) | 实盘 | ✅ active·**资金优先级最高** | `qqq_leap.json enabled` + `funding_priority:1` + `contract.ladder` |
| 挑战者影子验证 | paper | ⚠️ validating 但**指标不可用** (见下) | `learning.json enabled` |
| 周call 摩擦实测 (深ITM 买call) | paper | ✅ active | `weekly_calls.json enabled` |
| 周度动量轮动 | paper | ✅ active | `momentum.json enabled` |
| **SQQQ 短期看跌 QQQ** (QQQ 单日 ≥+2.5% → 买 SQQQ → **次日**收盘平) | **paper** | ✅ active·⚠️ 我的结论是「不建议做」, 用户知情下选 paper 先验 | `sqqq_short.json enabled` + `environment:paper_only` |
| 备兑开仓 overlay | paper | 视开关 | `options.json enabled` |
| 隔夜参数学习 A/B | paper | ⏸ 暂停 (2026-07-21) | `learning_overnight.json paused` |
| 个股防御实验 | paper | ⏸ 暂停 (2026-07-21, 已并入实盘) | `stocks.json enabled` |

### 会改变当日操作的告警 (必读)

- 🛟 **双段跑 fail-safe**: 当日无 `state/preclose_status.json` 的 completed 标记 → 17:45 wrapup
  **自动退化为完整主跑** (出场照下, 买单按 fail-safe 形态)。故收盘前挂掉最坏只退回改动前行为。
- 🔴 **期权实盘仓**: 2026-10-12 换 `single` (深ITM 买 call, moneyness 0.90), **同日起开仓/平仓均由会话
  自动下单** (单腿限价; 用户应用 autoMode 改动后实测放行)。多腿仍被 Robinhood API 400 拒 →
  形态必须是 `single`。**长 put 做不到** (引擎只认 call 且信号单向看多)。
  **单笔规模无闸** (用户 2026-10-19「取消5000上限 不设上线」把 10-12 定的 $5,000 置回 null; budget 总额与单仓 pct 本来也是 null) → 单张上限 = 实时可用现金; 并行生效实时
  `min(buying_power, cash)` + 熔断。长 call 可亏 100% 权利金, 而**现在没有任何闸按金额拦单** —— 唯一拦得住的是钱不够 (`insufficient_buying_power`)。
  当前账户规模下该形态**仍近乎零出单** (深ITM call 每张数千美元 vs 千元级可用现金), 即取消 $5,000 这一步**当下不改变出单与否**, 改变的是「一旦现金够, 单笔可以有多大」。
  前形态 credit_put_spread 战绩 8 回合 8 胜 / +$573.00, 参数留档在 `contract._retired_credit_put_spread`, 一键可回退。
- ⚠️ **挑战者 edge 仍不可用于晋级** (红线8), 但**不是**原先写的"分母按 3.8×账本 start_capital"
  —— evaluate 用的是 TWR, 分母已与各自 sizing 基准一致 (2026-10-14 查实)。真正的不可比是
  **实盘现金部署率远低于纸面** (受 cap 整单跳过) + **纸面整股向下取整**, 详见「已知未修的问题」第 5 条。
- ⚠️ **期权链是 `feed=indicative`** (OPRA 需签协议): 点差闸读的不是交易所真实 NBBO。

## 结构

详细设计依据与沿革见 `strategy/archive/track-rationale.md` 与 `journal/`。

### 契约与参数 (用户可改)

| 文件 | 内容 |
|---|---|
| `strategy/playbook.md` | **每日必读的执行契约**: 前置检查 / §4 下单协议 4A-4D / 回写 / 次日预览 / 异常原则 |
| `strategy/config.json` | 主策略与风控参数 (入场/出场/仓位/加仓/防御/宏观/熔断/`execution`) |
| `strategy/screen.json` + `universe.json` | 个股池筛选标准 + 筛选产出的当前 100 股池 |
| `strategy/weekly_calls.json` / `weekly_calls_live.json` | 周期权 paper 摩擦实测 / 实盘实验仓 |
| `strategy/qqq_leap.json` | **QQQ LEAP call 轨道 (实盘)**: 入场跌≥2%+牛市过滤 / **三档梯子 `contract.ladder` = 148/85/330 DTE 各 1 张** (列表顺序即资金优先级, 用户可重排; 空则回落单档) / delta 0.30 带[0.25,0.40] / 止盈 +20% (bid ≥ 入场ask×1.20) / 20 交易日时间止损 / 不设价格止损 / 不设并持上限 / `funding_priority:1`。每字段的回测来源见文件内注释与 `strategy/proposals/qqq-leap-call.md` |
| `strategy/learning.json` / `learning_overnight.json` | 自学习边界、搜索网格、晋级标准 |
| `strategy/sqqq_short.json` | **SQQQ 短期看跌轨道 (仅 paper)**: 入场 QQQ 单日 ≥+2.5% / 无趋势过滤 / 出场持满 **1 交易日无条件平** / `position_pct=10` / `environment:paper_only`。每个字段的回测来源见文件内注释与 `strategy/proposals/sqqq-short-qqq.py` |
| `strategy/momentum.json` / `options.json` / `stocks.json` / `overnight.json` | 各实验轨道参数 |
| `strategy/tracks.md` | 驱动器已接管轨道的**手工回退**完整命令 (平时不用) |
| `strategy/routines.md` | Routine 唤醒词原文与注意点 |
| `strategy/archive/` | 已停用轨道 / 已退役协议 / 轨道沿革 |
| `strategy/proposals/` | **构想中但未实现**的轨道设计稿 —— 无 config、无引擎改动、**无下单能力**。与 `archive/` 的区别: archive 是曾经跑过的, proposals 是**还没跑过的**。当前: `qqq-leap-call.md` —— ⚠️ **该设计稿已于 2026-10-19 实现并上实盘** (用户「20% 加入实盘」), 配置 `strategy/qqq_leap.json` + 引擎 `scripts/qqq_leap.py`; 文档留在 proposals 下作为**回测与参数来源的唯一出处** (§7 出场 / §12.6 阈值 / §15 载体 / §16 delta / §17 并持 / §18 止盈四档), 不再是「未实现」 |

### 脚本 (确定性引擎 + 闸门, 红线2: 只出信号不含会话判断)

| 脚本 | 职责 | 必须知道的 |
|---|---|---|
| `daily.py` | **每日主跑驱动器**, 一条命令跑完所有可脚本化步骤, 产出 `plan.json` | `--phase preclose\|wrapup\|full`; **只调引擎不含决策**; `--plan-only` 干跑; `pending_template` 等协议常量**由会话照抄不得凭记忆组装** (2026-09-10 事故) |
| `session.py` | 会话调度器: 判窗口、查幂等、读账本、打印本次精确清单 | **只调度不做交易决策** |
| `signals.py` | RSI-2 信号引擎 (signal/apply), 含**加仓机制** | 加仓: 收盘 ≤ 加权均价×(1−3%) → 补一档 (净值×10%), 每票≤2档 = 单票上限 20%; **触发价与止损价都比加权均价**; **时间止损时钟不因加仓重置**; 加仓单**排在新开仓单前吃现金, 执行时不得重排** |
| `position_check.py` | **持仓一致性闸**: 券商份额 vs 账本逐只比对 | 不一致即 anomaly 并在 preflight **停跑**; **只报告绝不改账本** |
| `price_check.py` | 行情管道交叉核对: 引擎 SIP 收盘 vs 券商官方收盘 | 单只偏差 >25bp 记 anomaly; 以**券商报出的那个日期**为准比对 (券商收盘滚动有延迟) |
| `screen.py` | 个股池确定性筛选 (pool/rank/finalize) | 只决定"能买什么", 买卖时机仍由引擎定 |
| `integrations.py` | 外部数据源 (Alpaca paper / FRED)、历史日线、宏观、新闻红旗 | ⚠️ **`quotes` 是 `delayed_sip` 延迟 15 分钟, 当日决策价必须走券商 `get_equity_quotes`**; 历史日线 `EQUITY_FEED=sip` |
| `weekly_calls.py` | 周期权引擎, 支持 `single`/`vertical_spread`/`credit_put_spread` | 实盘现行 `single`; **只认 call** (`_pick_contract` 硬过滤 `type != "C"`) |
| `qqq_leap.py` | **QQQ LEAP 轨道引擎** (`window`/`signal`/`apply`) | **两段式**: `window` 纯本地判信号+列出该取哪些合约; `signal` 需会话用 `--leap-chain` 传入**含 delta 的券商期权报价** (`integrations.py chains` 只落 {bid,ask} 无 delta)。**取数归会话、选档归引擎** (红线2)。止盈线在 `apply` 里按**实际成交价**重算 |
| `paper.py` | Alpaca 纸面执行器 (**仅 paper 环境**, 红线7) | 下单被拒逐单捕获**绝不中断整批**; 排队清单 `try/finally` 无论如何落盘; 同日卖+买冲突**保留卖单**; `_clamp_sell_qty` 防转空闸 |
| `overnight.py` / `momentum.py` / `options_overlay.py` | 隔夜 / 动量轮动 / 备兑开仓引擎 | 后两者仅 paper |
| `sqqq_short.py` | **SQQQ 短期看跌 QQQ 引擎** (`signal`) | **仅 paper**: `environment != paper_only` 即拒跑 (红线7 硬闸)。**信号源是 QQQ 不是 SQQQ** (第一版回测就错在这)。持有期 **1 个交易日无条件平**, 无止损无止盈。⚠️ 排在 **preclose** 而非 wrapup —— 口径是收盘买/次日收盘卖, 盘后下单会被排到次日开盘, 口径全错 |
| `learn.py` / `learn_overnight.py` | 参数学习器 (搜索/验证/晋级) | 红线8 边界内才可动 |

### 状态账本 (引擎回写, 会话不得手工改仓位数据)

| 文件 | 内容 |
|---|---|
| `state/positions.json` | **实盘**持仓与净值 (`strategy_positions` / `legacy_positions` / `trades` / `high_water_mark` / `halted`) |
| `state/pending_orders.json` | auto_buys 下是**执行记录** (`record_kind=execution_log`); 只有 4B-failsafe 才产出真·待执行清单 |
| `state/pending_option_orders.json` | 期权执行记录 (2026-10-12 起会话自动下单; 被拦才留待执行) |
| `state/preclose_status.json` | 收盘前阶段完成标记, wrapup 据此判是否 fail-safe |
| `state/learning.json` + `paper_positions.json` | 挑战者冠军/挑战者状态 + 纸面账本 |
| `state/weekly_call_*positions.json` | 周期权双轨账本 (`round_trips` / `skip_log` 是摩擦数据, 勿删) |
| `state/qqq_leap_positions.json` | **QQQ LEAP 轨道账本** (`positions` / `trades` / `round_trips` / `skip_log`); 当日信号产出在 `state/qqq_leap_last_orders.json` (`apply --context` 要用它) |
| `state/sqqq_short_positions.json` | **SQQQ 看跌轨道纸面账本** (与其他 paper 轨道同构, 回写复用 `signals.py apply`); `start_capital=10000` 刻意贴近实盘净值让数字可类比。⚠️ 回测是 100% 顺序满仓而本轨道 `position_pct=10`, **权益曲线要 ×10 才能与回测对比** |
| `state/momentum_positions.json` / `overnight_positions.json` / `stock_positions.json` | 各轨道独立账本 |
| `state/oneshot_tasks.json` | 一次性任务队列, `session.py brief` 会高亮 pending 项 |
| `journal/` | 每日运行日志 (git 记录, **即完整审计链**) |

### 已知未修的问题 (影响日常, 勿当成新发现)

1. **纸面 stale-fills —— 2026-10-19 实测复现, 原记载的后果与"安全"结论都偏轻, 已更正**:
   `wc.paper.run` 无单可下时不重写 `wc_fills.json` (`daily.py:519` 的
   `if o.get("buys") or o.get("sells")` 不成立 → run 整步跳过), `wc.paper.apply` 重放旧成交。
   **只在 preclose 阶段触发** (wrapup 不跑 `wc.*`)。
   - ⚠️ **后果不止"多一行重复流水"**: 当陈旧成交里含**已平合约的卖出**时,
     `apply` 直接 **rc=1 失败并产出 anomaly**, 一行都没写进去。2026-10-19 实测:
     重放 10-16 的卖 `NVDA261023C00200000`, 而该合约 10-16 已平进 round_trips
     → `卖出无持仓的合约` → 主跑多一条 anomaly。
   - ⚠️ **`daily.py:525` 的兜底无效**: 它是 `if not os.path.exists(fills)`,
     **只防文件缺失, 不防文件陈旧**。scratchpad 未被回收时文件就在, 兜底不触发。
     原文"已有 `{"fills": []}` 兜底, 安全"的说法**不成立**。
   - ✅ **账本不会被污染** (2026-10-19 核对: apply 失败前未写入, `trades` 无新增、
     `round_trips` 与 `positions` 均未变), 故不紧急; 但**每个 preclose 都会复现**。
   - 最小修法仍是 **run 前 `unlink`** (或把兜底改成"陈旧即重写"), 属改引擎, 待用户发话。
2. **`--plan-only` 并非零副作用 (2026-10-19 接线时两次实测确认, 确定性复现)**:
   `weekly_calls.py:_shadow_append` 的落盘路径**由账本路径派生**
   (`..._positions.json` → `..._model_edge.json`), 而账本路径在 `--plan-only` 下**不改写**,
   于是干跑也会往 `state/weekly_call_live_model_edge.json` 追加影子样本。
   其 docstring 写着「路径由账本路径派生, 故无需改 daily.py 或加 CLI 参数」—— 作者是有意派生的,
   但**没考虑 `--plan-only`**。影响: 纯追加的审计数据, **不碰任何账本、不参与任何判定**, 对交易无害;
   但「干跑不留痕」这个预期不成立, 干跑后 `git status` 会脏一个文件。
   修法: `_shadow_append` 增一个 dry-run 开关, 或由 daily.py 在 `--plan-only` 时把路径改到 workdir。**未修。**
3. **`signals.py` 部分卖出不更新 `cost`**: 实盘代码路径共用; `state/momentum_positions.json`
   的 QQQ 已失真 18.46×。
4. **财报黑窗 —— 2026-10-14 起已落地, 保留为纪律条目**: 根因是**取数漏做**而非代码缺陷
   (`daily.py` 拿到 `--earnings` 就会往下传, 401/479/508/566 行)。造成过实际损害:
   DAL 2026-10-08 在财报前 1 日入场、TSM 2026-10-09 在 6 日黑窗内入场。
   现行做法: 财报日缓存持久化在 **`state/earnings_cache.json`** (2026-10-14 建, 起因是 scratchpad
   被容器回收清空、两天累积的 17 只全丢), 每个主跑窗口 `--earnings state/earnings_cache.json`。
   ⚠️ **`next` 日期一过必须重取**, 否则 `days_to_earnings` 返回负数 → 闸门 fail-open 且不报错。
   已验证生效: 10-13 周call skip_log 出现 `TSM earnings_blackout(dte=2)`; 10-14 实盘出场链产出
   `TSM earnings_exit`。
   ✅ **2026-10-21 现状**: universe **100/100 有有效 next, 零 null 零缺失**, 147 个标的键。
   ⚠️ **「过期」= `next` < 运行日, 不是 `<=`** (2026-10-21 固定): `next == 今天` 时 dte=0 仍在黑窗内、
   本就该拦 (它今天发财报); 提前推进到下一季反而会**在财报当天放行**。10-15 的 BNY/TSM 与 10-20 的
   CB/GE 都属真过期 (次日起 dte 转负), 已各自当场重取。
   🔴 **第二种 fail-open: 个股 `next=null` (2026-10-21 查出当日修掉, 零实际损害但只差 4 个交易日)**:
   `signals.py` **分不出**「null = 本无财报 (ETF)」与「null = 窗口外未知 (个股)」—— 两者都让
   `days_to_earnings` 返回 `None` → 黑窗检查 `if ed is not None and ...` **整段跳过**; 且 **null 不带
   到期日**, 所以 `_refresh_policy` 的过期扫描**永远抓不到它**。
   - 怎么埋进去的: 10-19 周度刷新用 `get_earnings_calendar(days=14)` 给 19 只新进池标的取财报日,
     窗口外的 14 只记成 `next=null`, 并自认为「表示窗口外未知而非未取数」是安全的 —— **该判断是错的**。
     其中 **12 只在 100 股池内** (CAH DVN EOG MNST MRSH MTD OXY ROST SU SWKS TJX WAT), 对它们
     财报黑窗闸**整段失效**。
   - 📐 **窗口制从原理上就不够**: 黑窗须在财报**前 7 天**闭合, 该闭合点可能落在窗口**之内**, 故
     「窗口内无财报」**不蕴含**「黑窗期内无需闭合」。实测 11 只里 **8 只**的闭合点落在那个 14 日窗口内;
     真实财报日 SU/SWKS/WAT 11-03、CAH/DVN/EOG/MNST/MTD 11-05 → 最早出事日 **2026-10-27**。
   - ✅ 零实际损害 (10-19→10-21 两个交易日内 12 只均未进 candidates、未成交、不在持仓, 逐只核对过)。
   - 🔒 **已立的规矩**: 新进池标的一律**逐只 `get_earnings_results`** 取 next+past, **禁用
     `get_earnings_calendar`**; 写进 `strategy/screen.json _post_finalize_required`、
     `state/earnings_cache.json` 的 `_new_pool_entrant_rule` / `_null_next_incident` / 重写的
     `_refresh_policy`, 外加 oneshot `pool-entrant-earnings-2026-10` 让 `session.py brief` 每周一高亮。
   **覆盖率 ≠ 新鲜度, 新鲜度 ≠ 非空**: 每个主跑窗口按 `_refresh_policy` 扫三类 —— 缺失、过期([A])、
   **个股 null([B])**。
   ✅ **`past` 覆盖 (2026-10-21 用户授权补齐 17 只池内标的)**: `defense.earnings_gap_up_exempt`
   需要 `past` 才生效, 缺则**静默失效**(方向偏保守, 无风险敞口)。补完后实测过去一年 16 次 ≥8%
   上涨异动里 **10 次命中豁免** (BIIB/CAH×2/MNST/OXY/ROST×3/SBUX/WAT), 每次解锁约 20 个交易日
   → 约 **200 个「标的·日」/年** 的入场资格找回。仍空的 26 个 = ETF 22 (正常) + AMZN/AVGO/MSFT/TSLA
   (非池内, 期权轨道只用 next, 用户选择跳过)。
5. **同因子敞口无闸 —— 用户 2026-10-14 决定「不设」, 已结案**: `defense.max_per_sector=3`
   只数个股行业字段, 盖不住 ETF+个股混合 (AMAT 被数据源归在 Producer Manufacturing,
   SOXQ 是 ETF 不计数), 半导体实际敞口在 39~41% 徘徊而板块闸认为一切正常。
   **用户明确答复不加这道闸** (`config.json defense.same_factor_exposure_cap_pct = null`)。
   => **不再反复询问**; 战报里仍按报告义务**陈述**当日同因子敞口数字, 但不作为待用户动作项。
6. **挑战者 edge 不可用于晋级 —— 但原「口径污染」诊断 2026-10-14 已证明不准**:
   `learn.py` 的 evaluate 实际用的是**时间加权收益 (TWR)**, 对 live 与 paper **各自的真实权益曲线**
   逐段链接并剔除外部现金流 (`equity_history` + `live_flow`), 分母与各自的 sizing 基准**本来就一致**,
   不存在「按 3.8×账本 start_capital 算」那种分母错配。`paper_positions.json` 的
   `start_capital=10218.49` 确实与 Alpaca 真实权益 ($38.4k) 不符, 但 **evaluate 根本不读它**。
   真正残留的不可比是另一回事: ① **现金部署率不对称** —— 实盘受现金约束常整单跳过买单
   (2026-10-13 五张买单因 cap 只能成交一张, 闲置现金 19.5%), 纸面几乎满仓 (闲置 1.6%),
   闲置现金拖累实盘收益; ② **纸面是整股、实盘是零股美元额** (Alpaca 下整股会向下取整, 系统性少部署)。
   两条都使 edge 偏向"纸面更好", 方向与原诊断相反。**结论不变: edge 仍不能用于任何晋级判断 (红线8)**,
   但原因要按这里的说法理解。

7. **`daily.py` 曾静默截断引擎告警 —— 代码 2026-10-15 已修, 但历史报告受影响**:
   `daily.py:409` 原先 `warnings[:12]` / `candidates[:15]` 硬截断且**不留痕**, 而 `journal_facts`
   里根本没有 warnings 字段 —— 会话写战报/执行记录照 plan.json 写, 于是**凡当日告警 >12 条就漏报**。
   2026-10-15 实跑 17 条被丢 5 条 (SOXQ 加仓 + TT/JCI/FTNT/PANW 入场, 全是"有信号但资金不足"),
   candidates 实为 23 条。**订单从未受影响** (买卖读 `place_now`/`to_pending` = 引擎完整输出),
   受损的只有用户 2026-10-12 定的报告义务 —— 而取消下单前一票否决后, 事后点名是用户唯一的处置入口。
   现已放宽上限并输出 `warnings_total` / `candidates_total` / `*_truncated`, 截断不可能再隐身。
   ⚠️ **2026-10-15 之前的 journal/战报, 其"引擎跳过 N 条"可能偏少**, 审计时以当日
   `orders.json` (scratchpad, 可能已被容器回收) 或重跑为准, 不要把 plan.json 当完整记录。

8. **行业上限对「仍持仓但已掉出股票池」的个股失明 (2026-10-20 记录; 按 #5 口径已结案, 只陈述)**:
   `defense.max_per_sector=3` 的计数只认**当前 `universe.json` 的 sectors 映射** (`signals.py:132-133`
   并入, `:487-501` 计数, 查不到映射即当作无行业)。ETF 不计数是已知的; **新增认识是掉出池的在手个股
   也会变成同样的隐形状态** —— CDNS 10-19 掉出池 (未强制卖, 仍持净值 10.08%), 10-12 版映射为
   Technology Services 时**是**计入的 (10-16 的 DT 就因该行业满 3 被挡), 10-19 版无映射后贡献 0,
   10-20 的 NET (Technology Services) 因此畅通。与 #5 (行业字段盖不住 ETF+个股混合) 同方向、不同机制;
   用户 2026-10-14 已决定不加同因子闸 => **不再询问**, 战报里仅陈述数字。

### 已修复 / 已结案 (勿当成未决项)

- ✅ **QQQ LEAP 改为三档梯子 (2026-10-23 用户「以后qqq跌了 买三个期权」)**: 该指令有三种读法
  (3 张同一合约 / 3 个期限各 1 张 / 并持上限 3), 当日问清为**「3 个期限各 1 张」**。
  配置 `contract.ladder` = `[148, 85, 330]` DTE 各一档, 带宽 `[120,190] / [60,110] / [300,400]`
  (互不重叠, 191~299 的空档是故意的)。
  - 📐 **三张独立订单, 不是一张 3 contracts 的单** —— 这是本设计的要点: 红线9 的「整单跳过
    不缩量」按**单笔**判。独立单 → cap 不足时「能买几张买几张」; 合成一张 → 凑不出全额就
    **一张都不买**, 等于把原本成功的买入变成不买。当日现金仅 $293.53 / 净值 $9,816 而三档
    合计约 $5,240 = 53.4%, 这个问题不是理论上的。
  - 📐 **列表顺序 = 资金优先级, 由 config 声明不是引擎自排** (红线2)。148→85→330 的依据是
    10-22 的 `qqq-leap-dte-verdict.py` (148 是唯一在两种实测摩擦下都显著优于 330 的档) 与
    10-23 的 `qqq-leap-tp-85-148.py` (85 档止盈对下行的影响比 148 大 4.5 倍)。**用户可重排。**
  - ⚠️ **本模式下 `sizing.position_pct_of_portfolio` 不参与定量** (每档固定 1 张), 故每信号
    总规模从原先的「净值 20% 目标仓 → 常退化成 1 张」变为「三档单价之和 ≈ 净值 53%」。
    这是**用户知情选择的 sizing 放大**, 不是自学习 (红线8)。
  - 止盈仍是 +20% (10-23 扫描结论: 两档都维持, 往下调有显著证据反对、往上调是噪声形态)。
  - 引擎: `_pick` 加 `rung` 覆盖 target_dte/dte_band; `cmd_window` 的 `need_chain` 变成**数组**
    (每档一个区间, 三档报价落进同一个 chain 文件即可); `cmd_signal` 逐档循环并加
    `duplicate_occ_across_rungs` 守卫 (带宽重叠会让两档选到同一张, 而 apply 的同键合并会把
    「3 个期限各 1 张」静默变成「某期限 2 张」)。`ladder` 空/缺失则完整回落单档模式。
  - 验证 6 组全 PASS: window 出 3 档区间 / 现金充裕出 3 张 ($5,228 = 53.3% 净值, 点差
    148→0.94% · 85→1.10% · 330→7.26%) / **cash $2,000 → 只买 148 一张, 85 与 330 各自记
    insufficient_buying_power (证明不归零)** / cash $293.53 → 0 张 3 档各记原因 /
    去掉 ladder 回落单档 (`ladder:false`) / 带宽重叠时守卫触发。

- ✅ **QQQ LEAP 账本同 OCC 覆写 → 2026-10-22 当日查出当日修掉** (用户同日选方案 C「同键合并」):
  `qqq_leap.py` 的 `apply` 买入分支原先是 `positions[occ] = {...}` **直接赋值**, 既不合并 lot
  也无 collision 守卫。当日实盘第一次撞上同键 (`QQQ270917C00830000` 早 @24.74 + 收盘前 @24.39),
  照跑会把账本**从 2 张变 1 张**并丢掉 $2,474 那个成本基准。当场按红线6 停手未回写, 报告用户后按选择修复。
  - 现行语义: 同键再买 → `contracts` 相加 / `entry_cost_usd` 相加 / `entry_fill_price` 取**加权均价** /
    `take_profit_line` = 加权均价 × `take_profit_mult`; 逐笔留在仓位的 `_lots` 与 `trades[]`。
  - **`entry_date` 与 `entry_delta/dte/spread/spot/day_return` 一律不动** —— 前者使 20 交易日时间止损
    时钟**不因加仓重置** (`exit.holding_clock_resets=false`, 与 RSI-2 轨道同一纪律), 后者是开仓快照。
  - 顺带补了**部分卖出守卫**: 合并后仓位可 >1 张, 「卖的张数 ≠ 持有张数」第一次成为现实风险, 而原代码
    无条件 `pop` 会静默丢掉剩下的张数; 现在不相等即 `SystemExit`。
  - ⚠️ **这是语义改动不是纯 bug 修复**: 止盈基准由「单笔实际成交价」变为「加权均价」。用户知情选择。
  - ✅ 口径获独立验证: 合并算出的均价 24.565 与**券商自己的** `average_price 2456.50` 完全一致。
  - 验证: 5 组 (新键不碰撞 / 同键合并 / 整仓卖出 round_trip 用加权均价 / 部分卖出报错且账本不变 /
    2 张再并 2 张), 全 PASS; 回写后账本 4 张与券商逐张对上。

- ✅ **财报黑窗闸静默拦单 → 2026-10-20 当日查出当日修掉** (用户同日「两条一起补」授权):
  `signals.py` 的 `entry_signal_ok` 财报黑窗分支原先直接 `return False` 不记 warning, 于是
  `warnings_total=0` **并不等于**没有标的被拦。当日 META (rsi2 3.59, next=2026-10-21 dte=1)
  被正确拦下却零痕迹, 只能靠「candidates 有它 / buys 没它」反推才发现 —— 而 auto_buys 取消了
  下单前一票否决后, 事后点名是用户唯一的处置入口。
  - 现已在该分支加 `if not quiet: warnings.append(f"{sym}: 入场信号成立但 {ed} 天后财报, 防御性跳过入场")`,
    写法与加仓分支 (`触发加仓但 N 天后财报`) 对齐, `quiet` 守住出场侧冲突判定不被污染。
  - **验证 (同一输入复跑, 四组)**: ① 带 `--earnings` → 卖 DAL/STT、买 CSCO/NET $983.98×2
    **与当日实际成交逐字节一致**, warnings 新增且仅新增 META 那一行; ② 去掉 `--earnings` →
    买单仍是 CSCO/META/NET 三张 (不变); ③ `allow_unknown_earnings=false` 且无文件 → 买单空、
    告警仍只有原有那一行; ④ **把 `earnings_blackout_days` 调到 400 压力测 quiet 守卫** —— 三个
    原始信号标的各记一行、**零个持仓标的漏进 warnings**, 出场链不受影响。
  - ⚠️ **同时否掉了一个我自己提出的改动**: 原以为 `:258` 的「无 `--earnings` 且不允许未知」分支
    也是静默, 实际 `:142-146` 在进循环前已无条件通报一次 (「防御: 无财报日数据, 今日跳过全部新入场」),
    在分支里再记只会重复且按 universe 规模刷屏。该半已撤回, 并在原处留注释**禁止以后再往这里加**。
  - 沿革: 与 #7 (2026-10-15 修掉的 `daily.py` 硬截断) 同族 —— 都是"引擎拦了但报告里看不见",
    成因不同: #7 是截断, 本条是分支压根不记。两条都已闭合。

- ✅ **同日卖+买冲突 → 现行规则「有卖有买就不卖」** (2026-10-13 发现; 10-14 用户先选「出场优先」,
  同日改为**「有卖有买就不卖」**, 后者为现行):
  同一标的同日既触发出场、又满足入场信号 → **当日零动作**: 不卖, 也不加仓, 原仓位原样持有。
  - 判定口径: `signals.py` 的 **`entry_signal_ok(sym)`** = 入场信号 (close > SMA200 且
    rsi2 < rsi2_max) + **标的级**防御层 (财报黑窗 / 近20日异动 / 财报上涨跳空豁免),
    **不含**槽位、行业上限、现金可用性 —— 那些属分配阶段的"有没有位子", 不代表引擎不想要它。
    出场侧冲突判定与入场候选筛选**共用这一份函数**, 保证两处口径不漂移。
  - **连加仓一起跳过** (`conflict_held` 集合): 用户说的是「不卖」, 不是「买更多」。放任加仓会把
    一只刚跌破 −7% 止损线的票补到单票 20% 敞口, 且加仓把止损基准换成加权均价 (等效止损外移到
    ≈−8.4%), 风险叠加而非持平 —— 那是用户未批准过的组合效果 (红线3)。
  - 作用范围**仅策略仓**。存量仓 (legacy) 保护性止损/止盈/换仓照旧卖出 (它们承担给引擎供血的
    职责; 当前 `legacy_positions` 为空)。
  - ⚠️ **代价必须记住**: 这条规则会让 **−7% 止损在「超卖但仍在 SMA200 之上」的标的上失效** ——
    急跌砸出止损的票往往正好 rsi2 极低, 所以不是边角情形, 2026-10-14 一次就命中两只
    (AMAT −9.75% / SOXQ −4.96%, 两张 stop_loss 全被取消)。`time_stop` 同样会被抵消,
    理论上可出现"持仓天数到期却天天被取消"的长持。**用户已知并选择如此。**
  - ⚠️ 与纸面轨道**不再一致**: `paper.py:_wash_conflicts` 仍是「保留卖单跳过买单 (出场优先)」。
    两条轨道对同一冲突的处置相反, 比较挑战者 edge 时要记得这一项差异。

### journal 完成标记的硬约定 (2026-10-12 查出闸门假阴性后固定)

幂等闸正则 (`daily.py:276` / `session.py:139`) 是 `^\s*(status|preclose):\s*completed\s*$`,
**只认独立裸行**。

- **主跑 journal** (preclose / wrapup / full): 标记必须是**裸行** `status: completed`
  或 `preclose: completed`, **不加 `- ` 列表符号**。
- **侧记 journal** (morning / screen / 研究 / 测试): 一律写成 `- status: completed`,
  它的"匹配不上"**正是我们要的** —— 闸门扫当日全部 `journal/<date>*.md`, 侧记若被匹配上
  会把当晚主跑整天挡掉 (2026-09-03 假阳性事故)。
- ⚠️ 因此**绝不要把正则放宽成允许 bullet**。
- 沿革: 2026-09-21 → 10-09 我把主跑标记写成了 bullet, 闸门假阴性 16 个交易日 (未发生重复下单)。
