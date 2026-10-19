#!/usr/bin/env python3
"""QQQ LEAP call 回测 (Black-Scholes 模型定价) —— **仅研究用, 不是引擎, 不产生任何订单**。

⛔ 本文件**不被 daily.py / session.py 调用**, 放在 strategy/proposals/ 下即表示它不在交易路径上。
   红线2: 订单只能来自确定性引擎 (signals.py / weekly_calls.py / overnight.py / momentum.py /
   options_overlay.py)。本脚本只算历史收益分布, 供用户判断是否值得实现这条轨道。

用法: python3 strategy/proposals/qqq-leap-backtest.py [KAPPA]
输入: scratchpad 的 qqq_long.json (integrations.py bars --symbols QQQ --start 2015-01-01 产出)

结论与口径说明见 qqq-leap-call.md §12。
"""
import json, math, statistics, collections, sys
from datetime import date as _D
def _d(x): return _D.fromisoformat(x)
def caldays(a,b): return (_d(b)-_d(a)).days

SP='/tmp/claude-0/-home-user-Claudeaqos/3e6563b5-e5a0-5665-84d2-72b2406895d9/scratchpad'

# ---------- Black-Scholes ----------
def _nd(x): return 0.5*(1.0+math.erf(x/math.sqrt(2.0)))
def bs_call(S,K,T,r,q,sig):
    if T<=0: return max(S-K,0.0)
    if sig<=0: return max(S*math.exp(-q*T)-K*math.exp(-r*T),0.0)
    v=sig*math.sqrt(T)
    d1=(math.log(S/K)+(r-q+0.5*sig*sig)*T)/v
    d2=d1-v
    return S*math.exp(-q*T)*_nd(d1)-K*math.exp(-r*T)*_nd(d2)
def bs_delta(S,K,T,r,q,sig):
    if T<=0: return 1.0 if S>K else 0.0
    v=sig*math.sqrt(T)
    d1=(math.log(S/K)+(r-q+0.5*sig*sig)*T)/v
    return math.exp(-q*T)*_nd(d1)

# ---------- 数据 ----------
bars=json.load(open(SP+'/qqq_long.json'))['data']['results'][0]['bars']
dates=[b['begins_at'][:10] for b in bars]
closes=[float(b['close_price']) for b in bars]
n=len(closes)

# SMA200
sma200=[None]*n
for i in range(199,n): sma200[i]=sum(closes[i-199:i+1])/200.0

# 已实现波动率 (年化) — 20 日与 60 日
logret=[None]+[math.log(closes[i]/closes[i-1]) for i in range(1,n)]
def rvol(i,w):
    if i<w: return None
    seg=logret[i-w+1:i+1]
    return statistics.stdev(seg)*math.sqrt(252)
rv20=[rvol(i,20) for i in range(n)]
rv60=[rvol(i,60) for i in range(n)]

# IV 代理: 0.5*rv20 + 0.5*rv60, 再乘校准系数 KAPPA
# KAPPA 由"今天的模型 IV = 今天实测的 LEAP IV 0.2345"反解
def ivproxy_raw(i):
    if rv20[i] is None or rv60[i] is None: return None
    return 0.5*rv20[i]+0.5*rv60[i]
KAPPA = float(sys.argv[1]) if len(sys.argv)>1 else None
i_last=n-1
raw_last=ivproxy_raw(i_last)
if KAPPA is None:
    KAPPA = 0.2345/raw_last
def iv(i):
    r=ivproxy_raw(i)
    return None if r is None else max(0.08, min(0.80, KAPPA*r))

# 利率路径 (粗略, 按年份; rho 不可忽略故不用单一常数)
def rate(d):
    y=int(d[:4])
    return {2016:0.005,2017:0.010,2018:0.020,2019:0.023,2020:0.004,
            2021:0.002,2022:0.020,2023:0.050,2024:0.050,2025:0.043,2026:0.040}.get(y,0.03)
Q_DIV=0.005   # QQQ 股息率约 0.5%

# ---------- 回测 ----------
TARGET_DELTA=0.60
DTE_TARGET=365
TAKE=1.50          # +50% 止盈
HALF_SPREAD=0.020  # 单边 2% (实测点差 3.1~6.9% → 半点差 1.6~3.5%, 取 2% 居中)

def pick_strike(S,T,r,sig):
    """找使 delta 最接近 TARGET_DELTA 的行权价 (QQQ LEAP 行权价步长按 5 取整)"""
    best=None
    lo,hi=int(S*0.75/5)*5, int(S*1.35/5)*5
    k=lo
    while k<=hi:
        if k>0:
            d=bs_delta(S,k,T,r,Q_DIV,sig)
            e=abs(d-TARGET_DELTA)
            if best is None or e<best[1]: best=(k,e,d)
        k+=5
    return best

