#!/usr/bin/env python3
"""QQQ LEAP: 85 / 148 DTE 的止盈档该定多少? **仅研究用, 不产生任何订单。**

⛔ 不被 daily.py / session.py 调用 (红线2)。回答用户 2026-10-23「85和148 适当的止盈规则是什么」。

**为什么必须重定**: 现行 `take_profit_mult=1.2` 是 §18 在 **~365 DTE** 上扫出来的
(四档 +15/20/25/30%, 选 +20% 的理由是资金效率而非收益显著性 —— 四档收益配对检验全不显著)。
2026-10-22 的 dte-verdict 显示 148 DTE 在两种摩擦假设下都显著优于 330, 若要换档,
止盈必须在新 DTE 上重扫。

⚠️ **指标合法性**: 昨日已证「增量」不能用来选 **delta** (改 delta = 改杠杆, 增量单调无内部峰)。
但选**止盈**不同: 同一张合约、同一入场价、同一成本, 只有出场规则变 => 增量与配对检验
在这里是合法指标。资金占用则另算 (见第 4 问)。

摩擦用 2026-10-22 实测, 宽/紧两组当上下界 (同一张 K830 当天出现 4.37%/7.26%/9.94% 三个值)。
用法: python3 strategy/proposals/qqq-leap-tp-85-148.py
"""
import json, math, statistics
src = open('strategy/proposals/qqq-leap-dte-sweep.py').read().split('DTES=[30,')[0]
exec(src)

TIGHT = {85: 0.0112, 148: 0.0057, 330: 0.0437}
WIDE  = {85: 0.0292, 148: 0.0287, 330: 0.0994}
COST  = {85: 1197.50, 148: 1602.00, 330: 2383.50}
TPS   = [1.10, 1.15, 1.20, 1.25, 1.30, 1.40, 1.50]

import random
random.seed(20261022)
CTRL_S = random.sample(CTRL, 400)

def with_fixed_spread(rt, fn):
    g = globals(); keep = g['half_frac']
    g['half_frac'] = lambda S, mid, _h=rt/2.0: _h
    try:    return fn()
    finally: g['half_frac'] = keep

def run(dte, rt, take, maxhold=20):
    return with_fixed_spread(rt, lambda: (
        [t for t in (trade(i, dte, maxhold=maxhold, take=take) for i in SIGS) if t],
        [t for t in (trade(i, dte, maxhold=maxhold, take=take) for i in CTRL_S) if t]))

def dct(dte, rt, take, maxhold=20):
    return with_fixed_spread(rt, lambda: {
        i: t for i in SIGS if (t := trade(i, dte, maxhold=maxhold, take=take))})

def st(ts):
    p = [t['pct'] for t in ts]
    return dict(n=len(ts), mean=statistics.mean(p), med=statistics.median(p),
                win=sum(1 for x in p if x > 0)/len(p)*100,
                loss20=sum(1 for x in p if x <= -20)/len(p)*100,
                p10=statistics.quantiles(p, n=10)[0], worst=min(p),
                tp=sum(1 for t in ts if t['why'] == 'take_profit')/len(ts)*100,
                tstop=sum(1 for t in ts if t['why'] == 'time_stop')/len(ts)*100,
                held=statistics.mean(t['held'] for t in ts))

print("=" * 110)
print("第 1 问 (纯算术, 不依赖回测): 各止盈档实际要求公允价涨多少?")
print("  触发 bid_t >= 入场ask × m, 而 bid=mid(1-h)/ask=mid(1+h) => mid_t/mid_0 >= m(1+h)/(1-h)")
print("=" * 110)
print(f"{'止盈档':>7s}", end="")
for d in (85, 148, 330):
    print(f"{f'{d}DTE宽':>11s}{f'{d}DTE紧':>11s}", end="")
print()
print("-" * 110)
for m in TPS:
    print(f"{f'+{int((m-1)*100)}%':>7s}", end="")
    for d in (85, 148, 330):
        for S in (WIDE, TIGHT):
            h = S[d]/2
            print(f"{(m*(1+h)/(1-h)-1)*100:>10.1f}%", end="")
    print()

