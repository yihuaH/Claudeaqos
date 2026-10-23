#!/usr/bin/env python3
"""SQQQ 短期看跌 QQQ 引擎 (**仅 paper**, 红线7) — 信号源是 QQQ, 不是 SQQQ 自己。

规则 (参数全在 strategy/sqqq_short.json, 全部确定性, 纯 stdlib, 不联网):
  入场  QQQ **当日**涨幅 ≥ entry.single_day_gain_pct (默认 +2.5%), 无趋势过滤
  出场  持满 exit.max_holding_trading_days (= **1**) 个交易日 → **无条件**平仓, 无止损无止盈
  重叠  持仓期间的新信号**跳过** (忠于回测的 `if i <= last: continue`); 同日既平又触发 → 照常平, 不新买
  熔断  净值距 HWM 回撤 ≥ circuit_breaker.max_drawdown_pct_from_hwm → 该账本自行 halted, 不影响其他轨道

⚠️ **隔夜不是日内**: 收盘买 → 次日收盘卖, 过夜持仓, 不构成 PDT day trade。
⚠️ 回测口径是**收盘**涨幅, 而主跑窗在 ~15:20 用盘中价 —— 滑移已知且无法消除, 见 config `_entry_note`。
⚠️ 本引擎只出信号, 执行层照单下单不得修改 (红线2)。成交回写复用 `signals.py apply` (账本同构)。

依据: strategy/proposals/sqqq-short-qqq.py (2026-10-23)。
⚠️ 该研究的结论是「不建议做」(80~83% 收益来自 2020 一年, 剔 2020+2022 后 9 年只剩 +12.5%);
   用户知情下选择先建 paper 轨道验证工程正确性与摩擦。

用法:
  sqqq_short.py signal --config strategy/sqqq_short.json --state state/sqqq_short_positions.json \
      --bars BARS.json --quotes QUOTES.json --date YYYY-MM-DD \
      --portfolio-value PV --buying-power BP [--out ORDERS.json]
"""
import argparse, json, os, sys


def load(p, d=None):
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return d


def bars_series(paths):
    """{sym: ([dates asc], [closes asc])}; 接受 integrations.py bars 的原始输出或多个文件。"""
    out = {}
    for p in paths:
        raw = load(p, {}) or {}
        res = raw.get("data", {}).get("results", raw if isinstance(raw, list) else [])
        for x in res or []:
            b = x.get("bars") or []
            if not b:
                continue
            out[x["symbol"]] = ([q["begins_at"][:10] for q in b],
                                [float(q["close_price"]) for q in b])
    return out


