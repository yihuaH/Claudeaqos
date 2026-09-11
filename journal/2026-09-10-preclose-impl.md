# 实现: 拆分式盘前主跑 (用户 2026-09-10「直接实现」批准)

**授权**: 用户在看过三段量化 (`journal/2026-09-10-preclose-research.md`, 合计 +0.42 pp/笔
≈ 相对 +26~31%) 与运维风险陈述后回「直接实现」, 跳过 5 日影子验证。

**风险处置**: 用户选择跳过影子验证 = 接受「关键路径耗时与挂起率未实测」。
执行者据此把**fail-safe 做成硬约束**而非可选项 (见 §3) —— 盘前跑挂掉最坏退回改动前的行为,
绝不会出现「当日既没买也没出场」。这是本次实现里唯一超出用户明示范围的设计决定, 特此记录。

---

## 1. 关键发现: 引擎本来就支持盘前, 一行都不用改

`signals.py:183-189` **刻意排除当天日线**, 当日价一律从 `--quotes` 取:

```python
dates  = [d for d, _ in bars if d < today]
closes = [c for d, c in bars if d < today]
if sym in quotes:
    dates.append(today); closes.append(quotes[sym])
```

这套架构就是为 2026-07-24 退役的那个盘前主跑写的。**本次实现没碰任何引擎** (红线2),
全部改动在驱动器 (`daily.py`) 与调度器 (`session.py`) 两层。

## 2. 报价口径 —— 原以为是拦路石, 实际已有通路

盘前用 Alpaca 会拿到延迟 15 分钟的价 (`EQUITY_RT_FEED=delayed_sip`, CLAUDE.md 早已警示
"引入盘中决策须重评")。而 `signals.parse_quotes` 本就接受
**`mcp__cash_printer__get_equity_quotes` 原始输出**, 读 `quote.last_trade_price` ——
**券商实时价, 无延迟**。于是:

- `daily.py` 新增 `--quotes`: 给了就**完全取代** `integrations.py quotes`, **绝不混用**
  (混用会悄悄掺进延迟价)。归一化 (`normalize_quotes`, 口径与 `signals.parse_quotes` 一致)
  后写 `<workdir>/quotes.json`, 所以 momentum / options_overlay / price_check
  这些只认简单映射的下游**一行都不用改**。
- **两道硬闸** (都是 fatal, 不是 warn):
  ① 盘前阶段未传 `--quotes` → 拒跑; ② 报价覆盖率 <90% → 拒跑
  (缺的标的会用**昨日**收盘算信号, 大面积失真等于信号全错)。
- `daily.py --emit-symbols PATH`: 纯读本地文件、无网络、秒出驱动器需要报价的全部标的。
  **实测 129 只** (etf 8 / stock_pool 100 / live 7 / paper 11 / momentum 19 / option 23, 去重后),
  比直觉的「ETF+股池=108」多一截 —— 少取就会撞覆盖率闸。盘前流程第一步就是拿这份清单。

## 3. fail-safe: 盘前挂了怎么办

- 盘前跑成功 → 落 `state/preclose_status.json` (`status: completed`)。
- 17:45 的 `--phase wrapup` **读它决定自己干什么**:
  - 当日有 completed 标记 → 只跑纸面轨道 + 行情核对, **绝不重算正股/期权信号**
    (账本已被盘前成交更新, 重算会看到新持仓而重复出单);
  - **没有标记 → 自动退化为完整主跑** (`effective_phase=full`): 出场卖单照下、
    pending 按次日 09:45–15:55 窗口。**即改动前的行为。**
- 契约明写: **会话不得手动改 `--phase full` 绕过这个判定。**

## 4. 顺带修掉一个更严重的幂等缺陷 (本次新发现)

今天下午我修的是幂等**假阳性** (09-03: 整文件子串匹配)。今晚发现还有个**假阴性**, 更严重:

- 原实现只查 `journal/<date>.md`。但当日**哪个窗口先跑就先占这个文件名**,
  后到的窗口写 `-main.md` 等后缀文件。
- 实证: 09-02~09-09 主跑标记都在 `<date>.md`; **而 09-10 主跑的标记在 `-main.md` 里**
  (晨检先占了 `<date>.md`) —— 闸门**完全看不到**。
- 即 **主跑幂等实际一直是失效的**: Routine 若重复触发, 当日主跑会跑第二遍
  (重复出场卖单、重复 pending)。此前没出事只是因为没重复触发过。

