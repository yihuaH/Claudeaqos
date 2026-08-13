#!/usr/bin/env python3
"""图表指标策略统一回测 (研究用, 2026-08-13 用户「都回测一下」).

对 EMA交叉 / BOLL / KC+BOLL挤压突破 / SAR / Ichimoku / NINE(TD9) / WMSR(威廉%R)
各自的经典策略形态, 在与实盘同一宇宙 (8 ETF + universe.json 100 股) 上,
用与 learn.py 相同的组合模拟口径 (10%/仓, 先卖后买, 收盘成交, 无摩擦) 回测,
与 RSI-2 基准并排对比。VWAP/CDP 为盘中指标, EOD 数据无法回测, 不在此列。

仅研究报告用: 不读写任何 state/, 不产生订单 (红线2)。
用法: python3 scripts/research_indicators.py --bars <bars.json> [--start 2022-01-03] [--out <json>]
"""
import argparse
import json
import math


# ---------- 指标 ----------

def sma_series(x, w):
    out = [None] * len(x)
    s = 0.0
    for i, v in enumerate(x):
        s += v
        if i >= w:
            s -= x[i - w]
        if i >= w - 1:
            out[i] = s / w
    return out


def ema_series(x, w):
    out = [None] * len(x)
    k = 2.0 / (w + 1)
    e = None
    for i, v in enumerate(x):
        if e is None:
            if i >= w - 1:
                e = sum(x[:w]) / w
                out[i] = e
        else:
            e = v * k + e * (1 - k)
            out[i] = e
    return out


def rsi_series(close, period=2):
    n = len(close)
    out = [None] * n
    ag = al = None
    for i in range(1, n):
        d = close[i] - close[i - 1]
        g, l = max(d, 0.0), max(-d, 0.0)
        if i < period:
            continue
        if i == period:
            gains = losses = 0.0
            for j in range(1, period + 1):
                dd = close[j] - close[j - 1]
                gains += max(dd, 0.0)
                losses += max(-dd, 0.0)
            ag, al = gains / period, losses / period
        else:
            ag = (ag * (period - 1) + g) / period
            al = (al * (period - 1) + l) / period
        out[i] = 100.0 if al == 0 else 100.0 - 100.0 / (1.0 + ag / al)
    return out


def stdev_series(x, w):
    out = [None] * len(x)
    s = s2 = 0.0
    for i, v in enumerate(x):
        s += v
        s2 += v * v
        if i >= w:
            s -= x[i - w]
            s2 -= x[i - w] * x[i - w]
        if i >= w - 1:
            m = s / w
            var = max(s2 / w - m * m, 0.0)
            out[i] = math.sqrt(var)
    return out


def atr_series(high, low, close, w=20):
    n = len(close)
    tr = [None] * n
    for i in range(n):
        if i == 0:
            tr[i] = high[i] - low[i]
        else:
            tr[i] = max(high[i] - low[i], abs(high[i] - close[i - 1]),
                        abs(low[i] - close[i - 1]))
    out = [None] * n
    a = None
    for i in range(n):
        if i == w - 1:
            a = sum(tr[:w]) / w
            out[i] = a
        elif i >= w:
            a = (a * (w - 1) + tr[i]) / w
            out[i] = a
    return out


def rolling_max(x, w):
    out = [None] * len(x)
    for i in range(w - 1, len(x)):
        out[i] = max(x[i - w + 1:i + 1])
    return out


def rolling_min(x, w):
    out = [None] * len(x)
    for i in range(w - 1, len(x)):
        out[i] = min(x[i - w + 1:i + 1])
    return out


def williams_r(high, low, close, w=14):
    hh, ll = rolling_max(high, w), rolling_min(low, w)
    out = [None] * len(close)
    for i in range(len(close)):
        if hh[i] is None:
            continue
        rng = hh[i] - ll[i]
        out[i] = -50.0 if rng == 0 else (hh[i] - close[i]) / rng * -100.0
    return out


def td_setup(close):
    """TD 序列 setup 计数 (神奇九转)。buy[i]=连续 close<close[-4] 根数, sell 反之。"""
    n = len(close)
    buy, sell = [0] * n, [0] * n
    for i in range(4, n):
        if close[i] < close[i - 4]:
            buy[i] = buy[i - 1] + 1
        if close[i] > close[i - 4]:
            sell[i] = sell[i - 1] + 1
    return buy, sell


