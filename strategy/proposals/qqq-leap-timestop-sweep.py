#!/usr/bin/env python3
"""QQQ LEAP **时间止损扫描**: 固定 20 交易日 vs 按 DTE 比例。**仅研究用, 不产生任何订单**。

⛔ 不被 daily.py / session.py 调用 (红线2: 订单只能来自确定性引擎)。
回答用户 2026-10-22「我觉得时间限制不应该都是20天, 是不是应该根据期权的长度, 按百分比
或者其他参数来决定」。

本扫描要分清**两个互相冲突的理由**, 它们指向相反方向:
  (A) 「alpha 窗口论」—— 时间止损管的是**信号的有效期**, 不是合约的寿命。§12.5 认为跌≥2%
      反弹的 alpha 约两周内出清, 那么无论用什么载体表达这个观点, 持有期都该 ≈ 两周, **与 DTE 无关**。
      => 固定日数是对的, 且更短可能更好 (§12.5 实测 10 日增量 +8.8pp > 20 日 +6.7pp > 40 日 +5.2pp)。
  (B) 「别把短期权拿到到期」—— 30 天期权持 20 交易日 (≈28 日历日) 就是拿到归零,
      theta 在最后两周吃掉全部外在价值。=> 止损应随 DTE 缩放, 至少对短档要有个上限。

结论若两条都成立, 正解不是二选一, 而是 **min(固定日数, DTE 比例)** —— 固定档承重,
比例档只在短 DTE 上**兜底**。本脚本就是去证伪/证实这一点。

口径继承 qqq-leap-dte-sweep.py (再往上继承 concurrency.py): delta 0.30 / 止盈 +20% /
§16.1 点差拟合 / 入场 = 跌≥2% + 牛市过滤。**只改时间止损规则。**

⚠️ 单位陷阱: DTE 是**日历日**, 时间止损是**交易日**。比例规则须换算 ×252/365, 否则
"25% of DTE" 对 365 档会被当成 91 个交易日 (≈4.3 个月), 而真实意图是 91 个日历日 (≈63 交易日)。

用法: python3 strategy/proposals/qqq-leap-timestop-sweep.py
"""
import json, math, statistics
src = open('strategy/proposals/qqq-leap-dte-sweep.py').read().split('DTES=[30,')[0]
exec(src)

TD_PER_CAL = 252.0 / 365.0

def trade_rule(i, dte, maxhold):
    """同 trade(), 但 maxhold 由调用方按规则算好 (交易日)。"""
    return trade(i, dte, maxhold=maxhold)

def run(dte, maxhold):
    return [t for t in (trade_rule(i, dte, maxhold) for i in SIGS) if t]

def paired(a, b):
    """同一批入场上的配对检验 (a - b)。返回 (均值差, t, p近似)。"""
    d = [x['pct'] - y['pct'] for x, y in zip(a, b)]
    if len(d) < 3: return (0.0, 0.0, 1.0)
    m = statistics.mean(d); s = statistics.stdev(d)
    t = m / (s / math.sqrt(len(d))) if s > 0 else 0.0
    # 正态近似双尾 p (n≈89, t 分布与正态差别可忽略)
    p = 2 * (1 - 0.5 * (1 + math.erf(abs(t) / math.sqrt(2))))
    return (m, t, p)

DTES = [30, 60, 91, 182, 365, 730]

print("=" * 108)
print("第 1 问: 对每个 DTE, 最优固定时间止损是多少? (若最优值随 DTE 漂移 => 该缩放)")
print("=" * 108)
FLATS = [5, 8, 10, 13, 15, 20, 25, 30, 40, 60]
cache = {}
print(f"{'DTE':>5s} | " + "".join(f"{f'{f}d':>9s}" for f in FLATS) + "  | 最优")
print("-" * 108)
for dte in DTES:
    row, best = [], None
    for f in FLATS:
        ts = run(dte, f)
        if not ts: row.append(None); continue
        m = statistics.mean(t['pct'] for t in ts)
        cache[(dte, f)] = ts
        row.append(m)
        if best is None or m > best[1]: best = (f, m)
    print(f"{dte:>5d} | " + "".join(f"{v:>+8.2f}%" if v is not None else f"{'—':>9s}" for v in row)
          + f"  | {best[0]}d {best[1]:+.2f}%")

