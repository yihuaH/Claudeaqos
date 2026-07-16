#!/usr/bin/env python3
"""
隔夜均值回归引擎 (实盘, ETF + 个股) — IBS 收盘买入 / 次日收盘卖出。

  signal  由 历史bars + 当日实时OHLC + 财报日 + 宏观 + 账本 计算今天的订单

规则 (参数在 strategy/overnight.json, 全部确定性):
- 入场: 200日线上方 且 当日 IBS < ibs_max (收在日内区间底部), 按 IBS 从低到高排序。
- 出场: 持有满 1 个交易日无条件卖; 唯一例外: 当日 IBS 仍 < 阈值可顺延一天 (每仓一次)。
  止损 (盘中价距入场价 -stop%) 与 财报临近 强制卖出优先于顺延。
- 个股防御层: 财报回避/未知不入场、异动过滤、行业上限; ETF 豁免防御, 同受趋势过滤。
- 合规: 当天买的绝不当天卖 (不产生日内交易); 当天卖过的符号不当天回买。
- 资金: 可卖存量股腾钱 (每天 ≤ funding.max_sales_per_day, 最弱优先, 避开当日买入)。

成交回写: 复用 signals.py apply —
  bucket=strategy 的成交 → apply 到 state/overnight_positions.json
  bucket=legacy   的成交 → apply 到 state/positions.json (主账本)
执行层照单下单, 不得修改。纯 stdlib, 不联网。
"""
import argparse
import json
import os
import sys
from datetime import date as _date

sys.path.insert(0, __file__.rsplit("/", 1)[0])
from signals import load_json, sma, parse_historicals  # noqa: E402


def ibs_of(snap):
    h, l, c = snap["high"], snap["low"], snap["close"]
    if h is None or l is None or h <= l:
        return None
    return (c - l) / (h - l)


