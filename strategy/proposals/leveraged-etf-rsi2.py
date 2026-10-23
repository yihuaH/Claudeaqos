#!/usr/bin/env python3
"""把现行 RSI-2 规则照搬到 3 倍杠杆 ETF (TQQQ / SQQQ) 上会怎样。**仅研究用, 不产生任何订单。**

⛔ 不被 daily.py / session.py 调用 (红线2)。回答用户 2026-10-23「跑个回测看看 tqqq 和 sqqq」。
⛔ TQQQ/SQQQ **不在任何轨道的标的池里** (仓库全文搜无命中), 本脚本不改变这一点。

规则**逐字段照抄 `strategy/config.json`** (不凭记忆):
  入场  rsi2 < 10 (Wilder, period=2) 且 close > SMA200
  出场  优先级: stop_loss (px ≤ 入场×(1−7%)) → exit_strength (px > SMA5 **或** rsi2 ≥ 65)
                → time_stop (持满 10 个交易日)   ← **10 不是 20**; 20 是 LEAP 轨道的
  防御  近 20 日任一单日 |涨跌| ≥ 8% → 跳过入场。**但 defense.exempt_symbols 整体豁免该闸**,
        现行 exempt 名单是 8 只 1 倍 ETF, TQQQ/SQQQ **不在其中** → 照搬即受该闸约束。
  冲突  「有卖有买就不卖」(2026-10-14 用户定): 同日既触发出场又满足入场信号 → 当日零动作。

口径: 单标的、单仓位、顺序回测 (持仓时满仓, 空仓时现金)。
⚠️ **因此总收益与实盘不可比** —— 实盘是 130 只标的各占净值 10% 的组合。本脚本只回答
   「这套规则放在这个标的上行不行」, 不回答「组合会赚多少」。
⚠️ 未计交易成本。TQQQ/SQQQ 是成交量最大的 ETF 之一, 点差约 1bp, 对结论无影响; 但 SQQQ
   长期反向拆股, 早年绝对价高, 分数股/整股差异未建模。

用法: python3 strategy/proposals/leveraged-etf-rsi2.py
"""
import json, statistics

SP = '/tmp/claude-0/-home-user-Claudeaqos/3e6563b5-e5a0-5665-84d2-72b2406895d9/scratchpad'
RAW = json.load(open(f'{SP}/lev_bars.json'))['data']['results']
SER = {}
for x in RAW:
    b = x['bars']
    SER[x['symbol']] = ([q['begins_at'][:10] for q in b], [float(q['close_price']) for q in b])

CFG = json.load(open('strategy/config.json'))
RSI2_MAX = float(CFG['entry']['rsi2_max'])
RSI2_MIN = float(CFG['exit']['rsi2_min'])
STOP_DEF = float(CFG['exit']['stop_loss_pct'])
MAXHOLD = int(CFG['exit']['max_holding_days'])
MOVE_THR = float(CFG['defense']['max_daily_move_pct'])
MOVE_LOOK = int(CFG['defense']['move_lookback_days'])


def wilder_rsi_series(c, p=2):
    """滚动 Wilder RSI, 口径与 signals.py:88 的 rsi() 逐步一致 (同一递推, 只是保留整条序列)。"""
    out = [None] * len(c)
    if len(c) <= p:
        return out
    g = sum(max(c[i] - c[i - 1], 0.0) for i in range(1, p + 1)) / p
    l = sum(max(c[i - 1] - c[i], 0.0) for i in range(1, p + 1)) / p
    out[p] = 100.0 if l == 0 else 100.0 - 100.0 / (1.0 + g / l)
    for i in range(p + 1, len(c)):
        d = c[i] - c[i - 1]
        g = (g * (p - 1) + max(d, 0.0)) / p
        l = (l * (p - 1) + max(-d, 0.0)) / p
        out[i] = 100.0 if l == 0 else 100.0 - 100.0 / (1.0 + g / l)
    return out


def sma(c, n):
    out = [None] * len(c)
    s = 0.0
    for i, v in enumerate(c):
        s += v
        if i >= n:
            s -= c[i - n]
        if i >= n - 1:
            out[i] = s / n
    return out


def prep(sym):
    dts, c = SER[sym]
    return dict(dates=dts, c=c, rsi2=wilder_rsi_series(c, 2),
                s200=sma(c, 200), s5=sma(c, 5),
                ret=[None] + [(c[i] / c[i - 1] - 1) * 100 for i in range(1, len(c))])


