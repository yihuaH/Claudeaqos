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
   - **跨轨道资金优先级 = 期权优先 (常规规则, 2026-08-27 用户定, 此前三次逐次决定同向)**: 同日实盘股票买单与实盘周期权买单合计超过实时 `min(buying_power, cash)` 时, 先按期权单的**最大在险额** (credit_put_spread = 价差宽度×100×张数, 券商实扣) 留足抵押, 股票 cap = 可用资金 − 期权抵押, 超 cap 的股票单**整单跳过不缩量**; 期权单自 2026-10-12 起亦由会话自动下单 (playbook 4D, 仅单腿)。用户可单日一票推翻, 但**不再逐日询问**。
   - **实盘周call实验仓 (2026-08-04 用户授权)**: 期权买入 (buy_to_open) **自 2026-10-12 起由会话自动下单** (单腿限价; 用户当日应用 autoMode 改动后实测放行)。多腿仍被 Robinhood API 400 拒, 故形态必须 `single`。⚠️ **用户因此失去了每张期权的事前一票否决**, 而单张权利金上限仍是 null —— 该数字优先级显著上升 (红线3) (时效**照抄 plan.json 的 `option_pending_template`**, 两形态: **收盘前产出 = 当日 15:30–15:45 ET** [2026-09-11 用户「期权也可以当日马上执行」; 比股票的 15:55 早 10 分钟收口, 因 15:45 后是 4D 明令避开的收盘前极端点差区]; 收盘后产出 = 次一交易日 10:30 ET, 推荐 09:45–10:30 等开盘点差收窄, 2026-08-04 用户批准); 期权出场卖单 (sell_to_close) 属出场类, 照常全自动。**规模约束**: `contract.max_premium_per_contract_usd` = **$5,000** (用户 2026-10-12 答复「5000」定, 与 paper 轨道同值; 起因是换 single 后长 call 可亏 100% 权利金而开仓不再经用户事前过目)。`budget.max_open_premium_pct_of_portfolio` 仍为 null (总在险额不限, 2026-08-18 用户「不设上限」)。并行生效: 实时 `min(buying_power, cash)` 防杠杆闸 (红线3) + 熔断 (累计已实现亏 ≥ 净值×50%) + 点差/DTE/公允价边际闸。**实测该闸会真拦住贵档** (深ITM call 每张 $2,727~$8,122, SMH/TSM 已被 `premium_too_large` 拦下)。改这个数只能由用户定。

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
| 挑战者影子验证 | paper | ⚠️ validating 但**指标不可用** (见下) | `learning.json enabled` |
| 周call 摩擦实测 (深ITM 买call) | paper | ✅ active | `weekly_calls.json enabled` |
| 周度动量轮动 | paper | ✅ active | `momentum.json enabled` |
| 备兑开仓 overlay | paper | 视开关 | `options.json enabled` |
| 隔夜参数学习 A/B | paper | ⏸ 暂停 (2026-07-21) | `learning_overnight.json paused` |
| 个股防御实验 | paper | ⏸ 暂停 (2026-07-21, 已并入实盘) | `stocks.json enabled` |

### 会改变当日操作的告警 (必读)

- 🛟 **双段跑 fail-safe**: 当日无 `state/preclose_status.json` 的 completed 标记 → 17:45 wrapup
  **自动退化为完整主跑** (出场照下, 买单按 fail-safe 形态)。故收盘前挂掉最坏只退回改动前行为。
- 🔴 **期权实盘仓**: 2026-10-12 换 `single` (深ITM 买 call, moneyness 0.90), **同日起开仓/平仓均由会话
  自动下单** (单腿限价; 用户应用 autoMode 改动后实测放行)。多腿仍被 Robinhood API 400 拒 →
  形态必须是 `single`。**长 put 做不到** (引擎只认 call 且信号单向看多)。
  **单张权利金上限 $5,000** (用户 2026-10-12 定); budget 总额仍 null; 并行生效实时
  `min(buying_power, cash)` + 熔断。长 call 可亏 100% 权利金, 故该上限是唯一的单笔规模闸。
  当前账户规模下该形态**近乎零出单** (深ITM call 每张数千美元 vs 千元级现金)。
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
| `strategy/learning.json` / `learning_overnight.json` | 自学习边界、搜索网格、晋级标准 |
| `strategy/momentum.json` / `options.json` / `stocks.json` / `overnight.json` | 各实验轨道参数 |
| `strategy/tracks.md` | 驱动器已接管轨道的**手工回退**完整命令 (平时不用) |
| `strategy/routines.md` | Routine 唤醒词原文与注意点 |
| `strategy/archive/` | 已停用轨道 / 已退役协议 / 轨道沿革 |

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
| `paper.py` | Alpaca 纸面执行器 (**仅 paper 环境**, 红线7) | 下单被拒逐单捕获**绝不中断整批**; 排队清单 `try/finally` 无论如何落盘; 同日卖+买冲突**保留卖单**; `_clamp_sell_qty` 防转空闸 |
| `overnight.py` / `momentum.py` / `options_overlay.py` | 隔夜 / 动量轮动 / 备兑开仓引擎 | 后两者仅 paper |
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
| `state/momentum_positions.json` / `overnight_positions.json` / `stock_positions.json` | 各轨道独立账本 |
| `state/oneshot_tasks.json` | 一次性任务队列, `session.py brief` 会高亮 pending 项 |
| `journal/` | 每日运行日志 (git 记录, **即完整审计链**) |

