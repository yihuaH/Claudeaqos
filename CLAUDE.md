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
9. 半自动买入 (execution.mode=semi_auto, 用户 2026-07-20 设立, 取代原 confirm 闸门): 实盘新买入与配套换仓/加速清理卖单 (funding_rotation / accelerated_liquidation) 在无人值守会话**可 review、不得 place** (平台分类器只拦截无人值守 place, review 不受限; 不要反复尝试下单) — 只能由主流程写入 `state/pending_orders.json` (逐字段来自引擎输出), 待用户在有人值守会话明确说"执行"后按 playbook 4C 原样执行 (当日窗口市价; 盘外买单混合执行=整股即时限价①腿 + 余量分数市价排次开②腿, 2026-07-27 用户改进; 有效至次一交易日 09:25 ET; 隔夜轨道买单仅当日)。出场/止损/兜底卖出与纸面轨道不受限, 照常全自动 (可直接 place)。
   - **结算与杠杆规则 (2026-08-07 用户升级 limited margin 后重写; 原 07-31「现金账户 T+1 铁律」作废)**: 账户 802095265 现为 **`type=margin` (limited margin)** — 卖出款**即时可用**、`buying_power` 已含未结算款、**GFV (善意违规) 风险消失**。⚠️ **防杠杆闸**: 该账户目前**无借贷额度** (`unleveraged_buying_power == buying_power`); 4C/4D 执行买单**一律以实时 `min(buying_power, cash)` 为上限**, 超出**整单跳过不缩量** — 若某日 `buying_power > cash` 说明券商开放了借贷, 系统**绝不自动使用借来的钱**, 动用杠杆须用户明确授权 (红线3)。
   - **新闻旗标 + 宏观环境 (报告级, 2026-07-31 用户加)**: `integrations.py news` 对买单标的做确定性红旗分类、`macro` 的 FRED `context` 段, **均仅提示/展示, 绝不改引擎选股或金额** (红线2); 红旗只在 pending/战报点名, 由用户 4C 一票否决。
   - **实盘周call实验仓 (2026-08-04 用户授权)**: 期权买入 (buy_to_open) 同受 semi_auto 约束 — 无人值守只写 `state/pending_option_orders.json`, 用户「执行」后按 playbook 4D 下限价单 (有效至次一交易日 10:30 ET, 推荐执行窗 09:45–10:30 ET 等开盘点差收窄, 2026-08-04 用户批准); 期权出场卖单 (sell_to_close) 属出场类, 照常全自动。预算硬顶 = **账户净值 × 40%** (`weekly_calls_live.json budget`, 用户 2026-08-04 定百分比制, 随净值自动伸缩) 与实时 buying_power 双封顶 (红线3), 百分比只能由用户改。

## 分支约定 (系统级, 优先于 Routine 唤醒词)

**当前交易分支 = `Main`** (2026-07-22 仓库重组归一, 默认分支)。任何会话:
- 取数/回写/提交一律基于 `origin/Main` (`git fetch origin Main`); 若 Main 受保护不能直接 push,
  则 push 到工作分支再开 PR 合并回 Main。
- Routine 唤醒词中若仍写死已废弃分支名 (`claude/new-session-ty4g79` 等), **一律改用 Main**。
  过渡期 `claude/new-session-ty4g79` 保留为 Main 的镜像 (内容对齐), 待主流程 Routine 经界面重建指向
  Main 后即可删除。

## 轨道状态总览

(暂停/启用以各 config 的开关为准; 本表为速览, 恢复时同步更新)

