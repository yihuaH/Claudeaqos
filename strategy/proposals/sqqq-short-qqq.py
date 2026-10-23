#!/usr/bin/env python3
"""SQQQ 作为**短期看跌 QQQ** 的载体: 信号来自 QQQ 超买, 不是 SQQQ 自己。**仅研究, 零订单。**

⛔ 不被 daily.py / session.py 调用 (红线2)。SQQQ 不在任何轨道标的池里。
回答用户 2026-10-23「SQQQ要看qqq涨了多少才买, sqqq短期的, 看跌qqq」。

⚠️ **与同日 leveraged-etf-rsi2.py 的关键区别 (那份错了, 这份是修正)**:
   那份把 RSI-2 直接跑在 **SQQQ 自己的** 收盘序列上 → `close > SMA200` 牛市过滤对一个结构性
   下跌的标的几乎永不成立 (9.3% 的天数), 10.8 年只出 0~6 笔。**信号源选错了。**
   本脚本的信号**全部来自 QQQ**, SQQQ 只是执行看跌观点的载体。

📐 **对照组为什么必须有**: SQQQ 买入持有 10.8 年 = **−100.0% (年化 −54.6%)**。
   任何做多 SQQQ 的策略都从这个深负漂移起跑, 故"均值为负"本身说明不了问题。
   每个变体都配「同样持有期、**任意一天**买入」的对照组, 看**增量 = 信号组 − 对照组**。

触发器三种形态 (覆盖「涨了多少才买」的不同读法):
   A 单日涨幅  QQQ 当日 ≥ +X%            (LEAP 轨道「跌≥2% 买call」的镜像)
   B N 日累计  QQQ 近 N 日累计 ≥ +X%
   C RSI-2 超买 QQQ rsi2 ≥ X             (现行入场 rsi2<10 的镜像)
趋势过滤三种: 无 / `QQQ close > SMA200` (牛市里空反弹) / `< SMA200` (熊市里空反弹, 字面镜像)
出场: 持满 N 个交易日无条件平 (「短期的」), 另测 mirror exit (QQQ rsi2 ≤ 35 提前平)。
未计成本; SQQQ 点差约 1bp, 对结论无影响。

用法: python3 strategy/proposals/sqqq-short-qqq.py
"""
import json, math, statistics

SP = '/tmp/claude-0/-home-user-Claudeaqos/3e6563b5-e5a0-5665-84d2-72b2406895d9/scratchpad'
RAW = json.load(open(f'{SP}/lev_bars.json'))['data']['results']
S = {x['symbol']: ([q['begins_at'][:10] for q in x['bars']],
                   [float(q['close_price']) for q in x['bars']]) for x in RAW}
QD, QC = S['QQQ']
_, XC = S['SQQQ']
N = len(QC)
assert len(XC) == N and S['SQQQ'][0] == QD, "QQQ/SQQQ 日期轴未对齐"


def wilder(c, p=2):
    o = [None] * len(c)
    g = sum(max(c[i] - c[i-1], 0.0) for i in range(1, p+1)) / p
    l = sum(max(c[i-1] - c[i], 0.0) for i in range(1, p+1)) / p
    o[p] = 100.0 if l == 0 else 100.0 - 100.0/(1.0 + g/l)
    for i in range(p+1, len(c)):
        d = c[i] - c[i-1]
        g = (g*(p-1) + max(d, 0.0))/p
        l = (l*(p-1) + max(-d, 0.0))/p
        o[i] = 100.0 if l == 0 else 100.0 - 100.0/(1.0 + g/l)
    return o


def sma(c, n):
    o = [None]*len(c); s = 0.0
    for i, v in enumerate(c):
        s += v
        if i >= n: s -= c[i-n]
        if i >= n-1: o[i] = s/n
    return o


QR2 = wilder(QC, 2)
Q200 = sma(QC, 200)
QRET = [None] + [(QC[i]/QC[i-1]-1)*100 for i in range(1, N)]


