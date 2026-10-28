#!/usr/bin/env python3
"""「卖出 QQQ LEAP 后买 SQQQ 期权」—— 触发时点有没有 edge (研究用, 不是引擎, 不产生任何订单)。

⛔ 本文件**不被 daily.py / session.py 调用**, 放在 strategy/proposals/ 下即表示它不在交易路径上
   (红线2)。用户 2026-10-28 指示「以后卖出qqq期权后 买sqqq期权」, 本脚本只回答该规则的
   **触发时点本身**是否预示 QQQ 走弱 —— 即这条规则是在吃 edge, 还是只在付 -3x 漂移 + 期权 theta。

问的问题 (只有一个):
  把 LEAP 轨道的历史出场日当作信号日, QQQ 此后 1/2/3/5/10/20 个交易日的走势,
  与**无条件在任意一天**买入相比, 有没有差别?
  若无差别 => 该规则的期望收益 = 0 - (SQQQ 漂移 + 期权 theta) < 0, 纯成本。

口径:
  · 出场日由 **production config** (strategy/qqq_leap.json) 的真实规则模拟得出:
    入场 QQQ 单日 ≤ -2% 且 close>SMA200, delta 目标 0.30, 止盈 bid>=entry_ask*1.20,
    20 交易日时间止损, 三档 ladder 148/85/330 DTE。
  · SQQQ 收益用**真实 SQQQ 日线**, 不用 -3x 合成 —— 合成会漏掉杠杆 ETF 的波动拖累, 那正是成本来源。
  · 期权腿用 BS, SQQQ 的 IV 代理按实测校准 (2026-10-27 实测 SQQQ 3DTE call IV = 83.1%)。
  · 定价与利率路径沿用 qqq-leap-backtest.py 的同一套函数, 保证与前面几份研究可比。

用法: python3 strategy/proposals/sqqq-after-leap-exit.py
输入: scratchpad 的 qqq_long.json + sqqq.json (integrations.py bars 产出)
"""
import json, math, statistics, collections, sys
from datetime import date as _D

SP = '/tmp/claude-0/-home-user-Claudeaqos/3e6563b5-e5a0-5665-84d2-72b2406895d9/scratchpad'
REPO = '/home/user/Claudeaqos'

def _d(x): return _D.fromisoformat(x)
def caldays(a, b): return (_d(b) - _d(a)).days

# ---------- Black-Scholes (与 qqq-leap-backtest.py 逐字同源) ----------
def _nd(x): return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))
def bs_call(S, K, T, r, q, sig):
    if T <= 0: return max(S - K, 0.0)
    if sig <= 0: return max(S*math.exp(-q*T) - K*math.exp(-r*T), 0.0)
    v = sig*math.sqrt(T)
    d1 = (math.log(S/K) + (r-q+0.5*sig*sig)*T)/v
    return S*math.exp(-q*T)*_nd(d1) - K*math.exp(-r*T)*_nd(d1-v)
def bs_delta(S, K, T, r, q, sig):
    if T <= 0: return 1.0 if S > K else 0.0
    v = sig*math.sqrt(T)
    return math.exp(-q*T)*_nd((math.log(S/K)+(r-q+0.5*sig*sig)*T)/v)
def rate(d):
    return {2016:0.005,2017:0.010,2018:0.020,2019:0.023,2020:0.004,2021:0.002,
            2022:0.020,2023:0.050,2024:0.050,2025:0.043,2026:0.040}.get(int(d[:4]), 0.03)

# 正态近似双尾 p —— 与 qqq-leap-tp-85-148.py / tp-uniform-vs-perrung.py 同一口径
def pval(t): return 2*(1 - 0.5*(1 + math.erf(abs(t)/math.sqrt(2))))
def welch(a, b):
    if len(a) < 3 or len(b) < 3: return None, None
    ma, mb = statistics.mean(a), statistics.mean(b)
    va, vb = statistics.variance(a)/len(a), statistics.variance(b)/len(b)
    se = math.sqrt(va+vb)
    if se == 0: return ma-mb, None
    return ma-mb, pval((ma-mb)/se)