print("\n" + "=" * 108)
print("第 2 问: 均持天数与到期率 —— 20 日这道闸到底在多少比例的交易上真的咬住了?")
print("=" * 108)
print(f"{'DTE':>5s}{'n':>5s}{'均持(交易日)':>14s}{'止盈率':>8s}{'时间止损率':>12s}{'到期率':>8s}"
      f"{'止损单均值':>12s}{'止盈单均值':>12s}")
print("-" * 108)
for dte in DTES:
    ts = cache[(dte, 20)]
    tsl = [t for t in ts if t['why'] == 'time_stop']
    tp  = [t for t in ts if t['why'] == 'take_profit']
    print(f"{dte:>5d}{len(ts):>5d}{statistics.mean(t['held'] for t in ts):>14.1f}"
          f"{len(tp)/len(ts)*100:>7.1f}%{len(tsl)/len(ts)*100:>11.1f}%"
          f"{sum(1 for t in ts if t['why']=='expired')/len(ts)*100:>7.1f}%"
          f"{(statistics.mean(t['pct'] for t in tsl) if tsl else float('nan')):>+11.2f}%"
          f"{(statistics.mean(t['pct'] for t in tp) if tp else float('nan')):>+11.2f}%")

print("\n" + "=" * 108)
print("第 3 问: 比例规则 (maxhold = pct × DTE, 换算成交易日) 对比固定 20 日")
print("=" * 108)
PCTS = [0.10, 0.15, 0.20, 0.25, 0.33, 0.50]
print(f"{'DTE':>5s}{'固定20d':>10s} | " + "".join(f"{f'{int(p*100)}%':>11s}" for p in PCTS))
print(f"{'':>5s}{'':>10s} | " + "".join(f"{'(=Nd)':>11s}" for p in PCTS))
print("-" * 108)
for dte in DTES:
    base = statistics.mean(t['pct'] for t in cache[(dte, 20)])
    cells = []
    for p in PCTS:
        mh = max(1, int(round(p * dte * TD_PER_CAL)))
        ts = run(dte, mh)
        cells.append((mh, statistics.mean(t['pct'] for t in ts) if ts else None))
    print(f"{dte:>5d}{base:>+9.2f}% | " + "".join(
        f"{v:>+7.2f}%{f'({mh})':>4s}" if v is not None else f"{'—':>11s}" for mh, v in cells))

print("\n" + "=" * 108)
print("第 4 问: 混合规则 min(固定 F, pct×DTE) —— 比例档只对短 DTE 兜底")
print("=" * 108)
for F in (20, 13, 10):
    print(f"\n-- 固定档 F = {F} 交易日 --")
    print(f"{'DTE':>5s}{'纯固定':>10s} | " + "".join(f"{f'min(F,{int(p*100)}%)':>17s}" for p in (0.25, 0.33, 0.50)))
    print("-" * 80)
    for dte in DTES:
        base_ts = run(dte, F)
        base = statistics.mean(t['pct'] for t in base_ts)
        cells = []
        for p in (0.25, 0.33, 0.50):
            mh = max(1, min(F, int(round(p * dte * TD_PER_CAL))))
            ts = run(dte, mh)
            m, t, pv = paired(ts, base_ts)
            cells.append((mh, statistics.mean(x['pct'] for x in ts), m, pv, mh == F))
        print(f"{dte:>5d}{base:>+9.2f}% | " + "".join(
            f"{'  = 纯固定':>17s}" if same else f"{v:>+8.2f}%(={mh:>2d}){'':>1s}" + ("*" if pv < 0.05 else " ")
            for mh, v, m, pv, same in cells))
    print("  (* = 与纯固定档的配对检验 p<0.05; '= 纯固定' 表示该档比例值 ≥F, min 取到 F, 规则无差异)")

