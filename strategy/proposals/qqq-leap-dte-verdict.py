#!/usr/bin/env python3
"""QQQ LEAP: 29 / 85 / 148 DTE 是不是比 330 好? **仅研究用, 不产生任何订单。**

⛔ 不被 daily.py / session.py 调用 (红线2)。回答用户 2026-10-22「是不是29 85 148dte更好」。

与同日 qqq-leap-dte-sweep.py 的区别: 那份用 §16.1 拟合收摩擦, 而当晚 spread_probe 首次采样
**证伪了该拟合** (30档高估 5.6x / 365档低估 1.9x)。本脚本改用**实测点差**, 并且因为同一张
K830 当天出现过 4.37% (盘中研究时) / 7.26% (下单时) / 9.94% (收盘) 三个值, **用两组当上下界**,
不挑一个 —— 点差本身不是稳定数, 挑一个会制造虚假精度。

口径其余部分继承 dte-sweep (delta 0.30 / 止盈 +20% / 20 交易日时间止损 / 入场跌≥2%+牛市过滤)。
用法: python3 strategy/proposals/qqq-leap-dte-verdict.py
"""
import json, math, statistics
src = open('strategy/proposals/qqq-leap-dte-sweep.py').read().split('DTES=[30,')[0]
exec(src)

# 实测往返点差 (= bid-ask 占 mid 的比例)。两组:
TIGHT = {29: 0.0074, 85: 0.0112, 148: 0.0057, 330: 0.0437}   # 盘中 (dte-sweep 用的那组)
WIDE  = {29: 0.0216, 85: 0.0292, 148: 0.0287, 330: 0.0994}   # 收盘 spread_probe 首次采样
# 当日 delta≈0.30 档每张实际价格 (mid×100), 现货 704.59
COST  = {29: 924.00, 85: 1197.50, 148: 1602.00, 330: 2383.50}
DTES  = [29, 85, 148, 330]

def run_fixed(dte, rt_spread, maxhold=20, take=1.2):
    """把 half_frac 固定为该 DTE 的实测半点差后重跑。"""
    hf = rt_spread / 2.0
    g = globals(); keep = g['half_frac']
    g['half_frac'] = lambda S, mid, _h=hf: _h
    try:
        sig = [t for t in (trade(i, dte, maxhold=maxhold, take=take) for i in SIGS) if t]
        ctl = [t for t in (trade(i, dte, maxhold=maxhold, take=take) for i in CTRL_S) if t]
    finally:
        g['half_frac'] = keep
    return sig, ctl

def dict_fixed(dte, rt_spread, maxhold=20, take=1.2):
    """同 run_fixed 但返回 {入场bar下标: trade}, 供按入场配对用。"""
    hf = rt_spread / 2.0
    g = globals(); keep = g['half_frac']
    g['half_frac'] = lambda S, mid, _h=hf: _h
    try:
        return {i: t for i in SIGS if (t := trade(i, dte, maxhold=maxhold, take=take))}
    finally:
        g['half_frac'] = keep


import random
random.seed(20261022)
CTRL_S = random.sample(CTRL, 400)

def tail(ts):
    p = [t['pct'] for t in ts]
    tsl = [t['pct'] for t in ts if t['why'] == 'time_stop']
    return dict(n=len(ts), mean=statistics.mean(p), med=statistics.median(p),
                win=sum(1 for x in p if x > 0)/len(p)*100,
                loss20=sum(1 for x in p if x <= -20)/len(p)*100,
                worst=min(p), p10=statistics.quantiles(p, n=10)[0],
                ts_mean=(statistics.mean(tsl) if tsl else float('nan')),
                exp=sum(1 for t in ts if t['why'] == 'expired')/len(ts)*100,
                held=statistics.mean(t['held'] for t in ts))

print("=" * 104)
print("第 1 问: 同一条「+20% 止盈」规则, 各 DTE 实际需要公允价涨多少?")
print("  触发条件 bid_t >= 入场ask × 1.2, 而 bid=mid(1-h) / ask=mid(1+h)")
print("  => mid_t/mid_0 >= 1.2 × (1+h)/(1-h)。这一问**纯算术, 不依赖回测**。")
print("=" * 104)
print(f"{'DTE':>5s}{'收盘往返点差':>14s}{'需涨(收盘)':>12s}{'盘中往返点差':>14s}{'需涨(盘中)':>12s}")
print("-" * 104)
for d in DTES:
    hw, ht = WIDE[d]/2, TIGHT[d]/2
    print(f"{d:>5d}{WIDE[d]*100:>13.2f}%{(1.2*(1+hw)/(1-hw)-1)*100:>11.1f}%"
          f"{TIGHT[d]*100:>13.2f}%{(1.2*(1+ht)/(1-ht)-1)*100:>11.1f}%")