| 轨道 | 环境 | 状态 | 开关 |
|---|---|---|---|
| RSI-2 均值回归 (主策略, ETF+个股) — **每日收盘后单跑** (~17:45 ET, 全异步) | 实盘 | ✅ active (2026-08-07 参数首扫: 止损 5→7%, 其余四项已在最优位; 同日启用**加仓机制** 再跌3%补一档·每票≤2档) | `config.json enabled` + `config.json scale_in.enabled` |
| └ (已退役) 15:30 盘前主跑 | 实盘 | ⛔ 停用 (2026-07-24, 平台窗口内频繁挂起, 改收盘后单跑) | Routine disabled |
| 隔夜均值回归 — 入场 | 实盘 | ⏸ 暂停 (2026-07-21) | `overnight.json live_entries_paused` |
| 隔夜均值回归 — 出场/兜底 | 实盘 | ✅ active (照常) | 同上 (暂停只停入场) |
| 挑战者影子验证 | paper | 视 `learning.json` 有无 validating 挑战者 | `learning.json enabled` |
| 隔夜参数学习 A/B | paper | ⏸ 暂停 (2026-07-21) | `learning_overnight.json paused` |
| 个股防御实验 | paper | ⏸ 暂停 (2026-07-21, 已并入实盘) | `stocks.json enabled` |
| 备兑开仓 overlay | paper | 视 `options.json enabled` | `options.json enabled` |
| 周度动量轮动 | paper | ✅ active | `momentum.json enabled` |
| 周call 摩擦实测 (RSI-2×深ITM 买call) | paper | ✅ active (2026-08-04 起, 验证期) | `weekly_calls.json enabled` |
| 周call 实盘实验仓 (同形态, semi_auto 买入) | 实盘 | ✅ active·机会主义 (2026-08-04 用户定调「有合适就买, 没有就不买」; budget=净值×40% 自动伸缩, 注码=净值×20%/信号; 白名单18只·股票类专属 (2026-08-05 剔 GLD/TLT), 可负担档=XLF/XLE/BAC) | `weekly_calls_live.json enabled` + `budget` |

## 结构

