# 2026-10-02 新挑战者生成 (learn.py search)

- 用户 2026-10-01 指示「生成新挑战者」(挑战者空置已第 13 个交易日)
- 01:23 ET 执行 (休市时段, 无交易动作); 耗时 **1.06s**
- 数据: ETF 8 + 个股 100 = **108 只**, 5 年日线 (2021-09-28 起), **108/108 有数据**
- walk-forward: 3 折 × 120 测试日, 网格 5×3×3×3 = **135 组**

## 搜索结果: `decision = new_challenger`

| 参数 | 冠军记录 | **实盘 config** | 新挑战者 |
|---|---|---|---|
| entry.rsi2_max | 10.0 | 10.0 | 10.0 |
| exit.rsi2_min | 65.0 | 65.0 | 65.0 |
| exit.stop_loss_pct | 5.0 | **7.0** | **8.0** |
| exit.max_holding_days | 10 | 10 | **15** |

挑战者 score **3.0608** vs 记录中的冠军 score 2.9349, 100 笔。三折表现:

| 折 | 收益% | 最大回撤% | 笔数 | 胜 |
|---|---|---|---|---|
| 1 | 4.632 | 1.538 | 35 | 28 |
| 2 | 4.530 | 1.482 | 39 | 31 |
| 3 | 3.114 | 0.296 | 26 | 25 |

### 边界校验 (红线8) — 全部通过

| 参数 | 值 | 边界 | |
|---|---|---|---|
| entry.rsi2_max | 10.0 | [5.0, 15.0] | ✅ |
| exit.rsi2_min | 65.0 | [55.0, 80.0] | ✅ |
| exit.stop_loss_pct | 8.0 | [3.0, 8.0] | ✅ (贴上界) |
| exit.max_holding_days | 15 | [5, 15] | ✅ (贴上界) |

`strategy/config.json` **未被改动** (git status 仅 `state/learning.json` + `state/paper_positions.json`)。
晋级仍须跑完验证期 (min 15 / max 40 paper 日, ≥3 笔) 并由 evaluate 判 pass, 本次**只建挑战者不晋级**。

⚠️ 两个参数都**贴在网格上界**上 (stop_loss 8.0 = 边界 [3,8] 的上限; max_holding 15 = [5,15] 上限),
说明真实最优可能在边界之外, 网格限制了搜索。若后续验证通过, 值得考虑放宽边界再搜一次 (需用户批准, 红线8)。

---

## ⚠️ 三个问题 —— 影响本次验证期能否产出有意义结论

### 问题1: 冠军记录与实盘 config 不一致, 且实盘值不在搜索网格里

- `state/learning.json` 的 champion 记 `stop_loss_pct = 5.0`
- `strategy/config.json` 实盘跑的是 **7.0** (2026-08-07 用户批准参数扫描后改的)
- 搜索网格 `[3.0, 5.0, 8.0]` —— **7.0 根本没被评估过**

所以 "挑战者 3.0608 > 冠军 2.9349" 比的是**不在跑的那套参数**。实盘真正在跑的 7.0/10
既没进网格, 也不是记录里的冠军。这个对比的基准是双重错位的。

**成因**: 用户 08-07 手工改 config 时, 学习器的 champion 记录没同步更新。红线3 明确风控上限
由用户定、手工改是允许的 —— 但学习器的基准没跟上。

**建议** (待用户定): ① 把 champion 记录同步为实盘实际值 (7.0/10); ② 网格加入 7.0,
或改为围绕当前实盘值取邻域; ③ 两者都做。

### 问题2: Alpaca paper 账户与账本脱钩 —— 新挑战者的 edge 会被旧挑战者污染

`search` 重置了本地账本 (**设计如此**, docstring 写明"产出挑战者参数并初始化 paper 账本"):

| | before | after |
|---|---|---|
| start_capital | $100,877.65 | **$10,218.49** (= 实盘净值) |
| strategy_positions | **12 只** (DIA/ELV/ROKU/TRGP/DXCM/MCK/KO/IWM/STT/DE/TRV/FCX) | **0** |
| trades | 239 | 0 |
| high_water_mark | $102,241.39 | $10,218.49 |
| challenger_started | 2026-07-16 | 2026-10-02 |

**但 Alpaca paper 券商账户没有被重置** —— 那 12 只还在, 账户权益 $95,406.59 (现金 $20,886.18,
持仓约 $74.5k)。

后果: `ch.equity` 直接读券商, 所以 `record` 明天会写 `paper: ~95,406` 对 `live: ~10,218`,
而账本以为 start_capital 是 $10,218.49。**edge 计算会把旧挑战者的 $95k 账户当成新挑战者的表现**,
结论无效。而且那 12 只孤儿仓引擎读不到 (账本为空), **永远不会被卖出**。

**建议** (待用户定, 涉及对外动作故未执行): ① 在 Alpaca paper 账户清仓后再启动验证期;
② 或把 start_capital 设为券商当前权益 $95,406.59 而非实盘净值, 并接受孤儿仓的拖累;
③ 或给 `search` 加一步"重置账本时同步平掉券商持仓"。

### 问题3: edge 指标把实盘入金当成收益 —— 这解释了 09-15 那次荒谬读数

09-15 否决理由原文: `edge -86.151% (需 >= 0.0%); live +83.423% vs paper -2.728%`

查 live 净值曲线, 有两次明显入金型跳变:

| 日期 | live 净值变化 |
|---|---|
| 2026-08-04 | $2,155.69 → $6,172.37 (**+186.3%**) |
| 2026-08-14 | $6,287.49 → $10,185.97 (**+62.0%**) |

