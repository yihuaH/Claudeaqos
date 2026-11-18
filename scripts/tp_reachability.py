#!/usr/bin/env python3
"""止盈/止损可达性换算器 —— **报告级工具, 绝不产出任何订单、绝不改账本** (红线2)。

🔴 **为什么存在 (2026-11-16 用户纠正)**: 会话此前报可达性只报**权利金涨幅**
("755C 需 +134% → 彻底不可达")。该说法**在定性上是错的** —— 期权 delta < 1,
权利金涨幅对标的涨幅有巨大放大, 实测 755C **18.0×** / 735C **34.0×** / 725C 5.4×,
即「权利金 +275%」其实只是「QQQ +8.0%」。用户原话:「这个判断要看 qqq 要涨的百分比」。
=> **以后每个窗口的战报都必须带「距止盈需 QQQ ±X%」**, 权利金涨幅只能附带。

📐 **方法**:
  ① **用券商 mark 反解隐含波动率**, 使 BS 精确复现当日报价 —— 消掉 r/q 假设造成的约 −4% 偏差;
  ② 解「bid 达到止盈线」所需的 QQQ 价格。⚠️ 止盈判定口径是 **bid ≥ 止盈线**,
     故目标 mark = 止盈线 + 当前半点差 (假设点差不变);
  ③ 给两个时点: **今日即到** / **时间止损日** (已扣 theta, 后者必然更高);
  ④ 用 QQQ 历史日线算该涨幅在同样交易日数内的频率, **两种口径分开**:
     · `freq_close`  = 收盘对收盘 → 对应**引擎自己在 15:20 的止盈检查**;
     · `freq_touch`  = 期间最高价触及 → 对应**常驻 GTC** (盘中限价单, 触价即成交)。
     两者实测差 1.3~1.5 倍, **不可混用**。

⚠️ **假设与局限 (报告里要带)**:
  · IV 恒定 —— 实际上涨时 IV 通常回落, 故真实难度**高于**本估计 (本工具偏乐观);
  · 点差恒定; r=0.04 / q=0.005 与 `qqq_leap.py:_bs_delta` 同参;
  · 历史频率是**无条件频率**, 不是对当前市况的条件概率;
  · 时间止损日按"跳过周末 + 下方静态节假日表"推算 —— 表若过期会**少算**日历日,
    使所需涨幅偏低 (同样偏乐观)。加新年份时补 `_HOLIDAYS`。

📌 交易日与 DTE 口径**直接复用 `qqq_leap.py` 的 `parse_occ` / `dte` / `_held_trading_days`**,
   以保证与引擎不漂移。

用法:
  python3 scripts/tp_reachability.py \\
    --ledger state/qqq_leap_positions.json --config strategy/qqq_leap.json \\
    --chain <含 bid/ask 的链文件> --spot 661.82 --date 2026-11-16 \\
    [--bars <QQQ 日线>] [--out FILE] [--markdown]
"""
import argparse, json, math, os, sys
from datetime import date as _date, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from qqq_leap import parse_occ, dte, _held_trading_days  # noqa: E402  口径复用, 勿改为本地实现

# 美股休市日 (静态表; 只需覆盖当前持仓的时间止损视野)。过期会少算日历日 → 偏乐观。
_HOLIDAYS = {
    "2026-11-26", "2026-12-25",
    "2027-01-01", "2027-01-18", "2027-02-15", "2027-03-26", "2027-05-31",
    "2027-06-18", "2027-07-05", "2027-09-06", "2027-11-25", "2027-12-24",
}


