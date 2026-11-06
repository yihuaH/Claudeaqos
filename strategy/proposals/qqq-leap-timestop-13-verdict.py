#!/usr/bin/env python3
"""QQQ LEAP 时间止损 **13 交易日 vs 现行 20 交易日** 专项定论。**仅研究用, 不产生任何订单**。

⛔ 不被 daily.py / session.py 调用 (红线2: 订单只能来自确定性引擎)。
回答用户 2026-11-06「13日回测一下」。

## 为什么需要这一份 (10-22 的 timestop-sweep 已经扫过, 但有三处没做)

10-22 `qqq-leap-timestop-sweep.py` 的结论是「别换成百分比」, 顺带发现 **13 日的信号增量
在每个 DTE 上都 ≥ 20 日**。但它:
  (A) **只对均值做了配对检验** (第9问 p=0.407/0.719/0.766 全不显著), **增量没做任何显著性检验** ——
      而增量才是它用来下结论的那个指标。不检验就不知道「13 更好」是信号还是噪声。
  (B) **逐档单独测**, 而现行轨道是**同时买 148/85/330 三档各 1 张** (`contract.ladder`)。
      三档成本差 3 倍 (约 $1,383 / $1,612 / $2,488), 故组合口径必须**按成本加权**,
      逐档平均会把最贵的年期档的权重算轻。**三档作为一个组合从未被测过。**
  (C) **没做「去掉最好 5%」的稳健性检验** —— 而这正是 2026-10-30 推翻周call 深ITM 论点的
      那把尺子 (`journal/2026-10-30-research-rsi2-delta30.md`: 「三种期权形态全部在去掉
      最好 5% 后转负, 正股轨道不然」)。同一把尺子必须也量这里。

口径继承 `qqq-leap-dte-sweep.py` (再往上继承 `concurrency.py`): delta 0.30 / 止盈 +20%
(口径 `bid ≥ 入场ask × 1.20`, 双边点差已扣) / §16.1 点差拟合 / 入场 = 跌≥2% + 牛市过滤。
**只改 maxhold。** 对照组 = 牛市中每天买 (去掉跌≥2%), 同样规则。

⚠️ **牛市过滤的注意**: 本回测的入场**含**牛市过滤, 而实盘 2026-11-05 起
`require_close_above_sma200=false` 已把它关掉。故本结论严格说是「开着过滤时 13 vs 20」。
关掉过滤后入场集更大且更偏下跌趋势, 两档的相对优劣**未被本脚本覆盖** —— 见文末告警。

用法: python3 strategy/proposals/qqq-leap-timestop-13-verdict.py
"""
import json, math, random, statistics

src = open('strategy/proposals/qqq-leap-dte-sweep.py').read().split('DTES=[30,')[0]
exec(src)

random.seed(20261106)
TD_PER_CAL = 252.0 / 365.0
LADDER = [148, 85, 330]          # strategy/qqq_leap.json contract.ladder, 顺序即资金优先级
CTRL_S = random.sample(CTRL, 500)


def dist(xs):
    """全分布, 不只均值 —— §18 挑止盈档用的就是这套镜头。"""
    if not xs: return None
    s = sorted(xs)
    k = max(1, int(len(s) * 0.95))
    return dict(n=len(s), mean=statistics.mean(s), med=statistics.median(s),
                p10=s[max(0, int(len(s) * 0.10))], worst=s[0],
                win=100.0 * sum(1 for x in s if x > 0) / len(s),
                loss20=100.0 * sum(1 for x in s if x <= -20) / len(s),
                drop_best5=statistics.mean(s[:k]))


def paired(a, b):
    """同一批入场上的配对检验 (a - b)。返回 (均值差, t, p)。"""
    d = [x - y for x, y in zip(a, b)]
    if len(d) < 3: return (0.0, 0.0, 1.0)
    m, sd = statistics.mean(d), statistics.stdev(d)
    t = m / (sd / math.sqrt(len(d))) if sd > 0 else 0.0
    p = 2 * (1 - 0.5 * (1 + math.erf(abs(t) / math.sqrt(2))))
    return (m, t, p)


def boot_ci(d, B=4000):
    """配对差的 bootstrap 95% CI (不假设正态)。"""
    if len(d) < 3: return (0.0, 0.0)
    ms = sorted(statistics.mean(random.choices(d, k=len(d))) for _ in range(B))
    return (ms[int(B * 0.025)], ms[int(B * 0.975)])


