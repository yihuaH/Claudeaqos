"""镜像 RSI-2 看空信号的第一轮确定性检验 (纯读历史 bars, 不下任何单)。

多头 (现行, 已验证): close > SMA200 且 rsi2 < 10  → 买, 出场 close>SMA5 或 rsi2>=65 或 10日时间止损
镜像空头 (待检验):   close < SMA200 且 rsi2 > 90  → 做空, 出场 close<SMA5 或 rsi2<=35 或 10日时间止损

只衡量**标的本身**的方向性收益 (不含期权杠杆/时间价值), 这是信号是否存在的下限检验:
若标的层面没有负期望, 买 put 只会更差 (还要付时间价值)。
"""
import json, sys, statistics as st

def sma(v, n):
    return sum(v[-n:]) / n if len(v) >= n else None

def rsi2_series(cl, n=2):
    out = [None] * len(cl)
    if len(cl) < n + 1: return out
    g = [max(0.0, cl[i] - cl[i-1]) for i in range(1, len(cl))]
    l = [max(0.0, cl[i-1] - cl[i]) for i in range(1, len(cl))]
    ag, al = sum(g[:n]) / n, sum(l[:n]) / n
    out[n] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    for i in range(n, len(g)):
        ag = (ag * (n - 1) + g[i]) / n
        al = (al * (n - 1) + l[i]) / n
        out[i + 1] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    return out

res = json.load(open(sys.argv[1]))["data"]["results"]
MAXH = 10
long_tr, short_tr = [], []

for r in res:
    bars = r["bars"]
    cl = [float(b["close_price"]) for b in bars]
    if len(cl) < 260: continue
    R = rsi2_series(cl)
    for i in range(210, len(cl) - MAXH - 1):
        w = cl[:i+1]
        s200, s5, rv = sma(w, 200), sma(w, 5), R[i]
        if s200 is None or s5 is None or rv is None: continue
        c = cl[i]
        # 多头
        if c > s200 and rv < 10:
            for h in range(1, MAXH + 1):
                j = i + h
                wj = cl[:j+1]
                if cl[j] > sma(wj, 5) or (R[j] is not None and R[j] >= 65) or h == MAXH:
                    long_tr.append((cl[j] / c - 1) * 100); break
        # 镜像空头 (做空标的的收益 = -(价格变化))
        if c < s200 and rv > 90:
            for h in range(1, MAXH + 1):
                j = i + h
                wj = cl[:j+1]
                if cl[j] < sma(wj, 5) or (R[j] is not None and R[j] <= 35) or h == MAXH:
                    short_tr.append(-(cl[j] / c - 1) * 100); break

def rep(name, t):
    if not t:
        print(f"{name}: 无样本"); return
    t2 = sorted(t)
    win = sum(1 for x in t if x > 0)
    n = len(t)
    mean = sum(t) / n
    se = st.stdev(t) / (n ** 0.5) if n > 1 else float('nan')
    print(f"{name}: n={n:6d} | 均值 {mean:+.4f}% (±{1.96*se:.4f} 95%CI) | 中位 {t2[n//2]:+.3f}% "
          f"| 胜率 {win/n*100:.1f}% | p10 {t2[int(n*0.10)]:+.2f}% | p90 {t2[int(n*0.90)]:+.2f}% "
          f"| t={mean/se:+.2f}" if n > 1 else "")

print("=== 标的层面方向性收益 (同一组 bars, 同一出场逻辑, 镜像对称) ===")
rep("多头 RSI-2 (现行·已验证)", long_tr)
rep("镜像空头 RSI-2 (待检验) ", short_tr)