def trend_ok(i, mode):
    if mode == 'none': return True
    if Q200[i] is None: return False
    return (QC[i] > Q200[i]) if mode == 'bull' else (QC[i] < Q200[i])


def fire(i, kind, thr, look=1):
    if kind == 'A':
        return QRET[i] is not None and QRET[i] >= thr
    if kind == 'B':
        if i < look: return False
        return (QC[i]/QC[i-look]-1)*100 >= thr
    return QR2[i] is not None and QR2[i] >= thr


def trade(i, hold, mirror_exit):
    """i 日收盘买 SQQQ, 持满 hold 个交易日平; mirror_exit 时 QQQ rsi2<=35 提前平。"""
    for j in range(i+1, min(i+hold+1, N)):
        if mirror_exit and QR2[j] is not None and QR2[j] <= 35:
            return (XC[j]/XC[i]-1)*100, j-i
    j = min(i+hold, N-1)
    if j <= i: return None
    return (XC[j]/XC[i]-1)*100, j-i


def run(kind, thr, trend, hold, look=1, mirror_exit=False):
    sig = [i for i in range(1, N-1) if trend_ok(i, trend) and fire(i, kind, thr, look)]
    ctl = [i for i in range(1, N-1) if trend_ok(i, trend)]
    sp = [t for t in (trade(i, hold, mirror_exit) for i in sig) if t]
    cp = [t for t in (trade(i, hold, mirror_exit) for i in ctl) if t]
    if not sp: return None
    a = [x[0] for x in sp]; b = [x[0] for x in cp]
    # 顺序复利 (不重叠): 同一时刻只持一仓
    eq, last = 1.0, -1
    for i in sorted(sig):
        if i <= last: continue
        t = trade(i, hold, mirror_exit)
        if not t: continue
        eq *= 1 + t[0]/100.0
        last = i + t[1]
    m, mb = statistics.mean(a), (statistics.mean(b) if b else float('nan'))
    sd = statistics.stdev(a) if len(a) > 1 else 0.0
    t = (m-mb)/(sd/math.sqrt(len(a))) if sd > 0 else 0.0
    p = 2*(1-0.5*(1+math.erf(abs(t)/math.sqrt(2))))
    return dict(n=len(a), mean=m, med=statistics.median(a),
                win=sum(1 for x in a if x > 0)/len(a)*100,
                ctl=mb, inc=m-mb, p=p, eq=(eq-1)*100,
                best=max(a), worst=min(a),
                held=statistics.mean(x[1] for x in sp))


print("=" * 116)
print(f"样本 {QD[0]} ~ {QD[-1]}  ({N} 根, {N/252:.1f} 年)   信号源 = **QQQ**, 载体 = SQQQ")
print("SQQQ 买入持有基准: -100.0% (年化 -54.6%) ← 任何做多 SQQQ 的策略都从这个漂移起跑")
print("=" * 116)

print("\n【A】单日涨幅触发 (LEAP 轨道「跌≥2%买call」的镜像), 趋势过滤 = 无")
print(f"{'阈值':>8s}{'持有':>5s}{'笔数':>6s}{'胜率':>7s}{'信号均值':>10s}{'对照均值':>10s}{'增量':>10s}"
      f"{'p':>8s}{'最好':>9s}{'最差':>9s}{'顺序复利':>11s}")
print("-" * 116)
for thr in (1.0, 1.5, 2.0, 2.5):
    for hold in (1, 2, 3, 5):
        r = run('A', thr, 'none', hold)
        if r: print(f"{'+'+str(thr)+'%':>8s}{hold:>5d}{r['n']:>6d}{r['win']:>6.1f}%"
                    f"{r['mean']:>+9.2f}%{r['ctl']:>+9.2f}%{r['inc']:>+9.2f}pp{r['p']:>8.3f}"
                    f"{r['best']:>+8.1f}%{r['worst']:>+8.1f}%{r['eq']:>+10.1f}%")

