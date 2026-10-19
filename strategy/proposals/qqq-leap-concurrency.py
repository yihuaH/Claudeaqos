#!/usr/bin/env python3
"""QQQ LEAP 并持/连续触发策略回测 (组合层) —— **仅研究用, 不是引擎, 不产生任何订单**。

⛔ 本文件**不被 daily.py / session.py 调用**, 放在 strategy/proposals/ 下即表示它不在交易路径上。
   红线2: 订单只能来自确定性引擎。本脚本只回答 qqq-leap-call.md §10 的待定项 A
   (连续触发怎么处置), 把"逐笔独立统计"升级为"单一账户的组合层轨迹"。

与 qqq-leap-backtest.py 的区别: 那个脚本把 90 个信号当 90 个独立样本算分布 (§16);
本脚本把它们放进**同一个账户**, 按并持上限逐日推进, 因此能回答:
  - 不同并持策略各自取到多少笔、跳过多少笔
  - 被跳过的那批是好的还是坏的 (节流有没有砍掉 alpha)
  - 峰值同时持仓几张、峰值占用多少资金 (= 这个策略到底要多少钱)
  - 按"峰值占用资金"算的回报 (这才是跨策略可比的口径)

口径逐条照抄 qqq-leap-call.md §7 (出场) 与 §16 (delta/点差):
  入场 = 当日跌幅 ≥2% 且 close > SMA200 (牛市过滤)
  合约 = ~1 年期, $5 行权价网格, delta 最接近 0.30
  点差 = §16.1 实测拟合 绝对价差 ≈ 0.1477%×现货 + 0.953%×权利金 (n=3)
  出场 = bid ≥ 入场 ask × 1.20  或  满 20 个交易日 (按 bid 平)

用法: python3 strategy/proposals/qqq-leap-concurrency.py
"""
import json, math, statistics
from datetime import date as _D
def _d(x): return _D.fromisoformat(x)
def caldays(a, b): return (_d(b) - _d(a)).days

SP = '/tmp/claude-0/-home-user-Claudeaqos/3e6563b5-e5a0-5665-84d2-72b2406895d9/scratchpad'

def _nd(x): return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))
def bs_call(S, K, T, r, q, sig):
    if T <= 0: return max(S - K, 0.0)
    if sig <= 0: return max(S * math.exp(-q * T) - K * math.exp(-r * T), 0.0)
    v = sig * math.sqrt(T)
    d1 = (math.log(S / K) + (r - q + 0.5 * sig * sig) * T) / v
    return S * math.exp(-q * T) * _nd(d1) - K * math.exp(-r * T) * _nd(d1 - v)
def bs_delta(S, K, T, r, q, sig):
    if T <= 0: return 1.0 if S > K else 0.0
    v = sig * math.sqrt(T)
    return math.exp(-q * T) * _nd((math.log(S / K) + (r - q + 0.5 * sig * sig) * T) / v)

bars = json.load(open(SP + '/qqq_long.json'))['data']['results'][0]['bars']
dates = [b['begins_at'][:10] for b in bars]
closes = [float(b['close_price']) for b in bars]
n = len(closes)

sma200 = [None] * n
for i in range(199, n): sma200[i] = sum(closes[i - 199:i + 1]) / 200.0
logret = [None] + [math.log(closes[i] / closes[i - 1]) for i in range(1, n)]
def rv(i, w):
    if i < w: return None
    return statistics.stdev(logret[i - w + 1:i + 1]) * math.sqrt(252)
rv20 = [rv(i, 20) for i in range(n)]
rv60 = [rv(i, 60) for i in range(n)]
KAPPA = 1.280                      # 由"模型今日 IV = 实测 LEAP IV 23.45%"单点反解 (§12.1)
def iv(i):
    if rv20[i] is None or rv60[i] is None: return None
    return max(0.08, min(0.80, KAPPA * (0.5 * rv20[i] + 0.5 * rv60[i])))
RATE = {2016: 0.005, 2017: 0.010, 2018: 0.020, 2019: 0.023, 2020: 0.004,
        2021: 0.002, 2022: 0.020, 2023: 0.050, 2024: 0.050, 2025: 0.043, 2026: 0.040}
