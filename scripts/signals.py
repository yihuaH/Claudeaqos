#!/usr/bin/env python3
"""
Claudeaqos 每日自动交易 — 确定性信号引擎 (RSI-2 均值回归 + 长期趋势过滤)

子命令:
  signal  由 历史数据 + 实时报价 + 持仓状态 + 配置 计算出今天的订单清单
  apply   把实际成交回写到 state/positions.json

设计原则: 纯 stdlib、确定性(同样输入永远产出同样订单)、绝不联网。
执行层(Claude 会话)只负责搬运数据和按输出下单, 不得自行修改订单。
"""
import argparse
import json
import sys


# ---------- IO helpers ----------

def load_json(path):
    with open(path) as f:
        return json.load(f)


def save_json(path, obj):
    with open(path, "w") as f:
        json.dump(obj, f, indent=2, ensure_ascii=False)
        f.write("\n")


def parse_historicals(paths):
    """接受 get_equity_historicals 原始输出文件, 返回 {SYM: [(date, close), ...] 升序}"""
    out = {}
    for p in paths:
        raw = load_json(p)
        for r in raw["data"]["results"]:
            bars = {}
            for b in r["bars"]:
                if b.get("interpolated"):
                    continue
                if b.get("session") not in (None, "reg"):
                    continue
                bars[b["begins_at"][:10]] = float(b["close_price"])
            merged = out.setdefault(r["symbol"], {})
            merged.update(bars)
    return {sym: sorted(d.items()) for sym, d in out.items()}


def parse_quotes(path):
    """接受 简单映射 {"SYM": price} 或 get_equity_quotes 原始输出。返回 {SYM: price}"""
    raw = load_json(path)
    if "data" in raw:
        return {r["quote"]["symbol"]: float(r["quote"]["last_trade_price"])
                for r in raw["data"]["results"]
                if r["quote"].get("state") == "active"}
    return {k: float(v) for k, v in raw.items()}


def parse_positions(path):
    """接受 简单映射 {"SYM": {qty, available, intraday}} 或 get_equity_positions 原始输出。"""
    raw = load_json(path)
    if "data" in raw:
        return {p["symbol"]: {
                    "qty": float(p["quantity"]),
                    "available": float(p["shares_available_for_sells"]),
                    "intraday": float(p.get("intraday_quantity", 0)),
                } for p in raw["data"]["positions"]}
    return {k: {"qty": float(v["qty"]),
                "available": float(v.get("available", v["qty"])),
                "intraday": float(v.get("intraday", 0))}
            for k, v in raw.items()}


# ---------- indicators ----------

def sma(closes, n):
    if len(closes) < n:
        return None
    return sum(closes[-n:]) / n


