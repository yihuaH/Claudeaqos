"""稳健性检查: 镜像空头在几个阈值上是否都没有边际。
**不是在搜一个能赚钱的配置** (那是过拟合) —— 是检验"无边际"这个结论是否只是阈值巧合。
"""
import json, sys, statistics as st

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
print(f"{'入场阈值':<16}{'n':>7}{'均值%':>10}{'95%CI':>18}{'t':>8}{'胜率':>8}{'p10':>9}")
for thr, exit_rsi in [(90,35),(95,35),(98,35),(90,20),(95,20)]:
    tr=[]
    for r in res:
        cl=[float(b["close_price"]) for b in r["bars"]]
        if len(cl)<260: continue
        R=rsi2_series(cl)
        for i in range(210,len(cl)-MAXH-1):
            w=cl[:i+1]; s200,rv=sma(w,200),R[i]
            if s200 is None or rv is None: continue
            c=cl[i]
            if c<s200 and rv>thr:
                for h in range(1,MAXH+1):
                    j=i+h; wj=cl[:j+1]
                    if cl[j]<sma(wj,5) or (R[j] is not None and R[j]<=exit_rsi) or h==MAXH:
                        tr.append(-(cl[j]/c-1)*100); break
    if len(tr)<30:
        print(f"rsi2>{thr}/出场<={exit_rsi:<4} n={len(tr)} 样本太少"); continue
    n=len(tr); m=sum(tr)/n; se=st.stdev(tr)/(n**0.5); t2=sorted(tr)
    win=sum(1 for x in tr if x>0)/n*100
    print(f"rsi2>{thr}/出场<={exit_rsi:<6}{n:>7}{m:>+10.4f}  [{m-1.96*se:+.3f},{m+1.96*se:+.3f}]{m/se:>8.2f}{win:>7.1f}%{t2[int(n*0.10)]:>+9.2f}")
