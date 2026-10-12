"""镜像 RSI-2 看空信号 — 5 年样本检验 (2021-09 ~ 2026-10, 108 只)。
决定性问题: 第一轮在 2026-05~09 这 5 个月看到的 t=3.61, 放到跨 regime 的 5 年里还在不在?
纯读本地 bars, 不下任何单, 不改任何配置。
"""
import json, sys, statistics as st
from collections import defaultdict, Counter

def sma(v,n): return sum(v[-n:])/n if len(v)>=n else None
def rsi2_series(cl,n=2):
    out=[None]*len(cl)
    if len(cl)<n+1: return out
    g=[max(0.0,cl[i]-cl[i-1]) for i in range(1,len(cl))]
    l=[max(0.0,cl[i-1]-cl[i]) for i in range(1,len(cl))]
    ag,al=sum(g[:n])/n,sum(l[:n])/n
    out[n]=100.0 if al==0 else 100-100/(1+ag/al)
    for i in range(n,len(g)):
        ag=(ag*(n-1)+g[i])/n; al=(al*(n-1)+l[i])/n
        out[i+1]=100.0 if al==0 else 100-100/(1+ag/al)
    return out

res=json.load(open(sys.argv[1]))["data"]["results"]
MAXH=10
# trades[(side,thr)] = [(date, sym, ret%)]
trades=defaultdict(list)
THRS=[90,95,98]
for r in res:
    sym=r["symbol"]; bars=r["bars"]
    cl=[float(b["close_price"]) for b in bars]; dt=[b["begins_at"][:10] for b in bars]
    if len(cl)<260: continue
    R=rsi2_series(cl)
    for i in range(210,len(cl)-MAXH-1):
        w=cl[:i+1]; s200,rv=sma(w,200),R[i]
        if s200 is None or rv is None: continue
        c=cl[i]
        if c>s200 and rv<10:   # 多头对照
            for h in range(1,MAXH+1):
                j=i+h; 
                if cl[j]>sma(cl[:j+1],5) or (R[j] is not None and R[j]>=65) or h==MAXH:
                    trades[("long",10)].append((dt[i],sym,(cl[j]/c-1)*100)); break
        if c<s200:
            for thr in THRS:
                if rv>thr:
                    for h in range(1,MAXH+1):
                        j=i+h
                        if cl[j]<sma(cl[:j+1],5) or (R[j] is not None and R[j]<=35) or h==MAXH:
                            trades[("short",thr)].append((dt[i],sym,-(cl[j]/c-1)*100)); break

def stats(v):
    n=len(v); m=sum(v)/n; se=st.stdev(v)/(n**0.5) if n>1 else float('nan')
    s=sorted(v)
    return n,m,se,m/se if se==se and se>0 else float('nan'),sum(1 for x in v if x>0)/n*100,s[int(n*0.10)]

print("=== 5 年样本 (2021-09~2026-10, 108 只) 总体 ===")
print(f"{'信号':<22}{'n':>7}{'均值%':>10}{'95%CI':>20}{'t':>8}{'胜率':>8}{'p10':>9}")
for key,label in [(("long",10),"多头 rsi2<10 (对照)"),
                  (("short",90),"镜像空 rsi2>90"),
                  (("short",95),"镜像空 rsi2>95"),
                  (("short",98),"镜像空 rsi2>98")]:
    v=[x[2] for x in trades[key]]
    if len(v)<30: print(f"{label:<22}{len(v):>7}  样本太少"); continue
    n,m,se,t,win,p10=stats(v)
    print(f"{label:<22}{n:>7}{m:>+10.4f}  [{m-1.96*se:+.3f},{m+1.96*se:+.3f}]{t:>8.2f}{win:>7.1f}%{p10:>+9.2f}")

print("\n=== 按年 (决定性检验) ===")
print(f"{'年':<6}{'多头 n':>8}{'多头均值':>11}{'  |':>4}{'空98 n':>8}{'空98均值':>11}{'空98 t':>9}{'空98胜率':>10}")
for yr in ["2022","2023","2024","2025","2026"]:
    lv=[x[2] for x in trades[("long",10)] if x[0][:4]==yr]
    sv=[x[2] for x in trades[("short",98)] if x[0][:4]==yr]
    lm=f"{sum(lv)/len(lv):+.3f}%" if lv else "—"
    if len(sv)>=20:
        n,m,se,t,win,_=stats(sv)
        print(f"{yr:<6}{len(lv):>8}{lm:>11}{'  |':>4}{n:>8}{m:>+10.3f}%{t:>9.2f}{win:>9.1f}%")
    else:
        print(f"{yr:<6}{len(lv):>8}{lm:>11}{'  |':>4}{len(sv):>8}{'样本<20':>11}")

print("\n=== 空98 集中度 ===")
sh=trades[("short",98)]
c=Counter(s for _,s,_ in sh)
print(f"n={len(sh)} | 标的 {len(c)} 只 | 前 5: {c.most_common(5)}")
for drop in [1,3]:
    dd={s for s,_ in c.most_common(drop)}
    v2=[x[2] for x in sh if x[1] not in dd]
    if len(v2)>30:
        n,m,se,t,_,_=stats(v2)
        print(f"剔除最集中 {drop} 只: n={n}, 均值 {m:+.4f}%, t={t:.2f}")