print("\n【C】QQQ rsi2 超买触发 (现行 rsi2<10 入场的镜像), 趋势过滤 = 无")
print(f"{'rsi2≥':>8s}{'持有':>5s}{'笔数':>6s}{'胜率':>7s}{'信号均值':>10s}{'对照均值':>10s}{'增量':>10s}"
      f"{'p':>8s}{'最好':>9s}{'最差':>9s}{'顺序复利':>11s}")
print("-" * 116)
for thr in (90, 95, 98):
    for hold in (1, 2, 3, 5):
        r = run('C', thr, 'none', hold)
        if r: print(f"{thr:>8d}{hold:>5d}{r['n']:>6d}{r['win']:>6.1f}%"
                    f"{r['mean']:>+9.2f}%{r['ctl']:>+9.2f}%{r['inc']:>+9.2f}pp{r['p']:>8.3f}"
                    f"{r['best']:>+8.1f}%{r['worst']:>+8.1f}%{r['eq']:>+10.1f}%")

print("\n【B】QQQ 近 N 日累计涨幅触发, 趋势过滤 = 无")
print(f"{'窗口':>6s}{'阈值':>8s}{'持有':>5s}{'笔数':>6s}{'胜率':>7s}{'信号均值':>10s}{'对照均值':>10s}"
      f"{'增量':>10s}{'p':>8s}{'顺序复利':>11s}")
print("-" * 116)
for look, thr in ((5, 4.0), (5, 6.0), (10, 6.0), (10, 8.0), (20, 10.0), (20, 15.0)):
    for hold in (3, 5):
        r = run('B', thr, 'none', hold, look)
        if r: print(f"{look:>6d}{'+'+str(thr)+'%':>8s}{hold:>5d}{r['n']:>6d}{r['win']:>6.1f}%"
                    f"{r['mean']:>+9.2f}%{r['ctl']:>+9.2f}%{r['inc']:>+9.2f}pp{r['p']:>8.3f}"
                    f"{r['eq']:>+10.1f}%")

print("\n【趋势过滤的影响】取 A 档 +2% / 持有 3 日 与 C 档 rsi2>=95 / 持有 3 日")
print(f"{'触发':>14s}{'趋势过滤':>22s}{'笔数':>6s}{'胜率':>7s}{'信号均值':>10s}{'对照均值':>10s}{'增量':>10s}{'p':>8s}")
print("-" * 116)
for kind, thr, lbl in (('A', 2.0, '单日+2%'), ('C', 95, 'rsi2>=95')):
    for tm, tl in (('none', '无'), ('bull', 'QQQ>SMA200 牛市空反弹'), ('bear', 'QQQ<SMA200 熊市空反弹')):
        r = run(kind, thr, tm, 3)
        if r: print(f"{lbl:>14s}{tl:>22s}{r['n']:>6d}{r['win']:>6.1f}%{r['mean']:>+9.2f}%"
                    f"{r['ctl']:>+9.2f}%{r['inc']:>+9.2f}pp{r['p']:>8.3f}")
        else: print(f"{lbl:>14s}{tl:>22s}{'0 笔':>6s}")

print("\n【mirror exit: QQQ rsi2<=35 提前平 (不等持满)】A 档 +2%")
print(f"{'持有上限':>8s}{'提前平':>8s}{'笔数':>6s}{'均持':>6s}{'胜率':>7s}{'信号均值':>10s}{'增量':>10s}{'p':>8s}")
print("-" * 116)
for hold in (3, 5, 10):
    for me in (False, True):
        r = run('A', 2.0, 'none', hold, mirror_exit=me)
        if r: print(f"{hold:>8d}{('开' if me else '关'):>8s}{r['n']:>6d}{r['held']:>6.1f}"
                    f"{r['win']:>6.1f}%{r['mean']:>+9.2f}%{r['inc']:>+9.2f}pp{r['p']:>8.3f}")

