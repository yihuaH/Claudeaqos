#!/usr/bin/env python3
"""RSI-2 信号买 call: delta 0.30 + 月期权 vs 深 ITM + 周期权 —— **仅研究用, 不是引擎**。

⛔ 本文件**不被 daily.py / session.py 调用**, 放在 strategy/proposals/ 下即表示它不在交易路径上。
   红线2: 订单只能来自确定性引擎。本脚本只算历史收益分布, 供用户判断是否值得实现。

起因: 用户 2026-10-30 问「根据 rsi2 的信号来买期权合适吗」→ 明确意图「取代正股轨道」→
看完既有证据 (深 ITM 形态 paper 18 笔均值 −2.95%/累计 −$52) 后提出
**「能买0.3 的期权吗 月期权也可以」**。该组合**从未被回测过** (2026-08-04 那次只扫
moneyness 0.90~0.92 一带), 故有此脚本。

核心待答问题 = 两股反向力量谁大:
  · 利好: delta 0.30 杠杆约 18× vs 深 ITM 约 8× (实测 0.277×691.89×100÷1064=18.0)
          → 中位那笔 +0.664%×18 ≈ +12%, **首次能盖住 2~4% 往返点差**
  · 利空: **IV crush 回来了**。深 ITM 被选中的唯一理由就是免疫它 (原文「入场 IV 抬 45%
          只损 ~1pt」), 而 RSI-2 定义上就是买在恐慌超卖日 = IV 高位。
          实测量级: delta 0.277 的 QQQ call vega 1.064/权利金 10.56 = **10.1% 每 vol 点**
          → IV 掉 5 点 = 权利金 −50%。

方法 = **配对检验**: 同一个信号, 同一个出场点, 只换载体 (正股 vs 期权), 逐笔对比。
这样隔离出的就是"换载体"这一个变量, 与仓库既有的 scale_in 配对检验 (217 对) 同一方法论。

用法: python3 -I strategy/proposals/rsi2-delta30-call.py
输入: scratchpad 的 bt_bars.json (integrations.py bars --symbols-file bt_syms.json
      --start 2022-04-01 产出; 108 只 = universe 100 + etf_universe 8)
"""
import json, math, statistics, collections, sys, os
from datetime import date as _D

SP = '/tmp/claude-0/-home-user-Claudeaqos/3e6563b5-e5a0-5665-84d2-72b2406895d9/scratchpad'
REPO = '/home/user/Claudeaqos'

# ===================== Black-Scholes (bs_call/bs_delta 照抄 qqq-leap-backtest.py) =====================
def _nd(x): return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))
def _npdf(x): return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)

def bs_call(S, K, T, r, q, sig):
    if T <= 0: return max(S - K, 0.0)
    if sig <= 0: return max(S * math.exp(-q * T) - K * math.exp(-r * T), 0.0)
    v = sig * math.sqrt(T)
    d1 = (math.log(S / K) + (r - q + 0.5 * sig * sig) * T) / v
    d2 = d1 - v
    return S * math.exp(-q * T) * _nd(d1) - K * math.exp(-r * T) * _nd(d2)

def bs_delta(S, K, T, r, q, sig):
    if T <= 0: return 1.0 if S > K else 0.0
    v = sig * math.sqrt(T)
    d1 = (math.log(S / K) + (r - q + 0.5 * sig * sig) * T) / v
    return math.exp(-q * T) * _nd(d1)

def bs_vega(S, K, T, r, q, sig):
    """每 **1 个 vol 点** (0.01) 的权利金变化, 单位 = 每股美元。自检 #2 要用。"""
    if T <= 0 or sig <= 0: return 0.0
    v = sig * math.sqrt(T)
    d1 = (math.log(S / K) + (r - q + 0.5 * sig * sig) * T) / v
    return S * math.exp(-q * T) * _npdf(d1) * math.sqrt(T) * 0.01

def rate(d):
    """利率路径 (照抄 qqq-leap-backtest.py; rho 不可忽略故不用单一常数)"""
    y = int(d[:4])
    return {2016: .005, 2017: .010, 2018: .020, 2019: .023, 2020: .004, 2021: .002,
            2022: .020, 2023: .050, 2024: .050, 2025: .043, 2026: .040}.get(y, 0.03)

def strike_step(S):
    """行权价步长模型。实测锚点: QQQ($692)=5 / MCK($1003)有1060档 / DT($61)有65档。
    ⚠️ 这是**建模假设**, 真实链各标的不同; 因为我们按 delta 选档再定价, 步长只带来
    小的 delta 错配, 不影响结论方向。"""
    if S < 25: return 1.0
    if S < 50: return 2.5
    if S < 200: return 5.0
    if S < 500: return 10.0
    return 20.0

