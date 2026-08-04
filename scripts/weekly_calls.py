#!/usr/bin/env python3
"""
周 call 摩擦实测引擎 (RSI-2 × 深ITM 买入 call) — 仅用于 Alpaca paper 账户。

  signal  由 历史K线 + 报价 + 期权链 + 账本 计算今天的期权订单 (paper.py run 输入格式)
  apply   把成交回写账本; --context 传当日 signal 输出以附带 入场快照/出场快照/skip 记录
  report  账本盯市 + 摩擦统计 (点差/成交vs前收mid/模型偏差) + 验证进度 vs go_bar

策略形态 (2026-08 回测定型, 见 journal/2026-08-04-weekly-calls.md):
- 入场信号与实盘 RSI-2 同形: close>SMA200 且 RSI2<10, 按 RSI2 升序取新仓。
- 合约: 行权价 ≤ moneyness×现价 的最高档 (深ITM delta≈0.8), 到期 8-17 日历日最近档;
  点差 gate ≤ max_spread_pct — 超限跳过并记 skip_log (跳过率是 go/no-go 输入)。
- 出场跟正股同形规则 (强势反弹/正股止损/时间止损), **无期权级止损**; DTE≤1 强制平仓防行权。

本轨道唯一目的是实测真实摩擦 vs 回测模型; 纸面盈亏绝不驱动实盘订单 (红线7)。
设计原则: 纯 stdlib、确定性、绝不联网。数据由会话按 playbook §7C 搬运。
"""
import argparse
import json
import math
import statistics
import sys
from datetime import date as _date

import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from signals import (load_json, save_json, parse_historicals, parse_quotes,  # noqa: E402
                     sma, rsi, trading_days_since)
from options_overlay import parse_occ, dte  # noqa: E402

BUCKET = "weekly_calls"


# ---------- pricing helpers (仅用于记录模型偏差, 不参与下单决策) ----------

def _ncdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs_call(S, K, T, sig, r=0.04):
    if T <= 1e-9 or sig <= 1e-9:
        return max(S - K, 0.0)
    d1 = (math.log(S / K) + (r + 0.5 * sig * sig) * T) / (sig * math.sqrt(T))
    d2 = d1 - sig * math.sqrt(T)
    return S * _ncdf(d1) - K * math.exp(-r * T) * _ncdf(d2)


def realized_vol(closes, window):
    if len(closes) < window + 1:
        return None
    rets = [math.log(closes[i] / closes[i - 1]) for i in range(-window, 0)]
    return statistics.pstdev(rets) * math.sqrt(252)


def _mid(q):
    b, a = q.get("bid"), q.get("ask")
    if b is None or a is None or b <= 0 or a <= 0 or a < b:
        return None
    return (b + a) / 2.0


def _spread_pct(q):
    m = _mid(q)
    if not m:
        return None
    return (q["ask"] - q["bid"]) / m * 100.0


# ---------- signal ----------

def _pick_contract(sym, spot, chain, cc, today):
    """确定性合约选择: 到期升序 → 同到期行权价降序, 取首个
    [floor_m..moneyness]×spot 区间内 spread/bid/premium 全合规的 call。
    返回 (order_dict, skip_reason)。"""
    target_k = cc["moneyness"] * spot
    floor_k = cc["floor_moneyness"] * spot
    cands = []
    for occ, q in chain.items():
        meta = parse_occ(occ)
        if not meta or meta["type"] != "C":
            continue
        d = dte(meta["expiry"], today)
        if not (cc["min_dte_calendar"] <= d <= cc["max_dte_calendar"]):
            continue
        if not (floor_k <= meta["strike"] <= target_k):
            continue
        cands.append((d, -meta["strike"], occ, meta, q))
    if not cands:
        return None, "no_contract_in_window"
    cands.sort()
    best_spread = None
    for d, _negk, occ, meta, q in cands:
        m = _mid(q)
        if m is None or q["bid"] < cc["min_bid"]:
            continue
        sp = _spread_pct(q)
        best_spread = sp if best_spread is None else min(best_spread, sp)
        if sp > cc["max_spread_pct"]:
            continue
        if m * 100.0 > cc["max_premium_per_contract_usd"]:
            return None, f"premium_too_large({m * 100:.0f})"
        return {"occ": occ, "meta": meta, "quote": q, "mid": m,
                "spread_pct": round(sp, 3), "dte": d}, None
    if best_spread is not None:
        return None, f"spread_too_wide(best={best_spread:.2f}%)"
    return None, "no_valid_quote"


