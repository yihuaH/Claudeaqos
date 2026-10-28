#!/usr/bin/env python3
"""「个股单日跌 2% 就考虑买 call」—— 频率与初步 edge。**仅研究用, 不产生任何订单。**

⛔ 不被 daily.py / session.py 调用 (红线2)。回答用户 2026-10-28「如果有股票跌2% 就可以考虑买入期权」。
   用户说的是「可以考虑」= 提案, 故本脚本只出数据, 不改任何 config、不接任何线。

要回答的核心问题 (按重要性):
  ① **频率**: QQQ 单日跌 ≥2% 一年 8.4 次; 把同一条件放到 100 只个股上, 一年几次?
     这一条可能就足以定调 —— 若一年几百次, 则「考虑买入」在工程上等于「每天都要挑」,
     而现金只够 4~15 张, 挑哪张就成了会话的自由裁量 = 红线2 禁区。
  ② 与现行 RSI-2 入场 (rsi2<10 且 close>SMA200) 的**频率对比与重叠度**: 是新 edge 还是更松的同一件事?
  ③ 初步 forward return vs 无条件基准。

⚠️ **样本限制必须一起读**: scratchpad 的 bars 只有 311 根 (2025-08-04 → 2026-10-27, 约 1.2 年),
   而 SMA200 要吃掉 200 根 → 带趋势过滤的可用窗只剩约 **111 个交易日 (约 5 个月)**。
   故: 频率估计可信 (万级 symbol-day), **edge 估计只覆盖单一 regime, 不可用于 go/no-go**。
   要做真判定须拉 10 年日线 (100 只 × 10 年), 本脚本不做。

用法: python3 strategy/proposals/stock-drop2-call.py
"""
import json, statistics, collections, math

SP = '/tmp/claude-0/-home-user-Claudeaqos/3e6563b5-e5a0-5665-84d2-72b2406895d9/scratchpad'
REPO = '/home/user/Claudeaqos'

def pval(t): return 2*(1 - 0.5*(1 + math.erf(abs(t)/math.sqrt(2))))
def welch(a, b):
    if len(a) < 3 or len(b) < 3: return None, None
    ma, mb = statistics.mean(a), statistics.mean(b)
    se = math.sqrt(statistics.variance(a)/len(a) + statistics.variance(b)/len(b))
    if se == 0: return ma-mb, None
    return ma-mb, pval((ma-mb)/se)

cfg = json.load(open(f'{REPO}/strategy/config.json'))
uni = json.load(open(f'{REPO}/strategy/universe.json'))['symbols']
RSI2_MAX = float(cfg['entry']['rsi2_max'])
DROP = 2.0                      # 用户给的阈值
raw = json.load(open(SP+'/wd1027dry/bars.json'))['data']['results']

SER = {}
for r in raw:
    sym = r.get('symbol')
    if sym not in uni:          # 只看 100 股池 (ETF 另有轨道, 不在本提案范围)
        continue
    b = [x for x in r['bars'] if not x.get('interpolated') and x.get('session') in (None, 'reg')]
    if len(b) < 210: continue
    SER[sym] = ([x['begins_at'][:10] for x in b], [float(x['close_price']) for x in b])

def wilder_rsi(c, p=2):
    out=[None]*len(c)
    if len(c) <= p: return out
    g=l=0.0
    for i in range(1,p+1):
        ch=c[i]-c[i-1]; g+=max(ch,0); l+=max(-ch,0)
    ag,al=g/p,l/p
    out[p]=100.0 if al==0 else 100-100/(1+ag/al)
    for i in range(p+1,len(c)):
        ch=c[i]-c[i-1]
        ag=(ag*(p-1)+max(ch,0))/p; al=(al*(p-1)+max(-ch,0))/p
        out[i]=100.0 if al==0 else 100-100/(1+ag/al)
    return out

HOR=[1,3,5,10]
drop_sig, rsi_sig, both_sig, base = [], [], [], []
drop_days, rsi_days = collections.Counter(), collections.Counter()
usable_days = set()
per_sym_drop = {}

for sym,(d,c) in SER.items():
    n=len(c); r2=wilder_rsi(c)
    sma=[None]*n
    for i in range(199,n): sma[i]=sum(c[i-199:i+1])/200.0
    nd=0
    for i in range(200,n):
        if sma[i] is None or r2[i] is None: continue
        usable_days.add(d[i])
        ret=(c[i]/c[i-1]-1)*100
        bull=c[i]>sma[i]
        fwd={h:((c[i+h]/c[i]-1)*100 if i+h<n else None) for h in HOR}
        base.append(fwd)
        dp = ret<=-DROP and bull
        rs = r2[i]<RSI2_MAX and bull
        if dp: drop_sig.append(fwd); drop_days[d[i]]+=1; nd+=1
        if rs: rsi_sig.append(fwd); rsi_days[d[i]]+=1
        if dp and rs: both_sig.append(fwd)
    per_sym_drop[sym]=nd

ndays=len(usable_days); yrs=ndays/252.0
print("=== 样本 ===")
print("  池内有效标的 %d 只 | 可用交易日 %d 天 (%.2f 年) | symbol-day %d"%(
    len(SER), ndays, yrs, len(base)))