# ---------- 数据 ----------
def series(path, symbol):
    """⚠️ 必须按 symbol 取, 不能用 results[0] —— sqqq.json 里是 [QQQ, SQQQ, TQQQ] 三只,
    results[0] 是 QQQ。2026-10-28 第一版就错在这里, 靠末尾自检
    (「SQQQ 买入持有应为大幅负数」实际打印出 +548.2% 与 QQQ 一模一样) 当场抓到。"""
    res = json.load(open(path))['data']['results']
    hit = [r for r in res if r.get('symbol') == symbol]
    if not hit:
        raise SystemExit('%s 里没有 %s (有 %s)' % (path, symbol, [r.get('symbol') for r in res]))
    out = {}
    for b in hit[0]['bars']:
        if b.get('interpolated') or b.get('session') not in (None, 'reg'): continue
        out[b['begins_at'][:10]] = float(b['close_price'])
    return out

qmap, smap = series(SP+'/qqq_long.json', 'QQQ'), series(SP+'/sqqq.json', 'SQQQ')
assert abs(statistics.mean([smap[d] for d in sorted(smap)[-5:]]) / 36.94 - 1) < 0.5,     'SQQQ 价位不对 (末值应在 $37 附近), 可能又取错了 symbol'

dates = sorted(set(qmap) & set(smap))
Q = [qmap[d] for d in dates]
S_ = [smap[d] for d in dates]
n = len(dates)
idx = {d: i for i, d in enumerate(dates)}

sma200 = [None]*n
for i in range(199, n): sma200[i] = sum(Q[i-199:i+1])/200.0
lr = [None]+[math.log(Q[i]/Q[i-1]) for i in range(1, n)]
def rvol(v, i, w):
    if i < w: return None
    return statistics.stdev([math.log(v[k]/v[k-1]) for k in range(i-w+1, i+1)])*math.sqrt(252)
rvq = [(0.5*rvol(Q,i,20)+0.5*rvol(Q,i,60)) if (i>=60) else None for i in range(n)]
rvs = [(0.5*rvol(S_,i,20)+0.5*rvol(S_,i,60)) if (i>=60) else None for i in range(n)]

KQ = 0.2345/rvq[-1]              # 校准: 模型今日 IV = 实测 LEAP IV 23.45%
KS = 0.8305/rvs[-1]              # 校准: 模型今日 IV = 实测 SQQQ 3DTE call IV 83.05%
def ivq(i): return None if rvq[i] is None else max(0.08, min(0.80, KQ*rvq[i]))
def ivs(i): return None if rvs[i] is None else max(0.15, min(2.50, KS*rvs[i]))

cfg = json.load(open(REPO+'/strategy/qqq_leap.json'))
THR   = abs(float(cfg['entry']['daily_return_pct_max']))
TGT_D = float(cfg['contract']['delta_target'])
TAKE  = float(cfg['exit']['take_profit_mult'])
TSTOP = int(cfg['exit']['max_holding_trading_days'])
_lad  = cfg['contract'].get('ladder')
LADDER= ([int(r['target_dte_calendar']) for r in _lad] if _lad
         else [int(cfg['contract']['dte_target_calendar'])])
HALF  = 0.020
QDIV  = 0.005

print('=== 校准与参数 ===')
print('样本 %s → %s, %d 个交易日 (QQQ 与 SQQQ 的交集)' % (dates[0], dates[-1], n))
print('production config: 入场 QQQ ≤ -%.1f%% 且 close>SMA200 | delta %.2f | 止盈 x%.2f | 时间止损 %d 交易日 | ladder %s DTE'
      % (THR, TGT_D, TAKE, TSTOP, LADDER))
print('KAPPA_QQQ %.3f (模型今日 IV=23.45%%)  KAPPA_SQQQ %.3f (模型今日 IV=83.05%%)' % (KQ, KS))
print('今日 SQQQ 模型 IV %.1f%% | QQQ 模型 IV %.1f%%' % (ivs(n-1)*100, ivq(n-1)*100))
print()

def pick_strike(S, T, r, sig, step=5, tgt=TGT_D, qd=QDIV):
    best = None; k = max(step, int(S*0.6/step)*step)
    while k <= int(S*1.8/step)*step:
        e = abs(bs_delta(S, k, T, r, qd, sig) - tgt)
        if best is None or e < best[1]: best = (k, e)
        k += step
    return None if best is None else best[0]