def rate(ds): return RATE[int(ds[:4])]
Q_DIV, DTE_TARGET, DELTA_TGT, TAKE, MAXHOLD = 0.005, 365, 0.30, 1.20, 20

# §16.1 实测点差拟合 (n=3): 绝对价差 ≈ 0.1477%×现货 + 0.953%×权利金
def half_frac(S, mid):
    if mid <= 0: return 0.5
    return min(0.45, (0.001477 * S + 0.00953 * mid) / 2.0 / mid)

def pick(S, T, r, sig):
    best = None
    k = int(S / 5) * 5 - 200
    while k < S * 1.8:
        if k > 0:
            dl = bs_delta(S, k, T, r, Q_DIV, sig)
            if best is None or abs(dl - DELTA_TGT) < abs(best[1] - DELTA_TGT): best = (k, dl)
        k += 5
    return best

def signals():
    out = []
    for i in range(1, n):
        if sma200[i] is None or iv(i) is None: continue
        if closes[i] <= sma200[i]: continue                 # 牛市过滤
        if (closes[i] / closes[i - 1] - 1) * 100 > -2.0: continue
        out.append(i)
    return out

def trade_from(i):
    """从 bar i 开仓, 返回 (entry_cost, exit_value, exit_bar, reason)。单张 = ×100。"""
    S0, r0, sig0 = closes[i], rate(dates[i]), iv(i)
    T0 = DTE_TARGET / 365.0
    pk = pick(S0, T0, r0, sig0)
    if pk is None: return None
    K = pk[0]
    mid0 = bs_call(S0, K, T0, r0, Q_DIV, sig0)
    if mid0 <= 0.01: return None
    entry = mid0 * (1 + half_frac(S0, mid0))                # 按 ask 买
    for j in range(i + 1, n):
        Tj = max(0.0, (DTE_TARGET - caldays(dates[i], dates[j])) / 365.0)
        sj = iv(j) or sig0
        midj = bs_call(closes[j], K, Tj, rate(dates[j]), Q_DIV, sj)
        bid = midj * (1 - half_frac(closes[j], midj))
        if bid >= entry * TAKE: return (entry * 100, bid * 100, j, 'take_profit')
        if j - i >= MAXHOLD:    return (entry * 100, bid * 100, j, 'time_stop')
    return None                                              # 数据末端未结, 丢弃

SIG = signals()
PRE = {}
for i in SIG:
    t = trade_from(i)
    if t: PRE[i] = t

def simulate(max_conc):
    """max_conc=None 表示不限并持 (= 每次触发都开)。资金不设约束 (用户 2026-10-19)。"""
    open_pos, taken, skipped = [], [], []
    peak_conc, peak_cap = 0, 0.0
    deployed, pnl_cum, curve = 0.0, 0.0, []
    for i in range(n):
        for p in [p for p in open_pos if p['exit_bar'] == i]:
            open_pos.remove(p); deployed -= p['cost']; pnl_cum += p['pnl']
        if i in PRE:
            if max_conc is not None and len(open_pos) >= max_conc:
                skipped.append(i)
            else:
                cost, val, xb, why = PRE[i]
                p = dict(cost=cost, pnl=val - cost, exit_bar=xb, why=why,
                         pct=(val / cost - 1) * 100, bar=i)
                open_pos.append(p); taken.append(p)
                deployed += cost
                peak_conc = max(peak_conc, len(open_pos)); peak_cap = max(peak_cap, deployed)
        curve.append(pnl_cum)
    mdd = 0.0; pk = curve[0]
    for v in curve:
        pk = max(pk, v); mdd = min(mdd, v - pk)
    return dict(taken=taken, skipped=skipped, peak_conc=peak_conc, peak_cap=peak_cap,
                total=pnl_cum, mdd=mdd)

print('样本 %s → %s (%d bars, %.2f 年)' % (dates[0], dates[-1], n, n / 252.0))
print('牛市过滤 + 跌幅 >=2%% 信号 %d 个 (%.1f/年); 可完整结算 %d 个'
      % (len(SIG), len(SIG) / (n / 252.0), len(PRE)))
