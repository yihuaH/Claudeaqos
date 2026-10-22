#!/usr/bin/env python3
"""QQQ LEAP **到期 (DTE) 扫描** —— 月期权 / 半年 / 一年 / 更长。**仅研究用, 不产生任何订单**。

⛔ 不被 daily.py / session.py 调用 (红线2: 订单只能来自确定性引擎)。
回答用户 2026-10-22「再看一下 半年期权 或者月期权」。

⚠️ **为什么这个扫描必要**: DTE 是本策略唯一**从未被回测过**的维度 ——
   §1 把「到期 约一年」列为*用户给定的要素*, §4 只是执行它; §10 第 6 项明写「未单独回测」。
   delta 有 §16 扫描、止盈有 §18、时间止损有 §12.5, 只有 DTE 是空的。

口径继承 qqq-leap-concurrency.py: delta 0.30 / 止盈 +20% / 20 交易日时间止损 /
§16.1 点差拟合 / 入场 = 跌 ≥2% + 牛市过滤。**只改 DTE_TARGET。**

用法: python3 strategy/proposals/qqq-leap-dte-sweep.py
"""
import json, math, statistics
src = open('strategy/proposals/qqq-leap-concurrency.py').read().split("SIG = signals()")[0]
exec(src)

def trade(i, dte, maxhold=MAXHOLD, take=TAKE):
    """从 bar i 开仓一张 dte 日期权, 跑到止盈/时间止损/到期。"""
    S0, r0, sig0 = closes[i], rate(dates[i]), iv(i)
    T0 = dte / 365.0
    pk = pick(S0, T0, r0, sig0)
    if pk is None: return None
    K = pk[0]
    mid0 = bs_call(S0, K, T0, r0, Q_DIV, sig0)
    if mid0 <= 0.01: return None
    entry = mid0 * (1 + half_frac(S0, mid0))
    for j in range(i + 1, n):
        rem = dte - caldays(dates[i], dates[j])
        Tj = max(0.0, rem / 365.0)
        sj = iv(j) or sig0
        midj = bs_call(closes[j], K, Tj, rate(dates[j]), Q_DIV, sj)
        bid = midj * (1 - half_frac(closes[j], midj)) if Tj > 0 else midj  # 到期按内在价值结算, 无点差
        if bid >= entry * take:
            return dict(cost=entry*100, pct=(bid/entry-1)*100, held=j-i, why='take_profit',
                        delta=pk[1], K=K, entry_mid=mid0, half=half_frac(S0, mid0))
        if rem <= 0:
            return dict(cost=entry*100, pct=(bid/entry-1)*100, held=j-i, why='expired',
                        delta=pk[1], K=K, entry_mid=mid0, half=half_frac(S0, mid0))
        if j - i >= maxhold:
            return dict(cost=entry*100, pct=(bid/entry-1)*100, held=j-i, why='time_stop',
                        delta=pk[1], K=K, entry_mid=mid0, half=half_frac(S0, mid0))
    return None

def sig_signal():
    return [i for i in range(1, n) if sma200[i] is not None and iv(i) is not None
            and closes[i] > sma200[i] and (closes[i]/closes[i-1]-1)*100 <= -2.0]
def sig_control():
    return [i for i in range(1, n) if sma200[i] is not None and iv(i) is not None
            and closes[i] > sma200[i]]
SIGS, CTRL = sig_signal(), sig_control()

def stats(ts):
    p=[t['pct'] for t in ts]
    return dict(n=len(ts), win=sum(1 for x in p if x>0)/len(p)*100,
                mean=statistics.mean(p), med=statistics.median(p),
                p10=statistics.quantiles(p,n=10)[0], worst=min(p),
                held=statistics.mean(t['held'] for t in ts),
                cost=statistics.mean(t['cost'] for t in ts),
                half=statistics.mean(t['half'] for t in ts)*100,
                loss20=sum(1 for x in p if x<=-20)/len(p)*100,
                tp=sum(1 for t in ts if t['why']=='take_profit')/len(ts)*100,
                exp=sum(1 for t in ts if t['why']=='expired')/len(ts)*100)

DTES=[30,45,60,91,120,182,273,365,547,730]
print("=== 信号组 (跌≥2% + 牛市过滤), delta 0.30 · 止盈+20% · 20 交易日时间止损 ===")
print(f"{'DTE':>5s}{'n':>5s}{'每张$':>8s}{'半点差':>8s}{'胜率':>7s}{'均值':>8s}{'中位':>8s}"
      f"{'p10':>8s}{'最差':>8s}{'亏≥20%':>8s}{'止盈率':>7s}{'到期率':>7s}{'均持':>6s}")
res={}
for dte in DTES:
    ts=[t for t in (trade(i,dte) for i in SIGS) if t]
    if not ts: continue
    s=stats(ts); res[dte]=s
    print(f"{dte:>5d}{s['n']:>5d}{s['cost']:>8.0f}{s['half']:>7.2f}%{s['win']:>6.1f}%"
          f"{s['mean']:>+7.2f}%{s['med']:>+7.2f}%{s['p10']:>+7.1f}%{s['worst']:>+7.1f}%"
          f"{s['loss20']:>7.1f}%{s['tp']:>6.1f}%{s['exp']:>6.1f}%{s['held']:>6.1f}")