# ---------- 1) 模拟 LEAP 轨道, 收集出场日 ----------
exits = collections.defaultdict(list)     # reason -> [(exit_date_idx, rung, pnl_pct)]
entries = 0
for i in range(1, n):
    if sma200[i] is None or ivq(i) is None: continue
    if (Q[i]/Q[i-1]-1)*100 > -THR: continue
    if not (Q[i] > sma200[i]): continue
    entries += 1
    for dte in LADDER:
        T0 = dte/365.0; r0 = rate(dates[i]); s0 = ivq(i)
        K = pick_strike(Q[i], T0, r0, s0)
        if K is None: continue
        mid0 = bs_call(Q[i], K, T0, r0, QDIV, s0)
        if mid0 <= 0.01: continue
        entry = mid0*(1+HALF)
        for j in range(i+1, n):
            Tj = max(0.0, (dte-caldays(dates[i], dates[j]))/365.0)
            sj = ivq(j) or s0
            bid = bs_call(Q[j], K, Tj, rate(dates[j]), QDIV, sj)*(1-HALF)
            if bid >= entry*TAKE:
                exits['take_profit'].append((j, dte, (bid/entry-1)*100)); break
            if (j-i) >= TSTOP:
                exits['time_stop'].append((j, dte, (bid/entry-1)*100)); break
            if Tj <= 0:
                exits['expiry'].append((j, dte, (max(Q[j]-K,0.0)*(1-HALF)/entry-1)*100)); break

allx = sorted(set(j for v in exits.values() for j, _, _ in v))
print('=== LEAP 轨道历史出场日 (production 规则) ===')
print('入场信号 %d 次 (%.1f 次/年)' % (entries, entries/(n/252)))
for k in ('take_profit', 'time_stop', 'expiry'):
    v = exits[k]
    if v: print('  %-11s %4d 笔 (每档各计)  均值 %+.1f%%  中位 %+.1f%%'
                % (k, len(v), statistics.mean([p for _,_,p in v]), statistics.median([p for _,_,p in v])))
print('  去重后的**不同出场日** %d 天 (%.1f 天/年) —— 这就是新规则的触发次数' % (len(allx), len(allx)/(n/252)))
print()

# ---------- 2) 出场日之后 QQQ / SQQQ 走势 vs 无条件基准 ----------
HOR = [1, 2, 3, 5, 10, 20]
def fwd(v, i, h): return None if i+h >= n else (v[i+h]/v[i]-1)*100

for tag, src in (('全部出场日', allx),
                 ('仅止盈出场日', sorted(set(j for j,_,_ in exits['take_profit']))),
                 ('仅时间止损日', sorted(set(j for j,_,_ in exits['time_stop'])))):
    if not src: continue
    print('=== %s (n=%d) 之后的走势 ===' % (tag, len(src)))
    print('  期  |        QQQ 信号后      |      QQQ 无条件基准    | 增量(信号-基准)      |     真实 SQQQ 信号后  | SQQQ 基准')
    for h in HOR:
        sq = [fwd(Q,i,h) for i in src]; sq = [x for x in sq if x is not None]
        bq = [fwd(Q,i,h) for i in range(60, n-h)]
        ss = [fwd(S_,i,h) for i in src]; ss = [x for x in ss if x is not None]
        bs_ = [fwd(S_,i,h) for i in range(60, n-h)]
        if len(sq) < 3: continue
        d, p = welch(sq, bq)
        ds, ps = welch(ss, bs_)
        print('  %3dd| %+6.2f%% (胜%4.1f%%) | %+6.2f%% (胜%4.1f%%) | QQQ %+5.2fpp p=%.3f | %+7.2f%% (胜%4.1f%%) | %+7.2f%% | SQQQ 增量 %+5.2fpp p=%.3f'
              % (h, statistics.mean(sq), 100*sum(1 for x in sq if x>0)/len(sq),
                 statistics.mean(bq), 100*sum(1 for x in bq if x>0)/len(bq),
                 d, p if p is not None else float('nan'),
                 statistics.mean(ss), 100*sum(1 for x in ss if x>0)/len(ss),
                 statistics.mean(bs_), ds, ps if ps is not None else float('nan')))
    print()

