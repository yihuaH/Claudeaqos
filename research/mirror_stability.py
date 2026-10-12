"""对 rsi2>98 这一档做稳定性解剖: 按年分段 + 标的集中度 + 与多头同期对照。
目的是检验 t=3.61 是真信号还是少数标的/少数时段的产物。
"""
import json, sys, statistics as st
from collections import Counter, defaultdict

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
short=[]; longs=[]
for r in res:
    sym=r["symbol"]; bars=r["bars"]
    cl=[float(b["close_price"]) for b in bars]; dt=[b["begins_at"][:10] for b in bars]
    if len(cl)<260: continue
    R=rsi2_series(cl)
    for i in range(210,len(cl)-MAXH-1):
        w=cl[:i+1]; s200,rv=sma(w,200),R[i]
        if s200 is None or rv is None: continue
        c=cl[i]
        if c<s200 and rv>98:
            for h in range(1,MAXH+1):
                j=i+h; wj=cl[:j+1]
                if cl[j]<sma(wj,5) or (R[j] is not None and R[j]<=35) or h==MAXH:
                    short.append((dt[i],sym,-(cl[j]/c-1)*100)); break
        if c>s200 and rv<10:
            for h in range(1,MAXH+1):
                j=i+h; wj=cl[:j+1]
                if cl[j]>sma(wj,5) or (R[j] is not None and R[j]>=65) or h==MAXH:
                    longs.append((dt[i],sym,(cl[j]/c-1)*100)); break

def seg(rows,label):
    by=defaultdict(list)
    for d,s,v in rows: by[d[:7]].append(v)
    print(f"\n--- {label} 按月 (样本>=20 的月份) ---")
    ok=0; bad=0
    for m in sorted(by):
        v=by[m]
        if len(v)<20: continue
        mean=sum(v)/len(v)
        print(f"  {m}  n={len(v):4d}  均值 {mean:+7.3f}%  胜率 {sum(1 for x in v if x>0)/len(v)*100:5.1f}%")
        ok += mean>0; bad += mean<=0
    print(f"  → 正月份 {ok} / 负月份 {bad}")

print("=== rsi2>98 镜像空头 解剖 ===")
n=len(short); vals=[v for _,_,v in short]
m=sum(vals)/n; se=st.stdev(vals)/(n**0.5)
print(f"总体: n={n}, 均值 {m:+.4f}%, t={m/se:.2f}")
c=Counter(s for _,s,_ in short)
top=c.most_common(8)
print(f"涉及标的数: {len(c)} | 最集中前 8: {top}")
topn=sum(k for _,k in top)
print(f"前 8 只占样本: {topn}/{n} = {topn/n*100:.1f}%")
# 剔除最集中的 1 只后
for drop in [1,3]:
    dropped={s for s,_ in c.most_common(drop)}
    v2=[v for _,s,v in short if s not in dropped]
    if len(v2)>30:
        m2=sum(v2)/len(v2); se2=st.stdev(v2)/(len(v2)**0.5)
        print(f"剔除最集中 {drop} 只后: n={len(v2)}, 均值 {m2:+.4f}%, t={m2/se2:.2f}")
seg(short,"镜像空头 rsi2>98")
seg(longs,"多头 rsi2<10 (同期对照)")