def entry_ok(D, i, move_filter):
    if D['s200'][i] is None or D['rsi2'][i] is None:
        return False
    if not (D['c'][i] > D['s200'][i] and D['rsi2'][i] < RSI2_MAX):
        return False
    if move_filter:
        for j in range(max(1, i - MOVE_LOOK + 1), i + 1):
            if D['ret'][j] is not None and abs(D['ret'][j]) >= MOVE_THR:
                return False
    return True


def backtest(sym, move_filter=True, stop_pct=STOP_DEF, conflict_rule=True):
    D = prep(sym)
    n = len(D['c'])
    pos = None           # dict(entry_px, entry_i)
    trades, eq, curve = [], 1.0, []
    blocked_by_move = cancelled_by_conflict = 0
    for i in range(1, n):
        px = D['c'][i]
        if pos is not None:
            reason = None
            if px <= pos['entry_px'] * (1 - stop_pct / 100.0):
                reason = 'stop_loss'
            elif (D['s5'][i] is not None and px > D['s5'][i]) or (
                    D['rsi2'][i] is not None and D['rsi2'][i] >= RSI2_MIN):
                reason = 'exit_strength'
            elif i - pos['entry_i'] >= MAXHOLD:
                reason = 'time_stop'
            # 「有卖有买就不卖」: 出场与入场信号同日成立 → 当日零动作
            if reason and conflict_rule and entry_ok(D, i, move_filter):
                cancelled_by_conflict += 1
                reason = None
            if reason:
                r = px / pos['entry_px']
                eq *= r
                trades.append(dict(pct=(r - 1) * 100, why=reason,
                                   held=i - pos['entry_i'], date=D['dates'][i]))
                pos = None
        if pos is None and entry_ok(D, i, move_filter):
            pos = dict(entry_px=px, entry_i=i)
        elif pos is None and move_filter:
            # 统计「仅因异动闸被挡」的次数
            if entry_ok(D, i, False):
                blocked_by_move += 1
        curve.append(eq if pos is None else eq * px / pos['entry_px'])
    peak = dd = 0.0
    for v in curve:
        peak = max(peak, v)
        dd = min(dd, v / peak - 1)
    p = [t['pct'] for t in trades]
    yrs = n / 252.0
    return dict(
        sym=sym, n=len(trades), per_yr=len(trades) / yrs,
        win=(sum(1 for x in p if x > 0) / len(p) * 100) if p else float('nan'),
        mean=statistics.mean(p) if p else float('nan'),
        med=statistics.median(p) if p else float('nan'),
        worst=min(p) if p else float('nan'),
        held=statistics.mean(t['held'] for t in trades) if trades else float('nan'),
        total=(eq - 1) * 100, cagr=((eq ** (1 / yrs) - 1) * 100) if eq > 0 else -100.0,
        mdd=dd * 100, blocked=blocked_by_move, conflict=cancelled_by_conflict,
        mix={k: round(sum(1 for t in trades if t['why'] == k) / len(trades) * 100, 1)
             for k in ('stop_loss', 'exit_strength', 'time_stop')} if trades else {})


def bah(sym):
    dts, c = SER[sym]
    yrs = len(c) / 252.0
    r = c[-1] / c[0]
    peak = dd = 0.0
    for v in c:
        peak = max(peak, v)
        dd = min(dd, v / peak - 1)
    return dict(total=(r - 1) * 100, cagr=(r ** (1 / yrs) - 1) * 100, mdd=dd * 100)


print("=" * 112)
print(f"样本 2016-01-04 ~ 2026-10-23  ({len(SER['QQQ'][0])} 根, {len(SER['QQQ'][0])/252:.1f} 年)")
print("规则照抄 config.json: rsi2<10 且 close>SMA200 入场 | stop -7% → exit_strength → time_stop 10 日")
print("=" * 112)

print("\n【第 0 问】先看买入持有 —— 衰减到底有多大")
print(f"{'标的':>6s}{'总收益':>14s}{'年化':>10s}{'最大回撤':>12s}{'对比 QQQ^3':>14s}")
print("-" * 112)
q = bah('QQQ')
for s in ('QQQ', 'TQQQ', 'SQQQ'):
    b = bah(s)
    cmp3 = ''
    if s == 'TQQQ':
        ideal = ((1 + q['total'] / 100) ** 3 - 1) * 100
        cmp3 = f"理想 {ideal:+.0f}% → 实得 {b['total']:+.0f}%"
    print(f"{s:>6s}{b['total']:>+13.1f}%{b['cagr']:>+9.1f}%{b['mdd']:>+11.1f}%{cmp3:>14s}")