# ===================== 指标 (wilder_rsi 照抄 stock-drop2-call.py:49) =====================
def wilder_rsi(c, p=2):
    n = len(c); out = [None] * n
    if n < p + 1: return out
    gains = losses = 0.0
    for i in range(1, p + 1):
        ch = c[i] - c[i - 1]
        gains += max(ch, 0.0); losses += max(-ch, 0.0)
    ag, al = gains / p, losses / p
    out[p] = 100.0 if al == 0 else 100.0 - 100.0 / (1.0 + ag / al)
    for i in range(p + 1, n):
        ch = c[i] - c[i - 1]
        ag = (ag * (p - 1) + max(ch, 0.0)) / p
        al = (al * (p - 1) + max(-ch, 0.0)) / p
        out[i] = 100.0 if al == 0 else 100.0 - 100.0 / (1.0 + ag / al)
    return out

def sma(c, w):
    n = len(c); out = [None] * n; s = 0.0
    for i in range(n):
        s += c[i]
        if i >= w: s -= c[i - w]
        if i >= w - 1: out[i] = s / w
    return out

def rvol_series(closes):
    n = len(closes)
    lr = [None] + [math.log(closes[i] / closes[i - 1]) for i in range(1, n)]
    def rv(i, w):
        if i < w: return None
        return statistics.stdev(lr[i - w + 1:i + 1]) * math.sqrt(252)
    return [rv(i, 20) for i in range(n)], [rv(i, 60) for i in range(n)]

# ===================== 数据 =====================
PATH = SP + '/bt_bars.json'
if not os.path.exists(PATH):
    raise SystemExit(f"缺少 {PATH} —— 先跑: python3 scripts/integrations.py bars "
                     f"--symbols-file {SP}/bt_syms.json --start 2022-04-01 --out {PATH}")
RAW = json.load(open(PATH))['data']['results']

CFG = json.load(open(f'{REPO}/strategy/config.json'))
RSI2_MAX = float(CFG['entry']['rsi2_max'])          # 10.0
RSI2_MIN = float(CFG['exit']['rsi2_min'])           # 65.0
STOP_PCT = float(CFG['exit']['stop_loss_pct'])      # 7.0
MAX_HOLD = int(CFG['exit']['max_holding_days'])     # 10

SER = {}
for r in RAW:
    b = [x for x in r['bars'] if not x.get('interpolated') and x.get('session') in (None, 'reg')]
    if len(b) < 260: continue
    closes = [float(x['close_price']) for x in b]
    dates = [x['begins_at'][:10] for x in b]
    rv20, rv60 = rvol_series(closes)
    SER[r['symbol']] = dict(d=dates, c=closes, s200=sma(closes, 200), s5=sma(closes, 5),
                            rsi2=wilder_rsi(closes, 2), rv20=rv20, rv60=rv60)

# IV 代理 + 全局校准。KAPPA 由"模型 IV = 今日实测 IV"反解。
# 今日 (2026-10-30 12:47 ET) 实测 QQQ delta≈0.30 档 IV: 77DTE 0.1997 / 140DTE 0.2066 / 322DTE 0.2099
#  → 取 0.205 为标定目标。
IV_TARGET_TODAY = 0.205
def iv_raw(S, i):
    if S['rv20'][i] is None or S['rv60'][i] is None: return None
    return 0.5 * S['rv20'][i] + 0.5 * S['rv60'][i]

_q = SER.get('QQQM')  # 用 QQQM 代 QQQ (池里是 QQQM); 两者同标的指数
if _q is None: raise SystemExit("QQQM 不在数据里, 无法标定 KAPPA")
_raw_last = iv_raw(_q, len(_q['c']) - 1)
KAPPA = IV_TARGET_TODAY / _raw_last

def iv_at(S, i, bump=0.0):
    r = iv_raw(S, i)
    if r is None: return None
    return max(0.08, min(1.20, KAPPA * r * (1.0 + bump)))

# ===================== 配对回测 =====================
Q_DIV = 0.010   # 池内多为分红股, 取 1% (ETF 约 0.5%, 个股 1~2%)