def run(thr, bull_only=True, take=TAKE, half_spread=HALF_SPREAD, kappa_mult=1.0):
    trades=[]
    i=1
    while i<n:
        if sma200[i] is None or iv(i) is None: i+=1; continue
        ret=(closes[i]/closes[i-1]-1)*100
        if ret> -thr: i+=1; continue
        if bull_only and not (closes[i]>sma200[i]): i+=1; continue
        S0=closes[i]; T0=DTE_TARGET/365.0; r0=rate(dates[i]); sig0=iv(i)*kappa_mult
        pk=pick_strike(S0,T0,r0,sig0)
        if pk is None: i+=1; continue
        K,_,d0=pk
        mid0=bs_call(S0,K,T0,r0,Q_DIV,sig0)
        if mid0<=0.01: i+=1; continue
        entry=mid0*(1+half_spread)           # 按 ask 买入
        # 逐日推进
        exit_reason='expiry'; exit_px=None; held=0
        for j in range(i+1,n):
            Tj=max(0.0,(DTE_TARGET-caldays(dates[i],dates[j]))/365.0)   # ✅ 日历日
            sj=iv(j)
            if sj is None: sj=sig0
            sj*=kappa_mult
            midj=bs_call(closes[j],K,Tj,rate(dates[j]),Q_DIV,sj)
            bid=midj*(1-half_spread)
            held=j-i
            if bid>=entry*take:
                exit_reason='take_profit'; exit_px=bid; break
            if Tj<=0:
                exit_reason='expiry'; exit_px=max(closes[j]-K,0.0)*(1-half_spread) if closes[j]>K else 0.0
                break
        if exit_px is None:   # 数据末端未结
            exit_reason='open'; exit_px=bs_call(closes[-1],K,max(0.0,(DTE_TARGET-caldays(dates[i],dates[-1]))/365.0),
                                                rate(dates[-1]),Q_DIV,iv(n-1)*kappa_mult)*(1-half_spread)
            held=n-1-i
        pnl=(exit_px/entry-1)*100
        trades.append(dict(d=dates[i],ret=ret,S=S0,K=K,delta=round(d0,3),iv=round(sig0,4),
                           entry=round(entry,2),exit=round(exit_px,2),pnl=pnl,
                           reason=exit_reason,held=held))
        i+=1
    return trades

def summarize(ts,label):
    if not ts: print(label,'无交易'); return
    p=[t['pnl'] for t in ts]
    closed=[t for t in ts if t['reason']!='open']
    win=[x for x in p if x>0]
    tp=[t for t in ts if t['reason']=='take_profit']
    ex=[t for t in ts if t['reason']=='expiry']
    op=[t for t in ts if t['reason']=='open']
    print('%s  n=%d' % (label,len(ts)))
    print('   胜率 %.1f%%   均值 %+.1f%%   中位 %+.1f%%   最好 %+.0f%%   最差 %+.0f%%'%(
        len(win)/len(p)*100, statistics.mean(p), statistics.median(p), max(p), min(p)))
    print('   止盈 %d (%.0f%%) / 持到期 %d (%.0f%%) / 未结 %d   平均持有 %.0f 交易日'%(
        len(tp),len(tp)/len(ts)*100,len(ex),len(ex)/len(ts)*100,len(op),
        statistics.mean([t['held'] for t in ts])))
    if ex:
        exp_p=[t['pnl'] for t in ex]
        print('   └ 持到期那批: 均值 %+.1f%%  全损(-100%%) %d 笔'%(
            statistics.mean(exp_p), sum(1 for x in exp_p if x<=-99)))
    loss=[x for x in p if x<=-50]
    print('   亏 ≥50%% 的 %d 笔 (%.1f%%); 全损 %d 笔; 亏损笔均值 %+.1f%%'%(
        len(loss),len(loss)/len(p)*100,sum(1 for x in p if x<=-99),
        statistics.mean([x for x in p if x<=0]) if any(x<=0 for x in p) else 0))
    return dict(n=len(ts),win=len(win)/len(p)*100,mean=statistics.mean(p),med=statistics.median(p),
                tp=len(tp)/len(ts)*100,worst=min(p),best=max(p))

print('=== 校准 ===')
print('样本 %s → %s, %d 个交易日'%(dates[0],dates[-1],n))
print('今日 rv20=%.1f%% rv60=%.1f%% → IV 代理原始 %.3f'%(rv20[-1]*100,rv60[-1]*100,raw_last))
print('KAPPA = 0.2345 / %.4f = %.3f   (使模型今日 IV = 实测 LEAP IV 23.45%%)'%(raw_last,KAPPA))
print('半点差 %.1f%% 单边 (实测点差 3.1~6.9%%)'%(HALF_SPREAD*100))
print()
res={}
for thr in (1.0,2.0):
    print('=== 阈值 ≥%.0f%% + 牛市(close>SMA200) + 50%% 止盈 ==='%thr)
    ts=run(thr)
    res[thr]=summarize(ts,'  全样本')
    byyear=collections.defaultdict(list)
    for t in ts: byyear[t['d'][:4]].append(t['pnl'])
    print('   逐年均值:', '  '.join('%s %+.0f%%(n%d)'%(y,statistics.mean(v),len(v)) for y,v in sorted(byyear.items())))
    print()
print('=== 敏感性 (阈值 ≥2%) ===')
print('IV 水平 (KAPPA 乘数)   半点差   n    胜率    均值     中位   止盈%')
for km in (0.9,1.0,1.1):
    for hs in (0.010,0.020,0.035):
        ts=run(2.0,half_spread=hs,kappa_mult=km)
        p=[t['pnl'] for t in ts]
        tp=sum(1 for t in ts if t['reason']=='take_profit')
        print('  ×%.1f                %.1f%%   %3d  %5.1f%%  %+6.1f%%  %+6.1f%%  %5.1f%%'%(
            km,hs*100,len(ts),sum(1 for x in p if x>0)/len(p)*100,
            statistics.mean(p),statistics.median(p),tp/len(ts)*100))
json.dump(res,open(SP+'/leap_bt_res.json','w'))
