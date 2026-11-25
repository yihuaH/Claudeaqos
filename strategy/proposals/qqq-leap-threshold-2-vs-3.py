#!/usr/bin/env python3
"""QQQ LEAP 入场阈值扫描 (≥2% vs ≥3% 及邻档) —— **仅研究用, 不是引擎, 不产生任何订单**。

⛔ 不被 daily.py / session.py 调用。红线2: 订单只能来自确定性引擎。

起因: 用户 2026-11-25「跌3%再买吧」把 `entry.daily_return_pct_max` 由 −2.0 改为 −3.0。
而 §12.6 只测过 **1% / 2% / 2.5%** 三档, 且那次用的是**已被否决的旧形态**
(delta 0.60 / DTE 365 / +50% 止盈 / 无时间止损)。本脚本按**现行实盘配置**重测:

  · 三档梯子 target DTE 148 / 85 / 330, 带宽 [120,190] / [60,110] / [300,400]
  · delta 目标 0.30, 带 [0.25, 0.40]
  · 止盈 +20%, 口径 `bid >= 入场 ask × 1.20` (双边点差已扣)
  · **20 个交易日时间止损**, `holding_clock_resets=false`
  · `force_exit_dte_lte = 30` 兜底
  · **无价格止损** (`price_stop_pct: null`)
  · 牛市过滤**已关** (`require_close_above_sma200: false`, 用户 2026-11-05 指令)

口径与局限完全继承 qqq-leap-call.md §12.7: 模型价不是真实成交价, IV 代理只有一个校准点,
不含 IV 曲面偏斜/期限结构。**本脚本只比较阈值之间的相对差异**, 绝对数字不可当预期收益读。

用法: python3 strategy/proposals/qqq-leap-threshold-2-vs-3.py
输入: scratchpad 的 qqq_long.json (integrations.py bars --symbols QQQ --start 2015-01-01)
"""
import json, math, statistics, collections, os, sys
from datetime import date as _D

SP = os.environ.get('SP') or '/tmp/claude-0/-home-user-Claudeaqos/3e6563b5-e5a0-5665-84d2-72b2406895d9/scratchpad'

def _d(x): return _D.fromisoformat(x)
def caldays(a, b): return (_d(b) - _d(a)).days

# ---------- Black-Scholes (与 qqq-leap-backtest.py 同一实现, 刻意逐字复用) ----------
def _nd(x): return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))
def bs_call(S, K, T, r, q, sig):
    if T <= 0: return max(S - K, 0.0)
    if sig <= 0: return max(S * math.exp(-q * T) - K * math.exp(-r * T), 0.0)
    v = sig * math.sqrt(T)
    d1 = (math.log(S / K) + (r - q + 0.5 * sig * sig) * T) / v
    return S * math.exp(-q * T) * _nd(d1) - K * math.exp(-r * T) * _nd(d1 - v)
def bs_delta(S, K, T, r, q, sig):
    if T <= 0: return 1.0 if S > K else 0.0
    v = sig * math.sqrt(T)
    return math.exp(-q * T) * _nd((math.log(S / K) + (r - q + 0.5 * sig * sig) * T) / v)

# ---------- 数据 ----------
bars = json.load(open(SP + '/qqq_long.json'))['data']['results'][0]['bars']
dates = [b['begins_at'][:10] for b in bars]
closes = [float(b['close_price']) for b in bars]
n = len(closes)

logret = [None] + [math.log(closes[i] / closes[i - 1]) for i in range(1, n)]
def rvol(i, w):
    if i < w: return None
    return statistics.stdev(logret[i - w + 1:i + 1]) * math.sqrt(252)
rv20 = [rvol(i, 20) for i in range(n)]
rv60 = [rvol(i, 60) for i in range(n)]
def ivraw(i):
    if rv20[i] is None or rv60[i] is None: return None
    return 0.5 * rv20[i] + 0.5 * rv60[i]
# KAPPA 校准: 使模型今日 IV == 2026-11-25 实测 148 档 LEAP IV 0.2588
KAPPA = 0.2588 / ivraw(n - 1)
def iv(i):
    r = ivraw(i)
    return None if r is None else max(0.08, min(0.80, KAPPA * r))

def rate(d):
    return {2016: .005, 2017: .010, 2018: .020, 2019: .023, 2020: .004, 2021: .002,
            2022: .020, 2023: .050, 2024: .050, 2025: .043, 2026: .040}.get(int(d[:4]), .03)
Q_DIV = 0.005