def pick_strike(S0, T, r, sig, target):
    """target 两种写法:
      · float (0<t<1 且标成 ('delta', x))  → 按 delta 选档
      · ('mny', 0.90)                      → 按 moneyness 选档 = **仓库深 ITM 的真实口径**
    🔧 2026-10-30 修: 原先只有 delta 模式, 我拿 `delta 0.80` 当「深 ITM 对照」是**错的** ——
    仓库的深 ITM 是 `contract.moneyness: 0.90` (行权价 = ≤0.90×现价 的最高档)。
    两者在短 DTE 上差很远: S=100/K=90/14DTE/σ=0.25 的 delta 其实是 **0.98**, 不是 0.80;
    反过来「delta 0.80 @ 14DTE」的行权价约 0.97×S = 几乎平价, 于是 −7% 止损就打到全损 ——
    这正是自检① 第一次失败的原因 (全损率 5.8% vs 仓库说的 ≈0)。对照组必须按 moneyness 指定。"""
    step = strike_step(S0)
    mode, t = target
    if mode == 'mny':
        k = math.floor(S0 * t / step) * step     # 「≤0.90×现价 的最高档」
        if k <= 0: return None
        return (k, 0.0, bs_delta(S0, k, T, r, Q_DIV, sig))
    lo = math.floor(S0 * 0.55 / step) * step
    hi = math.ceil(S0 * 1.45 / step) * step
    best = None; k = lo
    while k <= hi:
        if k > 0:
            d = bs_delta(S0, k, T, r, Q_DIV, sig)
            e = abs(d - t)
            if best is None or e < best[1]: best = (k, e, d)
        k += step
    return best

def run_cell(target, dte, bump, half_life):
    """返回逐笔 [(stock_ret_pct, opt_mid_entry, opt_mid_exit, held, reason, meta)]"""
    out = []
    for sym, S in SER.items():
        c, d, s200, s5, r2 = S['c'], S['d'], S['s200'], S['s5'], S['rsi2']
        n = len(c)
        i = 200
        while i < n - 1:
            if s200[i] is None or r2[i] is None or iv_at(S, i) is None:
                i += 1; continue
            # --- 入场信号: signals.py:254 逐字段 ---
            if not (c[i] > s200[i] and r2[i] < RSI2_MAX):
                i += 1; continue
            S0 = c[i]; T0 = dte / 365.0; r0 = rate(d[i])
            sig0 = iv_at(S, i, bump)          # 入场日 IV 含 crush bump
            ps = pick_strike(S0, T0, r0, sig0, target)
            if ps is None: i += 1; continue
            K, _, d0 = ps
            mid0 = bs_call(S0, K, T0, r0, Q_DIV, sig0)
            if mid0 <= 0.01: i += 1; continue
            # --- 持有: 出场判定全部在**正股**上 (与 weekly_calls 同形, 无期权级止损) ---
            exit_j = None; reason = None
            for j in range(i + 1, min(i + 1 + MAX_HOLD, n)):
                held = j - i
                px = c[j]
                if px <= S0 * (1 - STOP_PCT / 100.0):
                    exit_j, reason = j, 'stop_loss'; break
                if (s5[j] is not None and px > s5[j]) or (r2[j] is not None and r2[j] >= RSI2_MIN):
                    exit_j, reason = j, 'exit_strength'; break
                if held >= MAX_HOLD:
                    exit_j, reason = j, 'time_stop'; break
            if exit_j is None:
                i += 1; continue
            held = exit_j - i
            # IV crush: 入场抬的 bump 在持有期内按半衰期衰减回基线
            decay = (0.5 ** (held / half_life)) if (bump > 0 and half_life > 0) else 0.0
            sigj = iv_at(S, exit_j, bump * decay)
            Tj = max(0.0, (dte - (_D.fromisoformat(d[exit_j]) - _D.fromisoformat(d[i])).days) / 365.0)
            mid1 = bs_call(c[exit_j], K, Tj, rate(d[exit_j]), Q_DIV, sigj)
            stock_ret = (c[exit_j] / S0 - 1.0) * 100.0
            out.append((stock_ret, mid0, mid1, held, reason,
                        dict(sym=sym, date=d[i], S0=S0, K=K, delta=d0, iv=sig0,
                             vega=bs_vega(S0, K, T0, r0, Q_DIV, sig0))))
            i = exit_j + 1
    return out

def opt_ret(mid0, mid1, hs):
    """买付 ask = mid*(1+hs), 卖收 bid = mid*(1-hs)"""
    entry = mid0 * (1 + hs); exit_ = mid1 * (1 - hs)
    return (exit_ / entry - 1.0) * 100.0