ROWS = [
    ('QQQ  基准 (实盘在跑的)',      'QQQ',  True,  STOP_DEF, True),
    ('TQQQ 照搬 (受异动闸)',        'TQQQ', True,  STOP_DEF, True),
    ('TQQQ 加入 exempt (免异动闸)', 'TQQQ', False, STOP_DEF, True),
    ('TQQQ 免闸 + 止损改 21%',      'TQQQ', False, 21.0,     True),
    ('SQQQ 照搬 (受异动闸)',        'SQQQ', True,  STOP_DEF, True),
    ('SQQQ 加入 exempt (免异动闸)', 'SQQQ', False, STOP_DEF, True),
    ('SQQQ 免闸 + 止损改 21%',      'SQQQ', False, 21.0,     True),
]
print("\n【第 1 问】规则照搬的结果")
print(f"{'情形':<28s}{'笔数':>5s}{'/年':>6s}{'胜率':>7s}{'均值':>9s}{'中位':>9s}{'最差':>9s}"
      f"{'均持':>6s}{'总收益':>12s}{'年化':>9s}{'最大回撤':>10s}{'异动挡':>7s}{'冲突免':>7s}")
print("-" * 112)
res = {}
for lbl, s, mf, st, cr in ROWS:
    r = backtest(s, mf, st, cr)
    res[lbl] = r
    print(f"{lbl:<28s}{r['n']:>5d}{r['per_yr']:>6.1f}{r['win']:>6.1f}%{r['mean']:>+8.2f}%"
          f"{r['med']:>+8.2f}%{r['worst']:>+8.1f}%{r['held']:>6.1f}{r['total']:>+11.1f}%"
          f"{r['cagr']:>+8.1f}%{r['mdd']:>+9.1f}%{r['blocked']:>7d}{r['conflict']:>7d}")

print("\n【第 2 问】出场原因构成 (止损占比越高说明 -7% 在该标的上咬得越频繁)")
print(f"{'情形':<28s}{'stop_loss':>11s}{'exit_strength':>15s}{'time_stop':>11s}")
print("-" * 112)
for lbl in res:
    m = res[lbl]['mix']
    if m:
        print(f"{lbl:<28s}{m['stop_loss']:>10.1f}%{m['exit_strength']:>14.1f}%{m['time_stop']:>10.1f}%")

print("\n【第 3 问】SQQQ 的结构性问题: close > SMA200 这个牛市过滤多久成立一次?")
print(f"{'标的':>6s}{'SMA200 之上的天数占比':>24s}{'且 rsi2<10 的天数':>20s}")
print("-" * 112)
for s in ('QQQ', 'TQQQ', 'SQQQ'):
    D = prep(s)
    v = [i for i in range(len(D['c'])) if D['s200'][i] is not None]
    above = [i for i in v if D['c'][i] > D['s200'][i]]
    both = [i for i in above if D['rsi2'][i] is not None and D['rsi2'][i] < RSI2_MAX]
    print(f"{s:>6s}{len(above)/len(v)*100:>23.1f}%{len(both):>15d} 天")

print("\n【第 4 问】「有卖有买就不卖」在 3 倍标的上的放大效应 (关掉它对比)")
print(f"{'情形':<28s}{'冲突规则':>10s}{'笔数':>6s}{'止损占比':>10s}{'均值':>9s}{'总收益':>12s}{'最大回撤':>11s}")
print("-" * 112)
for s, mf, st in (('QQQ', True, STOP_DEF), ('TQQQ', False, STOP_DEF), ('SQQQ', False, STOP_DEF)):
    for cr in (True, False):
        r = backtest(s, mf, st, cr)
        m = r['mix'].get('stop_loss', float('nan')) if r['mix'] else float('nan')
        print(f"{s + (' (免异动闸)' if not mf else ''):<28s}{('开' if cr else '关'):>10s}"
              f"{r['n']:>6d}{m:>9.1f}%{r['mean']:>+8.2f}%{r['total']:>+11.1f}%{r['mdd']:>+10.1f}%")