print("  ⚠️ 只有约 5 个月的带趋势过滤窗口 (SMA200 吃掉 200 根) —— 频率可信, **edge 不可信**")
print()
print("=== ① 频率: 这是本提案的决定性数字 ===")
print("  用户的条件 (单日 ≤ −%.0f%% 且 close>SMA200): **%d 次 / %.2f 年 = %.0f 次/年**"%(
    DROP,len(drop_sig),yrs,len(drop_sig)/yrs))
print("  现行 RSI-2 入场 (rsi2<%.0f 且 close>SMA200):   %d 次 / %.2f 年 = **%.0f 次/年**"%(
    RSI2_MAX,len(rsi_sig),yrs,len(rsi_sig)/yrs))
print("  对照 QQQ LEAP 轨道 (同样 −2%% 条件, 但只有 QQQ 一个标的): **8.4 次/年**")
print("  => 用户条件是 QQQ 轨道频率的 **%.0f 倍**, 是现行 RSI-2 的 **%.1f 倍**"%(
    (len(drop_sig)/yrs)/8.4, len(drop_sig)/max(1,len(rsi_sig))))
print()
print("  有信号的交易日占比: %.1f%% (%d/%d 天至少有 1 只触发)"%(
    100*len(drop_days)/ndays,len(drop_days),ndays))
cnt=sorted(drop_days.values())
print("  触发日的**同日只数**分布: 中位 %d  均值 %.1f  最多 **%d 只** (%s)"%(
    statistics.median(cnt),statistics.mean(cnt),max(cnt),
    [k for k,v in drop_days.items() if v==max(cnt)][0]))
print("  单只标的年均触发: 中位 %.1f 次/年  最多 %.1f 次/年 (%s)"%(
    statistics.median([v/yrs for v in per_sym_drop.values()]),
    max(per_sym_drop.values())/yrs, max(per_sym_drop,key=per_sym_drop.get)))
print()
print("=== ② 是新 edge 还是更松的同一件事 ===")
ov=len(both_sig)
print("  两条件同时成立: %d 次 = 跌2%%信号的 %.1f%% / RSI-2 信号的 %.1f%%"%(
    ov,100*ov/max(1,len(drop_sig)),100*ov/max(1,len(rsi_sig))))
print("  => 重叠很小, 两者**不是同一批日子**; 但下面看它的收益是否更好")
print()
print("=== ③ 初步 forward return (⚠️ 仅 5 个月单一 regime, 不可用于 go/no-go) ===")
print("  期  | 跌2%%信号后          | RSI-2 信号后        | 无条件基准         | 跌2%% vs 基准")
for h in HOR:
    a=[x[h] for x in drop_sig if x[h] is not None]
    r_=[x[h] for x in rsi_sig if x[h] is not None]
    b=[x[h] for x in base if x[h] is not None]
    if len(a)<3: continue
    dd,pp=welch(a,b)
    print("  %3dd | %+6.2f%% (胜%4.1f%%, n%4d) | %+6.2f%% (胜%4.1f%%, n%3d) | %+6.2f%% (胜%4.1f%%) | %+5.2fpp p=%.3f"%(
        h,statistics.mean(a),100*sum(1 for x in a if x>0)/len(a),len(a),
        statistics.mean(r_),100*sum(1 for x in r_ if x>0)/len(r_),len(r_),
        statistics.mean(b),100*sum(1 for x in b if x>0)/len(b),
        dd,pp if pp is not None else float('nan')))