# ---------- 现行配置 ----------
CFG = json.load(open(os.path.join(os.path.dirname(__file__), '..', 'qqq_leap.json')))
TARGET_DELTA = CFG['contract']['delta_target']
DELTA_BAND   = CFG['contract']['delta_band']
TAKE         = CFG['exit']['take_profit_mult']
MAXHOLD      = CFG['exit']['max_holding_trading_days']
FORCE_DTE    = CFG['exit']['force_exit_dte_lte']
LADDER       = [(r['target_dte_calendar'], r['dte_band_calendar']) for r in CFG['contract']['ladder']]
# 半点差: 按 2026-10/11 实测全点差的一半, 逐档不同 (330 档明显更宽)
HALF = {148: 0.0085, 85: 0.0090, 330: 0.0340}

def pick_strike(S, T, r, sig):
    """步长 5 取整, 选 delta 最接近 0.30 且落在带内的行权价"""
    best = None
    k = int(S * 0.70 / 5) * 5
    hi = int(S * 1.60 / 5) * 5
    while k <= hi:
        if k > 0:
            dl = bs_delta(S, k, T, r, Q_DIV, sig)
            if DELTA_BAND[0] <= dl <= DELTA_BAND[1]:
                e = abs(dl - TARGET_DELTA)
                if best is None or e < best[1]: best = (k, e, dl)
        k += 5
    return best

def run_rung(thr, tgt_dte, half):
    """单档回测: 返回每笔 (日期, 当日跌幅, 收益%, 出场原因, 持有交易日)"""
    out = []
    for i in range(1, n):
        if iv(i) is None: continue
        ret = (closes[i] / closes[i - 1] - 1) * 100
        if ret > -thr: continue
        S0, r0, sig0 = closes[i], rate(dates[i]), iv(i)
        T0 = tgt_dte / 365.0
        pk = pick_strike(S0, T0, r0, sig0)
        if pk is None: continue
        K = pk[0]
        mid0 = bs_call(S0, K, T0, r0, Q_DIV, sig0)
        if mid0 <= 0.05: continue
        entry_ask = mid0 * (1 + half)
        reason, px, held = None, None, 0
        for j in range(i + 1, n):
            cd = caldays(dates[i], dates[j])
            dte = tgt_dte - cd
            Tj = max(0.0, dte / 365.0)
            sj = iv(j) or sig0
            bid = bs_call(closes[j], K, Tj, rate(dates[j]), Q_DIV, sj) * (1 - half)
            held = j - i                      # 交易日数 (与引擎 _held_trading_days 同口径)
            if bid >= entry_ask * TAKE:
                reason, px = 'take_profit', bid; break
            if held >= MAXHOLD:
                reason, px = 'time_stop', bid; break
            if dte <= FORCE_DTE:
                reason, px = 'force_dte', bid; break
        if px is None:
            cd = caldays(dates[i], dates[-1])
            px = bs_call(closes[-1], K, max(0.0, (tgt_dte - cd) / 365.0),
                         rate(dates[-1]), Q_DIV, iv(n - 1)) * (1 - half)
            reason, held = 'open', n - 1 - i
        out.append(dict(d=dates[i], ret=ret, pnl=(px / entry_ask - 1) * 100,
                        reason=reason, held=held, K=K, entry=entry_ask))
    return out

def stat(ts):
    if not ts: return None
    p = [t['pnl'] for t in ts]
    yrs = len({t['d'][:4] for t in ts})
    span = (_d(dates[-1]) - _d(dates[0])).days / 365.25
    return dict(n=len(ts), per_year=len(ts) / span,
                win=sum(1 for x in p if x > 0) / len(p) * 100,
                mean=statistics.mean(p), med=statistics.median(p),
                tp=sum(1 for t in ts if t['reason'] == 'take_profit') / len(ts) * 100,
                worst=min(p), best=max(p),
                # 去掉最好 5% 后的均值 —— 2026-10-30 delta 回测用的同一道稳健性检验
                trim=statistics.mean(sorted(p)[:max(1, int(len(p) * 0.95))]))

print('=== 校准与口径 ===')
print('样本 %s → %s (%d 个交易日, %.1f 年)' % (dates[0], dates[-1], n, (_d(dates[-1]) - _d(dates[0])).days / 365.25))
print('KAPPA = 0.2588 / %.4f = %.3f  (使模型今日 IV == 11-25 实测 148 档 IV 25.88%%)' % (ivraw(n - 1), KAPPA))
print('delta 目标 %.2f 带 %s | 止盈 ×%.2f | 时间止损 %d 交易日 | force_dte<=%d | 无价格止损 | 牛市过滤 %s'
      % (TARGET_DELTA, DELTA_BAND, TAKE, MAXHOLD, FORCE_DTE,
         'ON' if CFG['entry']['require_close_above_sma200'] else 'OFF'))
print('半点差 (单边): ' + ' / '.join('%dDTE %.2f%%' % (k, v * 100) for k, v in HALF.items()))
print()