- `strategy/config.json` — 策略与风控参数 (用户可改)
- `strategy/playbook.md` — **每日必读的执行契约** (前置检查/§4 下单协议 4A-4D/回写/次日预览/异常原则; 2026-08-06 拆分后 258 行)
- `strategy/tracks.md` — 驱动器已接管的轨道规范与**手工回退**步骤 (取数/信号完整命令、挑战者、动量、周call双轨、期权池月度复核、参数搜索)
- `strategy/archive/paused-tracks.md` — 已停用轨道存档 (隔夜实盘、隔夜A/B学习、个股防御; 恢复时移回 tracks.md)
- `strategy/learning.json` — 自学习策略: 可学参数边界、搜索网格、晋级标准 (用户可改)
- `strategy/options.json` — 备兑开仓实验参数 (仅 paper, 用户可改)
- `strategy/momentum.json` — 周度动量轮动实验参数: universe、混合动量回看期、调仓节奏 (仅 paper, 用户可改)
- `strategy/stocks.json` — 个股防御实验参数 (仅 paper; **实验暂停中** 2026-07-21, 个股已并入实盘主策略, 防御层参数由 config.json defense 段沿用)
- `strategy/screen.json` — 个股池周度筛选标准 (用户可改); `strategy/universe.json` — 筛选产出的当前 100 股池 (screen.py 回写)
- `scripts/daily.py` — **每日主跑驱动器** (2026-08-06 用户「建」): 一条命令跑完 playbook 中所有可脚本化步骤 (取数→RSI-2信号→期权双轨→纸面轨道), 产出 `plan.json` (place_now 待会话下单 / to_pending 待用户执行 / journal_facts / command_log 审计); **只调用各引擎绝不含决策逻辑** (红线2); `--plan-only` 干预览不写账本。会话仍负责 MCP 取数、下单、写 journal
- `scripts/screen.py` — 个股池确定性筛选器 (pool / rank / finalize); 筛选只决定"能买什么", 买卖时机仍由引擎决定
- `scripts/signals.py` — 确定性信号引擎 (signal / apply); 含**加仓机制** (`config.json scale_in`, 2026-08-07 用户「做加仓」启用): 已持策略仓收盘 ≤ 加权均价×(1−3%) 且未触发出场 → 补一档 (净值×10%), 每票最多 2 档 = 单票敞口上限 20%; 加仓单 `reason=rsi2_scale_in`, 同受 semi_auto (红线9)/VIX/财报黑窗约束, 排在新开仓单前吃现金 (回测口径, 执行时不得重排)
- `scripts/overnight.py` — 隔夜均值回归引擎 (IBS 收盘买/次日收盘卖); **实盘入场暂停中** (live_entries_paused, 2026-07-21 用户指示, 出场/兜底与纸面学习照常); `strategy/overnight.json` 参数; `state/overnight_positions.json` 账本
- `scripts/integrations.py` — 外部数据源 (Alpaca paper / FRED) 自诊断、宏观数据、历史日线; `macro` 含 FRED 报告级 `context` (收益率曲线/信用利差, 仅展示不门控); `news` 确定性新闻红旗分类 (报告级, 仅提示不改单, 2026-07-31 用户加)。**股票行情口径 = SIP 合并行情** (2026-08-08 用户「采用 robinhood 的 sip」, 由 iex 改; 实测 Robinhood 官方收盘 `source=sip-list-exchange-close`, SIP 12/12 完全一致 vs IEX 2/12): 历史日线用 `EQUITY_FEED=sip`, 实时端点用 `EQUITY_RT_FEED=delayed_sip` (实时 SIP 未订阅, 延迟 15 分钟对收盘后主跑无影响; **引入盘中决策须重评**); `quotes` 取 snapshot `dailyBar.close` 而非 trades/latest (后者收盘后返回盘后成交价, 属既有 bug)。⚠️ **期权链仍是 `feed=indicative`** (OPRA 需签协议, 实测 403) — `weekly_calls` 点差闸读的不是交易所真实 NBBO, 首笔实盘成交后需回校
- `scripts/learn.py` — 参数学习器 (walk-forward 搜索 / 验证评估 / 晋级)
- `scripts/learn_overnight.py` — 隔夜策略学习器 (双纸面账本 A/B; **暂停中** 2026-07-21, 随隔夜实盘暂停冻结); `strategy/learning_overnight.json` 边界; `state/learning_overnight.json` + 两本 paper_overnight_* 账本
- `scripts/paper.py` — Alpaca 纸面账户执行器 (挑战者影子交易, 仅 paper 环境); `run --allow-queue --queued-out` 收盘后用 limit/day 挂至次开 (解收盘后主跑 paper 轨道拒单), `sync --queued --prune` 次日回收成交; 排队清单 `state/paper_queued_*.json`
- `scripts/options_overlay.py` — 备兑开仓确定性引擎 (signal / apply, 仅 paper)
- `scripts/momentum.py` — 周度动量轮动确定性引擎 (signal, 仅 paper); `state/momentum_positions.json` 账本
- `scripts/weekly_calls.py` — 周call 确定性引擎 (signal / apply / report; 2026-08-04 用户授权), RSI-2 同形信号 × 深ITM(0.90) 买call × 无期权止损 × 跟正股出场, 双轨共用:
  - paper 摩擦实测: `strategy/weekly_calls.json` + `state/weekly_call_positions.json` 账本 (round_trips/skip_log 摩擦数据) + `state/weekly_call_last_orders.json` (次日跨会话回收 context); 实测真实点差 vs 回测模型
  - 实盘实验仓: `strategy/weekly_calls_live.json` (budget+实时BP 双硬顶) + `state/weekly_call_live_positions.json` 账本 + `state/weekly_call_live_last_orders.json`; 买入 semi_auto 走 `state/pending_option_orders.json` (playbook 4D), 出场卖单全自动
- `state/pending_orders.json` — semi_auto 待执行清单 (主流程按需生成, 逐字段来自引擎; 用户回复「执行」后按 playbook 4C 消费; 当日无买单则不生成)
- `state/positions.json` — 实盘持仓与净值状态 (引擎回写)
- `state/learning.json` — 学习状态 (冠军/挑战者、净值曲线、晋级历史)
- `state/paper_positions.json` — 挑战者纸面账本 (与实盘 state 同构)
- `state/stock_positions.json` — 个股防御实验纸面账本 (独立于挑战者)
- `journal/` — 每日运行日志 (git 记录, 可审计)