def basket(i, maxhold):
    """在 bar i 按 ladder 买三档各 1 张, 各自独立出场。返回成本加权收益% 与逐档明细。
    成本加权是必须的: 三档单价差约 3 倍, 等权平均会把年期档算轻 (见本文件 (B))。"""
    legs = []
    for dte in LADDER:
        t = trade(i, dte, maxhold=maxhold)
        if t is None: return None          # 任一档取不到合约 -> 该信号整条弃用, 保持可比
        legs.append(t)
    cost = sum(l['cost'] for l in legs)
    wret = sum(l['cost'] * l['pct'] for l in legs) / cost
    return dict(pct=wret, cost=cost, legs=legs,
                held=max(l['held'] for l in legs),
                n_tp=sum(1 for l in legs if l['why'] == 'take_profit'))


print("=" * 104)
print("QQQ LEAP 时间止损 13 vs 20 交易日 —— 专项定论 (回答 2026-11-06「13日回测一下」)")
print("=" * 104)
print(f"入场信号 n={len(SIGS)}  对照组抽样 n={len(CTRL_S)} (全集 {len(CTRL)}, 种子 20261106)")
print(f"ladder = {LADDER} DTE 各 1 张; 止盈 +20%; delta 0.30; 含牛市过滤")

# ---------- 第 1 问: 三档梯子作为一个组合 (本脚本的主要新增) ----------
print("\n" + "=" * 104)
print("第 1 问 (新): 把 148/85/330 三档当成**一个按成本加权的组合**, 13 日 vs 20 日")
print("=" * 104)
B = {}
for mh in (10, 13, 15, 20, 25):
    rows = [b for b in (basket(i, mh) for i in SIGS) if b]
    B[mh] = rows
    d = dist([b['pct'] for b in rows])
    mark = "  <= 现行" if mh == 20 else ""
    print(f"  maxhold {mh:>2}d | n={d['n']:>3} 均值 {d['mean']:>+7.2f}%  中位 {d['med']:>+7.2f}%  "
          f"胜率 {d['win']:>5.1f}%  p10 {d['p10']:>+7.2f}%  最差 {d['worst']:>+7.2f}%  "
          f"亏≥20% {d['loss20']:>5.1f}%  均持 {statistics.mean(b['held'] for b in rows):>4.1f}d{mark}")

# 组合口径也按索引配对 (同上理由)
a13, a20 = [], []
for i in SIGS:
    ba, bb = basket(i, 13), basket(i, 20)
    if ba is None or bb is None: continue
    a13.append(ba['pct']); a20.append(bb['pct'])
if len(a13) >= 3:
    m, t, p = paired(a13, a20)
    lo, hi = boot_ci([x - y for x, y in zip(a13, a20)])
    print(f"\n  配对检验 (13 − 20), 组合口径 (按索引配对 n={len(a13)}): 均值差 {m:>+6.2f}pp  t={t:>+5.2f}  p={p:.3f}  "
          f"bootstrap 95%CI [{lo:+.2f}, {hi:+.2f}]pp")
    print(f"  => {'显著' if p < 0.05 else '**不显著**'}; CI {'不含 0' if lo*hi>0 else '**跨过 0**'}")

# ---------- 第 2 问: 增量的显著性 (10-22 没做的那一项) ----------
print("\n" + "=" * 104)
print("第 2 问 (新): 增量 (信号 − 对照) 的**配对显著性** —— 10-22 只报了点估计, 没做检验")
print("  口径: 信号侧与对照侧各自配对 (同一批入场比 13 vs 20), 再看两者之差。")
print("=" * 104)
print(f"{'DTE':>5} | {'信号13-20':>11} {'p':>6} | {'对照13-20':>11} {'p':>6} | {'增量变化':>10} | 判读")
print("-" * 104)
def pair_by_index(idxs, dte, mh_a, mh_b):
    """⚠️ **必须按索引配对, 不能按列表顺序**: trade() 对部分 bar 返回 None (取不到合约),
    两个 maxhold 的 None 集合不一定相同, 顺序配对会把不同入场日的收益配到一起。
    只保留两档**都**有成交的入场日。"""
    a, b = [], []
    for i in idxs:
        ta, tb = trade(i, dte, maxhold=mh_a), trade(i, dte, maxhold=mh_b)
        if ta is None or tb is None: continue
        a.append(ta['pct']); b.append(tb['pct'])
    return a, b

