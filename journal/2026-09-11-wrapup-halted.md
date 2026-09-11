# 2026-09-11 收盘后 wrapup — 数据源故障停跑 (红线6)

phase: wrapup
outcome: halted_data_source
(注: 本文件**刻意不写** 收尾完成标记 —— wrapup 未完成, 不得占用幂等键)

## 1. 结论

17:48 ET 的收盘后 wrapup 在 **preflight 数据源自诊断**阶段按红线6 停跑, 未执行任何交易、
未改任何账本。

```
fatal: 数据源不可用, 按红线6 停止
data_sources: {"fred": {"ok": true, "vix": "17.84", "date": "2026-09-10"},
               "alpaca": {"ok": false, "reason": "HTTP Error 401: Unauthorized"},
               "all_ok": false}
```

`git status` 干净, 无半截写入。

## 2. 诊断: Alpaca 凭据被拒 (非代理、非格式问题)

| 检查 | 结果 |
|---|---|
| `paper-api.alpaca.markets/v2/clock` | **401** |
| `paper-api.alpaca.markets/v2/account` | **401** |
| `data.alpaca.markets/v2/stocks/bars` | **401** |
| 401 来源 | Alpaca 自家 nginx (`WWW-Authenticate: Bearer, Basic realm="alpaca.markets"`, 带 `X-Request-ID` / HSTS) — **不是** 代理 |
| TLS 隧道 | `200 Connection Established`, 代理 `bundleCoversEveryHost: true` — 代理正常 |
| 环境变量 | `ALPACA_API_KEY_ID` len=26 前缀 `PK…`; `ALPACA_API_SECRET_KEY` len=44; 两者**无空白、纯 ASCII、未截断** |
| FRED | ok (VIX 17.84 @ 2026-09-10) — 同环境另一凭据正常 |

关键对照: **同一容器、同一组环境变量, 今日 15:22 ET 的盘前主跑 Alpaca 取数完全正常**
(见 `journal/2026-09-11-preclose.md`, bars 拉取成功)。2.5 小时内环境变量未变而 Alpaca 侧开始拒签
→ 判定为**密钥在 Alpaca 侧被吊销 / 轮换 / 过期**, 本会话无法自行修复 (红线5: 密钥只存在会话环境,
不入库, 会话也不得自行更换)。

## 3. 影响面

| 轨道 | 今日影响 | 说明 |
|---|---|---|
| **实盘 RSI-2 主策略** | **无** | 盘前 15:20 主跑已完成: 出场 2 单 (LLY/ABT) 15:24 ET 成交, 买单 4 单已入 pending。wrapup 阶段本就不产出实盘单 |
| 实盘待执行清单 | **无** | `state/pending_orders.json` 已落盘 (SBUX/CVS/BMY 共 $2,849.00, LLY 用户否决), 顺延至 **09-14 周一 09:45–15:55 ET**; 其执行走 MCP, **不依赖 Alpaca** |
| 挑战者影子验证 (paper) | 今日空跑 | 排队清单 `state/paper_queued_challenger.json` orders 为空 (仅 1 条 DIA 跳过记录), **无成交丢失** |
| 周度动量轮动 (paper) | 今日空跑 | 09-11 为周五, 非调仓日 (调仓 weekday=0) |
| 周call 摩擦实测 (paper) | 今日空跑 | 见下方时间闸 |
| 行情管道交叉核对 | **未做** | 需 Alpaca bars; 09-10 券商官方收盘已取回并存档于本文件 §5, 待补做 |

## 4. 时间闸 — 需在这些时点前恢复

1. **09-14 (周一) 15:20 ET 盘前主跑** ← 最紧要。Alpaca 若仍 401, preflight 会同样停跑,
   当日**不出信号、不算出场**。出场是全自动的关键路径, 停跑等于当日无止损/无出场评估。
2. **约 09-16 (周三)**: paper 周call 持仓 `XLP260918C00075000` (到期 2026-09-18, 1 张,
   入场权利金 9.35 @ 09-09) 将触及 `force_exit_dte_lte=2` 的强制平仓。paper 轨道届时若仍跑不动,
   会重演 XLI 的「到期未平 → ITM 自动行权 → 孤儿正股」路径 (纸面, 不涉真金, 但会污染摩擦实测数据)。

## 5. 待补: 2026-09-10 券商官方收盘 (供恢复后补做交叉核对)

全部 `source=sip-list-exchange-close`, date=2026-09-10, 取于 2026-09-11 17:50 ET:

MMM 162.86 · UNP 285.78 · DGX 231.14 · NSC 323.30 · DXCM 84.51 · SBUX 99.22 · CVS 95.29 ·
BMY 63.75 · LLY 1123.00 · AMGN 382.47 · MRK 144.71 · TGT 155.73 · REGN 793.26 · CTVA 84.49 ·
UNH 388.28 · GILD 144.81 · PFE 27.65 · CAH 236.02 · SLB 56.01 · XLV 165.66

## 6. 本次会话已核实的其他事实 (取数在 preflight 之前完成, 结论有效)

- **账户 802095265**: 净值 $10,027.295 · 现金 $3,850.60 · `buying_power` $3,850.60 ·
  `unleveraged_buying_power` $3,850.60 → **券商未开放借贷, 防杠杆闸正常** (红线9)。
- **券商持仓 5 只**, 与 `state/positions.json` 策略仓一致:
  MMM 6.997262 · UNP 6.970220 · DGX 7.329748 (intraday 3.011066 = 今日新协议成交那笔) ·
  NSC 1.043710 · DXCM 11.963244。LLY / ABT 已于盘前出场清零。
  (position_check 闸本身未跑到 —— 它排在数据源自诊断之后 —— 以上为人工逐只比对)
- **财报黑窗复核** (25 只: 持仓 + 待执行 + 今日候选 + paper 挑战者持仓): 最近一次财报为
  JNJ 2026-10-13, **距今 32 天**, 7 日黑窗**无一命中**。待执行的 SBUX (10-28) / CVS (10-28) /
  BMY (10-29) 均远在窗外 —— 周一执行不受财报闸影响。

## 7. 下一步 (需用户处理)

Alpaca **纸面**密钥需在 Alpaca 控制台重新生成, 并更新到会话环境变量
`ALPACA_API_KEY_ID` / `ALPACA_API_SECRET_KEY` (红线5: 不入库)。恢复后本会话/后续会话可:

1. `python3 scripts/integrations.py status` 确认 all_ok;
2. 补跑 `daily.py --date 2026-09-11 --phase wrapup` (幂等键未被占用, 可直接重跑);
3. 补做 09-10 的行情管道交叉核对 (数据见 §5)。
