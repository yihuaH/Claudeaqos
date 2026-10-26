#!/usr/bin/env python3
"""QQQ LEAP 梯子: 三档都用 +20% 止盈合理吗? **仅研究用, 不产生任何订单。**

⛔ 不被 daily.py / session.py 调用 (红线2)。回答用户 2026-10-26「qqqleap 三个时间段都是20%止盈 合理嘛」。

**为什么还要再扫一次** (10-23 的 tp-85-148.py 不够):
  ① 那次 **第2问/第3问 只跑了 85 与 148**, **330 档从未在同一口径下扫过 TP 网格** ——
     330 的 +20% 至今仍来自 §18 在 ~365 DTE 上的四档扫描, 而 §18 选 +20% 的理由是
     **资金效率而非收益显著性** (四档配对检验全不显著)。三档里有一档的参数来源比另两档弱。
  ② 那次点差用 **10-22 单日样本**。现已有 10-22/10-23 两日实测 (state/option_spread_samples.json),
     365 档中位 8.709% —— 比当时 WIDE 用的 9.94% 低, 但远高于 §16.1 模型的 5.32%。

**核心机制 (本脚本第 1 问, 纯算术)**: 止盈触发条件是 `bid_t >= 入场ask × m`,
而 bid/ask 各自离中价 h = 点差/2。故实际要求的**公允价涨幅**是 m(1+h)/(1-h) - 1, 与 h 强相关。
330 档点差是 85/148 档的 ~3.5 倍 => **名义上三档同为 +20%, 公允价口径下 330 档的门槛高得多。**
「统一 20%」统一的是**净收益**而非**标的涨幅** —— 这正是用户这一问的要害, 两种读法见第 1 问。

用法: python3 strategy/proposals/qqq-leap-tp-uniform-vs-perrung.py
"""
import json, math, statistics, random
src = open('strategy/proposals/qqq-leap-dte-sweep.py').read().split('DTES=[30,')[0]
exec(src)

# 实测点差 (state/option_spread_samples.json, 2026-10-22 + 10-23 两日中位)
# 梯子三档 DTE → 探针 bucket: 85→90, 148→180, 330→365
MEAS = {85: 0.02556, 148: 0.02427, 330: 0.08709}
MEAS_LO = {85: 0.02189, 148: 0.01983, 330: 0.07474}   # 两日里较窄那天
MEAS_HI = {85: 0.02923, 148: 0.02871, 330: 0.09943}   # 较宽那天
RUNGS = [85, 148, 330]
TPS = [1.10, 1.125, 1.15, 1.20, 1.25, 1.30, 1.40, 1.50]
BASE = 1.20

random.seed(20261022)
CTRL_S = random.sample(CTRL, 400)

def with_spread(rt, fn):
    g = globals(); keep = g['half_frac']
    g['half_frac'] = lambda S, mid, _h=rt/2.0: _h
    try:    return fn()
    finally: g['half_frac'] = keep

def runs(dte, rt, take, maxhold=20):
    return with_spread(rt, lambda: [t for t in (trade(i, dte, maxhold=maxhold, take=take)
                                                for i in SIGS) if t])
def dct(dte, rt, take, maxhold=20):
    return with_spread(rt, lambda: {i: t for i in SIGS
                                    if (t := trade(i, dte, maxhold=maxhold, take=take))})
def ctl(dte, rt, take, maxhold=20):
    return with_spread(rt, lambda: [t for t in (trade(i, dte, maxhold=maxhold, take=take)
                                                for i in CTRL_S) if t])
def st(ts):
    p = [t['pct'] for t in ts]
    return dict(n=len(ts), mean=statistics.mean(p), med=statistics.median(p),
                win=sum(1 for x in p if x > 0)/len(p)*100,
                loss20=sum(1 for x in p if x <= -20)/len(p)*100,
                p10=statistics.quantiles(p, n=10)[0], worst=min(p),
                tp=sum(1 for t in ts if t['why'] == 'take_profit')/len(ts)*100,
                tstop=sum(1 for t in ts if t['why'] == 'time_stop')/len(ts)*100,
                held=statistics.mean(t['held'] for t in ts),
                usd=statistics.mean(t['pct']/100*t['cost'] for t in ts))

def paired(a, b):
    """a,b 为 {bar: trade} —— 同一入场 bar 配对, 返回 (n, 均值差, t, p)。"""
    k = sorted(set(a) & set(b))
    d = [a[i]['pct'] - b[i]['pct'] for i in k]
    if len(d) < 3: return (len(d), float('nan'), float('nan'), float('nan'))
    m, sd = statistics.mean(d), statistics.stdev(d)
    t = m/(sd/math.sqrt(len(d))) if sd > 0 else 0.0
    # 正态近似双尾 p (df=88, 与 10-23 的 tp-85-148.py 同一口径, 保证可比)
    p = 2*(1-0.5*(1+math.erf(abs(t)/math.sqrt(2))))
    return (len(d), m, t, min(1.0, max(0.0, p)))