def _N(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def bs_call(S, K, T, sig, r=0.04, q=0.005):
    if T <= 0:
        return max(S - K, 0.0)
    d1 = (math.log(S / K) + (r - q + 0.5 * sig * sig) * T) / (sig * math.sqrt(T))
    return S * math.exp(-q * T) * _N(d1) - K * math.exp(-r * T) * _N(d1 - sig * math.sqrt(T))


def implied_vol(price, S, K, T):
    lo, hi = 0.005, 3.0
    for _ in range(200):
        m = (lo + hi) / 2
        if bs_call(S, K, T, m) < price:
            lo = m
        else:
            hi = m
    return (lo + hi) / 2


def spot_for_price(target, K, T, sig):
    lo, hi = 1.0, 20000.0
    for _ in range(200):
        m = (lo + hi) / 2
        if bs_call(m, K, T, sig) < target:
            lo = m
        else:
            hi = m
    return (lo + hi) / 2


def add_trading_days(start_iso, n):
    """从 start 往后数 n 个交易日, 返回那天的 ISO 日期 (跳过周末与 _HOLIDAYS)。"""
    d = _date.fromisoformat(start_iso)
    left = n
    while left > 0:
        d += timedelta(days=1)
        if d.weekday() < 5 and d.isoformat() not in _HOLIDAYS:
            left -= 1
    return d.isoformat()


def hist_freq(closes, highs, need_pct, n_days):
    """need_pct 的涨幅在 n_days 个交易日内达成的历史频率 (两种口径)。"""
    if len(closes) <= n_days + 1:
        return None
    hc = ht = tot = 0
    for i in range(len(closes) - n_days):
        tot += 1
        tgt = closes[i] * (1 + need_pct / 100.0)
        if closes[i + n_days] >= tgt:
            hc += 1
        if max(highs[i + 1:i + n_days + 1]) >= tgt:
            ht += 1
    return {"n": tot, "close_hits": hc, "touch_hits": ht,
            "freq_close_pct": round(hc / tot * 100, 1),
            "freq_touch_pct": round(ht / tot * 100, 1)}


def load_chain(path, underlying):
    raw = json.load(open(path))
    return raw.get(underlying, raw)


def missing_trading_days(last_bar, today):
    """last_bar 之后、today 之前的交易日 (跳周末与 _HOLIDAYS)。非空 = 日线文件过期。"""
    d = _date.fromisoformat(last_bar)
    end = _date.fromisoformat(today)
    out = []
    while True:
        d += timedelta(days=1)
        if d >= end:
            break
        if d.weekday() < 5 and d.isoformat() not in _HOLIDAYS:
            out.append(d.isoformat())
    return out


def load_bars(path, today):
    """返回 (closes, highs, trading_dates, span)。
    `trading_dates` 与 `qqq_leap._load_hist` 同口径: **不含当日** —— 该函数靠这一点给当日 +1。

    🔴 **过期日线硬闸 (2026-11-18 实测踩到后加)**: 本工具复用引擎的 `_held_trading_days`
    来保证交易日口径不漂移, 但**那个函数的输入是这里给的 `trading_dates`** ——
    喂一个过期的日线文件, 它会**静默少数交易日**, 于是 `trading_days_left` 偏大、
    时间止损日往后错。当日实测: 用 11-16 拉的文件跑 11-18, 算出剩 8/19/8 日、
    止损日 2026-12-01/12-16/12-01; 换成当日文件后是 **7/18/7 日、11-30/12-15/11-30**
    (与引擎及 CLAUDE.md 记录一致)。**1 天的误差落在出场那一天, 不可接受。**
    故这里直接 `SystemExit`, 不给"警告后继续"的选项。"""
    raw = json.load(open(path))
    res = raw["data"]["results"] if "data" in raw else raw["results"]
    bars = res[0]["bars"]
    last = bars[-1]["begins_at"][:10]
    miss = missing_trading_days(last, today)
    if miss:
        raise SystemExit(
            f"日线文件过期: 最后一根 bar 是 {last}, 但 {today} 之前还有 "
            f"{len(miss)} 个交易日未覆盖 {miss}。\n"
            f"→ 交易日计数会偏少, 时间止损日会往后错。请重新拉日线后再跑:\n"
            f"   python3 scripts/integrations.py bars --symbols QQQ --start 2016-01-01 --out <新文件>")
    cl = [float(b["close_price"]) for b in bars]
    hi = [float(b.get("high_price") or b["close_price"]) for b in bars]
    td = [b["begins_at"][:10] for b in bars if b["begins_at"][:10] < today]
    return cl, hi, td, f"{bars[0]['begins_at'][:10]} ~ {last} ({len(cl)} 个交易日)"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ledger", required=True)
    ap.add_argument("--config", required=True)
    ap.add_argument("--chain", required=True, help='{"QQQ": {OCC: {bid, ask}}} 或扁平 {OCC: {...}}')
    ap.add_argument("--spot", type=float, required=True, help="标的当日价 (券商 last_trade_price)")
    ap.add_argument("--date", required=True)
    ap.add_argument("--bars", help="标的历史日线 (integrations.py bars 产出); 给了才算历史频率")
    ap.add_argument("--out")
    ap.add_argument("--markdown", action="store_true", help="同时打印可直接贴进战报的表格")
    a = ap.parse_args()

    cfg = json.load(open(a.config))
    led = json.load(open(a.ledger))
    und = cfg.get("underlying", "QQQ")
    chain = load_chain(a.chain, und)
    ts_limit = cfg["exit"].get("max_holding_trading_days")

    cl = hi = trading_dates = None
    bars_span = None
    if a.bars:
        cl, hi, trading_dates, bars_span = load_bars(a.bars, a.date)

    rows = []
    for occ, pos in sorted(led.get("positions", {}).items()):
        q = chain.get(occ)
        if not q or not q.get("bid") or not q.get("ask"):
            rows.append({"occ": occ, "error": "链里没有该合约的 bid/ask, 无法换算"})
            continue
        meta = parse_occ(occ)
        K = float(meta["strike"])
        bid, ask = float(q["bid"]), float(q["ask"])
        mark = (bid + ask) / 2.0
        d_now = dte(meta["expiry"], a.date)
        tp = float(pos["take_profit_line"])
        half = mark - bid                      # 判定口径是 bid, 故 mark 要高出半点差
        target_mark = tp + half
        iv = implied_vol(mark, a.spot, K, d_now / 365.0)

        s_now = spot_for_price(target_mark, K, d_now / 365.0, iv)
        # ⚠️ 账本**不存** held_trading_days (引擎 cmd_signal 是运行时从 entry_date 算的),
        # 故这里用引擎同一个函数重算; 没传 --bars 就只能退化为"只给今日即到"那一列。
        held = pos.get("held_trading_days")
        if held is None and trading_dates is not None and pos.get("entry_date"):
            held = _held_trading_days(trading_dates, pos["entry_date"], a.date)
        left = None if (ts_limit is None or held is None) else max(int(ts_limit) - int(held), 0)
        r = {"occ": occ, "entry_date": pos.get("entry_date"),
             "entry_fill_price": pos.get("entry_fill_price"),
             "take_profit_line": round(tp, 4), "bid": bid, "ask": ask, "mark": round(mark, 4),
             "implied_vol": round(iv, 4), "dte": d_now,
             "premium_gain_needed_pct": round((tp / bid - 1) * 100, 1),
             "spot_now": round(s_now, 2),
             "spot_now_pct": round((s_now / a.spot - 1) * 100, 2),
             "trading_days_left": left}
        if left is not None:
            stop_iso = add_trading_days(a.date, left)
            cal = (_date.fromisoformat(stop_iso) - _date.fromisoformat(a.date)).days
            s_stop = spot_for_price(target_mark, K, max(d_now - cal, 1) / 365.0, iv)
            r.update({"time_stop_date": stop_iso, "calendar_days_to_stop": cal,
                      "spot_at_stop": round(s_stop, 2),
                      "spot_at_stop_pct": round((s_stop / a.spot - 1) * 100, 2),
                      "amplification_x": round(r["premium_gain_needed_pct"]
                                               / ((s_stop / a.spot - 1) * 100), 1)
                      if s_stop > a.spot else None})
            if cl:
                r["hist"] = hist_freq(cl, hi, (s_stop / a.spot - 1) * 100, left)
        rows.append(r)

    out = {"date": a.date, "underlying": und, "spot": a.spot,
           "take_profit_mult": cfg["exit"].get("take_profit_mult"),
           "max_holding_trading_days": ts_limit,
           "bars_span": bars_span, "positions": rows,
           "_caveats": ["IV 恒定 (上涨时真实 IV 通常回落 → 实际更难, 本估计偏乐观)",
                        "点差恒定; 目标 mark = 止盈线 + 半点差 (判定口径是 bid ≥ 止盈线)",
                        "历史频率是无条件频率, 不是对当前市况的条件概率",
                        "freq_close = 引擎 15:20 自检口径; freq_touch = 常驻 GTC 口径, 不可混用",
                        "_HOLIDAYS 是静态表, 过期会少算日历日 → 所需涨幅偏低"],
           "_note": "报告级工具: 不产出订单、不改账本 (红线2)"}
    print(json.dumps(out, ensure_ascii=False, indent=1))
    if a.out:
        json.dump(out, open(a.out, "w"), ensure_ascii=False, indent=1)

    if a.markdown:
        print("\n--- 可直接贴进战报 ---")
        print(f"| OCC | 成本 | bid | 止盈线 | 权利金需涨 | **距止盈需 {und}** | 止损日需 {und} | 历史频率 (收盘/触及) |")
        print("|---|---|---|---|---|---|---|---|")
        for r in rows:
            if r.get("error"):
                print(f"| {r['occ']} | — | — | — | — | **{r['error']}** | — | — |")
                continue
            h = r.get("hist") or {}
            fq = (f"{h['freq_close_pct']}% / {h['freq_touch_pct']}%" if h else "—")
            sp = r.get("spot_at_stop_pct")
            stop_cell = f"{sp:+.2f}%" if sp is not None else "—"
            print(f"| {r['occ']} | {r['entry_fill_price']} | {r['bid']} | {r['take_profit_line']} "
                  f"| +{r['premium_gain_needed_pct']}% | **{r['spot_now_pct']:+.2f}%** "
                  f"| {stop_cell} | {fq} |")


if __name__ == "__main__":
    main()