def parabolic_sar(high, low, af_step=0.02, af_max=0.2):
    """标准 Wilder SAR。返回 (sar[], up[]): up[i]=True 表示当日为上升段。"""
    n = len(high)
    sar, up = [None] * n, [None] * n
    if n < 2:
        return sar, up
    trend_up = high[1] + low[1] > high[0] + low[0]
    cur = low[0] if trend_up else high[0]
    ep = high[1] if trend_up else low[1]
    af = af_step
    sar[1], up[1] = cur, trend_up
    for i in range(2, n):
        cur = cur + af * (ep - cur)
        if trend_up:
            cur = min(cur, low[i - 1], low[i - 2])
            if low[i] < cur:  # 翻转
                trend_up, cur, ep, af = False, ep, low[i], af_step
            elif high[i] > ep:
                ep, af = high[i], min(af + af_step, af_max)
        else:
            cur = max(cur, high[i - 1], high[i - 2])
            if high[i] > cur:
                trend_up, cur, ep, af = True, ep, high[i], af_step
            elif low[i] < ep:
                ep, af = low[i], min(af + af_step, af_max)
        sar[i], up[i] = cur, trend_up
    return sar, up


def ichimoku(high, low, shift=26):
    """返回 (tenkan, kijun, span_a_at, span_b_at): span*_at[i] 为画在第 i 天的云 (已前移)。"""
    n = len(high)
    t_h, t_l = rolling_max(high, 9), rolling_min(low, 9)
    k_h, k_l = rolling_max(high, 26), rolling_min(low, 26)
    b_h, b_l = rolling_max(high, 52), rolling_min(low, 52)
    tenkan = [(t_h[i] + t_l[i]) / 2 if t_h[i] is not None else None for i in range(n)]
    kijun = [(k_h[i] + k_l[i]) / 2 if k_h[i] is not None else None for i in range(n)]
    raw_a = [(tenkan[i] + kijun[i]) / 2 if tenkan[i] is not None and kijun[i] is not None else None
             for i in range(n)]
    raw_b = [(b_h[i] + b_l[i]) / 2 if b_h[i] is not None else None for i in range(n)]
    span_a = [raw_a[i - shift] if i >= shift else None for i in range(n)]
    span_b = [raw_b[i - shift] if i >= shift else None for i in range(n)]
    return tenkan, kijun, span_a, span_b


def precompute(sym_bars):
    close = [b["c"] for b in sym_bars]
    high = [b["h"] for b in sym_bars]
    low = [b["l"] for b in sym_bars]
    dates = [b["d"] for b in sym_bars]
    n = len(close)
    sma20 = sma_series(close, 20)
    sd20 = stdev_series(close, 20)
    atr20 = atr_series(high, low, close, 20)
    ema20 = ema_series(close, 20)
    bb_u = [sma20[i] + 2 * sd20[i] if sma20[i] is not None else None for i in range(n)]
    bb_l = [sma20[i] - 2 * sd20[i] if sma20[i] is not None else None for i in range(n)]
    kc_u = [ema20[i] + 1.5 * atr20[i] if ema20[i] is not None and atr20[i] is not None else None
            for i in range(n)]
    kc_l = [ema20[i] - 1.5 * atr20[i] if ema20[i] is not None and atr20[i] is not None else None
            for i in range(n)]
    squeeze = [bb_u[i] is not None and kc_u[i] is not None
               and bb_u[i] < kc_u[i] and bb_l[i] > kc_l[i] for i in range(n)]
    sar, sar_up = parabolic_sar(high, low)
    tenkan, kijun, span_a, span_b = ichimoku(high, low)
    td_buy, td_sell = td_setup(close)
    return {
        "dates": dates, "idx": {d: i for i, d in enumerate(dates)},
        "close": close, "high": high, "low": low,
        "sma5": sma_series(close, 5), "sma20": sma20, "sma200": sma_series(close, 200),
        "ema20": ema20, "ema50": ema_series(close, 50),
        "rsi2": rsi_series(close, 2),
        "bb_u": bb_u, "bb_l": bb_l, "kc_u": kc_u, "kc_l": kc_l, "squeeze": squeeze,
        "wr14": williams_r(high, low, close, 14),
        "wr2": williams_r(high, low, close, 2),
        "hh20": rolling_max(high, 20),
        "sar": sar, "sar_up": sar_up,
        "tenkan": tenkan, "kijun": kijun, "span_a": span_a, "span_b": span_b,
        "td_buy": td_buy, "td_sell": td_sell,
    }


# ---------- 策略定义 ----------
# entry(p,i) -> None 或 排序分 (越小越优先); exit(p,i,pos) -> 原因字符串或 None
# max_hold=None 表示不设时间出场 (趋势类需要持仓空间)