def floor6(x):
    return int(float(x) * 1e6) / 1e6


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("signal")
    s.add_argument("--config", required=True)
    s.add_argument("--state", required=True)
    s.add_argument("--bars", required=True, nargs="+")
    s.add_argument("--quotes", required=True)
    s.add_argument("--date", required=True)
    s.add_argument("--portfolio-value", required=True, type=float)
    s.add_argument("--buying-power", required=True, type=float)
    s.add_argument("--out")
    a = ap.parse_args()

    cfg = load(a.config)
    if cfg is None:
        print(json.dumps({"error": f"配置读不到: {a.config}"}, ensure_ascii=False)); return 1
    if cfg.get("environment") != "paper_only":
        # 红线7 的硬闸: 本引擎只服务 paper 轨道, 配置被改成别的就拒跑。
        print(json.dumps({"error": "environment 必须是 paper_only (红线7)"},
                         ensure_ascii=False)); return 1

    src, inst = cfg.get("signal_source", "QQQ"), cfg.get("instrument", "SQQQ")
    st = load(a.state, {}) or {}
    held = (st.get("strategy_positions") or {})
    out = {"date": a.date, "track": "sqqq_short", "environment": "paper_only",
           "signal_source": src, "instrument": inst,
           "sells": [], "buys": [], "skips": [], "warnings": []}

    if not cfg.get("enabled"):
        out["skips"].append({"reason": "track_disabled"})
        _emit(out, a.out); return 0
    if st.get("halted"):
        out["skips"].append({"reason": "halted", "_why": "熔断已触发, 只读 (红线4)"})
        _emit(out, a.out); return 0

    ser = bars_series(a.bars)
    quotes = load(a.quotes, {}) or {}
    for need in (src, inst):
        if need not in ser:
            out["warnings"].append(f"{need}: 日线缺失 → 本轨道今日不动作")
            out["skips"].append({"reason": f"no_bars_{need}"})
            _emit(out, a.out); return 0
        if quotes.get(need) is None:
            out["warnings"].append(f"{need}: 实时报价缺失 → 本轨道今日不动作")
            out["skips"].append({"reason": f"no_quote_{need}"})
            _emit(out, a.out); return 0

    sdts, sclo = ser[src]
    # 排除当天日线 (与 signals.py 同纪律): 当日价只从 quotes 来
    prev = [(d, c) for d, c in zip(sdts, sclo) if d < a.date]
    if not prev:
        out["skips"].append({"reason": "no_prior_bar"}); _emit(out, a.out); return 0
    prev_close = prev[-1][1]
    px_src = float(quotes[src])
    day_ret = (px_src / prev_close - 1) * 100.0
    px_inst = float(quotes[inst])
    out["signal_ctx"] = {"prev_close": round(prev_close, 4), "price": round(px_src, 4),
                         "prev_close_date": prev[-1][0],
                         "day_return_pct": round(day_ret, 3),
                         "_note": "当日价取 quotes (券商实时), 前收取日线最后一根 < date 的收盘"}

    # ---------- 出场 (优先): 持满 N 个交易日 → 无条件平 ----------
    ex = cfg["exit"]
    maxhold = int(ex["max_holding_trading_days"])
    # 已过交易日数 = 「日线里 ≤ 今天的日期」∪「今天」 中晚于入场日的个数。
    # ⚠️ **必须把今天并进去**: `integrations.py bars` 的末根可能是当天 (实测 10-23 有),
    #    也可能被上游排除 (signals.py 的纪律是排除当天日线), 两种都得对。
    #    若只数 bars 里晚于入场日的日期, 在"bars 不含当天"的情形下出场日会算出 hd=0
    #    → **本轨道 maxhold=1, 这个 off-by-one 会让它永不按期出场** (2026-10-23 测试③ 实测踩到)。
    #    RSI-2 轨道 maxhold=10 时同一偏差只是 10% 误差, 所以之前没暴露。
    tdays = sorted({d for d in ser[inst][0] if d <= a.date} | {a.date})
    sold_today = False
    for sym, pos in sorted(held.items()):
        entry_date = pos.get("entry_date")
        hd = sum(1 for d in tdays if d > entry_date) if entry_date else 0
        base = {"symbol": sym, "qty": floor6(pos.get("qty", 0)), "bucket": "strategy",
                "held_trading_days": hd, "entry_price": pos.get("entry_price"),
                "price": px_inst if sym == inst else quotes.get(sym)}
        if hd >= maxhold:
            out["sells"].append({**base, "reason": "time_exit_1d",
                                 "_why": f"持满 {hd}/{maxhold} 个交易日 → 无条件平仓 "
                                         "(SQQQ 每多持一天付约 0.25% 结构性漂移)"})
            sold_today = True
        else:
            out["skips"].append({**base, "reason": "hold",
                                 "_why": f"仅持 {hd}/{maxhold} 个交易日"})

    # ---------- 入场 ----------
    thr = float(cfg["entry"]["single_day_gain_pct"])
    ov = cfg.get("overlap", {})
    if day_ret < thr:
        out["skips"].append({"stage": "entry", "reason": "no_signal",
                             "_why": f"{src} 当日 {day_ret:+.3f}% < +{thr}%"})
    elif held and not ov.get("allow_entry_while_holding"):
        out["skips"].append({"stage": "entry", "reason": "already_holding",
                             "_why": "持仓期间的新信号按回测口径**跳过** "
                                     "(顺序复利的 `if i <= last: continue`)"})
    elif sold_today and not ov.get("allow_same_day_exit_and_entry"):
        out["skips"].append({"stage": "entry", "reason": "same_day_exit_skip_entry",
                             "_why": "同日已平仓 → 不再新买 (忠于回测的不重叠口径, "
                                     "且避免卖出再买回的空转)"})
    else:
        sz = cfg["sizing"]
        pv = float(a.portfolio_value)
        want = round(pv * float(sz["position_pct_of_portfolio"]) / 100.0, 2)
        avail = float(a.buying_power)
        if want > avail:
            out["skips"].append({"stage": "entry", "symbol": inst,
                                 "reason": f"insufficient_buying_power(want={want:.2f},avail={avail:.2f})",
                                 "_why": "超可用资金 → 整单跳过不缩量 (与实盘同纪律)"})
        else:
            out["buys"].append({
                "symbol": inst, "dollar_amount": want, "bucket": "strategy",
                "reason": "qqq_up_short", "est_price": round(px_inst, 4),
                "trigger_day_return_pct": round(day_ret, 3),
                "trigger_threshold_pct": thr,
                "planned_exit": f"次一交易日收盘前窗口无条件平仓 (持满 {maxhold} 个交易日)",
                "_overnight_note": "隔夜持仓, **不构成 PDT day trade**",
            })

    _emit(out, a.out)
    print(json.dumps({"date": a.date, "day_return_pct": round(day_ret, 3),
                      "threshold": thr, "fired": bool(out["buys"]),
                      "sells": [s["symbol"] for s in out["sells"]],
                      "buys": [b["symbol"] for b in out["buys"]],
                      "halted": bool(st.get("halted"))}, ensure_ascii=False))
    return 0


def _emit(o, p):
    if p:
        with open(p, "w", encoding="utf-8") as f:
            json.dump(o, f, ensure_ascii=False, indent=1)


if __name__ == "__main__":
    sys.exit(main())
