# 2026-10-20 (周二) 侧记 · 修掉财报黑窗闸的静默拦单

用户授权: 同日「一个一个来」→ 第 1 条选 **「两条一起补」**。
改动范围: 仅 `scripts/signals.py` 的 warning 记录, **判定逻辑与订单输出零改动** (红线2)。

## 改了什么

`entry_signal_ok` 的财报黑窗分支 (原 `signals.py:264-266`) 原先直接 `return False`, 不记任何
warning。现加:

```python
ed = days_to_earnings(sym)
if ed is not None and 0 <= ed <= int(defense.get("earnings_blackout_days", 10)):
    if not quiet:
        warnings.append(f"{sym}: 入场信号成立但 {ed} 天后财报, 防御性跳过入场")
    return False
```

`if not quiet` 是关键: 该函数被两处共用 —— ① 入场候选筛选 (loud) ② 「有卖有买就不卖」的冲突
判定 (quiet)。②不该往入场相关的告警里写东西。

## 撤回了我自己提的另一半 (必须记下来, 否则以后会有人再提一次)

我在提问时说第 258 行「无 `--earnings` 且 `allow_unknown_earnings=false`」分支也是静默 ——
**这个判断是错的**。该条件已由 `signals.py:142-146` 在进任何循环之前无条件通报一次:

```
防御: 无财报日数据, 今日跳过全部新入场 (出场照常)
```

在分支里再记一遍只会重复, 且 universe 有多少只就会刷多少遍。那半已撤回, 并在原处留注释
**明确禁止以后再往这里加**。

## 验证 (同一输入四组复跑, 全部用今日真实数据)

⚠️ 第一次复跑出现假阳性: 买单变空。原因是我在 wrapup 时把 `<wd>/positions.json` (券商持仓文件)
覆盖成了**卖出后**的 8 只, DAL/STT 不在里面 → `broker_qty` 判 0 → 两张出场单被丢 → 没有回款,
现金只剩 $3.03 → 两张买单被资金闸拦。**是夹具的问题, 不是补丁的问题。** 从账本重建卖出前的
券商持仓 (`pos_broker_pre.json`) 后重测:

| 组 | 条件 | 卖单 | 买单 | warnings |
|---|---|---|---|---|
| 1 | 带 `--earnings` (今日真实情形) | DAL/STT exit_strength | **CSCO + NET $983.98×2** —— 与当日实际成交逐字节一致 | **新增且仅新增** `META: 入场信号成立但 1 天后财报, 防御性跳过入场` |
| 2 | 去掉 `--earnings` | 同上 | CSCO + **META** + NET (不变) | 仍只有原有的「无财报日数据, 按配置个股仍可入场」 |
| 3 | `allow_unknown_earnings=false` + 无文件 | 同上 | 空 | 仍只有原有的「无财报日数据, 今日跳过全部新入场」**一行** (证明撤回是对的) |
| 4 | **`earnings_blackout_days=400` 压力测 quiet 守卫** | 同上 | 空 (全拦) | 三个原始信号标的 (META dte=1 / NET dte=16 / CSCO dte=23) **各记一行**, **零个持仓标的漏进 warnings** |

第 4 组是决定性的: 把黑窗拉到 400 天后, 每个有财报日的标的都会落进该分支, 包括 8 只持仓在
冲突判定里走的 quiet 路径。warnings 里一个持仓标的都没出现 → `quiet` 守卫成立。出场链四组
全部不变 (DAL/STT exit_strength)。

## 账面处理

- CLAUDE.md: 已知问题 **#8 摘出 → 移入「已修复 / 已结案」**; 原 #9 (行业上限对掉出池的
  在手个股失明) **顺位变成 #8**。
- ⚠️ `journal/2026-10-20-preclose.md` 里写的「已记入 CLAUDE.md 已知问题 #8」指的是**当时的**
  编号, 现已结案并改号 —— 审计时按本篇为准, 不要去改那篇的历史记录。

## 本次之后仍未动的 (用户按「一个一个来」逐条处理中)

第 2 条起: 补余 21 只个股的 `past` / LEAP `max_spread_pct=6.0` 的依据 / LEAP 价格止损 /
paper 周call 的 $5,000 上限 / LEAP 止盈改 +15% / `position_check` 崩溃行为 A1-A3。

- status: completed