W = 112
print("=" * W)
print("第 1 问 (纯算术, 不依赖回测): 名义 +20% 在三档上实际要求公允价涨多少?")
print("  触发 bid_t >= 入场ask × m;  bid = mid(1-h), ask = mid(1+h),  h = 点差/2")
print("  => 要求 mid_t/mid_0 >= m(1+h)/(1-h)。点差用 10-22+10-23 两日实测中位。")
print("=" * W)
print(f"{'档':>6s}{'实测点差':>12s}{'h':>8s}{'名义+20%':>11s}{'→公允价门槛':>14s}"
      f"{'窄日门槛':>11s}{'宽日门槛':>11s}")
print("-" * W)
for d in RUNGS:
    h = MEAS[d]/2
    eff = (BASE*(1+h)/(1-h)-1)*100
    lo = (BASE*(1+MEAS_LO[d]/2)/(1-MEAS_LO[d]/2)-1)*100
    hi = (BASE*(1+MEAS_HI[d]/2)/(1-MEAS_HI[d]/2)-1)*100
    print(f"{d:>5d}d{MEAS[d]*100:>11.2f}%{h*100:>7.2f}%{'+20.0%':>11s}"
          f"{eff:>13.1f}%{lo:>10.1f}%{hi:>10.1f}%")
print()
print(f"  330 档的公允价门槛比 85/148 档高约 "
      f"{((BASE*(1+MEAS[330]/2)/(1-MEAS[330]/2))-(BASE*(1+MEAS[85]/2)/(1-MEAS[85]/2)))*100:.1f}pp"
      f", 纯粹因为它点差宽 3.5 倍。")
print()
print("  ⚠️ **这个差异有两种读法, 选哪种决定了「统一 20%」是缺陷还是特性**:")
print("    (a) 若希望统一的是「标的要涨多少」 → 现行规则**不统一**, 330 档被悄悄设成了 ~+31%,")
print("        要修就得按下面的反推给三档不同名义值。")
print("    (b) 若希望统一的是「我实际能落袋多少」 → 现行规则**恰好是对的**: 触发条件")
print("        `bid >= 入场ask × 1.20` 的字面含义就是「现在卖掉能拿回已付成本的 1.20 倍」,")
print("        即**扣完点差后的真实净收益 +20%**, 三档完全一致。")
print("    => 我的判断是 **(b) 才是对本账户有意义的不变量** (钱是按净收益算的, 不是按公允价),")
print("       故现行规则不是 bug。但代价转移到了**到得了止盈线的概率**上 —— 见第 2 问的止盈率与均持。")
print()
print("  反推 (仅在采用读法 (a) 时才需要): 若要三档面对**同一个公允价门槛**")
print("  (以 85 档的 +20% 名义为基准),")
eff85 = BASE*(1+MEAS[85]/2)/(1-MEAS[85]/2)
for d in RUNGS:
    h = MEAS[d]/2
    m_eq = eff85*(1-h)/(1+h)
    print(f"    {d:>4d}d 的名义止盈应是 **+{(m_eq-1)*100:.1f}%** (现行 +20%)")
print()

print("=" * W)
print("第 2 问: 补上 330 档从未做过的 TP 网格 (实测点差 8.709%, 20 交易日时间止损)")
print("=" * W)
for d in RUNGS:
    print(f"\n-- DTE {d} · 实测点差 {MEAS[d]*100:.2f}% --")
    print(f"{'止盈':>8s}{'信号组':>10s}{'对照组':>10s}{'增量':>11s}{'中位':>10s}"
          f"{'胜率':>8s}{'止盈率':>8s}{'时止率':>8s}{'均持':>7s}{'亏>=20%':>9s}"
          f"{'p10':>9s}{'最差':>9s}{'每张$':>9s}")
    print("-" * W)
    for m in TPS:
        s = st(runs(d, MEAS[d], m)); c = st(ctl(d, MEAS[d], m))
        mark = "  ← 现行" if abs(m-BASE) < 1e-9 else ""
        print(f"{f'+{(m-1)*100:.1f}%':>8s}{s['mean']:>+9.2f}%{c['mean']:>+9.2f}%"
              f"{s['mean']-c['mean']:>+10.2f}pp{s['med']:>+9.2f}%{s['win']:>7.1f}%"
              f"{s['tp']:>7.1f}%{s['tstop']:>7.1f}%{s['held']:>6.1f}{s['loss20']:>8.1f}%"
              f"{s['p10']:>8.1f}%{s['worst']:>8.1f}%{s['usd']:>+8.0f}{mark}")
    base_d = dct(d, MEAS[d], BASE)
    print(f"\n  配对检验 vs 现行 +20% (同一批入场 bar 配对):")
    for m in TPS:
        if abs(m-BASE) < 1e-9: continue
        nn, mm, tt, pp = paired(dct(d, MEAS[d], m), base_d)
        flag = " * 显著" if pp < 0.05 else ""
        print(f"    +{(m-1)*100:>5.1f}% vs +20%: n={nn:<4d} 均值差 {mm:>+7.2f}%  "
              f"t={tt:>+6.2f}  p={pp:.3f}{flag}")