print("\n=== 对照组 (牛市中每天买, 无跌幅要求) + 信号增量 ===")
print(f"{'DTE':>5s}{'信号均值':>10s}{'对照均值':>10s}{'增量(pp)':>10s}")
for dte in DTES:
    if dte not in res: continue
    ct=[t for t in (trade(i,dte) for i in CTRL) if t]
    if not ct: continue
    cm=statistics.mean(t['pct'] for t in ct)
    print(f"{dte:>5d}{res[dte]['mean']:>+9.2f}%{cm:>+9.2f}%{res[dte]['mean']-cm:>+9.2f}")

# ---------- 配对显著性 (同一批 89 个入场点, 必须配对比) ----------
print("\n=== 配对检验 vs 现行 365 日 (同入场点逐笔配对) ===")
base={i:t for i in SIGS if (t:=trade(i,365))}
print(f"{'DTE':>5s}{'配对n':>7s}{'均值差(pp)':>12s}{'95%CI':>22s}{'胜该档比例':>11s}{'结论':>10s}")
for dte in DTES:
    if dte==365: continue
    cur={i:t for i in SIGS if (t:=trade(i,dte))}
    both=sorted(set(base)&set(cur))
    d=[cur[i]['pct']-base[i]['pct'] for i in both]
    m=statistics.mean(d); se=statistics.stdev(d)/math.sqrt(len(d))
    lo,hi=m-1.96*se, m+1.96*se
    wins=sum(1 for x in d if x>0)/len(d)*100
    sig='**显著**' if lo*hi>0 else '不显著'
    print(f"{dte:>5d}{len(both):>7d}{m:>+11.2f}  [{lo:>+7.2f},{hi:>+7.2f}]{wins:>10.1f}%{sig:>10s}")

# ---------- 敏感性: 用 2026-10-22 实测点差替换 §16.1 拟合 ----------
# ⚠️ n=5 单时点、delta 不齐 (0.27~0.43), **不是新的标定模型**, 只做方向性敏感性检验。
# 实测往返点差: 29d 0.74% · 85d 1.12% · 148d 0.57% · 238d 2.43% · 330d 4.37%
REAL = [(29,0.74),(85,1.12),(148,0.57),(238,2.43),(330,4.37)]
def real_half(dte):
    """按 DTE 线性插值实测往返点差, 取一半。两端外推用端点值 (不外插)。"""
    xs=[x for x,_ in REAL]
    if dte<=xs[0]:  full=REAL[0][1]
    elif dte>=xs[-1]: full=REAL[-1][1]
    else:
        for (x0,y0),(x1,y1) in zip(REAL,REAL[1:]):
            if x0<=dte<=x1: full=y0+(y1-y0)*(dte-x0)/(x1-x0); break
    return full/200.0

import builtins
_orig_half = half_frac
def trade_real(i, dte):
    global half_frac
    hf = real_half(dte)
    half_frac = lambda S, mid, _h=hf: _h       # 固定为该 DTE 的实测半点差
    try:    return trade(i, dte)
    finally: half_frac = _orig_half

print("\n=== 敏感性: 换成 2026-10-22 实测点差后重跑 (⚠️ n=5 单时点, 方向性参考) ===")
print(f"{'DTE':>5s}{'实测往返':>9s}{'原模型往返':>11s}{'均值':>9s}{'(原均值)':>10s}{'差':>8s}"
      f"{'胜率':>7s}{'p10':>8s}{'最差':>8s}{'亏≥20%':>8s}")
for dte in DTES:
    ts=[t for t in (trade_real(i,dte) for i in SIGS) if t]
    if not ts: continue
    s=stats(ts); o=res.get(dte)
    print(f"{dte:>5d}{real_half(dte)*200:>8.2f}%{o['half']*2:>10.2f}%{s['mean']:>+8.2f}%"
          f"{o['mean']:>+9.2f}%{s['mean']-o['mean']:>+7.2f}{s['win']:>6.1f}%"
          f"{s['p10']:>+7.1f}%{s['worst']:>+7.1f}%{s['loss20']:>7.1f}%")

print("\n=== 实测点差下的配对检验 vs 现行 365 日 ===")
base_r={i:t for i in SIGS if (t:=trade_real(i,365))}
print(f"{'DTE':>5s}{'配对n':>7s}{'均值差(pp)':>12s}{'95%CI':>22s}{'胜该档':>8s}{'结论':>10s}")
for dte in DTES:
    if dte==365: continue
    cur={i:t for i in SIGS if (t:=trade_real(i,dte))}
    both=sorted(set(base_r)&set(cur))
    d=[cur[i]['pct']-base_r[i]['pct'] for i in both]
    m=statistics.mean(d); se=statistics.stdev(d)/math.sqrt(len(d))
    lo,hi=m-1.96*se,m+1.96*se
    print(f"{dte:>5d}{len(both):>7d}{m:>+11.2f}  [{lo:>+7.2f},{hi:>+7.2f}]"
          f"{sum(1 for x in d if x>0)/len(d)*100:>7.1f}%{'**显著**' if lo*hi>0 else '不显著':>10s}")