def cmd_signal(a):
    cfg = load_json(a.config)
    ledger = load_json(a.ledger)
    hist = parse_historicals(a.bars)
    quotes = parse_quotes(a.quotes)
    chains = load_json(a.chains) if a.chains else {}
    earnings = load_json(a.earnings) if a.earnings else None
    today = a.date
    warnings, skips = [], []
    out = {"date": today, "halted": False, "sells": [], "buys": [],
           "skips": skips, "warnings": warnings}

    if not cfg.get("enabled"):
        out["note"] = "weekly_calls.json enabled=false, 空跑"
        return _emit(out, a.out)
    if ledger.get("halted"):
        out["halted"] = True
        out["note"] = "账本 halted=true, 只读"
        return _emit(out, a.out)

    # 与 signals.py 同法: 实时报价补今天的临时收盘
    series = {}
    for sym, bars in hist.items():
        dates = [d for d, _ in bars if d < today]
        closes = [c for d, c in bars if d < today]
        if sym in quotes:
            dates.append(today)
            closes.append(quotes[sym])
        series[sym] = (dates, closes)

    ind = {}
    for sym, (dates, closes) in series.items():
        ind[sym] = {"close": closes[-1] if closes else None,
                    "sma5": sma(closes, 5), "sma200": sma(closes, 200),
                    "rsi2": rsi(closes, 2),
                    "rv": realized_vol(closes, int(cfg["model"]["rv_window_days"]))}

    ex = cfg["exit"]
    cc = cfg["contract"]
    positions = ledger.get("positions", {})

    # --- 出场 (跟正股同形规则, 无期权级止损) ---
    exiting_und = set()
    for occ, pos in sorted(positions.items()):
        u = pos["underlying"]
        i = ind.get(u)
        reason = None
        d = dte(pos["expiry"], today)
        if d < 0:
            warnings.append(f"{occ}: 已过期未平仓 (expiry {pos['expiry']}), "
                            "疑似被自动行权/交割 — 停该仓操作, 人工对账 (红线6)")
            continue
        if d <= int(ex["force_exit_dte_lte"]):
            reason = "expiry_close"
        elif i and i["rsi2"] is not None and i["close"] is not None:
            if i["close"] <= float(pos["entry_underlying"]) * (1 - ex["underlying_stop_loss_pct"] / 100.0):
                reason = "underlying_stop"
            elif (i["sma5"] is not None and i["close"] > i["sma5"]) or i["rsi2"] >= ex["rsi2_min"]:
                reason = "exit_strength"
            elif trading_days_since(series[u][0], pos["entry_date"]) >= int(ex["max_holding_days"]):
                reason = "time_stop"
        else:
            warnings.append(f"{occ}: 底层 {u} 指标数据不足, 跳过出场检查 (DTE={d})")
        if not reason:
            continue
        q = (chains.get(u) or {}).get(occ) or {}
        m = _mid(q)
        if m is None:  # 无链报价 → 内在价值兜底定限价
            spot = (ind.get(u) or {}).get("close")
            m = max((spot or pos["strike"]) - pos["strike"], 0.01)
            warnings.append(f"{occ}: 无链报价, est_price 按内在价值 {m:.2f} 兜底")
        exiting_und.add(u)
        out["sells"].append({
            "symbol": occ, "qty": int(pos["contracts"]),
            "position_intent": "sell_to_close", "bucket": BUCKET,
            "reason": reason, "est_price": round(m, 2),
            "exit_quote": {"bid": q.get("bid"), "ask": q.get("ask"),
                           "mid": round(m, 4) if _mid(q) else None,
                           "spread_pct": round(_spread_pct(q), 3) if _spread_pct(q) else None},
        })

    # --- 轨道级熔断: 累计已实现亏损超限 → 只出不进 ---
    # 阈值支持绝对额 (usd) 或账户百分比 (pct_of_portfolio, 需 --portfolio-value)
    pv = float(a.portfolio_value) if getattr(a, "portfolio_value", None) is not None else None
    cum_real = sum(rt.get("pnl_usd", 0.0) for rt in ledger.get("round_trips", []))
    cbc = cfg["circuit_breaker"]
    cb = cbc.get("max_cumulative_realized_loss_usd")
    if cb is None and cbc.get("max_cumulative_realized_loss_pct_of_portfolio") is not None:
        if pv is None:
            raise SystemExit("熔断配置为百分比但未传 --portfolio-value")
        cb = pv * float(cbc["max_cumulative_realized_loss_pct_of_portfolio"]) / 100.0
    cb = float(cb)
    if cum_real <= -cb:
        out["note"] = (f"轨道熔断: 累计已实现 {cum_real:.0f} ≤ -{cb:.0f}, "
                       "停止新入场 (出场照常), 等用户决定")
        return _emit(out, a.out)

    # --- 入场 (RSI-2 同形) ---
    def days_to_earnings(sym):
        if not earnings or earnings.get(sym) is None:
            return None
        try:
            return (_date.fromisoformat(earnings[sym]) - _date.fromisoformat(today)).days
        except ValueError:
            return None

    # 实盘硬顶 (红线3, 上限不是建议): budget = 未平仓权利金总额封顶, 支持绝对额 (usd) 或
    # 账户百分比 (pct_of_portfolio × --portfolio-value, 2026-08-04 用户定 40%, 随净值自动伸缩);
    # --buying-power = 实时购买力封顶。paper 配置无 budget 段则不启用。
    bud = cfg.get("budget") or {}
    bud_cap = bud.get("max_open_premium_usd")
    if bud_cap is None and bud.get("max_open_premium_pct_of_portfolio") is not None:
        if pv is None:
            raise SystemExit("budget 配置为百分比但未传 --portfolio-value")
        bud_cap = pv * float(bud["max_open_premium_pct_of_portfolio"]) / 100.0
    open_prem = sum(float(p["entry_premium"]) * 100 * int(p["contracts"])
                    for p in positions.values())
    bp_left = float(a.buying_power) if getattr(a, "buying_power", None) is not None else None
    spent = 0.0

    held_und = {p["underlying"] for p in positions.values()} - exiting_und
    cands = []
    for sym in cfg["universe"]:
        i = ind.get(sym)
        if not i or sym in held_und or i["sma200"] is None or i["rsi2"] is None:
            continue
        if not (i["close"] > i["sma200"] and i["rsi2"] < cfg["entry"]["rsi2_max"]):
            continue
        ed = days_to_earnings(sym)
        if ed is not None and 0 <= ed <= int(cfg["defense"]["earnings_blackout_days"]):
            skips.append({"symbol": sym, "reason": f"earnings_blackout(dte={ed})",
                          "rsi2": round(i["rsi2"], 2)})
            continue
        cands.append((i["rsi2"], sym))
    cands.sort()

    slots = max(0, min(int(cfg["sizing"]["max_open_positions"]) - len(held_und) - len(out["buys"]),
                       int(cfg["sizing"]["max_new_entries_per_day"])))
    for rsi2v, sym in cands:
        if len(out["buys"]) >= slots:
            break
        spot = ind[sym]["close"]
        pick, skip = _pick_contract(sym, spot, chains.get(sym) or {}, cc, today)
        if pick is None:
            skips.append({"symbol": sym, "reason": skip or "no_chain",
                          "rsi2": round(rsi2v, 2), "spot": round(spot, 2)})
            continue
        qty = int(cfg["sizing"]["contracts_per_position"])
        cost = pick["mid"] * 100.0 * qty
        if bud_cap is not None and open_prem + spent + cost > float(bud_cap):
            skips.append({"symbol": sym, "reason": f"budget_exceeded(cost={cost:.0f},"
                          f"cap={float(bud_cap):.0f},open={open_prem + spent:.0f})",
                          "rsi2": round(rsi2v, 2)})
            continue
        if bp_left is not None and spent + cost > bp_left:
            skips.append({"symbol": sym, "reason": f"insufficient_buying_power"
                          f"(cost={cost:.0f},bp_left={bp_left - spent:.0f})",
                          "rsi2": round(rsi2v, 2)})
            continue
        spent += cost
        rv = ind[sym]["rv"]
        iv = max(0.15, min(1.5, (rv or 0.30) * float(cfg["model"]["iv_rv_mult"])))
        model = bs_call(spot, pick["meta"]["strike"], pick["dte"] / 365.0, iv,
                        float(cfg["model"]["risk_free"]))
        out["buys"].append({
            "symbol": pick["occ"], "qty": qty,
            "position_intent": "buy_to_open", "bucket": BUCKET,
            "reason": "rsi2_entry_call", "est_price": round(pick["mid"], 2),
            "underlying": sym, "strike": pick["meta"]["strike"],
            "expiry": pick["meta"]["expiry"], "dte": pick["dte"],
            "spot": round(spot, 4), "rsi2": round(rsi2v, 2),
            "model_price": round(model, 4),
            "entry_quote": {"bid": pick["quote"]["bid"], "ask": pick["quote"]["ask"],
                            "mid": round(pick["mid"], 4),
                            "spread_pct": pick["spread_pct"]},
        })
    _emit(out, a.out)