# ---------- 3) SQQQ 期权腿: 真下单会赚吗 ----------
print('=== 在出场日买一张 SQQQ call, 持 H 日后卖出 (BS, 单边半点差 2%; 真实 SQQQ 路径) ===')
print('  ⚠️ 2026-10-27 用户实买的是 3 DTE 略 OTM (strike/spot = 37.50/36.94 = 1.015)')
print('  DTE  moneyness  持有  |   n  | 均值收益   中位   胜率  全损率 | 基准(任意日同买法) | 增量   p')
for dte, mny in ((3, 1.015), (3, 1.00), (7, 1.00), (30, 1.00), (30, 1.05)):
    for hold in (1, 2, 3):
        if hold >= dte: continue
        def leg(i):
            if i+hold >= n or ivs(i) is None: return None
            S0 = S_[i]; T0 = dte/365.0; r0 = rate(dates[i]); s0 = ivs(i)
            K = round(S0*mny*2)/2.0
            if K <= 0: return None
            m0 = bs_call(S0, K, T0, r0, 0.0, s0)
            if m0 < 0.05: return None
            buy = m0*(1+HALF)
            Tj = max(0.0, (dte - caldays(dates[i], dates[i+hold]))/365.0)
            sj = ivs(i+hold) or s0
            sell = (bs_call(S_[i+hold], K, Tj, rate(dates[i+hold]), 0.0, sj)*(1-HALF)
                    if Tj > 0 else max(S_[i+hold]-K, 0.0)*(1-HALF))
            return (sell/buy-1)*100
        sig = [x for x in (leg(i) for i in allx) if x is not None]
        base = [x for x in (leg(i) for i in range(60, n-hold, 3)) if x is not None]
        if len(sig) < 3: continue
        d, p = welch(sig, base)
        print('  %3d   %5.3f      %dd  | %4d | %+8.1f%% %+7.1f%% %5.1f%% %6.1f%% | %+8.1f%%        | %+6.1fpp p=%.3f'
              % (dte, mny, hold, len(sig), statistics.mean(sig), statistics.median(sig),
                 100*sum(1 for x in sig if x>0)/len(sig), 100*sum(1 for x in sig if x<=-99)/len(sig),
                 statistics.mean(base), d, p if p is not None else float('nan')))
print()

# ---------- 4) 自检 ----------
print('=== 自检 ===')
print('  pval(0.41) = %.3f  (应 ≈0.68; 若为 0.000 说明 p 值实现又坏了)' % pval(0.41))
print('  pval(1.96) = %.3f  (应 ≈0.050)' % pval(1.96))
print('  SQQQ 10.8 年买入持有: %+.1f%% (结构性漂移 sanity check, 应为大幅负数)' % ((S_[-1]/S_[0]-1)*100))
print('  QQQ  10.8 年买入持有: %+.1f%%' % ((Q[-1]/Q[0]-1)*100))
b1 = [fwd(S_,i,1) for i in range(60, n-1)]
print('  SQQQ 无条件 1 日均值 %+.3f%% (应 ≈ -0.2~-0.3%%, 即每日漂移)' % statistics.mean(b1))

# ---------- 5) 把 edge 与 wrapper 成本放在同一把尺子上 ----------
print()
print('=== 关键对照: 信号值多少 vs 期权外壳要多少 ===')
hold = 1
need = {}
for dte, mny in ((3,1.015),(3,1.00),(7,1.00),(30,1.00)):
    i = n-1
    S0 = S_[i]; T0 = dte/365.0; r0 = rate(dates[i]); s0 = ivs(i)
    K = round(S0*mny*2)/2.0
    buy = bs_call(S0,K,T0,r0,0.0,s0)*(1+HALF)
    lo, hi = S0*0.5, S0*2.0
    for _ in range(200):
        m = (lo+hi)/2
        if bs_call(m,K,max(0.0,(dte-1)/365.0),r0,0.0,s0)*(1-HALF) < buy: lo = m
        else: hi = m
    need[(dte,mny)] = (lo/S0-1)*100
    print('  %2d DTE moneyness %5.3f: 持 1 日打平需 SQQQ %+6.2f%%  = QQQ %+6.2f%%  (买 %.2f 含点差)'
          % (dte, mny, (lo/S0-1)*100, -(lo/S0-1)/3*100, buy))
sig1 = [x for x in (fwd(S_,i,1) for i in allx) if x is not None]
print('  ---')
print('  信号日实测 SQQQ 1 日均值 %+.2f%% (中位 %+.2f%%, 胜率 %.1f%%)'
      % (statistics.mean(sig1), statistics.median(sig1), 100*sum(1 for x in sig1 if x>0)/len(sig1)))
