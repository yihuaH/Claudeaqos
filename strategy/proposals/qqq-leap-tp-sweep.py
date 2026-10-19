#!/usr/bin/env python3
"""QQQ LEAP 止盈档扫描 (+15%~+50%) + 对照组 + 配对显著性 —— **仅研究用, 不产生任何订单**。

⛔ 不被 daily.py / session.py 调用。回答 qqq-leap-call.md §18 (用户 2026-10-19 要 +20% vs +30%)。
口径继承 qqq-leap-concurrency.py (delta 0.30 / 20 日时间止损 / §16.1 点差拟合 / 牛市过滤)。
用法: python3 strategy/proposals/qqq-leap-tp-sweep.py
"""
# 口径继承 qqq-leap-concurrency.py (delta 0.30 / 20 日时间止损 / §16.1 点差拟合 / 牛市过滤)。
import json, math, statistics
src = open('strategy/proposals/qqq-leap-concurrency.py').read().split("SIG = signals()")[0]
exec(src)

TODAYQ = None
def trade(i, take):
    """从 bar i 开仓, 返回 dict。take = 止盈倍数。"""
    S0, r0, sig0 = closes[i], rate(dates[i]), iv(i)
    T0 = DTE_TARGET / 365.0
    pk = pick(S0, T0, r0, sig0)
    if pk is None: return None
    K = pk[0]
    mid0 = bs_call(S0, K, T0, r0, Q_DIV, sig0)
    if mid0 <= 0.01: return None
    entry = mid0 * (1 + half_frac(S0, mid0))
    for j in range(i + 1, n):
        Tj = max(0.0, (DTE_TARGET - caldays(dates[i], dates[j])) / 365.0)
        sj = iv(j) or sig0
        midj = bs_call(closes[j], K, Tj, rate(dates[j]), Q_DIV, sj)
        bid = midj * (1 - half_frac(closes[j], midj))
        if bid >= entry * take:
            return dict(bar=i, cost=entry*100, val=bid*100, pct=(bid/entry-1)*100,
                        held=j-i, why='take_profit', delta=pk[1], K=K)
        if j - i >= MAXHOLD:
            return dict(bar=i, cost=entry*100, val=bid*100, pct=(bid/entry-1)*100,
                        held=j-i, why='time_stop', delta=pk[1], K=K)
    return None

def sig_signal():   # 跌幅 ≥2% + 牛市过滤
    return [i for i in range(1, n) if sma200[i] is not None and iv(i) is not None
            and closes[i] > sma200[i] and (closes[i]/closes[i-1]-1)*100 <= -2.0]
def sig_control():  # 对照: 牛市中每天买, 无跌幅要求
    return [i for i in range(1, n) if sma200[i] is not None and iv(i) is not None
            and closes[i] > sma200[i]]

SIGS, CTRL = sig_signal(), sig_control()

def stats(ts):
    p = [t['pct'] for t in ts]
    tp = [t for t in ts if t['why'] == 'take_profit']
    return dict(n=len(ts), win=sum(1 for x in p if x > 0)/len(p)*100,
                mean=statistics.mean(p), med=statistics.median(p),
                p10=statistics.quantiles(p, n=10)[0], worst=min(p), best=max(p),
                tp_rate=len(tp)/len(ts)*100,
                held=statistics.mean([t['held'] for t in ts]),
                loss20=sum(1 for x in p if x <= -20)/len(p)*100)

def portfolio(ts):
    """不设并持上限 (§17 结论), 逐日推进。"""
    pre = {t['bar']: t for t in ts}
    open_p, dep, tot, curve, peakc, peakcap = [], 0.0, 0.0, [], 0, 0.0
    for i in range(n):
        for q in [q for q in open_p if q['bar'] + q['held'] == i]:
            open_p.remove(q); dep -= q['cost']; tot += q['val'] - q['cost']
        if i in pre:
            q = pre[i]; open_p.append(q); dep += q['cost']
            peakc = max(peakc, len(open_p)); peakcap = max(peakcap, dep)
        curve.append(tot)
    mdd, pk = 0.0, curve[0]
    for v in curve: pk = max(pk, v); mdd = min(mdd, v - pk)
    return dict(total=tot, peakcap=peakcap, peakc=peakc, mdd=mdd)

