# 2026-09-14 (周一) 股票池补刷

今天 13:00 ET 的例行周度刷新因 Alpaca 密钥 401 未跑成 (见 `journal/2026-09-14-screen.md`)。
密钥恢复后于 **14:02–14:07 ET** 补跑完整筛选管线, 在 15:10 ET 硬截止前 finalize, 15:20 收盘前主跑可读到新池。

## 管线执行

| 步骤 | 结果 |
|---|---|
| 自检 | `integrations.py status`: alpaca.ok=true, fred.ok=true (VIX 15.84 @09-11); cash_printer 连接器在线 |
| a 资产 | active 14,260 → 可选股票 5,319 |
| b 池 | 近端日线 (2026-08-05 起) 5,318/5,319 有数据 → 流动性前 1000 (池内最低日均额 $71.2M) |
| c 长历史 | 1000/1000 只取到 2025-06-01 起日线 |
| d 热门榜 | Robinhood "100 most popular" 取到 100 只, 已用于 `--popular` 加分 |
| e 打分 | 硬过滤后 survivors 796 (剔除: 异动 163 / 历史不足 18 / 波动 15 / 价格 8 / 流动性 0) → 候选 150 |
| f 行业 | **新查 57 只** (`get_equity_fundamentals`, 每批 10) + **复用 93 只** (沿用 09-07 `universe.json` 的 `sectors`); 150/150 有标签, 无 null |
| g finalize | 行业上限 20 内取前 100, **skipped=0** |
| h 校验 | 恰 100 只 ✅ / 各行业 ≤20 (最大 Finance 17) ✅ / generated=2026-09-14 ✅ / `stocks.json` 同步一致 ✅ |

行业标签复用说明: sector 是同源静态字段, 周与周之间不变, 且只用于 finalize 的每行业上限、不参与打分; 
与上周 09-07 (66 新查 + 84 复用) 同一做法。本次 57 只新查是因为候选集中有 57 只不在上周的 100 只池内。

## 池变动 (23 进 / 23 出)

**新增 (23)**

| 符号 | 行业 |
|---|---|
| ROKU | Technology Services |
| QCOM | Electronic Technology |
| PM | Consumer Non-Durables |
| EOG | Energy Minerals |
| DVN | Energy Minerals |
| META | Technology Services |
| DIS | Consumer Services |
| FANG | Energy Minerals |
| OKE | Industrial Services |
| MCK | Distribution Services |
| ADM | Process Industries |
| COST | Retail Trade |
| MDLZ | Consumer Non-Durables |
| WBD | Consumer Services |
| BP | Energy Minerals |
| PBR | Energy Minerals |
| MO | Consumer Non-Durables |
| DINO | Energy Minerals |
| OVV | Energy Minerals |
| CPAY | Commercial Services |
| MDT | Health Technology |
| EBAY | Retail Trade |
| ROST | Retail Trade |

**剔除 (23)**

| 符号 | 原行业 |
|---|---|
| AMGN | Health Technology |
| MRVL | Electronic Technology |
| PANW | Technology Services |
| GS | Finance |
| FCX | Non-Energy Minerals |
| CAH | Distribution Services |
| RY | Finance |
| ATI | Non-Energy Minerals |
| DASH | Transportation |
| CTVA | Process Industries |
| REGN | Health Technology |
| APH | Electronic Technology |
| GE | Electronic Technology |
| MMM | Producer Manufacturing |
| BLK | Finance |
| F | Consumer Durables |
| SLB | Industrial Services |
| GWW | Distribution Services |
| RTX | Electronic Technology |
| ADI | Electronic Technology |
| NUE | Non-Energy Minerals |
| CNC | Health Services |
| BA | Electronic Technology |

⚠️ 已持仓但被剔除出 universe 的股票**不强制卖出**, 按正常出场规则走完 (本次不处理任何持仓)。

## 行业分布 (新池)

| 行业 | 只数 | (上周) |
|---|---|---|
| Commercial Services | 3 | 2 |
| Communications | 2 | 2 |
| Consumer Durables | 1 | 2 |
| Consumer Non-Durables | 5 | 2 |
| Consumer Services | 3 | 1 |
| Distribution Services | 1 | 2 |
| Electronic Technology | 12 | 17 |
| Energy Minerals | 15 | 8 |
| Finance | 17 | 20 |
| Health Services | 4 | 5 |
| Health Technology | 15 | 16 |
| Industrial Services | 2 | 2 |
| Non-Energy Minerals | 1 | 4 |
| Process Industries | 1 | 1 |
| Producer Manufacturing | 1 | 2 |
| Retail Trade | 5 | 2 |
| Technology Services | 9 | 8 |
| Transportation | 3 | 4 |

## 合规

- 本次会话**只刷新股票池**: 未下任何单、未改 `state/` 下任何账本、未碰持仓与资金。
- 筛选只决定"能买什么", 何时买/买多少仍由 `signals.py` 引擎决定 (红线2)。
- 未向仓库写入任何密钥 (红线5)。
- 改动文件: `strategy/universe.json`, `strategy/stocks.json`, 本 journal。