print("\n" + "=" * 108)
print("第 5 问: 只看短 DTE —— 比例兜底救不救得了 30/60 档?")
print("=" * 108)
for dte in (30, 60, 91):
    b20 = cache[(dte, 20)]
    print(f"\n-- DTE {dte} (固定20日基准: 均值 {statistics.mean(t['pct'] for t in b20):+.2f}%, "
          f"到期率 {sum(1 for t in b20 if t['why']=='expired')/len(b20)*100:.1f}%) --")
    print(f"{'maxhold':>8s}{'=pct of DTE':>13s}{'均值':>9s}{'中位':>9s}{'胜率':>8s}"
          f"{'到期率':>8s}{'亏≥20%':>8s}{'vs20d':>9s}{'p':>8s}")
    for mh in (3, 5, 8, 10, 13, 15, 20):
        ts = run(dte, mh)
        if not ts: continue
        p = [t['pct'] for t in ts]
        m, tt, pv = paired(ts, b20)
        print(f"{mh:>8d}{mh/(dte*TD_PER_CAL)*100:>12.0f}%{statistics.mean(p):>+8.2f}%"
              f"{statistics.median(p):>+8.2f}%{sum(1 for x in p if x>0)/len(p)*100:>7.1f}%"
              f"{sum(1 for t in ts if t['why']=='expired')/len(ts)*100:>7.1f}%"
              f"{sum(1 for x in p if x<=-20)/len(p)*100:>7.1f}%{m:>+8.2f}%{pv:>8.3f}")

print("\n" + "=" * 108)
print("第 6 问 (决定性): 长 DTE 拿久一点赚更多 —— 是信号的 alpha, 还是牛市里拿 call 的 beta?")
print("  做法同 dte-sweep: 对照组 = 牛市中每天买 (去掉跌≥2% 要求), 同样的止损规则。")
print("  若信号组的改善在对照组同样出现 => 那是 beta, 把时间止损放长等于偷偷加杠杆, 不是改进。")
print("=" * 108)
import random
random.seed(20261022)
CTRL_S = random.sample(CTRL, 400)   # 对照组 2400+ 个, 抽 400 控制算力
for dte in (30, 91, 365, 730):
    print(f"\n-- DTE {dte} --")
    print(f"{'maxhold':>8s}{'信号组均值':>12s}{'对照组均值':>12s}{'增量(信号-对照)':>18s}"
          f"{'信号组均持':>12s}{'信号组到期率':>14s}")
    base_inc = None
    for mh in (8, 13, 20, 30, 40, 60, 90, 126):
        if mh > dte * TD_PER_CAL * 1.05: continue
        s_ts = run(dte, mh)
        c_ts = [t for t in (trade(i, dte, maxhold=mh) for i in CTRL_S) if t]
        if not s_ts or not c_ts: continue
        sm = statistics.mean(t['pct'] for t in s_ts)
        cm = statistics.mean(t['pct'] for t in c_ts)
        inc = sm - cm
        if base_inc is None and mh == 20: base_inc = inc
        mark = ""
        if mh == 20: mark = "  <= 现行"
        print(f"{mh:>8d}{sm:>+11.2f}%{cm:>+11.2f}%{inc:>+17.2f}pp"
              f"{statistics.mean(t['held'] for t in s_ts):>12.1f}"
              f"{sum(1 for t in s_ts if t['why']=='expired')/len(s_ts)*100:>13.1f}%{mark}")

print("\n" + "=" * 108)
print("第 7 问: 时间止损单的均值随 DTE 怎么变 (= 为什么短 DTE 才真的需要兜底)")
print("=" * 108)
print(f"{'DTE':>5s}{'止损单n':>9s}{'止损单均值':>12s}{'止损单中位':>12s}{'止损单最差':>12s}"
      f"{'止损时剩余DTE':>15s}{'剩余/原DTE':>12s}")
print("-" * 108)
for dte in DTES:
    ts = [t for t in cache[(dte, 20)] if t['why'] == 'time_stop']
    p = [t['pct'] for t in ts]
    rem = dte - 20 / TD_PER_CAL     # 20 交易日 ≈ 29 日历日
    print(f"{dte:>5d}{len(ts):>9d}{statistics.mean(p):>+11.2f}%{statistics.median(p):>+11.2f}%"
          f"{min(p):>+11.2f}%{rem:>14.0f}d{rem/dte*100:>11.0f}%")