def _emit(out, path):
    s = json.dumps(out, indent=2, ensure_ascii=False)
    if path:
        with open(path, "w") as f:
            f.write(s + "\n")
    print(s)


# ---------- apply ----------

def cmd_apply(a):
    ledger = load_json(a.ledger)
    fills = load_json(a.fills)
    ctx = load_json(a.context) if a.context else {}
    ctx_buys = {o["symbol"]: o for o in ctx.get("buys", [])}
    ctx_sells = {o["symbol"]: o for o in ctx.get("sells", [])}
    positions = ledger.setdefault("positions", {})
    today = a.date

    for f in fills["fills"]:
        occ, side = f["symbol"], f["side"]
        qty, price = int(float(f["qty"])), float(f["price"])
        meta = parse_occ(occ)
        if not meta:
            raise SystemExit(f"非期权成交混入 weekly_calls apply: {occ}")
        if side == "buy":
            c = ctx_buys.get(occ, {})
            positions[occ] = {
                "underlying": meta["underlying"], "strike": meta["strike"],
                "expiry": meta["expiry"], "contracts": qty,
                "entry_premium": price, "entry_date": today,
                "entry_underlying": c.get("spot"), "entry_rsi2": c.get("rsi2"),
                "entry_quote": c.get("entry_quote"), "model_price": c.get("model_price"),
            }
        else:
            pos = positions.pop(occ, None)
            if pos is None:
                raise SystemExit(f"卖出无持仓的合约: {occ} (人工对账)")
            c = ctx_sells.get(occ, {})
            ep = float(pos["entry_premium"])
            eq = pos.get("entry_quote") or {}
            xq = c.get("exit_quote") or {}
            ledger.setdefault("round_trips", []).append({
                "occ": occ, "underlying": meta["underlying"],
                "entry_date": pos["entry_date"], "exit_date": today,
                "entry_premium": ep, "exit_premium": price,
                "pnl_pct": round((price / ep - 1) * 100.0, 2) if ep else None,
                "pnl_usd": round((price - ep) * 100.0 * qty, 2),
                "reason": f.get("reason", c.get("reason", "")),
                "entry_spread_pct": eq.get("spread_pct"),
                "exit_spread_pct": xq.get("spread_pct"),
                "entry_mid": eq.get("mid"), "exit_mid": xq.get("mid"),
                "model_price": pos.get("model_price"),
            })
        ledger.setdefault("trades", []).append({
            "date": today, "symbol": occ, "side": side, "qty": qty,
            "price": price, "bucket": BUCKET, "reason": f.get("reason", ""),
        })

    # skip_log: 摩擦实测的一部分 (被 spread gate 拦掉多少机会)。
    # 幂等: 排队日与次日回收会用同一 context 各 apply 一次, 按 (date,symbol,reason) 去重。
    slog = ledger.setdefault("skip_log", [])
    seen = {(s.get("date"), s.get("symbol"), s.get("reason")) for s in slog}
    for s in ctx.get("skips", []):
        rec = {"date": ctx.get("date", today), **s}
        key = (rec.get("date"), rec.get("symbol"), rec.get("reason"))
        if key not in seen:
            slog.append(rec)
            seen.add(key)

    save_json(a.ledger, ledger)
    print(f"weekly_calls 账本已更新: {len(fills['fills'])} 笔成交, "
          f"{len(ctx.get('skips', []))} 条 skip")