print("\n" + "=" * 116)
print("【稳健性 1】逐年分解 —— 收益是不是全来自 2020-03 和 2022?")
print("  这是本研究最要紧的一道检验: 做多 SQQQ 的策略极易把全部收益集中在两段崩盘里。")
print("=" * 116)


def seq(kind, thr, hold, trend='none', look=1):
    """顺序不重叠复利, 返回 (逐笔 [(date,pct)], 权益曲线, MDD%)。"""
    sig = [i for i in range(1, N-1) if trend_ok(i, trend) and fire(i, kind, thr, look)]
    out, eq, last, curve = [], 1.0, -1, []
    for i in sorted(sig):
        if i <= last: continue
        t = trade(i, hold, False)
        if not t: continue
        eq *= 1 + t[0]/100.0
        out.append((QD[i], t[0]))
        curve.append(eq)
        last = i + t[1]
    peak = dd = 0.0
    for v in curve:
        peak = max(peak, v); dd = min(dd, v/peak - 1)
    return out, eq, dd*100


for lbl, kind, thr, hold in (('A 单日+2.0% / 持1日', 'A', 2.0, 1),
                             ('A 单日+2.5% / 持1日', 'A', 2.5, 1),
                             ('C rsi2>=95 / 持1日',  'C', 95, 1),
                             ('C rsi2>=95 / 持3日',  'C', 95, 3)):
    tr, eq, mdd = seq(kind, thr, hold)
    by = {}
    for d, p in tr:
        by.setdefault(d[:4], []).append(p)
    print(f"\n-- {lbl} --  共 {len(tr)} 笔, 顺序复利 {(eq-1)*100:+.1f}%, "
          f"MDD {mdd:+.1f}%, 在市 {len(tr)*hold}/{N} 天 = {len(tr)*hold/N*100:.1f}%")
    yrs = sorted(by)
    print('   ' + ''.join(f'{y[2:]:>7s}' for y in yrs))
    print('   ' + ''.join(f'{sum(by[y]):>+6.1f}%' for y in yrs) + '   ← 该年累加 (非复利)')
    print('   ' + ''.join(f'{len(by[y]):>7d}' for y in yrs) + '   ← 笔数')
    pos = sum(1 for y in yrs if sum(by[y]) > 0)
    tot = sum(sum(v) for v in by.values())
    top = max(by, key=lambda y: sum(by[y]))
    print(f"   赚钱年份 {pos}/{len(yrs)} | 全样本累加 {tot:+.1f}% | "
          f"最赚的一年 {top} 贡献 {sum(by[top]):+.1f}% = 全部的 {sum(by[top])/tot*100:.0f}%")

print("\n" + "=" * 116)
print("【稳健性 2】剔除 2020 与 2022 两年后还剩多少")
print("=" * 116)
print(f"{'情形':<22s}{'全样本累加':>12s}{'剔 2020':>11s}{'剔 2022':>11s}{'两年都剔':>11s}{'剩余年份数':>11s}")
print("-" * 116)
for lbl, kind, thr, hold in (('A +2.0% / 持1日', 'A', 2.0, 1), ('A +2.5% / 持1日', 'A', 2.5, 1),
                             ('C rsi2>=95 / 持1日', 'C', 95, 1), ('C rsi2>=95 / 持3日', 'C', 95, 3)):
    tr, _, _ = seq(kind, thr, hold)
    f = lambda ex: sum(p for d, p in tr if d[:4] not in ex)
    ys = {d[:4] for d, _ in tr}
    print(f"{lbl:<22s}{f(set()):>+11.1f}%{f({'2020'}):>+10.1f}%{f({'2022'}):>+10.1f}%"
          f"{f({'2020','2022'}):>+10.1f}%{len(ys - {'2020','2022'}):>11d}")