区间 (07-15 → 10-01) live 名义收益 **+423.39%**, 但其中绝大部分是**入金, 不是策略收益**。
`edge = paper% − live%` 把入金算进 live 一侧, 于是挑战者无论多好都不可能过关。

📌 **这意味着 09-15 那次否决的依据不可靠** —— 被否的参数 (stop_loss 3.0) 可能并不比冠军差,
只是 edge 公式把入金当成了冠军的本事。

**建议** (待用户定): edge 应改为**资金流调整后收益** (time-weighted return 或扣除入金),
否则只要实盘继续入金, 任何挑战者都会被误判为 fail。

---

## 📌 被 search 删掉的审计记录 (原文保存, 供恢复)

`state/paper_positions.json` 的 `excluded_from_edge` 字段在重置中**整个消失**。该字段记录了
2026-08-21 执行层事故的裁定依据 (幽灵腿/重复腿盈亏从 edge 剔除), 并引用 2026-08-11
「虚假止损」为先例。新账本从零开始确实不需要剔除旧事故, 但**这条裁定本身是历史记录, 不该丢**。

会话未手工改账本 (红线6/引擎拥有账本)。原文完整抄录于此, 即使 scratchpad 快照失效也可追溯:

```json
[
 {
  "date": "2026-08-21",
  "kind": "execution_layer_accident",
  "title": "重复卖单致转空 (2026-08-18~21) — 幽灵腿盈亏, 从挑战者 edge 计算中剔除",
  "precedent": "同 2026-08-11「虚假止损」处理",
  "root_cause": "paper.py _submit 遇 403 raise SystemExit → 排队清单未落盘 → 成交回不到账本 → 引擎重复出卖单。已于 2026-08-21 修补 4 项, 详 journal/2026-08-21-paper-wash-trade-fix.md",
  "legitimate_exits_applied": [
   {
    "symbol": "AVGO",
    "qty": 25,
    "price": 372.1864,
    "date": "2026-08-19",
    "reason": "stop_loss"
   },
   {
    "symbol": "STLD",
    "qty": 38,
    "price": 226.06,
    "date": "2026-08-20",
    "reason": "stop_loss"
   }
  ],
  "phantom_legs": [
   {
    "symbol": "AVGO",
    "sells": [
     {
      "qty": 25,
      "price": 365.318,
      "date": "2026-08-20"
     },
     {
      "qty": 25,
      "price": 371.0112,
      "date": "2026-08-21"
     }
    ],
    "cover": {
     "qty": 50,
     "price": 367.65,
     "date": "2026-08-21"
    },
    "pnl_usd": 25.73
   },
   {
    "symbol": "STLD",
    "sells": [
     {
      "qty": 38,
      "price": 225.049211,
      "date": "2026-08-21"
     }
    ],
    "cover": {
     "qty": 38,
     "price": 228.72,
     "date": "2026-08-21"
    },
    "pnl_usd": -139.49
   }
  ],
  "phantom_pnl_total_usd": -517.29,
  "note": "幽灵腿净亏 $113.76 已实际发生于纸面账户 equity, 但不属策略行为, 评估挑战者 edge 时须剔除。",
  "duplicate_entry_legs": {
   "cause": "同一根因 — 首次买入未回账本, 引擎次日重出同一张入场单, 仓位变成 2 倍",
   "unwound_at": "2026-08-21 (卖掉重复腿, 恢复原定 1 档)",
   "legs": [
    {
     "symbol": "PH",
     "qty": 9,
     "dup_buy_price": 1019.277778,
     "unwind_sell_price": 998.31,
     "pnl_usd": -188.71
    },
    {
     "symbol": "DAL",
     "qty": 118,
     "dup_buy_price": 81.891552,
     "unwind_sell_price": 82.41,
     "pnl_usd": 61.18
    },
    {
     "symbol": "FITB",
     "qty": 176,
     "dup_buy_price": 54.76,
     "unwind_sell_price": 54.36,
     "pnl_usd": -70.4
    },
    {
     "symbol": "MTB",
     "qty": 40,
     "dup_buy_price": 244.31,
     "unwind_sell_price": 239.17,
     "pnl_usd": -205.6
    }
   ],
   "pnl_total_usd": -403.53,
   "note": "重复腿买价用 08-20 那笔 (被撤销的一档); 首档按原价保留在账本。此段盈亏同样从 edge 剔除。"
  },
  "missing_buys_backfilled": [
   {
    "symbol": "CRWD",
    "qty": 48,
    "price": 198.592292,
    "date": "2026-08-20"
   },
   {
    "symbol": "PH",
    "qty": 9,
    "price": 1019.277778,
    "date": "2026-08-20"
   },
   {
    "symbol": "DAL",
    "qty": 116,
    "price": 81.891552,
    "date": "2026-08-20"
   },
   {
    "symbol": "FITB",
    "qty": 176,
    "price": 54.76,
    "date": "2026-08-20"
   },
   {
    "symbol": "MTB",
    "qty": 39,
    "price": 244.31,
    "date": "2026-08-20"
   }
  ]
 }
]
```

## 本次动作清单

- ✅ 取数 108 只 × 5 年日线
- ✅ `learn.py search` 执行, decision=new_challenger, 参数全在边界内
- ✅ `config.json` 未动, 未晋级, 未下任何单
- ⚠️ 三个问题已记录, **均未自行处置**, 待用户定
- ⚠️ 被删的 `excluded_from_edge` 原文已存档于本 journal