# ---------- report ----------

def _median(xs):
    xs = [x for x in xs if x is not None]
    return round(statistics.median(xs), 3) if xs else None


def cmd_report(a):
    cfg = load_json(a.config)
    ledger = load_json(a.ledger)
    chains = load_json(a.chains) if a.chains else {}
    today = a.date
    rts = ledger.get("round_trips", [])
    positions = ledger.get("positions", {})

    open_mtm = []
    for occ, pos in sorted(positions.items()):
        q = (chains.get(pos["underlying"]) or {}).get(occ) or {}
        mark = q.get("bid")  # 保守: 按 bid 盯市
        upnl = round((mark - pos["entry_premium"]) * 100 * pos["contracts"], 2) if mark else None
        open_mtm.append({"occ": occ, "entry_premium": pos["entry_premium"],
                         "entry_date": pos["entry_date"], "mark_bid": mark,
                         "unrealized_usd": upnl, "dte": dte(pos["expiry"], today)})

    pnls = [rt["pnl_pct"] for rt in rts if rt.get("pnl_pct") is not None]
    legs = ([rt.get("entry_spread_pct") for rt in rts]
            + [rt.get("exit_spread_pct") for rt in rts]
            + [(p.get("entry_quote") or {}).get("spread_pct") for p in positions.values()])
    # 成交 vs 信号时 mid: 含隔夜漂移, 只作参考; 模型偏差检验回测 IV 假设
    slip = [round((rt["entry_premium"] / rt["entry_mid"] - 1) * 100, 2)
            for rt in rts if rt.get("entry_mid")]
    mdev = [round((rt["entry_mid"] / rt["model_price"] - 1) * 100, 2)
            for rt in rts if rt.get("model_price") and rt.get("entry_mid")]

    v = cfg["validation"]
    started = v["start_date"]
    days_run = (_date.fromisoformat(today) - _date.fromisoformat(started)).days
    n = len(pnls)
    med_leg = _median(legs)
    med_pnl = _median(pnls)
    mature = n >= int(v["min_round_trips"]) or days_run >= int(v["min_trading_days"]) * 1.5
    verdict = "in_progress"
    if mature and n >= 5:
        ok_spread = med_leg is not None and med_leg <= float(v["go_bar"]["max_median_leg_spread_pct"])
        ok_pnl = med_pnl is not None and med_pnl > float(v["go_bar"]["min_median_trade_pnl_pct"])
        verdict = "GO_candidate" if (ok_spread and ok_pnl) else "NO_GO"

    out = {
        "date": today, "round_trips": n,
        "cum_realized_usd": round(sum(rt.get("pnl_usd", 0) for rt in rts), 2),
        "win_rate_pct": round(100 * sum(1 for x in pnls if x > 0) / n, 1) if n else None,
        "median_pnl_pct": med_pnl, "mean_pnl_pct": _median(pnls) if n < 2 else round(statistics.mean(pnls), 2),
        "median_leg_spread_pct": med_leg,
        "median_fill_vs_prev_mid_pct": _median(slip),
        "median_mid_vs_model_pct": _median(mdev),
        "skips_total": len(ledger.get("skip_log", [])),
        "skips_spread_gate": sum(1 for s in ledger.get("skip_log", [])
                                 if str(s.get("reason", "")).startswith("spread_too_wide")),
        "open_positions": open_mtm,
        "validation": {"started": started, "calendar_days": days_run,
                       "verdict": verdict, "go_bar": v["go_bar"]},
    }
    print(json.dumps(out, indent=2, ensure_ascii=False))