print('样本 %s → %s (%.2f 年) · 信号 %d 个 (%.1f/年) · 对照 %d 个'
      % (dates[0], dates[-1], n/252.0, len(SIGS), len(SIGS)/(n/252.0), len(CTRL)))
print('配置: QQQ · 跌幅 ≥2% · 牛市过滤 · ~1 年期 · delta 0.30 · 20 日时间止损 · 不设并持上限')
print('点差: §16.1 实测拟合 (绝对价差 ≈ 0.1477%×现货 + 0.953%×权利金, n=3)\n')

CACHE = {}
def runset(idxs, take, tag):
    key = (tag, take)
    if key not in CACHE:
        CACHE[key] = [t for t in (trade(i, take) for i in idxs) if t]
    return CACHE[key]

print('=' * 104)
print('%-8s %4s %7s %9s %9s %9s %9s %8s %8s %8s %9s' % (
    '止盈', 'n', '胜率', '均值', '中位', 'p10', '最差', '亏≥20%', '止盈率', '持有日', '对照均值'))
print('=' * 104)
rows = {}
for take, lbl in [(1.15,'+15%'), (1.20,'+20%'), (1.25,'+25%'), (1.30,'+30%'), (1.35,'+35%'), (1.50,'+50%')]:
    s = runset(SIGS, take, 'sig'); c = runset(CTRL, take, 'ctrl')
    ss, cs = stats(s), stats(c)
    rows[lbl] = (ss, cs, s)
    print('%-8s %4d %6.1f%% %+8.2f%% %+8.2f%% %+8.1f%% %+8.1f%% %7.1f%% %7.1f%% %8.1f %+8.2f%%' % (
        lbl, ss['n'], ss['win'], ss['mean'], ss['med'], ss['p10'], ss['worst'],
        ss['loss20'], ss['tp_rate'], ss['held'], cs['mean']))

print('\n' + '=' * 104)
print('🎯 信号增量 (= 信号均值 − 对照均值; §12.7 说这是本回测最该采信的输出)')
print('=' * 104)
print('%-8s %10s %10s %12s' % ('止盈', '信号均值', '对照均值', '增量'))
for lbl, (ss, cs, _) in rows.items():
    print('%-8s %+9.2f%% %+9.2f%% %+11.2f pp' % (lbl, ss['mean'], cs['mean'], ss['mean']-cs['mean']))

print('\n' + '=' * 104)
print('组合层 (不设并持上限, 同一账户逐日推进)')
print('=' * 104)
print('%-8s %11s %11s %8s %10s %10s' % ('止盈', '总盈亏$', '峰值占资$', '峰值张', '倍数', '回撤$'))
for lbl, (_, _, s) in rows.items():
    pf = portfolio(s)
    print('%-8s %+11.0f %11.0f %8d %9.2fx %+10.0f' % (
        lbl, pf['total'], pf['peakcap'], pf['peakc'], pf['total']/pf['peakcap'], pf['mdd']))

print('\n' + '=' * 104)
print('+20% vs +30% 逐年 (信号组)')
print('=' * 104)
by = {}
for lbl in ('+20%','+30%'):
    for t in rows[lbl][2]:
        by.setdefault(dates[t['bar']][:4], {}).setdefault(lbl, []).append(t)
print('%-6s %5s %12s %10s %5s %12s %10s' % ('年','笔数','+20%盈亏$','+20%均值','笔数','+30%盈亏$','+30%均值'))
for y in sorted(by):
    g = by[y]
    def f(l):
        v = g.get(l, [])
        return (len(v), sum(x['val']-x['cost'] for x in v),
                statistics.mean([x['pct'] for x in v]) if v else 0.0)
    a, b = f('+20%'), f('+30%')
    print('%-6s %5d %+12.0f %+9.1f%% %5d %+12.0f %+9.1f%%' % (y, a[0], a[1], a[2], b[0], b[1], b[2]))