for label, SP in (("收盘实测点差 (宽)", WIDE), ("盘中实测点差 (紧)", TIGHT)):
    print("\n" + "=" * 104)
    print(f"第 2 问 [{label}]: 回测表现")
    print("=" * 104)
    print(f"{'DTE':>5s}{'每张$':>8s}{'信号组均值':>12s}{'对照组':>9s}{'增量':>10s}"
          f"{'每张美元':>10s}{'中位':>9s}{'胜率':>7s}{'亏>=20%':>9s}{'p10':>8s}{'最差':>8s}"
          f"{'止损单均值':>12s}{'到期率':>7s}")
    print("-" * 104)
    rows = {}
    for d in DTES:
        sig, ctl = run_fixed(d, SP[d])
        s, c = tail(sig), tail(ctl)
        inc = s['mean'] - c['mean']
        rows[d] = (s, c, inc)
        print(f"{d:>5d}{COST[d]:>8.0f}{s['mean']:>+11.2f}%{c['mean']:>+8.2f}%{inc:>+9.2f}pp"
              f"{COST[d]*s['mean']/100:>+10.0f}{s['med']:>+8.2f}%{s['win']:>6.1f}%"
              f"{s['loss20']:>8.1f}%{s['p10']:>+7.1f}%{s['worst']:>+7.1f}%"
              f"{s['ts_mean']:>+11.2f}%{s['exp']:>6.1f}%")
    print(f"\n  配对检验 vs 现行 330 DTE (同一批入场, **基准也用实测 330 摩擦**):")
    b = dict_fixed(330, SP[330])
    for d in DTES:
        if d == 330: continue
        a = dict_fixed(d, SP[d])
        keys = sorted(set(a) & set(b))          # 按**入场 bar 下标**配对, 不按列表位置
        dd = [a[k]['pct'] - b[k]['pct'] for k in keys]
        m, sd = statistics.mean(dd), statistics.stdev(dd)
        t = m/(sd/math.sqrt(len(dd))) if sd > 0 else 0.0
        pv = 2*(1-0.5*(1+math.erf(abs(t)/math.sqrt(2))))
        print(f"    {d:>4d} DTE vs 330: n={len(dd)}  均值差 {m:>+7.2f}%  t={t:>6.2f}  "
              f"p={pv:.3f}" + ("  * 显著" if pv < 0.05 else "  (不显著)"))

print("\n" + "=" * 104)
print("第 3 问: 账户规模约束 (净值 $9,806, 当前现金 $294, LEAP 资金优先级最高)")
print("=" * 104)
NAV = 9805.96
print(f"{'DTE':>5s}{'每张$':>9s}{'占净值':>9s}{'满净值可买':>11s}{'每张美元收益(收盘摩擦)':>24s}")
print("-" * 104)
for d in DTES:
    sig, _ = run_fixed(d, WIDE[d])
    pnl = COST[d] * statistics.mean(t['pct'] for t in sig) / 100
    print(f"{d:>5d}{COST[d]:>9.0f}{COST[d]/NAV*100:>8.1f}%{int(NAV//COST[d]):>10d}张{pnl:>+23.0f}")
print("\n⚠️ LEAP 是资金优先级 1: 每张占净值越大, 当日 RSI-2 买单越容易被整单挤掉。")
print("   330 档单张占净值 24.3% —— 今日就是它先占 $2,439 导致 AME 整单跳过。")

print("\n" + "=" * 104)
print("口径局限 (必读)")
print("=" * 104)
print("""1. **点差只有 1 天样本, 且当天就不稳定**: 同一张 K830 今日出现 4.37% (盘中研究)
   / 7.26% (下单时) / 9.94% (收盘) 三个值。故本脚本给上下界而不给点估计。
   要定量必须靠 spread_probe 攒多日样本。
2. IV 仍由 KAPPA=1.280 **单点反解**; 回测的 IV 期限结构是模型给的, 不是实测的。
   实测当日 IV: 29d 0.229 / 85d 0.203 / 148d 0.203 / 330d 0.207 —— 近月偏高 (微笑/事件),
   模型未复现这一点, 会**低估**短 DTE 的买入成本。
3. n=90 个信号 / 10.4 年。到期结算按内在价值、无点差。
4. 每张美元收益按**当日这一个现货价**算; 不同年份的合约价格不同, 该列只做量级比较。""")
