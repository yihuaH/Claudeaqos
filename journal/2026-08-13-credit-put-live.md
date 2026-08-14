# 2026-08-13 · 实盘期权轨道换形态: credit_put_spread 直接部署 (用户「直接实盘」)

授权链: 用户看完 `journal/2026-08-13-pcs-bt.md` 对比回测 (卖方 6/6 窗口胜买方、回撤减半、
全 8 假设格正收益) 后明确指示**「直接实盘」** — 沿 2026-08-07 vertical_spread「直接部署到实盘」
同款先例跳过 paper 验证。执行者已当面陈述五条模型局限, 用户仍选择直上 (риск自担语境同
08-04「接受全赔」)。原 vertical_spread 轨道 08-07→08-13 共 0 成交 / 1 skip, 无持仓, 切换无遗留。

## 变更清单 (全部本次 commit)

| 文件 | 变更 |
|---|---|
| `scripts/integrations.py` | chains 拉取 call+put 双类型 (原硬编码 type=call) |
| `scripts/weekly_calls.py` | 新增 `bs_put` / `_pick_credit_put` / 三处形态分派 (入场·出场·near_signals 保留额) / apply 回写 (卖腿主键·风险归一盈亏·long_occ) / report 盯市 / 敞口按在险额计 |
| `strategy/weekly_calls_live.json` | contract → credit_put_spread (卖 ≤0.97P 最高档 / 保护 ≤0.88P 最高档); 点差**双档闸** pct 8/16% **或** abs $0.10/股/腿 (绝对档 = 回测盈亏假设口径); validation 重置 start 08-14, 点差 bar 2.25→8.0 重校; 五条风险注记 |
| `strategy/playbook.md` §4D | credit 形态执行差异: 开仓 credit 组合单 semi_auto (限价下限 est×0.97, 收钱方向保护反转)、平仓 debit 全自动 (est×1.03 封顶)、回写主键=卖腿 OCC |
| `CLAUDE.md` | 轨道表与脚本说明同步 |

## 测试

- 合成全链路: 入场选腿 (卖174P/买158P, 贷记1.47, 风险$1,453, D20=1张) → apply 开仓 →
  spot −5.6% 触发 underlying_stop → 平仓单 (direction=debit, net=卖腿ask−保护bid) →
  apply 平仓 (pnl_usd=−305=贷记145−买回450 ✓, pnl_pct=−20.99 按风险归一 ✓) → report 摩擦统计全通
- 真实链冒烟: Alpaca `type=put` 可用 (XLI 174 张 put); 深夜 indicative 报价点差 $0.22 被
  绝对档闸正确拦下 (skip) — 收盘后主跑时段报价新鲜度待首个信号日实测
- 纸面 call 轨道回归: 无 structure 配置走原 single 路径, 不受影响 (paper 继续攒 call 点差数据,
  与新实盘形态分叉属已接受现状, 月度复核可再议)

## 首批实盘观察项 (记 journal, 任一异常按红线 6 停轨道)

1. **平仓分类器兼容性 (最高优先)**: credit 形态平仓 = 无人值守 buy_to_close 组合单 —
   若被平台当「买入」拦截: 不重试, journal + PushNotification 用户人工触发。
2. 信号日真实贷记 vs 模型 (put skew 应使实收 > 模型; report 的 `median_mid_vs_model_pct` 追踪)
3. 双档点差闸的实际通过率 (skip_log; 全 skip = 闸需重校, 月度复核处理)
4. 短腿跌进实值后的行权/指派事件 (force_exit_dte_lte=2 + −5% 止损两道护栏)
5. 组合单成交质量 (限价 est×0.97 下限的成交率)

## 未动

- 预算/注码百分比 (40%/20%) — 用户上一轮已确认不改 (reserve-bt 结论)
- paper 摩擦轨道 (仍 call 单腿)、隔夜/动量/挑战者各轨道、股票主策略

---

## 补充 (2026-08-14 ~04:10 ET): 用户「都改到50%和5000」— 三上限上调, DIA 解锁

背景: 凌晨 plan-only 预览显示 DIA 预警一组在险额 $4,687, 被三道上限拦住 (单张 $3,000 /
贵档 40% 净值 / 预算 40% 净值)。执行者陈述集中度风险 (一组 DIA ≈ 半个净值) 与 XLI 替代论后,
用户仍明确指示上调 (红线3: 上限归用户)。

| 参数 | 旧 | 新 |
|---|---|---|
| `contract.max_premium_per_contract_usd` (单张风险硬顶) | 3000 | **5000** |
| `sizing.max_single_position_pct_of_portfolio` (贵档单仓, 新增配置项; 原引擎硬编码 2×注码=40%) | 40% | **50%** |
| `budget.max_open_premium_pct_of_portfolio` (轨道总在险额) | 40% | **50%** |
| `circuit_breaker.max_cumulative_realized_loss_pct_of_portfolio` (熔断, 与 budget 同步) | 40% | **50%** |

- 引擎改动: 贵档 1 张例外从硬编码 `2×target` 参数化为 `max_single_position_pct_of_portfolio`
  (缺省回落原行为, paper 配置不受影响)。边界测试: 风险 $1,453 在 pv=2900 (50%=1450) 正确 skip、
  pv=2910 (50%=1455) 正确出 1 张。
- 当前净值 $10,154 沙盘: DIA 一组 $4,687 ≤ 贵档 $5,077 ✓ ≤ 单张 $5,000 ✓ ≤ 预算 $5,077 ✓ → 可出 1 张。
- ⚠️ 风险注记: 贵档一张 = 半个净值押单笔; 预算+熔断同步 50% = 「接受全赔」桶扩大到半仓。
  均为用户知情决定, 已写入各配置 note。