### 已知未修的问题 (影响日常, 勿当成新发现)

1. **纸面 stale-fills**: `wc.paper.run` 无单可下时不重写 `wc_fills.json`, `wc.paper.apply`
   会重放旧成交。**只在 preclose 阶段触发** (wrapup 不跑 `wc.*`); 后果仅是
   `trades[]` 多一行重复流水 (`positions[occ]=rec` 是幂等的), 不影响仓位与 round_trips。
   最小修法: run 前 `unlink` —— `daily.py:495` 已有 `{"fills": []}` 兜底, **安全**。
2. **`signals.py` 部分卖出不更新 `cost`**: 实盘代码路径共用; `state/momentum_positions.json`
   的 QQQ 已失真 18.46×。
3. **财报黑窗 —— 2026-10-14 起已落地, 保留为纪律条目**: 根因是**取数漏做**而非代码缺陷
   (`daily.py` 拿到 `--earnings` 就会往下传, 401/479/508/566 行)。造成过实际损害:
   DAL 2026-10-08 在财报前 1 日入场、TSM 2026-10-09 在 6 日黑窗内入场。
   现行做法: 财报日缓存持久化在 **`state/earnings_cache.json`** (2026-10-14 建, 起因是 scratchpad
   被容器回收清空、两天累积的 17 只全丢), 每个主跑窗口 `--earnings state/earnings_cache.json`。
   ⚠️ **`next` 日期一过必须重取**, 否则 `days_to_earnings` 返回负数 → 闸门 fail-open 且不报错。
   已验证生效: 10-13 周call skip_log 出现 `TSM earnings_blackout(dte=2)`; 10-14 实盘出场链产出
   `TSM earnings_exit`。
4. **同因子敞口无闸 —— 用户 2026-10-14 决定「不设」, 已结案**: `defense.max_per_sector=3`
   只数个股行业字段, 盖不住 ETF+个股混合 (AMAT 被数据源归在 Producer Manufacturing,
   SOXQ 是 ETF 不计数), 半导体实际敞口在 39~41% 徘徊而板块闸认为一切正常。
   **用户明确答复不加这道闸** (`config.json defense.same_factor_exposure_cap_pct = null`)。
   => **不再反复询问**; 战报里仍按报告义务**陈述**当日同因子敞口数字, 但不作为待用户动作项。
5. **挑战者 edge 不可用于晋级 —— 但原「口径污染」诊断 2026-10-14 已证明不准**:
   `learn.py` 的 evaluate 实际用的是**时间加权收益 (TWR)**, 对 live 与 paper **各自的真实权益曲线**
   逐段链接并剔除外部现金流 (`equity_history` + `live_flow`), 分母与各自的 sizing 基准**本来就一致**,
   不存在「按 3.8×账本 start_capital 算」那种分母错配。`paper_positions.json` 的
   `start_capital=10218.49` 确实与 Alpaca 真实权益 ($38.4k) 不符, 但 **evaluate 根本不读它**。
   真正残留的不可比是另一回事: ① **现金部署率不对称** —— 实盘受现金约束常整单跳过买单
   (2026-10-13 五张买单因 cap 只能成交一张, 闲置现金 19.5%), 纸面几乎满仓 (闲置 1.6%),
   闲置现金拖累实盘收益; ② **纸面是整股、实盘是零股美元额** (Alpaca 下整股会向下取整, 系统性少部署)。
   两条都使 edge 偏向"纸面更好", 方向与原诊断相反。**结论不变: edge 仍不能用于任何晋级判断 (红线8)**,
   但原因要按这里的说法理解。

### 已修复 / 已结案 (勿当成未决项)

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