def rsi(closes, period=2):
    """Wilder RSI"""
    if len(closes) <= period:
        return None
    gains = losses = 0.0
    for i in range(1, period + 1):
        d = closes[i] - closes[i - 1]
        gains += max(d, 0.0)
        losses += max(-d, 0.0)
    ag, al = gains / period, losses / period
    for i in range(period + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        ag = (ag * (period - 1) + max(d, 0.0)) / period
        al = (al * (period - 1) + max(-d, 0.0)) / period
    if al == 0:
        return 100.0
    return 100.0 - 100.0 / (1.0 + ag / al)


def trading_days_since(dates, since_date):
    return sum(1 for d in dates if d > since_date)


# ---------- signal ----------

def cmd_signal(a):
    cfg = load_json(a.config)
    state = load_json(a.state)
    hist = parse_historicals(a.historicals)
    quotes = parse_quotes(a.quotes)
    broker = parse_positions(a.positions) if a.positions else None
    today = a.date
    warnings = []

    # 用实时报价补上今天的临时收盘价
    series = {}
    for sym, bars in hist.items():
        dates = [d for d, _ in bars if d < today]
        closes = [c for d, c in bars if d < today]
        if sym in quotes:
            dates.append(today)
            closes.append(quotes[sym])
        else:
            warnings.append(f"{sym}: 无实时报价, 使用最后一根历史K线 ({dates[-1] if dates else 'n/a'})")
        series[sym] = (dates, closes)

    ind = {}
    for sym, (dates, closes) in series.items():
        ind[sym] = {
            "close": round(closes[-1], 4) if closes else None,
            "sma5": sma(closes, 5),
            "sma20": sma(closes, 20),
            "sma200": sma(closes, 200),
            "rsi2": rsi(closes, 2),
        }

    pv = float(a.portfolio_value)
    bp = float(a.buying_power)
    hwm = max(float(state.get("high_water_mark", pv)), pv)
    drawdown_pct = (hwm - pv) / hwm * 100.0
    cb_limit = cfg["circuit_breaker"]["max_drawdown_pct_from_hwm"]
    circuit_breaker = drawdown_pct >= cb_limit
    halted = bool(state.get("halted", False))

    out = {
        "date": today,
        "portfolio_value": pv,
        "buying_power": bp,
        "high_water_mark": hwm,
        "drawdown_pct": round(drawdown_pct, 2),
        "halted": halted,
        "circuit_breaker_triggered": circuit_breaker,
        "sells": [],
        "buys": [],
        "indicators": {s: {k: (round(v, 4) if isinstance(v, float) else v)
                           for k, v in i.items()} for s, i in ind.items()},
        "warnings": warnings,
    }

    if halted or circuit_breaker:
        out["note"] = ("熔断触发: 回撤 %.2f%% ≥ %.1f%%。停止一切交易, 等待用户人工决定。"
                       % (drawdown_pct, cb_limit)) if circuit_breaker else "state.halted=true, 跳过交易。"
        _emit(out, a.out)
        return

    strat = state.get("strategy_positions", {})
    legacy = state.get("legacy_positions", {})

    def broker_qty(sym, fallback):
        if broker is None:
            return fallback
        return min(fallback, broker.get(sym, {}).get("available", 0.0))

    def bought_today(sym):
        if broker is None:
            return False
        return broker.get(sym, {}).get("intraday", 0.0) > 0

    # --- 卖出: 策略仓位出场 ---
    exiting = set()
    for sym, pos in strat.items():
        i = ind.get(sym)
        if not i or i["rsi2"] is None:
            warnings.append(f"{sym}: 指标数据不足, 跳过出场检查")
            continue
        px, entry = i["close"], float(pos["entry_price"])
        reason = None
        if px <= entry * (1 - cfg["exit"]["stop_loss_pct"] / 100.0):
            reason = "stop_loss"
        elif (i["sma5"] is not None and px > i["sma5"]) or i["rsi2"] >= cfg["exit"]["rsi2_min"]:
            reason = "exit_strength"
        elif trading_days_since(series[sym][0], pos["entry_date"]) >= cfg["exit"]["max_holding_days"]:
            reason = "time_stop"
        if reason:
            qty = broker_qty(sym, float(pos["qty"]))
            if qty > 0:
                exiting.add(sym)
                out["sells"].append({"symbol": sym, "qty": round(qty, 6), "bucket": "strategy",
                                     "reason": reason, "est_price": i["close"]})

    # --- 卖出: 存量持仓保护性止损 (相对纳管基准价) ---
    for sym, pos in legacy.items():
        i = ind.get(sym)
        if not i:
            warnings.append(f"{sym}: 存量持仓无行情数据")
            continue
        ref = float(pos["adoption_ref_price"])
        if i["close"] <= ref * (1 - cfg["legacy"]["protective_stop_pct"] / 100.0):
            if cfg["legacy"]["avoid_selling_same_day_buys"] and bought_today(sym):
                warnings.append(f"{sym}: 触发保护止损但今日有买入(GFV保护), 顺延到明天")
                continue
            qty = broker_qty(sym, float(pos["qty"]))
            if qty > 0:
                exiting.add(sym)
                out["sells"].append({"symbol": sym, "qty": round(qty, 6), "bucket": "legacy",
                                     "reason": "legacy_protective_stop", "est_price": i["close"]})

    # --- 买入: ETF 入场信号 ---
    held = set(strat) - {s["symbol"] for s in out["sells"] if s["bucket"] == "strategy"}
    cands = []
    for sym in cfg["etf_universe"]:
        i = ind.get(sym)
        if not i or sym in held or i["sma200"] is None or i["rsi2"] is None:
            continue
        if i["close"] > i["sma200"] and i["rsi2"] < cfg["entry"]["rsi2_max"]:
            cands.append((i["rsi2"], sym))
    cands.sort()

    slots = max(0, min(cfg["sizing"]["max_strategy_positions"] - len(held),
                       cfg["sizing"]["max_new_entries_per_day"]))
    pos_usd = round(pv * cfg["sizing"]["position_pct_of_portfolio"] / 100.0, 2)
    cash = bp - cfg["sizing"]["min_cash_reserve_usd"]
    cash += sum(s["qty"] * s["est_price"] for s in out["sells"])

    funding_sales = 0
    # 弱势排序的可卖存量持仓 (价格/SMA20 比值最低 = 最弱)
    def weakness(sym):
        i = ind.get(sym) or {}
        if i.get("sma20"):
            return i["close"] / i["sma20"]
        return 1.0

    fundable = sorted(
        [s for s in legacy
         if s not in exiting and s not in cfg["etf_universe"]
         and ind.get(s) and not (cfg["legacy"]["avoid_selling_same_day_buys"] and bought_today(s))],
        key=weakness)

    for _, sym in cands[:slots]:
        need = pos_usd
        while (cash < need and cfg["legacy"]["funding_sales_allowed"]
               and funding_sales < cfg["legacy"]["max_funding_sales_per_day"] and fundable):
            lsym = fundable.pop(0)
            lqty = broker_qty(lsym, float(legacy[lsym]["qty"]))
            if lqty <= 0:
                continue
            lpx = ind[lsym]["close"]
            out["sells"].append({"symbol": lsym, "qty": round(lqty, 6), "bucket": "legacy",
                                 "reason": "funding_rotation", "est_price": lpx})
            exiting.add(lsym)
            cash += lqty * lpx
            funding_sales += 1
        amt = round(min(need, cash), 2)
        if amt >= cfg["sizing"]["min_order_usd"]:
            out["buys"].append({"symbol": sym, "dollar_amount": amt,
                                "reason": "rsi2_entry", "est_price": ind[sym]["close"],
                                "rsi2": round(ind[sym]["rsi2"], 2)})
            cash -= amt
        else:
            warnings.append(f"{sym}: 有入场信号但可用资金不足 (${cash:.2f}), 跳过")

    _emit(out, a.out)


def _emit(out, path):
    s = json.dumps(out, indent=2, ensure_ascii=False)
    if path:
        with open(path, "w") as f:
            f.write(s + "\n")
    print(s)


# ---------- apply ----------

def cmd_apply(a):
    state = load_json(a.state)
    fills = load_json(a.fills)
    today = a.date
    for f in fills["fills"]:
        sym, side = f["symbol"], f["side"]
        qty, price = float(f["qty"]), float(f["price"])
        bucket = f.get("bucket", "strategy")
        if side == "buy":
            state.setdefault("strategy_positions", {})[sym] = {
                "qty": qty, "entry_price": price, "entry_date": today,
                "cost": round(qty * price, 2),
            }
        else:
            book = state.get("strategy_positions" if bucket == "strategy" else "legacy_positions", {})
            if sym in book:
                remaining = float(book[sym]["qty"]) - qty
                if remaining <= 1e-6:
                    del book[sym]
                else:
                    book[sym]["qty"] = round(remaining, 6)
        state.setdefault("trades", []).append({
            "date": today, "symbol": sym, "side": side, "qty": qty,
            "price": price, "bucket": bucket, "reason": f.get("reason", ""),
        })
    if a.portfolio_value:
        pv = float(a.portfolio_value)
        state["high_water_mark"] = max(float(state.get("high_water_mark", pv)), pv)
    save_json(a.state, state)
    print(f"state updated: {a.state} ({len(fills['fills'])} fills)")


# ---------- main ----------

def main():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("signal", help="计算今天的订单")
    s.add_argument("--config", required=True)
    s.add_argument("--state", required=True)
    s.add_argument("--historicals", nargs="+", required=True,
                   help="get_equity_historicals 原始输出文件(可多个)")
    s.add_argument("--quotes", required=True,
                   help='实时报价: {"SYM": price} 或 get_equity_quotes 原始输出')
    s.add_argument("--positions", help="券商持仓文件(简单映射或原始输出), 用于校准可卖数量/当日买入")
    s.add_argument("--date", required=True, help="今天日期 YYYY-MM-DD")
    s.add_argument("--portfolio-value", required=True)
    s.add_argument("--buying-power", required=True)
    s.add_argument("--out", help="订单 JSON 输出路径")
    s.set_defaults(func=cmd_signal)

    ap = sub.add_parser("apply", help="把成交回写进 state")
    ap.add_argument("--state", required=True)
    ap.add_argument("--fills", required=True,
                    help='{"fills": [{symbol, side, qty, price, bucket, reason}]}')
    ap.add_argument("--date", required=True)
    ap.add_argument("--portfolio-value")
    ap.set_defaults(func=cmd_apply)

    a = p.parse_args()
    a.func(a)


if __name__ == "__main__":
    main()
