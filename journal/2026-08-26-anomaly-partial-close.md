# ⚠️ 引擎限制: `weekly_calls.py apply` 不支持部分平仓 (2026-08-26 发现)

## 触发经过

用户手动平掉 BAC 信用put价差 **3/4 张** (引擎 exit_strength 信号本为全平 4 张)。
回写账本时发现 `apply` 会**抹掉剩余 1 张**。

## 缺陷位置

`scripts/weekly_calls.py` `cmd_apply` 卖出分支 (约 L722):

```python
else:
    pos = positions.pop(occ, None)          # ← 整仓移除, 不看 qty
    ...
    pnl_usd = round((ep - price) * 100.0 * qty, 2)   # 盈亏按 qty 算 (正确)
```

盈亏按 `qty` 计算是对的, 但持仓是 **`pop` 整条**。部分平仓 3/4 张后, 账本里剩余的 1 张会**凭空消失**
→ 账本与券商脱钩 → 与 2026-08-18 挑战者转空事故同一类根因 (账本失真后引擎按错误状态出单)。

**此前未暴露**: 引擎只出全平信号, 部分平仓只可能来自手动通道。

## 本次处置: **未跑 apply, 手工对账**

- `round_trips` 记 3 张往返: 入场贷记 $0.22 → 平仓借记 $0.11, **+$33** (pnl_pct 按每张风险 $4.78 归一 = +2.30%)
- 持仓 `contracts` 4 → **1**, 并加 `partial_closes[]` 留痕
- `trades` 记 qty=3 卖出
- 账本与券商现已一致: BAC 1 张 · NVDA 1 张 ✓

## 待修 (需用户批准)

给卖出分支加部分平仓支持:
```python
held = int(pos.get("contracts", 0))
if qty < held:
    pos["contracts"] = held - qty       # 部分平: 减量保留
    positions[occ] = pos
else:
    positions.pop(occ, None)            # 全平: 移除
```
`round_trips` 相应记 `contracts` 字段以区分部分/全部。**本次未改代码** (盘中不改引擎)。

## 同时记录: 用户对 BAC 的实际操作序列

| 时间 (ET) | 动作 | 结果 |
|---|---|---|
| 11:33 | 挂 1 张 @ 净借记 $0.09 | 未成交 |
| 11:36 | 挂 3 张 @ 净借记 $0.11 | ✅ **成交** (买回 P59 @0.12 / 卖出 P54 @0.01), **+$33** |
| 11:37 | 撤销 11:33 那张 | cancelled |
| 11:37 | 挂 1 张 @ 净借记 **$0.01** | confirmed, ⚠️ **不可能成交** (短腿 P59 现 bid $0.10), gfd 今日收盘作废 |

剩余 1 张为用户主动选择保留 (引擎信号为全平)。