THRS = [2.0, 2.5, 3.0, 3.5, 4.0]
allres = {}
for tgt, band in LADDER:
    half = HALF[tgt]
    print('=== 档 target_dte=%d (带 %s), 半点差 %.2f%% ===' % (tgt, band, half * 100))
    print('  阈值   n   次/年   胜率    均值     中位   止盈%%   去最好5%%后均值   最差')
    for thr in THRS:
        ts = run_rung(thr, tgt, half)
        s = stat(ts)
        allres[(tgt, thr)] = s
        if not s: print('  ≥%.1f%%  无交易' % thr); continue
        print('  ≥%.1f%% %4d  %5.1f  %5.1f%%  %+6.1f%%  %+6.1f%%  %5.1f%%   %+8.1f%%     %+6.0f%%'
              % (thr, s['n'], s['per_year'], s['win'], s['mean'], s['med'], s['tp'], s['trim'], s['worst']))
    print()

print('=== 三档合计 (每信号三张, 等权) ===')
print('  阈值   信号/年   每张均值   每张中位   去最好5%后   止盈占比')
for thr in THRS:
    pool = []
    for tgt, _b in LADDER:
        pool += [t['pnl'] for t in run_rung(thr, tgt, HALF[tgt])]
    if not pool: continue
    sig_n = allres[(LADDER[0][0], thr)]['per_year'] if allres.get((LADDER[0][0], thr)) else 0
    print('  ≥%.1f%%   %5.1f    %+7.1f%%   %+7.1f%%   %+9.1f%%' % (
        thr, sig_n, statistics.mean(pool), statistics.median(pool),
        statistics.mean(sorted(pool)[:max(1, int(len(pool) * 0.95))])))

print()
print('=== 点差敏感性 (阈值 2% vs 3%, 148 档) ===')
print('  半点差   ≥2% 均值   ≥3% 均值    差')
for h in (0.0085, 0.015, 0.025):
    a = stat(run_rung(2.0, 148, h)); b = stat(run_rung(3.0, 148, h))
    print('  %.2f%%    %+7.1f%%   %+7.1f%%   %+6.1f pp' % (h * 100, a['mean'], b['mean'], b['mean'] - a['mean']))

# ============================================================================
# 🔴 KAPPA 敏感性 —— **必读, 否则上面的绝对数字会严重误导**
#
# KAPPA 由「模型今日 IV == 今日实测 LEAP IV」反解。而 2026-11-25 是本样本里
# 已实现波动率最高的几天之一 (ivraw=0.4105), 于是反解出的 KAPPA 只有 0.630 ——
# 这等于说「模型 IV = 0.63 × 已实现波动率混合」, 在平静期会把 IV 压到 11% 左右,
# 远低于 QQQ 期权真实的 15~20%。**IV 压低 → 权利金过便宜 → 入场成本偏低 →
# 上面所有档的绝对收益都被系统性放大。**
#
# 故真正能用的只有**阈值之间的相对次序**。本段检验该次序对 KAPPA 是否稳健。
# ============================================================================
print()
print('=== 🔴 KAPPA 敏感性: 绝对值不可信, 只看 ≥3% 相对 ≥2% 的差 ===')
print('这一段把 KAPPA 从 0.63 (今日反解, 偏低) 一路提到 1.30 (平静期更合理的量级)')
print()
_K0 = KAPPA
print('  KAPPA  平静期隐含IV*   148档: ≥2%均值 → ≥3%均值 (差)      三档合计: ≥2% → ≥3% (差)')
for k in (0.63, 0.80, 1.00, 1.15, 1.30):
    KAPPA = k
    calm = max(0.08, min(0.80, k * 0.18))   # 平静期 rv 混合约 0.18 时的模型 IV
    a = stat(run_rung(2.0, 148, HALF[148])); b = stat(run_rung(3.0, 148, HALF[148]))
    pool2, pool3 = [], []
    for tgt, _b in LADDER:
        pool2 += [t['pnl'] for t in run_rung(2.0, tgt, HALF[tgt])]
        pool3 += [t['pnl'] for t in run_rung(3.0, tgt, HALF[tgt])]
    m2, m3 = statistics.mean(pool2), statistics.mean(pool3)
    print('  %.2f      %5.1f%%        %+6.1f%% → %+6.1f%%  (%+5.1f pp)        %+6.1f%% → %+6.1f%%  (%+5.1f pp)'
          % (k, calm * 100, a['mean'], b['mean'], b['mean'] - a['mean'], m2, m3, m3 - m2))
KAPPA = _K0
print()
print('*「平静期隐含IV」= KAPPA × 0.18, 只用来说明该 KAPPA 对应的 IV 量级是否合理。')
print(' QQQ 长期期权真实 IV 大致 15~20%, 故 KAPPA ≈ 0.85~1.10 才是合理区间; 0.63 偏低。')
