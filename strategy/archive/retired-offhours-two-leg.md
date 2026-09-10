# 已退役: 4C 盘外两腿混合执行协议 (2026-07-20 → 2026-09-10)

> 2026-09-10 用户批准换为「次日盘中单笔市价」后退役。留档以便回退与审计。
> 退役依据与实测数据见 `journal/2026-09-10-exec-window.md`。
> 现行协议见 `strategy/playbook.md` §4C-2。

## 原协议全文 (playbook §4C 步骤 2, 退役前最后版本)

```
2. 按 seq 顺序执行 (先换仓卖后买), 每单 review_equity_order → 无预期外告警 → place_equity_order
   (ref_id=每单一个 UUID, 重试必须复用), 订单类型按执行时刻分两种模式:
   - 当日市价模式 (trade_date 当天 09:30–15:55 ET 且开市): market + regular_hours, 同原规则;
     买单遇购买力不足告警 → 按告警金额下调 dollar_amount (不低于 min_order_usd, 否则跳过)。
   - 盘外限价模式 (15:55 ET 后至次一交易日 09:25 ET, 用户任意时间触发): 改用 limit 单 +
     market_hours=all_day_hours (2026-07-21 用户指示: 盘后/隔夜/盘前时段即时生效, 能成交就成交,
     不必等开盘); 标的不支持 24h 时段或下单被拒 → 依次降级 extended_hours → regular_hours 排队开盘,
     降级记 journal。限价保护不变, 盘外薄流动性只影响成交概率、不影响成交价上限。
     混合执行 (2026-07-27 用户改进: 整股即时限价 + 余量分数市价排开盘, 解决整股欠配)。
     背景: Robinhood 限价单拒绝分数股 (2026-07-20 实测 API 400), 故整股走即时限价、零头走市价排开盘。
     每个买单 (dollar_amount=D, est_price=E) 拆两腿, 合计 恰好 ≤ D、绝不放大:
     - ① 整股即时限价腿: limit_price = round(E×1.010, 2) (信号价 +1.0% 容差), time_in_force=gfd;
       whole = floor(D ÷ limit_price)。whole ≥ 1 → place limit (whole 股, all_day_hours, ref_id①) 盘后即时成交;
       标的不支持 24h 或被拒 → 降级 extended_hours → regular_hours 排开盘 (降级记 journal)。取实际成交额
       cost₁ = filled_qty × avg_price; 该腿未成/部分成交 → 撤掉未成交量 (防其后续成交与②腿重复), cost₁ 只计已成交。
     - ② 余量分数市价腿: remaining = D − cost₁。remaining ≥ min_order_usd → place
       market + regular_hours + dollar_amount=remaining (ref_id②), 分数股排次一开盘按开盘价成交,
       补足整股腿吃不下的零头; remaining < min_order_usd → 跳过零头记 journal。
       whole == 0 (买不起 1 整股) → 无①腿, 整单 D 直接走②腿 (全额分数市价排开盘)。
     - 两腿 cost₁ + remaining = D (①按实成、②补差), 合计不超过 D; 为凑整向上加钱属放大 绝不允许; ref_id 每腿一个、重试复用。
     - ①腿今夜成交、②腿次一开盘成交: 今夜写回只记①腿实成交; ②腿开盘成交由报告窗口 10:45 ET 晨检回写 (或次日主跑 §1 按券商持仓自愈)。
     - funding_rotation / accelerated_liquidation 换仓与加速清理卖单夜间一律跳过
       (存量多为分数股, 盘外无法限价卖出), 留待次日主流程重算。
       注: 原"卖款 T+1 无法支持本批买单"的理由已随 2026-08-07 margin 升级失效, 但分数股限制仍在。
```

时效: 买单与换仓卖单有效至**次一交易日 09:25 ET**; 逐单 `valid_until: "next_open_0925_et"`。

## 退役理由 (三条, 详见 journal)

1. **09:25 截止比出场回款到账 (09:30 开盘) 早 5 分钟** —— 引擎按 `实时BP + 当日出场回款` 分配金额,
   这批钱结构上永远吃不到。13 批 pending / 56 张买单 / $51,566 中 **$16,604 (32%)** 属此类。
2. **①腿实测多付 0.55%** (中位 +0.59%, n=10), 且被 +1.0% 限价从上方截断, 真实成本更高;
   同期盘中入场为 +0.27~0.37%。
3. **两腿机制本身的运维成本** —— whole==0 分支、24h→extended→regular 降级阶梯、孤儿①腿、
   ②腿延后需晨检补下、双重成交风险判断; 全部只为绕开「RH 拒绝分数股**限价**单」这一条,
   而盘中美元额**市价**单原生支持分数股。

## 如何回退

把上面代码块内容替换回 `playbook.md` §4C 步骤 2, 并同步改回:
`config.json execution.rule` · `CLAUDE.md` 红线9 · `scripts/session.py` 的 `exec_window()` 与 CHECKLIST。