print()
print("=" * W)
print("第 3 问: 「点差中性」止盈 —— 把 330 档名义降到与 85 档同公允价门槛, 更好还是更坏?")
print("  即 330 用名义 +%s%% 替代 +20%%" % f"{(eff85*(1-MEAS[330]/2)/(1+MEAS[330]/2)-1)*100:.1f}")
print("=" * W)
m_neutral = eff85*(1-MEAS[330]/2)/(1+MEAS[330]/2)
a = dct(330, MEAS[330], m_neutral); b = dct(330, MEAS[330], BASE)
sa, sb = st(list(a.values())), st(list(b.values()))
print(f"{'方案':>26s}{'均值':>10s}{'中位':>10s}{'胜率':>8s}{'止盈率':>8s}{'均持':>7s}"
      f"{'亏>=20%':>9s}{'p10':>9s}{'每张$':>9s}")
print("-" * W)
for lab, s in ((f"点差中性 +{(m_neutral-1)*100:.1f}%", sa), ("现行统一 +20.0%", sb)):
    print(f"{lab:>26s}{s['mean']:>+9.2f}%{s['med']:>+9.2f}%{s['win']:>7.1f}%"
          f"{s['tp']:>7.1f}%{s['held']:>6.1f}{s['loss20']:>8.1f}%{s['p10']:>8.1f}%{s['usd']:>+8.0f}")
nn, mm, tt, pp = paired(a, b)
print(f"\n  配对: n={nn}  均值差 {mm:+.2f}%  t={tt:+.2f}  p={pp:.3f}"
      f"{'  * 显著' if pp < 0.05 else '  (不显著)'}")
print()
print("=" * W)
print("第 4 问: 各档的「最优」名义止盈真的随 DTE 漂移吗? (还是多重比较下的噪声?)")
print("=" * W)
print(f"{'档':>6s}{'按均值最优':>14s}{'按增量最优':>14s}{'按每张$最优':>14s}"
      f"{'+20% 的均值':>13s}{'最优-20% 差':>13s}{'该差 p 值':>11s}")
print("-" * W)
for d in RUNGS:
    rows = [(m, st(runs(d, MEAS[d], m)), st(ctl(d, MEAS[d], m))) for m in TPS]
    best_mean = max(rows, key=lambda r: r[1]['mean'])
    best_inc  = max(rows, key=lambda r: r[1]['mean']-r[2]['mean'])
    best_usd  = max(rows, key=lambda r: r[1]['usd'])
    cur = [r for r in rows if abs(r[0]-BASE) < 1e-9][0]
    nn, mm, tt, pp = paired(dct(d, MEAS[d], best_mean[0]), dct(d, MEAS[d], BASE))
    print(f"{d:>5d}d{f'+{(best_mean[0]-1)*100:.1f}%':>14s}{f'+{(best_inc[0]-1)*100:.1f}%':>14s}"
          f"{f'+{(best_usd[0]-1)*100:.1f}%':>14s}{cur[1]['mean']:>+12.2f}%"
          f"{mm:>+12.2f}%{pp:>11.3f}")
print()
print("=" * W)
print("口径局限 (必读)")
print("=" * W)
print("""1. 点差仅 **2 日样本/档** (10-22, 10-23), 且同一张合约当日就能出现 4.37%/7.26%/9.94% 三个值。
   第 1 问的算术结论对点差只要求「330 比 85/148 宽得多」这个定性事实, 对此很稳健;
   第 2/3 问的**具体数字**对点差敏感。
2. IV 由 KAPPA 单点反解, 期限结构是模型给的不是实测的 —— 跨 DTE 比较的最大不确定来源。
3. n=89 入场 / 10.4 年, 信号 8.4 次/年。配对检验在这个样本量下只能发现很大的差异;
   **多重比较**: 本脚本跑了 3 档 × 7 个对照 = 21 次检验, 期望有 ~1 次假阳性 p<0.05。
   单个 p<0.05 不构成改档依据 (10-23 已踩过这个坑: 12 次检验 2 次显著落在不同格)。
4. 止盈与时间止损的**二维交互未扫**。20 交易日固定。
5. 三档并持时现金约束的相互挤占未建模 (本脚本逐档独立评估)。""")