def cmd_signal(a):
    cfg = load_json(a.config)
    state = load_json(a.state)
    main_state = load_json(a.main_state)
    if a.window == "close" and not a.bars:
        raise SystemExit("close 窗口需要 --bars")
    hist = parse_historicals(a.bars if isinstance(a.bars, list) else [a.bars]) if a.bars else {}
    snaps = load_json(a.snapshots)
    earnings = load_json(a.earnings) if a.earnings else None
    macro = load_json(a.macro) if a.macro else None
    broker = load_json(a.positions) if a.positions else {}
    today = a.date
    pv, bp = float(a.portfolio_value), float(a.buying_power)
    warnings = []

    sf = cfg["stocks_from"]
    if not os.path.isabs(sf) and not os.path.exists(sf):
        sf = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(a.config))), sf)
    stocks_cfg = load_json(sf)
    stock_syms = stocks_cfg["symbols"]
    sectors = stocks_cfg.get("sectors", {})
    etfs = cfg["universe_etfs"]
    universe = etfs + [s for s in stock_syms if s not in etfs]

    out = {"date": today, "track": "overnight", "sells": [], "buys": [],
           "warnings": warnings}

    if not cfg.get("enabled"):
        out["note"] = "overnight disabled"
        _emit(out, a.out)
        return
    if state.get("halted") or main_state.get("halted"):
        out["note"] = "halted (本轨道或主账本), 只读不交易"
        _emit(out, a.out)
        return

    # 今日快照数据过期检查 (休市日/数据故障时快照日期不是今天 → 不交易)
    ref = snaps.get("SPY") or next(iter(snaps.values()), None)
    if not ref or ref.get("date") != today:
        out["note"] = f"快照日期 {ref.get('date') if ref else 'n/a'} != {today}, 不交易"
        _emit(out, a.out)
        return

    def price(sym):
        return snaps[sym]["close"] if sym in snaps else None

    def days_to_earnings(sym):
        if not earnings or earnings.get(sym) is None:
            return None
        try:
            return (_date.fromisoformat(earnings[sym]) - _date.fromisoformat(today)).days
        except ValueError:
            return None

    # --- 出场 ---
    exit_window = cfg["exit"].get("window", "next_close")
    pos_book = state.get("strategy_positions", {})
    sold_today = set()
    for sym, pos in sorted(pos_book.items()):
        if pos["entry_date"] >= today:
            continue  # 当天买的绝不当天卖
        px = price(sym)
        if px is None:
            warnings.append(f"{sym}: 无今日快照, 持仓顺延一天 (人工留意)")
            continue
        if a.window == "open":
            # 晨间窗口: 隔夜仓位无条件开盘卖出
            reason = "open_exit"
        else:
            cur_ibs = ibs_of(snaps[sym])
            reason = None
            ed = days_to_earnings(sym)
            if px <= float(pos["entry_price"]) * (1 - cfg["exit"]["stop_loss_pct"] / 100.0):
                reason = "stop_loss"
            elif ed is not None and 0 <= ed <= int(cfg["exit"]["earnings_exit_days"]):
                reason = "earnings_exit"
            elif (exit_window == "next_close"
                  and cur_ibs is not None and cur_ibs < cfg["exit"]["extend_once_if_ibs_below"]
                  and not pos.get("extended")):
                # 顺延仅在"次日收盘卖"模式下启用; next_open 模式的收盘窗口只做兜底清仓
                out.setdefault("extends", []).append(sym)
                continue
            else:
                reason = "overnight_exit" if exit_window == "next_close" else "close_backstop_exit"
        qty = float(pos["qty"])
        if sym in broker:
            qty = min(qty, float(broker[sym].get("available", qty)))
        if qty > 0:
            sold_today.add(sym)
            out["sells"].append({"symbol": sym, "qty": round(qty, 6), "bucket": "strategy",
                                 "reason": reason, "est_price": px})

    if a.window == "open":
        out["note"] = f"晨间窗口: {len(out['sells'])} 笔开盘卖出, 不开新仓"
        _emit(out, a.out)
        return

    # 顺延标记持久化 (extends 直接写回 state, 与订单无关且确定性)
    if out.get("extends"):
        for sym in out["extends"]:
            state["strategy_positions"][sym]["extended"] = True
        with open(a.state, "w") as f:
            json.dump(state, f, indent=2, ensure_ascii=False)
            f.write("\n")

    # --- 宏观闸门 (只拦新开仓) ---
    risk_off = False
    if macro and "vix" in macro:
        mc = cfg["macro"]
        stale = False
        if macro.get("vix_date"):
            try:
                age = (_date.fromisoformat(today) - _date.fromisoformat(macro["vix_date"])).days
                stale = age < 0 or age > int(mc.get("vix_max_staleness_days", 5))
            except ValueError:
                stale = True
        if not stale and float(macro["vix"]) >= mc["vix_no_new_entries_above"]:
            risk_off = True
            out["note"] = f"VIX {macro['vix']} ≥ {mc['vix_no_new_entries_above']}, 今日不开新仓"

    # --- 入场 ---
    d = cfg["defense"]
    no_earnings_data = earnings is None
    if no_earnings_data:
        warnings.append("无财报日数据: 个股全部跳过, 仅 ETF 可入场")
    held = set(pos_book) - sold_today
    cands = []
    if not risk_off:
        for sym in universe:
            if sym in held or sym in sold_today or sym not in snaps:
                continue
            if snaps[sym].get("date") != today:
                continue
            bars_c = [c for dt, c in hist.get(sym, []) if dt < today]
            px = price(sym)
            bars_c.append(px)
            s200 = sma(bars_c, 200)
            if s200 is None or px <= s200:
                continue
            v = ibs_of(snaps[sym])
            if v is None or v >= cfg["entry"]["ibs_max"]:
                continue
            is_etf = sym in etfs
            if not is_etf:
                if no_earnings_data or sym not in (earnings or {}):
                    continue
                ed = days_to_earnings(sym)
                if ed is not None and 0 <= ed <= int(d["earnings_blackout_days"]):
                    continue
                look = int(d["move_lookback_days"])
                w = bars_c[-(look + 1):]
                if any(w[j - 1] and abs(w[j] / w[j - 1] - 1) * 100.0 >= float(d["max_daily_move_pct"])
                       for j in range(1, len(w))):
                    continue
            cands.append((round(v, 4), sym, is_etf))
    cands.sort()

    # 行业上限 (ETF 豁免) + 数量选择
    slots = max(0, min(cfg["sizing"]["max_strategy_positions"] - len(held),
                       cfg["sizing"]["max_new_entries_per_day"]))
    sec_count = {}
    for s in held:
        sc = sectors.get(s)
        if sc:
            sec_count[sc] = sec_count.get(sc, 0) + 1
    picked = []
    for v, sym, is_etf in cands:
        if len(picked) >= slots:
            break
        sc = sectors.get(sym)
        if not is_etf and sc and sec_count.get(sc, 0) >= int(d["max_per_sector"]):
            continue
        picked.append((v, sym))
        if sc and not is_etf:
            sec_count[sc] = sec_count.get(sc, 0) + 1

    # --- 资金与换仓 ---
    pos_usd = round(pv * cfg["sizing"]["position_pct_of_portfolio"] / 100.0, 2)
    cash = bp - cfg["sizing"]["min_cash_reserve_usd"]
    cash += sum(s["qty"] * s["est_price"] for s in out["sells"])

    legacy = main_state.get("legacy_positions", {})
    funding_sales = 0

    def weakness(sym):
        bars_c = [c for dt, c in hist.get(sym, []) if dt < today]
        if sym in snaps:
            bars_c.append(price(sym))
        s20 = sma(bars_c, 20)
        return (bars_c[-1] / s20) if (s20 and bars_c) else 1.0

    def bought_today(sym):
        return float(broker.get(sym, {}).get("intraday", 0)) > 0

    fundable = sorted([s for s in legacy
                       if s not in sold_today and s not in held
                       and hist.get(s) and not bought_today(s)], key=weakness)

    for v, sym in picked:
        need = pos_usd
        while (cash < need and cfg["funding"]["allowed"]
               and funding_sales < cfg["funding"]["max_sales_per_day"] and fundable):
            lsym = fundable.pop(0)
            lqty = float(legacy[lsym]["qty"])
            if lsym in broker:
                lqty = min(lqty, float(broker[lsym].get("available", lqty)))
            lpx = price(lsym)
            if lqty <= 0 or lpx is None:
                continue
            out["sells"].append({"symbol": lsym, "qty": round(lqty, 6), "bucket": "legacy",
                                 "reason": "funding_rotation", "est_price": lpx})
            sold_today.add(lsym)
            cash += lqty * lpx
            funding_sales += 1
        amt = round(min(need, cash), 2)
        if amt >= cfg["sizing"]["min_order_usd"]:
            out["buys"].append({"symbol": sym, "dollar_amount": amt, "reason": "ibs_entry",
                                "ibs": v, "est_price": price(sym)})
            cash -= amt
        else:
            warnings.append(f"{sym}: IBS 信号但资金不足 (${cash:.2f}), 跳过")

    _emit(out, a.out)


