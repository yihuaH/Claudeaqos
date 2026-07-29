# 2026-07-29 风控参数调整 — earnings_blackout_days 10 → 7

用户 2026-07-29 授权,将实盘入场财报回避窗口 `config.json defense.earnings_blackout_days` 从 **10 天改为 7 天**。基于 RSI-2 历史事件研究(100 股池、914 笔回测交易、639 个历史财报日,2025-01 → 2026-07)。

## 变更

| 字段 | 旧 | 新 |
|---|---|---|
| `defense.earnings_blackout_days` | 10 | **7** |

其余财报参数不动:`earnings_exit_days=1`(持仓中财报明日到则提前卖)、`allow_unknown_earnings=true`、`exit.max_holding_days=10`。exempt_symbols(8 ETF)照旧豁免。

## 依据 (事件研究)

**降到 7 是"去掉过度保守",不引入财报尾部风险:**

- 10→7 多放行 **17 笔**边际交易(entry 距财报 8–10 天):均值 **+1.30%**、中位 +0.91%、胜率 82%、最差 −5.6%。
- 这 17 笔中 **穿过财报日的 = 0**(全部在财报前就因反弹止盈或止损离场)。
- 即多做 17 笔平均为正的好交易,而"抱着穿财报"的高风险交易一笔没漏进来。

**为何不再降到 5:** 7→5 会再放行 27 笔,其中 **8 笔穿财报**,单独看均值 −3.26%、胜率 38%、最差 −11.4%(XYZ/PANW 隔夜跳空穿透 5% 止损)。5% 止损防不了财报隔夜跳空,只能靠"不持有穿过财报"来防。故 5 天被否,保留 7。

**财报对超短持仓影响小:** 持仓 1 日跨财报 n=7 均值 +5.18%、胜率 100%;风险随持仓天数增长,真正要防的是"抱着穿过财报日"而非"隔日"。

## 影响面

- 仅影响实盘 RSI-2 主策略的**入场准入**(signals.py 用 `earnings_blackout_days` 判 `0<=dte<=N` 则拦)。
- 不改 sizing / 熔断 / 止损 / 宏观 / max_holding 等其他风控。
- paper 轨道(挑战者/期权/动量)不受影响(各自独立配置)。

## 分析物料

事件研究脚本与数据在会话 scratchpad(未入库):`earn_study.py`(逐股 RSI-2 回测→`rsi2_trades.json`)、`earn_analyze.py`(财报打标对比)、`earnings_hist.json`(100 股 639 个财报日)。

status: completed (earnings_blackout_days 10→7,已改 config;实盘下一交易日入场生效)
