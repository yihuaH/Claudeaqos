"""止盈可达性换算器 —— 用户 2026-11-16 纠正后建立的口径。

🔴 **为什么存在**: 会话此前报止盈可达性时只报**权利金涨幅** ("755C 需 +134% → 彻底不可达"),
这个说法在定性上就是错的。期权 delta < 1, 故权利金涨幅对标的涨幅有巨大放大 ——
实测 755C 放大 **18 倍**、735C **34 倍**。+275% 的权利金只对应 QQQ **+8.0%**。
用户原话: 「这个判断要看 qqq 要涨的百分比」。

📐 **本脚本做三件事**:
  ① 用券商 mark 反解隐含波动率 (消掉 BS 对 r/q 假设造成的约 -4% 偏差), 使模型精确复现当日报价;
  ② 解出「bid 达到止盈线」所需的 QQQ 价格 —— 两个时点: 今日即到 / 时间止损日 (已扣 theta);
     ⚠️ 止盈判定口径是 **bid ≥ 止盈线**, 故目标 mark = 止盈线 + 当前半点差 (假设点差不变);
  ③ 用 QQQ 十年日线算该涨幅在同样交易日数内的**历史频率**, 分两种口径:
     · **收盘对收盘** —— 对应引擎自己在 15:20 的止盈检查;
     · **期间最高价触及** —— 对应常驻 GTC (盘中限价单, 触价即成交)。两者差别显著, 不要混用。

⚠️ **假设与局限**: IV 恒定 (实际上涨时 IV 通常回落, 使真实难度**高于**本估计);
点差恒定; r=0.04/q=0.005 与 qqq_leap.py:_bs_delta 同参。历史频率是无条件频率,
**不是**对当前市况的条件概率。

用法: 改 S / POS 两处常量后直接跑。数据文件由 integrations.py bars 产出。
"""
import math,json
SP='/tmp/claude-0/-home-user-Claudeaqos/3e6563b5-e5a0-5665-84d2-72b2406895d9/scratchpad'
def N(x): return 0.5*(1+math.erf(x/math.sqrt(2)))
def bs(S,K,T,sig,r=0.04,q=0.005):
    if T<=0: return max(S-K,0.0)
    d1=(math.log(S/K)+(r-q+0.5*sig*sig)*T)/(sig*math.sqrt(T)); d2=d1-sig*math.sqrt(T)
    return S*math.exp(-q*T)*N(d1)-K*math.exp(-r*T)*N(d2)
def iv_of(price,S,K,T):
    lo,hi=0.01,3.0
    for _ in range(200):
        m=(lo+hi)/2
        if bs(S,K,T,m)<price: lo=m
        else: hi=m
    return (lo+hi)/2
def spot_for(price,K,T,sig):
    lo,hi=1.0,5000.0
    for _ in range(200):
        m=(lo+hi)/2
        if bs(m,K,T,sig)<price: lo=m
        else: hi=m
    return (lo+hi)/2

S=661.82
POS=[("755C 27-03-19",755.0,123, 8.07, 8.210,18.900,14, 9),
     ("735C 27-01-15",735.0, 60, 3.91, 3.960,14.652,14, 9),
     ("725C 27-03-19",725.0,123,15.43,15.570,18.600,29,20)]
rows=[]
print("=== 用 mark 反解 IV (消掉 -4% 模型偏差) ===")
for nm,K,dte,bid,mark,tp,cal,tdays in POS:
    T0=dte/365.0; iv=iv_of(mark,S,K,T0)
    half=mark-bid; tgt=tp+half
    s_now=spot_for(tgt,K,T0,iv); s_stop=spot_for(tgt,K,max(dte-cal,1)/365.0,iv)
    rows.append((nm,tp/bid-1,s_now,s_stop,tdays,iv))
    print(f"{nm}: 反解 IV {iv:.4f} (券商报 ~0.215-0.225) · BS 复现 mark {bs(S,K,T0,iv):.3f}")
print()
print(f"{'合约':<16}{'权利金需涨':>11}{'今日达线QQQ':>13}{'↑%':>8}{'止损日达线QQQ':>15}{'↑%':>8}{'剩余交易日':>11}")
for nm,prem,s_now,s_stop,td,iv in rows:
    print(f"{nm:<16}{prem*100:>10.1f}%{s_now:>13.1f}{(s_now/S-1)*100:>+8.2f}%{s_stop:>15.1f}{(s_stop/S-1)*100:>+8.2f}%{td:>11}")

print()
print("=== 历史频率: QQQ 在 N 个交易日内涨到 +X% 的概率 (收盘对收盘, 期间任一日触及即算) ===")
b=json.load(open(f'{SP}/qqq_bars_long.json'))
bars=[x for x in b['data']['results'][0]['bars']]
cl=[float(x['close_price']) for x in bars]; hi=[float(x.get('high_price') or x['close_price']) for x in bars]
print(f"样本: {bars[0]['begins_at'][:10]} ~ {bars[-1]['begins_at'][:10]}, {len(cl)} 个交易日")
for need,nd,lab in ((7.44,9,'755C'),(8.08,9,'735C'),(3.83,20,'725C')):
    hitc=hitt=tot=0
    for i in range(len(cl)-nd):
        tot+=1
        tgt=cl[i]*(1+need/100)
        if cl[i+nd]>=tgt: hitc+=1
        if max(hi[i+1:i+nd+1])>=tgt: hitt+=1
    print(f"  {lab}: 需 {need:+.2f}% / {nd} 交易日 → 按收盘 {hitc}/{tot} = {hitc/tot*100:.1f}% · 按期间最高价触及 {hitt}/{tot} = {hitt/tot*100:.1f}%")