def _emit(out, path):
    s = json.dumps(out, indent=2, ensure_ascii=False)
    if path:
        with open(path, "w") as f:
            f.write(s + "\n")
    print(s)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("signal")
    s.add_argument("--config", required=True, help="strategy/overnight.json")
    s.add_argument("--state", required=True, help="state/overnight_positions.json")
    s.add_argument("--main-state", required=True, help="state/positions.json (读存量/halted)")
    s.add_argument("--bars", nargs="+", help="日线历史 (integrations.py bars); open 窗口可省略")
    s.add_argument("--window", choices=["open", "close"], default="close",
                   help="open=晨间窗口(只卖不买), close=主窗口(入场+兜底出场)")
    s.add_argument("--snapshots", required=True, help="当日实时 OHLC (integrations.py snapshots)")
    s.add_argument("--earnings", help="财报日映射; 缺省则个股全部不入场")
    s.add_argument("--macro", help="VIX 宏观文件")
    s.add_argument("--positions", help='券商持仓映射 {"SYM":{qty,available,intraday}}')
    s.add_argument("--date", required=True)
    s.add_argument("--portfolio-value", required=True)
    s.add_argument("--buying-power", required=True)
    s.add_argument("--out")
    s.set_defaults(func=cmd_signal)
    a = p.parse_args()
    a.func(a)


if __name__ == "__main__":
    main()