**改法**: 扫当日**全部** `journal/<date>*.md`, 且**按阶段各自的标记**判定 ——
否则盘前写完 `status: completed`, 同日 17:45 的 wrapup 会被自己挡住:

| 阶段 | 认的标记 (须独立成行) |
|---|---|
| `--phase preclose` | `preclose: completed` |
| `--phase wrapup` / `full` | `status: completed` |
| (晨检窗口) | `morning_check: completed` |

实测: 今日 `--phase wrapup` 现在**确实被挡住**了 (标记在 `2026-09-10-main.md`),
而修复前它会静默重跑一遍。

## 5. 协议常量搬进驱动器 (治今晚那次 09:25 误用)

今晚主跑 (commit 79a49ad) 把清单时效写成已退役的 09:25 —— 根因是会话 fetch 到新代码但
**沿用自己对话记忆里的旧口径**。散文契约靠会话自觉, 机器可抄的字段才靠得住。

`daily.py` 新增 `PENDING_TEMPLATES` / `pending_template(eff_phase)`, 把
`valid_until` / `order_valid_until` / `exec_window_et` / `exec_day` / `funding_note`
按 `effective_phase` 产出到 `plan.json` 的 `to_pending.pending_template`,
playbook 4B 明写**逐字段照抄, 不得凭记忆组装**。
正常 wrapup 返回 `applicable: false` (本阶段不产出 pending) —— **不能回落到次日模板**,
否则会话会误以为要再写一份, 那就是同型事故。

## 6. 验证 (全部实跑, 非推演)

| 用例 | 预期 | 结果 |
|---|---|---|
| `--phase preclose` 干跑 (09-10 口径) | 复现当晚真跑信号 | ✅ DGX $696.64 rsi2_scale_in, 0 anomalies |
| `--phase wrapup` + 当日 marker | 跳过信号, 只跑纸面/核对 | ✅ effective=wrapup, 0 买 0 卖 |
| `--phase wrapup` 无 marker | fail-safe 退化 full | ✅ effective=full, 次日模板, 记 anomaly |
| 盘前未传 `--quotes` | fatal 拒跑 | ✅ |
| 报价覆盖 60/129 | fatal 拒跑 | ✅ |
| 报价覆盖 129/129 端到端 | 通过, 用券商价 | ✅ 0 anomalies, quotes_source=override |
| `--emit-symbols` | 秒出 129 只 | ✅ 分组明细正确 |
| 分阶段幂等 | wrapup 挡 / preclose 放行 | ✅ 两者都对 |
| price_check 在盘前 | 跳过 (无官方收盘) | ✅ |
| `pending_template` 三形态 | same_day / 不适用 / next_session | ✅ |
| `exec_window()` 边界 9 例 | 不变 | ✅ (09:45–15:55 envelope 覆盖两种形态) |
| `session.py brief` 四窗口 + `--json` | 正常 | ✅ |

## 7. 上线状态 —— 代码已就绪, 盘前段尚未启动

⚠️ **合并本 PR 不会改变任何行为**, 因为:
- 盘前段要靠一条**新的 15:20 ET Routine** 才会跑, 而经 API 建的触发器**带不上 Robinhood 连接器**
  (既有平台限制), 需**用户在 claude.ai Routines 界面手工创建并挂 Robinhood 工具**。
  唤醒词已写好, 见 `strategy/routines.md` ④。
- 在那之前, 17:45 的 `--phase wrapup` 每天都会 fail-safe 退化为完整主跑 = **与今天完全相同的行为**。

也就是说: 合并是零风险的, 启用与否完全由用户创建那条 Routine 的动作决定。

## 8. 观察项 (盘前首日必记)

- [ ] **关键路径真实耗时** —— 从会话唤醒到 pending 落盘用了几分钟 (估 3–5, 未实测)。
      这是当初退役的真正原因, 也是用户跳过影子验证后唯一没有数据的一项
- [ ] 129 只 `get_equity_quotes` 分批调用的耗时与缺口率
- [ ] 盘前出场卖单是否即时成交 (盘中市价, 应秒成) 与其成交价 vs 引擎 est
- [ ] 当日 pending 在 15:30–15:55 窗口的实际成交价 vs 引擎 est
      —— 验证研究预测的 **−0.054%** (对照隔夜方案的 +0.350%)
- [ ] 期权轨道仍在 17:45 出单、次日 10:30 执行窗: 其最大在险额按 2D 从股票 cap 扣减,
      **那笔现金今日不得被股票占用** —— 首日核对是否真的留住了