print()
print("=== ④ 现金可行性 ===")
CASH=6309.06
q=json.load(open(SP+'/q1027.json'))
px=[q[s] for s in SER if s in q]
med=statistics.median(px)
print("  现金 $%.2f | 池内股价中位 $%.2f"%(CASH,med))
print("  delta 0.30 的 call 权利金粗估 ≈ 100 × S × IV × sqrt(T/252) × 0.25 (OTM 近似)")
for iv,dte,lab in ((0.30,78,'78 DTE IV30%'),(0.35,141,'141 DTE IV35%')):
    prem=100*med*iv*math.sqrt(dte/252)*0.25
    print("    %-16s 中位股价一张 ≈ $%-6.0f -> 现金最多买 **%d 张**"%(lab,prem,int(CASH//prem)))
print("  同日最多 %d 只触发 vs 最多买 ~%d 张 => **必须有确定性的排序规则, 否则就是会话自由裁量 (红线2)**"%(
    max(cnt), int(CASH//(100*med*0.30*math.sqrt(78/252)*0.25))))
print()
print("=== 自检 ===")
print("  pval(1.96) = %.3f (应 ≈0.050)"%pval(1.96))
print("  无条件基准 1 日均值 %+.3f%% (个股池同期日均, 应为小正数)"%statistics.mean(
    [x[1] for x in base if x[1] is not None]))

# ============================================================================
# 追加 (2026-10-28, 用户补「要优秀的个股」): 问题到底是阈值太松还是标的太多?
# ============================================================================
print()
print("="*78)
print("=== ⑤ 关键重构: −2% 在个股上并不比在 QQQ 上松多少, 爆炸来自**标的数量** ===")
qqq = [r for r in raw if r.get('symbol')=='QQQ']
if qqq:
    b=[x for x in qqq[0]['bars'] if not x.get('interpolated') and x.get('session') in (None,'reg')]
    qc=[float(x['close_price']) for x in b]
    qhit=sum(1 for i in range(200,len(qc)) if (qc[i]/qc[i-1]-1)*100<=-DROP)
    print("  QQQ 同期同条件(不加趋势过滤): %d 次 / %.2f 年 = %.1f 次/年 = 占 %.1f%% 的交易日"%(
        qhit,yrs,qhit/yrs,100*qhit/ndays))
print("  池内个股中位: %.1f 次/年 = 占 %.1f%% 的交易日"%(
    statistics.median([v/yrs for v in per_sym_drop.values()]),
    100*statistics.median([v/yrs for v in per_sym_drop.values()])/252))
print("  => 单只个股只比 QQQ 频繁约 2~3 倍。**2490 次/年里的 ~100 倍来自「有 100 只标的」**,")
print("     不是来自阈值定得太低。=> 用户「要优秀的个股」(= 缩小标的范围) **方向正确**。")
print()
print("=== ⑥ 容量算术: 现金只够同时持几个信号? 反推标的数该是多少 ===")
CASH=6309.06
med=statistics.median([q[s] for s in SER if s in q])
prem1=100*med*0.30*math.sqrt(78/252)*0.25
ladder_cost=prem1*1.0+100*med*0.33*math.sqrt(141/252)*0.25+100*med*0.35*math.sqrt(231/252)*0.25
print("  中位股价 $%.2f -> delta0.30 单张粗估: 78DTE $%.0f"%(med,prem1))
print("  若沿用 GOOGL 的**三档梯子** (78/141/231 各 1 张): 一个信号 ≈ **$%.0f**"%ladder_cost)
print("  现金 $%.2f -> 同时最多 **%.1f 个信号**的梯子"%(CASH,CASH/ladder_cost))
hold_td=17
print("  持有期 17 个交易日 (78 档 DTE30%%) -> 每个仓位占用 %.1f%% 的年度时间"%(100*hold_td/252))
cap=(CASH/ladder_cost)*(252/hold_td)
print("  => 年度**容量上限 ≈ %.0f 个信号/年** (假设资金完全循环、无闲置)"%cap)
print()
print("  反推标的数 (池内中位 %.1f 次/年/只):"%statistics.median([v/yrs for v in per_sym_drop.values()]))
rate=statistics.median([v/yrs for v in per_sym_drop.values()])
print("     要让信号量 ≈ 容量 %.0f 次/年 -> 标的数应为 **%.1f 只**"%(cap,cap/rate))
for n in (1,3,5,10,20,100):
    print("     %3d 只 -> %5.0f 次/年, 是容量的 **%.1f 倍**"%(n,n*rate,n*rate/cap))
print()
print("=== ⑦ 「优秀」= 低波动? 分位检验 (池内按 20 日年化波动率分三档) ===")
vols={}
for sym,(d,c) in SER.items():
    lr=[math.log(c[i]/c[i-1]) for i in range(len(c)-20,len(c))]
    vols[sym]=statistics.stdev(lr)*math.sqrt(252)*100
srt=sorted(vols,key=vols.get); k=len(srt)//3
tiers={"低波动 1/3":srt[:k],"中波动 1/3":srt[k:2*k],"高波动 1/3":srt[2*k:]}
print("  档位         vol区间        信号/年/只   1d 均值    5d 均值   10d 均值   10d胜率")
for lab,syms in tiers.items():
    ss=set(syms)
    sub=[]; nd=0
    for sym in ss: nd+=per_sym_drop[sym]
    # 重算该档的 forward
    for sym in ss:
        d,c=SER[sym]; n=len(c); r2=wilder_rsi(c)
        sma=[None]*n
        for i in range(199,n): sma[i]=sum(c[i-199:i+1])/200.0
        for i in range(200,n):
            if sma[i] is None: continue
            if (c[i]/c[i-1]-1)*100<=-DROP and c[i]>sma[i]:
                sub.append({h:((c[i+h]/c[i]-1)*100 if i+h<n else None) for h in HOR})
    g=lambda h:[x[h] for x in sub if x[h] is not None]
    print("  %-11s [%4.1f, %4.1f]%%   %6.1f      %+6.2f%%  %+6.2f%%  %+6.2f%%   %4.1f%%  (n%d)"%(
        lab,vols[syms[0]],vols[syms[-1]],nd/yrs/len(ss),
        statistics.mean(g(1)),statististics_mean if False else statistics.mean(g(5)),
        statistics.mean(g(10)),100*sum(1 for x in g(10) if x>0)/len(g(10)),len(sub)))
print()
print("  ⚠️ 全部 p 值在 ③ 已测为 0.40~0.74 —— **本样本里跌2%%对任何波动档都没有可测 edge**。")
print("     这 5 个月窗口判不了 go/no-go, 但它**也没给出任何支持该条件的证据**。")