print("\n" + "=" * 108)
print("第 8 问: 改时间止损会不会推翻 delta 0.30? (文档 §7 明写「想用 10 日必须重跑 §16」)")
print("  §16 的全部 delta 扫描都跑在 20 日档上。若 13 日档的最优 delta 仍是 0.30 附近,")
print("  那道阻塞就自行消解; 若最优 delta 漂走, 则时间止损与 delta 必须一起重定。")
print("=" * 108)
_orig_tgt = DELTA_TGT
for dte in (91, 365):
    print(f"\n-- DTE {dte} · 行按 maxhold, 列按 delta 目标, 格内 = 信号增量 (信号组 - 对照组) --")
    hdr = [0.20, 0.25, 0.30, 0.35, 0.40, 0.50, 0.60]
    print(f"{'maxhold':>8s} | " + "".join(f"{d:>10.2f}" for d in hdr) + "  | 最优 delta")
    print("-" * 100)
    for mh in (8, 13, 20, 30):
        cells, best = [], None
        for d in hdr:
            globals()['DELTA_TGT'] = d
            s = [t for t in (trade(i, dte, maxhold=mh) for i in SIGS) if t]
            c = [t for t in (trade(i, dte, maxhold=mh) for i in CTRL_S) if t]
            if not s or not c: cells.append(None); continue
            inc = statistics.mean(t['pct'] for t in s) - statistics.mean(t['pct'] for t in c)
            cells.append(inc)
            if best is None or inc > best[1]: best = (d, inc)
        print(f"{mh:>8d} | " + "".join(f"{v:>+9.2f}p" if v is not None else f"{'—':>10s}" for v in cells)
              + f"  | {best[0]:.2f} ({best[1]:+.1f}pp)" + ("   <= 现行" if mh == 20 else ""))
globals()['DELTA_TGT'] = _orig_tgt

print("\n" + "=" * 108)
print("第 9 问: 落到我们**实际持有的三张** DTE 上, 13 日 vs 20 日的配对检验")
print("=" * 108)
print(f"{'DTE':>5s}{'(对应持仓)':>22s}{'20日均值':>11s}{'13日均值':>11s}{'差':>9s}{'t':>8s}{'p':>8s}"
      f"{'20日增量':>11s}{'13日增量':>11s}")
print("-" * 108)
for dte, who in ((85, 'QQQ270115C755'), (148, 'QQQ270319C775'), (330, 'QQQ270917C830')):
    a = [t for t in (trade(i, dte, maxhold=13) for i in SIGS) if t]
    b = [t for t in (trade(i, dte, maxhold=20) for i in SIGS) if t]
    ca = [t for t in (trade(i, dte, maxhold=13) for i in CTRL_S) if t]
    cb = [t for t in (trade(i, dte, maxhold=20) for i in CTRL_S) if t]
    m, tt, pv = paired(a, b)
    ia = statistics.mean(t['pct'] for t in a) - statistics.mean(t['pct'] for t in ca)
    ib = statistics.mean(t['pct'] for t in b) - statistics.mean(t['pct'] for t in cb)
    print(f"{dte:>5d}{who:>22s}{statistics.mean(t['pct'] for t in b):>+10.2f}%"
          f"{statistics.mean(t['pct'] for t in a):>+10.2f}%{m:>+8.2f}%{tt:>8.2f}{pv:>8.3f}"
          f"{ib:>+10.2f}p{ia:>+10.2f}p")

print("\n" + "=" * 108)
print("第 10 问: 若真要加 DTE 相关的兜底, 正确形状是「到期前留几个日历日」而非「百分比」")
print("  理由见第 7 问: 短 DTE 的伤害来自**止损点落进合约最后两周** (theta 把亏单清零),")
print("  而不是「20 占 30 的比例太高」。缓冲是 DTE 无关的, 百分比不是。")
print("=" * 108)
F = 13
print(f"固定档 F={F} 交易日; 规则 = min(F, (DTE - buffer) × 252/365)")
print(f"{'buffer':>8s} | " + "".join(f"{f'DTE{d}':>12s}" for d in (30, 60, 91, 182, 365, 730)))
print("-" * 92)
for buf in (0, 7, 14, 21, 30):
    cells = []
    for d in (30, 60, 91, 182, 365, 730):
        mh = max(1, min(F, int(round((d - buf) * TD_PER_CAL))))
        cells.append((mh, mh == F))
    print(f"{buf:>8d} | " + "".join(f"{f'{mh}d':>12s}" if not same else f"{f'{mh}d (=F)':>12s}"
                                    for mh, same in cells))