def summ(v):
    if not v: return dict(n=0)
    return dict(n=len(v), mean=statistics.mean(v), med=statistics.median(v),
                win=100.0 * sum(1 for x in v if x > 0) / len(v),
                p10=statistics.quantiles(v, n=10)[0] if len(v) > 9 else min(v),
                worst=min(v), best=max(v),
                total_loss=100.0 * sum(1 for x in v if x <= -90.0) / len(v))

# ===================== 自检 (任一条不过 → 不报结论) =====================
print("=" * 92)
print("RSI-2 买 call 配对回测: delta 0.30 + 月期权 vs 深 ITM + 周期权")
print("=" * 92)
_dts = sorted({dd for S in SER.values() for dd in S['d']})
print(f"数据: {len(SER)} 只标的, {_dts[0]} ~ {_dts[-1]} ({len(_dts)} 个交易日)")
print(f"IV 校准: KAPPA={KAPPA:.4f} (由 QQQM 今日模型 IV = 实测 {IV_TARGET_TODAY} 反解)")
print(f"规则照抄 config.json: rsi2<{RSI2_MAX} & close>SMA200 入场 | "
      f"止损 {STOP_PCT}% / rsi2>={RSI2_MIN} 或 close>SMA5 / 时间止损 {MAX_HOLD} 交易日")
print()

base = run_cell(('mny', 0.90), 14, 0.0, 5)     # ← 仓库深 ITM 的真实口径, 作对照组
chk = []
_sr = [t[0] for t in base]
s_stock = summ(_sr)
# 自检①: **深 ITM (moneyness 0.90, 8-17DTE)** 全损率必须 ≈0 —— 2026-08-04 回测结论
dmny = summ([opt_ret(t[1], t[2], 0.02) for t in base])
_d_med = statistics.median([t[5]['delta'] for t in base])
chk.append((f"① 深ITM(moneyness0.90@14DTE, 实测中位delta {_d_med:.2f}) 全损率≈0",
            dmny['total_loss'] <= 2.0, f"{dmny['total_loss']:.1f}%"))
# 自检②: delta0.30 的 vega/权利金。**DTE 必须与标定来源一致** ——
# 今日实测 10.1% 来自 QQQ 2027-01-15 C740 = **77 DTE**, 故这里也用 77 DTE 检,
# 拿 77DTE 的区间去检 30DTE 的格是我第一次自检②失败的原因 (规格错, 非定价错)。
c77 = run_cell(('delta', 0.30), 77, 0.0, 5)
vr_all = [100.0 * t[5]['vega'] / t[1] for t in c77 if t[1] > 0]
# 🔧 2026-10-30 二次修: 必须**同口径**比。实测 10.1% 来自 **QQQ** (低 IV ≈0.20) 的
# 2027-01-15 C740 @77DTE; 而全篮子中位是 108 只**个股**的数, 个股 IV 更高 → 同 delta 下
# 权利金更贵 → vega/权利金**本就该更低**。拿 QQQ 的区间去检个股篮子仍是规格错
# (与自检② 第一次失败同一类错误), 且**不能靠放宽区间去迁就** —— 那是调整检验迁就结果。
# 正确做法: 只取模型里的 QQQM 与实测的 QQQ 对照, 篮子中位数仅作参考输出。
vr_q = [100.0 * t[5]['vega'] / t[1] for t in c77 if t[1] > 0 and t[5]['sym'] == 'QQQM']
mv = statistics.median(vr_q) if vr_q else float('nan')
mv_all = statistics.median(vr_all)
chk.append((f"② **QQQM**@77DTE/delta0.30 vega/权利金 ∈ [8,12]%/volpt "
            f"(对标今日实测 QQQ 10.1%; 108只篮子中位 {mv_all:.1f}% 因个股 IV 更高而偏低, 属预期)",
            8.0 <= mv <= 12.0, f"{mv:.1f}% (n={len(vr_q)})"))
# 自检③: 正股基准均值须落在 +0.17% ~ +1.03% (两个已知样本)
chk.append(("③ 正股基准均值 ∈ [0.17,1.03]%", 0.17 <= s_stock['mean'] <= 1.03,
            f"{s_stock['mean']:+.3f}%"))
print("自检:")
ok_all = True
for name, ok, val in chk:
    print(f"  {'✅' if ok else '❌'} {name} → 实测 {val}")
    ok_all = ok_all and ok
print()
if not ok_all:
    print("❌ 自检未全过 —— **不要引用下面的结论**, 先查定价/口径。")
print(f"正股基准 (同信号同出场, n={s_stock['n']}): 均值 {s_stock['mean']:+.3f}% / "
      f"中位 {s_stock['med']:+.3f}% / 胜率 {s_stock['win']:.1f}% / "
      f"最差 {s_stock['worst']:+.2f}% / 最好 {s_stock['best']:+.2f}%")