# ---------- main ----------

def main():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("signal")
    s.add_argument("--config", required=True, help="strategy/weekly_calls.json")
    s.add_argument("--ledger", required=True, help="state/weekly_call_positions.json")
    s.add_argument("--bars", nargs="+", required=True, help="integrations.py bars 输出 (universe, ≥450天)")
    s.add_argument("--quotes", required=True, help='{"SYM": price} 或 get_equity_quotes 原始输出')
    s.add_argument("--chains", help="integrations.py chains 输出 (universe + 持仓底层, dte-max 17)")
    s.add_argument("--earnings", help='{"SYM": "YYYY-MM-DD"|null} (复用主跑 earnings.json)')
    s.add_argument("--buying-power", type=float,
                   help="实盘配置用: 实时购买力, 新买入权利金合计不得超过 (paper 可省略)")
    s.add_argument("--portfolio-value", type=float,
                   help="账户净值 (get_portfolio total_value); budget/熔断配置为百分比时必传")
    s.add_argument("--date", required=True)
    s.add_argument("--out", help="订单输出 (paper.py run --orders 输入)")
    s.set_defaults(func=cmd_signal)

    ap = sub.add_parser("apply")
    ap.add_argument("--ledger", required=True)
    ap.add_argument("--fills", required=True)
    ap.add_argument("--context", help="当日 signal 输出文件 (附带入场/出场快照与 skip 记录)")
    ap.add_argument("--date", required=True)
    ap.set_defaults(func=cmd_apply)

    r = sub.add_parser("report")
    r.add_argument("--config", required=True)
    r.add_argument("--ledger", required=True)
    r.add_argument("--chains", help="盯市用链报价 (可省略, 省略则开仓不盯市)")
    r.add_argument("--date", required=True)
    r.set_defaults(func=cmd_report)

    a = p.parse_args()
    a.func(a)


if __name__ == "__main__":
    main()