print('  => 最便宜的外壳 (30 DTE ATM) 都要 SQQQ 涨 %.2f%% 才打平, 而信号只给 %.2f%% => 缺口 %.2f pp'
      % (need[(30,1.00)], statistics.mean(sig1), need[(30,1.00)]-statistics.mean(sig1)))
print('  => 用户实买的 3 DTE OTM 要 %.2f%%, 缺口 %.2f pp'
      % (need[(3,1.015)], need[(3,1.015)]-statistics.mean(sig1)))

print()
print('=== 与已有 paper 轨道 (sqqq_short.json: QQQ 单日 >= +2.5pct -> 买 SQQQ 股 -> 持 1 日) 的关系 ===')
up25 = [i for i in range(60, n-1) if (Q[i]/Q[i-1]-1)*100 >= 2.5]
ov = sorted(set(allx) & set(up25))
print('  现有触发器 n=%d (%.1f 次/年); LEAP 出场日 n=%d; **重叠仅 %d 天**'
      % (len(up25), len(up25)/(n/252), len(allx), len(ov)))
a = [x for x in (fwd(S_,i,1) for i in up25) if x is not None]
print('  SQQQ 股持 1 日:  +2.5%% 触发器 %+.2f%% (n=%d, 胜%.1f%%)  |  LEAP 出场触发器 %+.2f%% (n=%d, 胜%.1f%%)'
      % (statistics.mean(a), len(a), 100*sum(1 for x in a if x>0)/len(a),
         statistics.mean(sig1), len(sig1), 100*sum(1 for x in sig1 if x>0)/len(sig1)))
d, p = welch(sig1, a)
print('  两个触发器之差 %+.2fpp p=%.3f => %s'
      % (d, p, '无法区分 (本质是同一类信号的两种写法)' if p and p > 0.1 else '有差别'))
byyear = collections.defaultdict(list)
for i in allx:
    v = fwd(S_, i, 1)
    if v is not None: byyear[dates[i][:4]].append(v)
print('  LEAP 出场触发器逐年 (SQQQ 股 1 日均值):')
print('   ', '  '.join('%s %+.2f%%(n%d)' % (y, statistics.mean(v), len(v)) for y, v in sorted(byyear.items())))

print()
print('=== 剔除 2020 与 2022 (与 2026-10-23 那份 SQQQ 研究同一个检验) ===')
ex = [i for i in allx if dates[i][:4] not in ('2020', '2022')]
e1 = [x for x in (fwd(S_, i, 1) for i in ex) if x is not None]
k1 = [x for x in (fwd(S_, i, 1) for i in allx if dates[i][:4] in ('2020','2022')) if x is not None]
print('  全样本      n=%3d  SQQQ 股 1 日均值 %+.2f%%' % (len(sig1), statistics.mean(sig1)))
print('  仅 2020+2022 n=%3d  %+.2f%%' % (len(k1), statistics.mean(k1)))
print('  **剔除后**   n=%3d  %+.2f%%  (中位 %+.2f%%, 胜率 %.1f%%)'
      % (len(e1), statistics.mean(e1), statistics.median(e1),
         100*sum(1 for x in e1 if x > 0)/len(e1)))
print('  近 4 年 (2023-2026) n=%d  %+.2f%%'
      % (len([i for i in allx if dates[i][:4] >= '2023']),
         statistics.mean([x for x in (fwd(S_,i,1) for i in allx if dates[i][:4] >= '2023') if x is not None])))
print('  => 剔除后连**股票版**都是负的 => 期权版 (还要先付 %.2f%% 打平线) 无论怎么调参都是负期望'
      % need[(30,1.00)])
print()
print('=== 结论 ===')
print('  1) 触发时点**确实有信号**: QQQ 出场后 1 日 -0.36pp (p=0.037), 20 日 -1.06pp (p=0.044), 六个期限同号。')
print('  2) 但信号幅度 (SQQQ 股 +0.79%/日) **小于任何期权外壳的打平线** (最便宜 30DTE ATM 需 +0.99%)。')
print('  3) 期权版 13 种参数组合**均值全负** (-2.7% ~ -16.0%), 对基准的增量 p 最好只有 0.064, 不显著。')
print('  4) 那 +0.79% 几乎全部来自 2020(n28)+2022(n5); 剔除后转负, 2023-2026 四年连续为负。')
print('  5) 与现有 paper 轨道的 +2.5% 触发器**统计上无法区分** (p=0.602) —— 是同一类信号的两种写法, 重叠仅 21/114 天。')