def _above_cloud(p, i):
    return (p["span_a"][i] is not None and p["span_b"][i] is not None
            and p["close"][i] > max(p["span_a"][i], p["span_b"][i]))


def strength_exit(p, i, rsi2_min=65.0):
    return ((p["sma5"][i] is not None and p["close"][i] > p["sma5"][i])
            or (p["rsi2"][i] is not None and p["rsi2"][i] >= rsi2_min))


STRATEGIES = {
    # --- 基准: 实盘 RSI-2 (现行参数) ---
    "baseline_rsi2": {
        "family": "均值回归", "max_hold": 10,
        "entry": lambda p, i: p["rsi2"][i]
        if (p["sma200"][i] is not None and p["rsi2"][i] is not None
            and p["close"][i] > p["sma200"][i] and p["rsi2"][i] < 10.0) else None,
        "exit": lambda p, i, pos: "strength" if strength_exit(p, i) else None,
    },
    # --- WMSR 威廉 %R: 经典 14 日, 超卖入/超买出 ---
    "wmsr14": {
        "family": "均值回归", "max_hold": 10,
        "entry": lambda p, i: p["wr14"][i]
        if (p["sma200"][i] is not None and p["wr14"][i] is not None
            and p["close"][i] > p["sma200"][i] and p["wr14"][i] <= -80.0) else None,
        "exit": lambda p, i, pos: "wr_overbought"
        if (p["wr14"][i] is not None and p["wr14"][i] >= -20.0) else None,
    },
    # --- WMSR 短周期 (2日, RSI-2 孪生对照) ---
    "wmsr2": {
        "family": "均值回归", "max_hold": 10,
        "entry": lambda p, i: p["wr2"][i]
        if (p["sma200"][i] is not None and p["wr2"][i] is not None
            and p["close"][i] > p["sma200"][i] and p["wr2"][i] <= -95.0) else None,
        "exit": lambda p, i, pos: "strength" if strength_exit(p, i) else None,
    },
    # --- NINE 神奇九转: TD buy setup 数到 9 入场, 反向 setup 9 或反弹出场 ---
    "nine_td9": {
        "family": "均值回归", "max_hold": 10,
        "entry": lambda p, i: -p["td_buy"][i]
        if (p["sma200"][i] is not None and p["close"][i] > p["sma200"][i]
            and p["td_buy"][i] >= 9) else None,
        "exit": lambda p, i, pos: "strength"
        if (p["td_sell"][i] >= 9 or strength_exit(p, i)) else None,
    },
    # --- BOLL 下轨均值回归: 收盘破下轨买, 回中轨卖 ---
    "boll_meanrev": {
        "family": "均值回归", "max_hold": 10,
        "entry": lambda p, i: ((p["close"][i] - p["bb_l"][i]) / (p["bb_u"][i] - p["bb_l"][i])
                               if p["bb_u"][i] != p["bb_l"][i] else 0.0)
        if (p["sma200"][i] is not None and p["bb_l"][i] is not None
            and p["close"][i] > p["sma200"][i] and p["close"][i] < p["bb_l"][i]) else None,
        "exit": lambda p, i, pos: "mid_band"
        if (p["sma20"][i] is not None and p["close"][i] >= p["sma20"][i]) else None,
    },
    # --- EMA 金叉/死叉 (20/50) ---
    "ema_cross": {
        "family": "趋势", "max_hold": None,
        "entry": lambda p, i: -(p["ema20"][i] / p["ema50"][i] - 1.0)
        if (i > 0 and p["ema50"][i] is not None and p["ema50"][i - 1] is not None
            and p["ema20"][i] > p["ema50"][i] and p["ema20"][i - 1] <= p["ema50"][i - 1]) else None,
        "exit": lambda p, i, pos: "death_cross"
        if (p["ema50"][i] is not None and p["ema20"][i] < p["ema50"][i]) else None,
    },
    # --- SAR 抛物线转向: 翻多入场, 翻空出场 ---
    "sar_trend": {
        "family": "趋势", "max_hold": None,
        "entry": lambda p, i: (p["close"][i] - p["sar"][i]) / p["close"][i]
        if (i > 0 and p["sar_up"][i] is True and p["sar_up"][i - 1] is False) else None,
        "exit": lambda p, i, pos: "sar_flip" if p["sar_up"][i] is False else None,
    },
    # --- Ichimoku 一目均衡表: 价上云 + 转换>基准 (当日转真) 入, 跌破基准线出 ---
    "ichimoku": {
        "family": "趋势", "max_hold": None,
        "entry": lambda p, i: -(p["close"][i] / p["kijun"][i] - 1.0)
        if (i > 0 and p["kijun"][i] is not None and p["tenkan"][i] is not None
            and _above_cloud(p, i) and p["tenkan"][i] > p["kijun"][i]
            and not (_above_cloud(p, i - 1) and p["tenkan"][i - 1] is not None
                     and p["kijun"][i - 1] is not None
                     and p["tenkan"][i - 1] > p["kijun"][i - 1])) else None,
        "exit": lambda p, i, pos: "below_kijun"
        if (p["kijun"][i] is not None and p["close"][i] < p["kijun"][i]) else None,
    },
    # --- KC+BOLL 挤压突破: 近5日内有挤压 + 收盘破20日高, 跌破 SMA20 出 ---
    "squeeze_brk": {
        "family": "突破", "max_hold": None,
        "entry": lambda p, i: ((p["bb_u"][i] - p["bb_l"][i]) / (p["kc_u"][i] - p["kc_l"][i])
                               if p["kc_u"][i] != p["kc_l"][i] else 1.0)
        if (i >= 1 and p["hh20"][i - 1] is not None and p["kc_u"][i] is not None
            and p["bb_u"][i] is not None and any(p["squeeze"][max(0, i - 5):i])
            and p["close"][i] > p["hh20"][i - 1]) else None,
        "exit": lambda p, i, pos: "below_sma20"
        if (p["sma20"][i] is not None and p["close"][i] < p["sma20"][i]) else None,
    },
    # --- 出场件对比: RSI-2 入场不变, 出场换成 SAR 移动止损 (让盈利奔跑) ---
    "rsi2_sar_exit": {
        "family": "出场变体", "max_hold": None,
        "entry": lambda p, i: p["rsi2"][i]
        if (p["sma200"][i] is not None and p["rsi2"][i] is not None
            and p["close"][i] > p["sma200"][i] and p["rsi2"][i] < 10.0) else None,
        "exit": lambda p, i, pos: "sar_flip" if p["sar_up"][i] is False else None,
    },
}