print()

# ===================== 主网格 =====================
TARGETS = [('delta', 0.30), ('delta', 0.50), ('mny', 0.90)]  # mny0.90 = 仓库深 ITM 现行口径
DTES = [14, 30, 45]
SPREADS = [0.010, 0.020, 0.030]
BUMPS = [0.0, 0.20, 0.45]
HALF_LIVES = [2, 5]

print("=" * 92)
print("主网格 (每格: 均值% / 中位% / 胜率% / 全损率% / 最差%)")
print("  全损率 = 权利金亏 ≥90% 的笔数占比")
print("=" * 92)
rows = []
for tg in TARGETS:
    for dte in DTES:
        for bump in BUMPS:
            hls = [HALF_LIVES[0]] if bump == 0 else HALF_LIVES
            for hl in hls:
                ts = run_cell(tg, dte, bump, hl)
                lbl = f"d{tg[1]:.2f}" if tg[0] == 'delta' else f"m{tg[1]:.2f}"
                for hs in SPREADS:
                    v = [opt_ret(t[1], t[2], hs) for t in ts]
                    st = summ(v)
                    rows.append(dict(tg=lbl, dte=dte, bump=bump, hl=hl, hs=hs, **st))
hdr = f"{'形态':>7}{'DTE':>5}{'IVbump':>8}{'半衰':>5}{'点差':>6}{'n':>6}{'均值':>9}{'中位':>9}{'胜率':>7}{'全损':>7}{'最差':>9}"
print(hdr); print("-" * len(hdr))
for r in rows:
    print(f"{r['tg']:>7}{r['dte']:>5}{r['bump']*100:>7.0f}%{r['hl']:>5}{r['hs']*100:>5.0f}%"
          f"{r['n']:>6}{r['mean']:>+8.2f}%{r['med']:>+8.2f}%{r['win']:>6.1f}%{r['total_loss']:>6.1f}%"
          f"{r['worst']:>+8.1f}%")

# ===================== 判定 =====================
print()
print("=" * 92)
print("判定 (过线条件: **最悲观格** IVbump45%/半衰2日/点差3% 下, 均值仍 > 正股基准)")
print("=" * 92)
BASE = s_stock['mean']
for tg in TARGETS:
    lbl = f"d{tg[1]:.2f}" if tg[0] == 'delta' else f"m{tg[1]:.2f}"
    for dte in DTES:
        w = [r for r in rows if r['tg'] == lbl and r['dte'] == dte
             and r['bump'] == 0.45 and r['hl'] == 2 and abs(r['hs'] - 0.030) < 1e-9]
        if not w: continue
        r = w[0]
        verdict = "过" if r['mean'] > BASE else "否决"
        print(f"  {lbl} / {dte:>2}DTE → 最悲观格均值 {r['mean']:+.2f}% vs "
              f"正股基准 {BASE:+.2f}% → **{verdict}**  (全损率 {r['total_loss']:.1f}%)")
print()
best = max(rows, key=lambda r: r['mean'])
print(f"全网格最优格: {best['tg']} / {best['dte']}DTE / bump {best['bump']*100:.0f}% / "
      f"半衰 {best['hl']} / 点差 {best['hs']*100:.0f}% → 均值 {best['mean']:+.2f}%")
print()
print("⚠️ 口径限制 (引用前必读):")
print("  1. **未建模财报出场** (defense.earnings_exit_days=1) —— 实盘 2 笔均值 −6.45%,")
print("     占 1.8%。剔除它使本回测**略偏乐观**。")
print("  2. **未建模防御层** (max_daily_move 8% / 财报黑窗 7 日 / 行业上限 3) —— 2026-10-29")
print("     当日 4 个候选里 3 个被这些闸拦掉。故本回测信号数远多于实盘, **绝对水平不可比**;")
print("     可比的是**配对后的载体差异** (同信号同出场, 只换载体)。")
print("  3. IV 是**代理** (0.5×rv20+0.5×rv60 × KAPPA), 非真实 IV 曲面。")
print("     journal/2026-09-17-options-only-bt.md 已踩过: 「IV 假设一个旋钮就能翻转答案的正负号」")
print("     → 故本脚本**扫 bump 而不取单点**, 结论按最悲观格报。")
print("  4. 行权价步长是建模假设 (见 strike_step), 真实链各标的不同。")
print("  5. 无滑点/佣金建模, 只有点差。真实成交还会更差。")