for d in (85, 148):
    for lbl, S in (("收盘摩擦(宽)", WIDE), ("盘中摩擦(紧)", TIGHT)):
        print("\n" + "=" * 110)
        print(f"第 2 问: DTE {d} · {lbl} (点差 {S[d]*100:.2f}%) · 20 交易日时间止损")
        print("=" * 110)
        print(f"{'止盈':>7s}{'信号组':>9s}{'对照组':>9s}{'增量':>10s}{'中位':>9s}{'胜率':>7s}"
              f"{'止盈率':>7s}{'时止率':>7s}{'均持':>6s}{'亏>=20%':>9s}{'p10':>8s}{'最差':>8s}"
              f"{'每张$':>8s}{'每张/日$':>10s}")
        print("-" * 110)
        base = dct(d, S[d], 1.20)
        rows = {}
        for m in TPS:
            sig, ctl = run(d, S[d], m)
            s, c = st(sig), st(ctl)
            rows[m] = s
            per = COST[d]*s['mean']/100
            print(f"{f'+{int((m-1)*100)}%':>7s}{s['mean']:>+8.2f}%{c['mean']:>+8.2f}%"
                  f"{s['mean']-c['mean']:>+9.2f}pp{s['med']:>+8.2f}%{s['win']:>6.1f}%"
                  f"{s['tp']:>6.1f}%{s['tstop']:>6.1f}%{s['held']:>6.1f}{s['loss20']:>8.1f}%"
                  f"{s['p10']:>+7.1f}%{s['worst']:>+7.1f}%{per:>+8.0f}{per/s['held']:>+10.1f}")
        print(f"\n  配对检验 vs 现行 +20% (同一批入场, 按入场 bar 配对):")
        for m in TPS:
            if m == 1.20: continue
            a = dct(d, S[d], m)
            k = sorted(set(a) & set(base))
            dd = [a[x]['pct'] - base[x]['pct'] for x in k]
            mu, sd = statistics.mean(dd), statistics.stdev(dd)
            t = mu/(sd/math.sqrt(len(dd))) if sd > 0 else 0.0
            pv = 2*(1-0.5*(1+math.erf(abs(t)/math.sqrt(2))))
            print(f"    +{int((m-1)*100):>3d}% vs +20%: n={len(dd)}  均值差 {mu:>+7.2f}%  "
                  f"t={t:>6.2f}  p={pv:.3f}" + ("  * 显著" if pv < 0.05 else ""))

print("\n" + "=" * 110)
print("第 3 问: §18 的核心结论在 85/148 上还成立吗? (「下行完全由时间止损决定, 止盈只是旋钮」)")
print("  §18.4 原话: 止盈 +15~+50% 六档 p10 全 -43.6% / 最差全 -69.3% —— 若各档 p10 与最差相同,")
print("  说明止盈不影响下行, 它只调上行形状与资金占用。")
print("=" * 110)
for d in (85, 148):
    print(f"\n-- DTE {d} (收盘摩擦) --")
    print(f"{'止盈':>7s}{'p10':>9s}{'最差':>9s}{'亏>=20%':>9s}")
    for m in TPS:
        sig, _ = run(d, WIDE[d], m)
        s = st(sig)
        print(f"{f'+{int((m-1)*100)}%':>7s}{s['p10']:>+8.1f}%{s['worst']:>+8.1f}%{s['loss20']:>8.1f}%")

print("\n" + "=" * 110)
print("第 4 问: 「每张/日$」这个指标的陷阱 (读上表时必须知道)")
print("=" * 110)
print("""该列 = 每张美元收益 ÷ 平均持有交易日, 衡量**资金占用效率**。它在本账户很重要 ——
现金是约束 (2026-10-22 实测: LEAP 先占 $2,439 导致 AME 整单跳过)。

⚠️ **但它隐含「腾出的现金立刻按同样速率再赚一遍」, 而这个假设在本轨道不成立**:
信号只有 **8.4 次/年**, 平均间隔约 6 周, 而持有期只有 8 天 => **LEAP 轨道绝大多数时间是空仓的**。
腾出的钱不会回到 LEAP, 只会流向 RSI-2 正股 (另一条轨道, 另一个收益率)。
=> **周转在这里的价值远低于该列显示的**, 故读表时应给「每张美元总收益」更高权重,
   给「每张/日」更低权重。这个方向是**偏向抬高止盈**的。""")

print("\n" + "=" * 110)
print("口径局限")
print("=" * 110)
print("""1. 点差 1 天样本且当天不稳定 (同一张 K830: 4.37%/7.26%/9.94%), 故给宽/紧两组上下界。
2. IV 由 KAPPA=1.280 单点反解, 期限结构是模型给的不是实测的。
3. n=89 入场 / 10.4 年。**12 次配对检验里只有 2 次 p<0.05 且落在不同格** ——
   这正是多重比较下的噪声形态, 不构成改档依据。
4. 时间止损固定 20 交易日 (同日 timestop-sweep 的结论: 按增量看最优 13 日且不随 DTE 漂移,
   但三档实际持仓上配对检验全不显著, 故未改)。止盈与时间止损的交互未做二维扫描。""")