for dte in LADDER + [91, 365]:
    s13, s20 = pair_by_index(SIGS, dte, 13, 20)
    c13, c20 = pair_by_index(CTRL_S, dte, 13, 20)
    if len(s13) < 3 or len(c13) < 3:
        print(f"{dte:>5} | 可配对样本不足 (信号 {len(s13)} / 对照 {len(c13)}), 跳过"); continue
    sm, st, sp = paired(s13, s20)
    cm, ct, cp = paired(c13, c20)
    inc_chg = sm - cm
    verdict = ("信号侧显著改善" if sp < 0.05 and sm > 0 else
               "**两侧都不显著 = 噪声**" if sp >= 0.05 and cp >= 0.05 else
               "仅对照侧显著 (= beta)" if cp < 0.05 and sp >= 0.05 else "两侧均显著")
    print(f"{dte:>5} | {sm:>+10.2f}pp {sp:>6.3f} | {cm:>+10.2f}pp {cp:>6.3f} | "
          f"{inc_chg:>+9.2f}pp | {verdict}")

# ---------- 第 3 问: 去掉最好 5% 的稳健性 (本仓库自己的尺子) ----------
print("\n" + "=" * 104)
print("第 3 问 (新): 去掉最好 5% 后还站得住吗? —— 2026-10-30 推翻周call 深ITM 用的同一把尺子")
print("=" * 104)
print(f"{'口径':>16} | {'全样本均值':>11} {'去最好5%':>10} {'塌掉':>9} | {'中位':>9} {'p10':>9}")
print("-" * 104)
for label, xs in [("组合 13d", [b['pct'] for b in B[13]]),
                  ("组合 20d", [b['pct'] for b in B[20]])]:
    d = dist(xs)
    print(f"{label:>16} | {d['mean']:>+10.2f}% {d['drop_best5']:>+9.2f}% "
          f"{d['drop_best5']-d['mean']:>+8.2f}pp | {d['med']:>+8.2f}% {d['p10']:>+8.2f}%")
for dte in LADDER:
    for mh in (13, 20):
        xs = [t['pct'] for t in (trade(i, dte, maxhold=mh) for i in SIGS) if t]
        d = dist(xs)
        print(f"{f'DTE{dte} {mh}d':>16} | {d['mean']:>+10.2f}% {d['drop_best5']:>+9.2f}% "
              f"{d['drop_best5']-d['mean']:>+8.2f}pp | {d['med']:>+8.2f}% {d['p10']:>+8.2f}%")

# ---------- 第 4 问: 资金效率 (§18 挑 +20% 用的镜头) ----------
print("\n" + "=" * 104)
print("第 4 问: 资金效率 —— 同样的钱占用更少天数, 每元回报与年化占用")
print("=" * 104)
print(f"{'maxhold':>8} | {'均持(交易日)':>12} {'均值':>9} {'每持仓日收益':>13} {'止盈率':>8} {'时间止损率':>11}")
print("-" * 104)
for mh in (10, 13, 15, 20, 25):
    rows = B[mh]
    hold = statistics.mean(b['held'] for b in rows)
    mean = statistics.mean(b['pct'] for b in rows)
    tp_legs = sum(b['n_tp'] for b in rows); tot_legs = 3 * len(rows)
    print(f"{mh:>7}d | {hold:>12.1f} {mean:>+8.2f}% {mean/hold:>+12.3f}%/d "
          f"{100*tp_legs/tot_legs:>7.1f}% {100*(tot_legs-tp_legs)/tot_legs:>10.1f}%")

print("\n" + "=" * 104)
print("样本与口径限制 (必读, 不要只看上面的表)")
print("=" * 104)
print(f"""1. n={len(SIGS)} 个信号 —— 对「均值差几个 pp」这种量级, 这个样本量**判不出显著性**,
   上面第 1/2 问的 p 值已经把这件事说清了。
2. **牛市过滤**: 本回测入场含 `close > SMA200`, 而实盘自 2026-11-05 起**已关闭该过滤**
   (用户「照改照买」)。关掉后入场集变大且更偏下跌趋势, **13 vs 20 的相对优劣本脚本未覆盖**。
   这是本次结论最大的外部有效性缺口。
3. 定价是 Black-Scholes + §16.1 点差拟合, 不是真实历史期权报价; IV 用的是模型值。
4. 对照组抽样 {len(CTRL_S)} 个 (全集 {len(CTRL)}), 种子 20261106 固定可复现。
5. 组合口径按**成本加权**, 且「任一档取不到合约则整条信号弃用」以保持 13/20 可比。
6. 本脚本**不改任何 config, 不产生任何订单** (红线2/红线3): 换不换 13 日只能由用户定。""")