_i=n-1
_pk=pick(closes[_i], DTE_TARGET/365.0, rate(dates[_i]), iv(_i))
_mid=bs_call(closes[_i], _pk[0], DTE_TARGET/365.0, rate(dates[_i]), Q_DIV, iv(_i))
print('末日 (%s) 单张参考: 现货 %.2f, K=%d, delta %.3f, ask $%.0f/张\n'
      % (dates[_i], closes[_i], _pk[0], _pk[1], _mid*(1+half_frac(closes[_i],_mid))*100))

print('%-26s %5s %5s %7s %8s %9s %9s %8s %8s' % (
    '并持策略', '取到', '跳过', '胜率', '均值%', '总盈亏$', '峰值占资$', '峰值张', '回撤$'))
print('-' * 96)
rows = {}
for label, mc in [('(i)  同时只持 1 张', 1), ('(iii) 上限 2 张', 2),
                  ('(iii) 上限 3 张', 3), ('(iii) 上限 5 张', 5),
                  ('(ii)  不限 / 每次都开', None)]:
    R = simulate(mc)
    t = R['taken']; p = [x['pct'] for x in t]
    rows[label] = R
    print('%-26s %5d %5d %6.1f%% %+8.2f %+9.0f %9.0f %8d %9.0f' % (
        label, len(t), len(R['skipped']), sum(1 for x in p if x > 0) / len(p) * 100,
        statistics.mean(p), R['total'], R['peak_cap'], R['peak_conc'], R['mdd']))

print('\n== 关键比较: 按「峰值占用资金」算的回报 (跨策略唯一可比的口径) ==')
yrs = n / 252.0
print('%-26s %11s %11s %9s %11s' % ('并持策略', '峰值占资$', '总盈亏$', '倍数', '年化%'))
print('-' * 74)
for label, R in rows.items():
    mult = R['total'] / R['peak_cap']
    ann = ((1 + mult) ** (1 / yrs) - 1) * 100
    print('%-26s %11.0f %+11.0f %8.2fx %+10.1f%%' % (label, R['peak_cap'], R['total'], mult, ann))

print('\n== 节流有没有砍掉 alpha: (i) 取到 vs 跳过的那批 ==')
R1 = rows['(i)  同时只持 1 张']
tk = [x['pct'] for x in R1['taken']]
sk = [PRE[i] for i in R1['skipped']]
skp = [(v / c - 1) * 100 for c, v, _, _ in sk]
print('(i) 取到 n=%d  均值 %+.2f%%  中位 %+.2f%%  胜率 %.1f%%' % (
    len(tk), statistics.mean(tk), statistics.median(tk), sum(1 for x in tk if x > 0) / len(tk) * 100))
if skp:
    print('(i) 跳过 n=%d  均值 %+.2f%%  中位 %+.2f%%  胜率 %.1f%%' % (
        len(skp), statistics.mean(skp), statistics.median(skp),
        sum(1 for x in skp if x > 0) / len(skp) * 100))
    print('=> 被节流掉的那批均值%s取到的那批 %.2f pp'
          % ('高于' if statistics.mean(skp) > statistics.mean(tk) else '低于',
             abs(statistics.mean(skp) - statistics.mean(tk))))

print('\n== 信号聚集程度: 任意 20 交易日窗口内的触发次数分布 ==')
cnt = {}
for i in SIG:
    c = sum(1 for j in SIG if i <= j < i + MAXHOLD)
    cnt[c] = cnt.get(c, 0) + 1
for c in sorted(cnt): print('  同窗口 %d 次: %d 个信号' % (c, cnt[c]))

print('\n== 逐年 (不限并持) ==')
by = {}
for x in rows['(ii)  不限 / 每次都开']['taken']:
    y = dates[x['bar']][:4]; by.setdefault(y, []).append(x)
print('%-6s %4s %9s %8s' % ('年', '笔数', '盈亏$', '均值%'))
for y in sorted(by):
    g = by[y]
    print('%-6s %4d %+9.0f %+7.1f%%' % (y, len(g), sum(x['pnl'] for x in g),
                                        statistics.mean([x['pct'] for x in g])))