# ---------- 组合模拟 (口径同 learn.py: 10%/仓, 先卖后买, 收盘成交, 无摩擦) ----------

def simulate(pre, universe, window_dates, strat, stop_pct=0.07,
             pos_pct=0.10, reserve=5.0, min_order=5.0, start_capital=1000.0):
    entry_fn, exit_fn, max_hold = strat["entry"], strat["exit"], strat["max_hold"]
    cash, positions, last_px = start_capital, {}, {}
    trade_rets, trade_holds = [], []
    peak, maxdd = start_capital, 0.0
    equity_curve = []  # (date, equity)
    pos_days = 0

    for d in window_dates:
        for sym in list(positions):
            i = pre[sym]["idx"].get(d)
            if i is None:
                continue
            p, pos = pre[sym], positions[sym]
            px = p["close"][i]
            reason = None
            if px <= pos["entry_px"] * (1 - stop_pct):
                reason = "stop"
            elif exit_fn(p, i, pos):
                reason = "signal"
            elif max_hold is not None and i - pos["entry_i"] >= max_hold:
                reason = "time"
            if reason:
                cash += pos["qty"] * px
                trade_rets.append(px / pos["entry_px"] - 1.0)
                trade_holds.append(i - pos["entry_i"])
                del positions[sym]

        equity = cash
        for sym, pos in positions.items():
            i = pre[sym]["idx"].get(d)
            if i is not None:
                last_px[sym] = pre[sym]["close"][i]
            equity += pos["qty"] * last_px[sym]
        pos_days += len(positions)

        cands = []
        for sym in universe:
            i = pre[sym]["idx"].get(d)
            if i is None or sym in positions:
                continue
            score = entry_fn(pre[sym], i)
            if score is not None:
                cands.append((score, sym, i))
        cands.sort()
        pos_usd = equity * pos_pct
        for _, sym, i in cands:
            px = pre[sym]["close"][i]
            amt = min(pos_usd, cash - reserve)
            if amt < min_order:
                break
            positions[sym] = {"qty": amt / px, "entry_px": px, "entry_i": i}
            last_px[sym] = px
            cash -= amt

        peak = max(peak, equity)
        maxdd = max(maxdd, (peak - equity) / peak * 100.0)
        equity_curve.append((d, equity))

    n = len(trade_rets)
    wins = sum(1 for r in trade_rets if r > 0)
    sorted_r = sorted(trade_rets)
    yearly = {}
    prev_eq, prev_year = start_capital, window_dates[0][:4]
    for d, eq in equity_curve:
        y = d[:4]
        if y != prev_year:
            base = yearly.get(prev_year, (start_capital, None))[0]
            yearly[prev_year] = (base, last_year_eq)
            prev_year = y
            yearly[y] = (last_year_eq, None)
        elif prev_year not in yearly:
            yearly[prev_year] = (start_capital, None)
        last_year_eq = eq
    yearly[prev_year] = (yearly.get(prev_year, (start_capital, None))[0], last_year_eq)
    yearly_ret = {y: round((b / a - 1) * 100, 1) for y, (a, b) in yearly.items() if b}

    return {
        "curve": [e for _, e in equity_curve],
        "return_pct": round((equity / start_capital - 1) * 100, 1),
        "maxdd_pct": round(maxdd, 1),
        "trades": n,
        "win_rate": round(wins / n * 100, 1) if n else None,
        "avg_trade_pct": round(sum(trade_rets) / n * 100, 2) if n else None,
        "median_trade_pct": round(sorted_r[n // 2] * 100, 2) if n else None,
        "p10_trade_pct": round(sorted_r[max(0, n // 10 - 1)] * 100, 2) if n else None,
        "avg_hold_days": round(sum(trade_holds) / n, 1) if n else None,
        "avg_positions": round(pos_days / len(window_dates), 1),
        "yearly_ret": yearly_ret,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bars", required=True)
    ap.add_argument("--start", default="2022-01-03")
    ap.add_argument("--out")
    a = ap.parse_args()

    raw = json.load(open(a.bars))
    hist = {}
    for r in raw["data"]["results"]:
        bs = []
        for b in r["bars"]:
            if b.get("high") is None or b.get("low") is None:
                continue
            bs.append({"d": b["begins_at"][:10], "c": float(b["close_price"]),
                       "h": float(b["high"]), "l": float(b["low"])})
        if len(bs) >= 60:
            hist[r["symbol"]] = bs
    pre = {s: precompute(b) for s, b in hist.items()}
    universe = sorted(pre)
    all_dates = sorted({d for p in pre.values() for d in p["dates"]})
    window = [d for d in all_dates if d >= a.start]
    print(f"universe={len(universe)} 只, 窗口 {window[0]} → {window[-1]} ({len(window)} 交易日)\n")

    results = {}
    for name, strat in STRATEGIES.items():
        results[name] = simulate(pre, universe, window, strat)
        r = results[name]
        print(f"[{strat['family']}] {name}: 总收益 {r['return_pct']:+.1f}%  回撤 {r['maxdd_pct']:.1f}%  "
              f"{r['trades']}笔  胜率 {r['win_rate']}%  均值/笔 {r['avg_trade_pct']}%  "
              f"p10 {r['p10_trade_pct']}%  持仓 {r['avg_hold_days']}天  "
              f"平均仓数 {r['avg_positions']}  分年 {r['yearly_ret']}")

    # 分散价值: 日收益与基准的相关性 + 与基准 50/50 (每日再平衡) 组合表现
    def daily_rets(curve):
        return [curve[i] / curve[i - 1] - 1 for i in range(1, len(curve))]

    base = daily_rets(results["baseline_rsi2"]["curve"])
    print("\n与基准 (baseline_rsi2) 的互补性:")
    for name in results:
        if name == "baseline_rsi2":
            continue
        r2 = daily_rets(results[name]["curve"])
        mb, mr = sum(base) / len(base), sum(r2) / len(r2)
        cov = sum((a_ - mb) * (b_ - mr) for a_, b_ in zip(base, r2)) / len(base)
        sb = math.sqrt(sum((x - mb) ** 2 for x in base) / len(base))
        sr = math.sqrt(sum((x - mr) ** 2 for x in r2) / len(r2))
        corr = cov / (sb * sr) if sb > 0 and sr > 0 else float("nan")
        eq, peak, dd = 1.0, 1.0, 0.0
        for a_, b_ in zip(base, r2):
            eq *= 1 + (a_ + b_) / 2
            peak = max(peak, eq)
            dd = max(dd, (peak - eq) / peak * 100)
        results[name]["corr_vs_baseline"] = round(corr, 2)
        results[name]["blend5050_return_pct"] = round((eq - 1) * 100, 1)
        results[name]["blend5050_maxdd_pct"] = round(dd, 1)
        print(f"  {name}: 相关性 {corr:+.2f}  50/50组合 收益 {(eq - 1) * 100:+.1f}% / 回撤 {dd:.1f}%")

    if a.out:
        for r in results.values():
            r.pop("curve", None)
        with open(a.out, "w") as f:
            json.dump(results, f, ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