print("\n=> buffer 只在 DTE 30 (以及 buffer≥30 时的 DTE 60) 上咬住, 其余全部退化为纯固定 F。")
print("   而我们实际持有的最短一张是 85 DTE => **这道兜底对现有仓位全程不生效**。")

print("\n" + "=" * 108)
print("第 11 问 (结案): 现行 config 其实已经有这道闸 —— exit.force_exit_dte_lte = 30")
print("  qqq_leap.py:322 `elif dleft <= force_exit_dte_lte: reason='force_exit_dte'`")
print("  它等价于 maxhold_eff = min(20 交易日, 到 DTE 剩 30 为止) —— 正是第 10 问推出的")
print("  「日历日缓冲」形状, 不是百分比。原注释「理论上不可能触发」是只买 365 DTE 时写的。")
print("=" * 108)
FORCE_DTE = 30
MH = 20
print(f"{'DTE':>6s}{'20日止损时剩DTE':>17s}{'force闸咬不咬':>15s}{'实际maxhold':>13s}"
      f"{'纯20日均值':>12s}{'含force闸均值':>15s}")
print("-" * 108)
for dte in (30, 45, 60, 75, 85, 91, 148, 182, 330, 365, 730):
    rem_at_20 = dte - MH / TD_PER_CAL
    mh_eff = max(0, min(MH, int((dte - FORCE_DTE) * TD_PER_CAL)))
    bites = rem_at_20 <= FORCE_DTE
    a = run(dte, MH)
    b = run(dte, mh_eff) if mh_eff >= 1 else None
    print(f"{dte:>6d}{rem_at_20:>16.0f}d{('咬' if bites else '不咬'):>15s}"
          f"{(f'{mh_eff}d' if mh_eff >= 1 else '入场即平'):>13s}"
          f"{(statistics.mean(t['pct'] for t in a) if a else float('nan')):>+11.2f}%"
          f"{(statistics.mean(t['pct'] for t in b) if b else float('nan')):>+14.2f}%")
print("\n=> 分界线在 DTE ≈ 59 (= 30 + 20/0.690)。**59 以上现行规则就是纯 20 交易日**,")
print("   59 以下 force 闸自动把持有期压短, DTE ≤ 30 则入场即平 (等于拒绝该档)。")
print("   我们三张 85 / 148 / 330 DTE **全在分界线之上 => 现行行为就是纯 20 日, 闸不咬**。")
print("   换言之: 用户直觉要的「按期权长度缩放」**已经在配置里了**, 只是以 DTE 地板形式表达,")
print("   且因为我们只买长档, 它平时不动作 —— 这正是 §7.2 注释「纯防御兜底」的本意。")

print("\n" + "=" * 108)
print("⚠️ 本扫描的已知口径局限 (写报告时必须一并交代)")
print("=" * 108)
print("""1. **delta 那一列不可用于改 delta** (第 8 问): 增量在 0.20→0.60 上**单调递减、无内部峰值**,
   这是**加杠杆的特征而非最优值的特征** —— 更深 OTM 就是同一个赌注的杠杆版, 而「均值增量」
   奖励杠杆却不罚方差。真有最优值会看到内部峰。故第 8 问只能用来回答它被设计来回答的那个问题:
   **「改 maxhold 会不会改变 delta 的排序」—— 答案是不会** (8/13/20/30 四档排序完全一致),
   §7 注释里「想用 10 日必须重跑 §16」这道阻塞因此解除; 但**不构成把 0.30 换成 0.20 的依据**。
2. IV 由 KAPPA=1.280 **单点反解** (§12.1), 点差用 §16.1 的 n=3 拟合 —— 后者已于 2026-10-22
   被实盘报价证伪在短 DTE 上**高估约 17 倍**。对本扫描影响有限 (点差在各 maxhold 档之间对称),
   但 DTE 30 的绝对数字仍不可信。到期结算按内在价值、无点差, 故第 7 问的 −93.64% 不受此影响。
3. n=89 个信号 / 10.4 年。第 9 问三档 p = 0.41 / 0.72 / 0.77, 全部不显著。
4. 对照组抽样 400 个 (全集 2400+), 随机种子固定 20261022。""")
