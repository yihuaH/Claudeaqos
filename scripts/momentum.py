#!/usr/bin/env python3
"""
周度动量轮动引擎 (仅 paper) — 3/6/12月混合动量, 每周调仓, 绝对动量过滤。

  signal  由 日线历史 + 实时报价 + 账本 + 配置 计算今天的订单

规则 (参数在 strategy/momentum.json, 全部确定性):
- 评分: score = mean( close[t-skip] / close[t-skip-L] - 1, L ∈ lookback_days )。
- 调仓日 (每周 rebalance.weekday; 账本从未调仓过时立即调仓; 节假日错过则由
  max_days_between 兜底补调): 目标组合 = 排名前 top_n 且 score > abs_momentum_min;
  已持仓且排名仍在 keep_rank 内的优先保留 (滞后带, 降低换手)。
  不在目标内的持仓全卖 (momentum_rotation), 新入选的按 position_pct 买入。
  合格标的不足 top_n 时缺口留现金 — 绝对动量过滤自带风险开关, 故不设 VIX 闸门。
- 非调仓日: 只检查硬止损 (相对入场价 -hard_stop_pct), 不开新仓。
- 熔断: 净值距 HWM 回撤 ≥ 上限 → 输出熔断标记, 该账本自行 halted, 不影响其他轨道。

成交回写: 复用 signals.py apply (账本与其他 paper 轨道同构)。
执行层照单下单, 不得修改。纯 stdlib, 不联网。
"""
import argparse
import json
import sys
from datetime import date as _date

sys.path.insert(0, __file__.rsplit("/", 1)[0])
from signals import load_json, parse_historicals, parse_quotes  # noqa: E402


def momentum_score(closes, lookbacks, skip):
    """混合动量: 各回看期收益率的均值; 历史不足返回 None。"""
    end = len(closes) - 1 - int(skip)
    if end < 0:
        return None
    rets = []
    for lb in lookbacks:
        j = end - int(lb)
        if j < 0 or closes[j] <= 0:
            return None
        rets.append(closes[end] / closes[j] - 1.0)
    return sum(rets) / len(rets)


def cmd_signal(a):
    cfg = load_json(a.config)
    state = load_json(a.state)
    hist = parse_historicals(a.bars)
    quotes = parse_quotes(a.quotes)
    today = a.date
    pv, bp = float(a.portfolio_value), float(a.buying_power)
    warnings = []

    out = {"date": today, "track": "momentum", "sells": [], "buys": [],
           "warnings": warnings}
    if not cfg.get("enabled"):
        out["note"] = "momentum disabled"
        _emit(out, a.out)
        return

    hwm = max(float(state.get("high_water_mark", pv)), pv)
    drawdown_pct = (hwm - pv) / hwm * 100.0
    cb_limit = cfg["risk"]["circuit_breaker"]["max_drawdown_pct_from_hwm"]
    out.update({"portfolio_value": pv, "buying_power": bp, "high_water_mark": hwm,
                "drawdown_pct": round(drawdown_pct, 2),
                "halted": bool(state.get("halted")),
                "circuit_breaker_triggered": drawdown_pct >= cb_limit})
    if out["halted"] or out["circuit_breaker_triggered"]:
        out["note"] = ("熔断触发: 回撤 %.2f%% ≥ %.1f%%, 账本待人工处理"
                       % (drawdown_pct, cb_limit)) if out["circuit_breaker_triggered"] \
            else "halted, 跳过交易"
        _emit(out, a.out)
        return

    # 收盘序列 (历史 + 今日实时价), 与 signals.py 同法
    series = {}
    for sym in cfg["universe"]:
        closes = [c for d, c in hist.get(sym, []) if d < today]
        if sym in quotes:
            closes.append(quotes[sym])
        elif closes:
            warnings.append(f"{sym}: 无实时报价, 使用最后一根历史K线")
        series[sym] = closes

    sig = cfg["signal"]
    scores = {}
    for sym, closes in series.items():
        sc = momentum_score(closes, sig["lookback_days"], sig["skip_recent_days"])
        if sc is None:
            warnings.append(f"{sym}: 历史数据不足, 不参与排名")
        else:
            scores[sym] = sc
    ranked = sorted(scores, key=lambda s: -scores[s])
    rank = {sym: i + 1 for i, sym in enumerate(ranked)}
    out["scores"] = [{"symbol": s, "score": round(scores[s], 4), "rank": rank[s]}
                     for s in ranked]

    held = state.get("strategy_positions", {})

    def px(sym):
        cs = series.get(sym)
        return cs[-1] if cs else None

    last = state.get("last_rebalance")
    wd = _date.fromisoformat(today).weekday()
    rebalance = (last is None
                 or wd == int(cfg["rebalance"]["weekday"])
                 or (_date.fromisoformat(today) - _date.fromisoformat(last)).days
                 >= int(cfg["rebalance"]["max_days_between"]))
    out["rebalance_day"] = rebalance

    siz = cfg["sizing"]
    abs_min = float(sig["abs_momentum_min"])

    # --- 硬止损 (任何交易日都检查) ---
    stopped = set()
    for sym, pos in sorted(held.items()):
        p = px(sym)
        if p is None:
            warnings.append(f"{sym}: 持仓无行情数据, 人工留意")
            continue
        if p <= float(pos["entry_price"]) * (1 - cfg["risk"]["hard_stop_pct"] / 100.0):
            stopped.add(sym)
            out["sells"].append({"symbol": sym, "qty": round(float(pos["qty"]), 6),
                                 "bucket": "strategy", "reason": "hard_stop",
                                 "est_price": p})

    if not rebalance:
        if not out["sells"]:
            out["note"] = "非调仓日, 无硬止损触发, 无订单"
        _emit(out, a.out)
        return

    # --- 目标组合: 滞后带保留 + 排名补足 ---
    eligible = [s for s in ranked if scores[s] > abs_min]
    keep = sorted([s for s in held
                   if s not in stopped and rank.get(s, 10 ** 6) <= int(siz["keep_rank"])
                   and scores.get(s, abs_min) > abs_min],
                  key=lambda s: rank[s])
    target = list(keep)
    for s in eligible:
        if len(target) >= int(siz["top_n"]):
            break
        if s not in target and s not in stopped:
            target.append(s)
    out["target"] = target
    if len(target) < int(siz["top_n"]):
        warnings.append(f"合格标的不足: 目标持仓 {len(target)}/{siz['top_n']}, 其余留现金")

    # --- 轮动卖出 ---
    for sym, pos in sorted(held.items()):
        if sym in stopped or sym in target:
            continue
        p = px(sym)
        if p is None:
            continue
        out["sells"].append({"symbol": sym, "qty": round(float(pos["qty"]), 6),
                             "bucket": "strategy", "reason": "momentum_rotation",
                             "est_price": p})

    # --- 买入 ---
    pos_usd = round(pv * siz["position_pct_of_portfolio"] / 100.0, 2)
    cash = bp - siz["min_cash_reserve_usd"]
    cash += sum(s["qty"] * s["est_price"] for s in out["sells"])
    for sym in target:
        if sym in held and sym not in stopped:
            continue
        amt = round(min(pos_usd, cash), 2)
        if amt >= siz["min_order_usd"]:
            out["buys"].append({"symbol": sym, "dollar_amount": amt,
                                "reason": "momentum_entry",
                                "score": round(scores[sym], 4), "est_price": px(sym)})
            cash -= amt
        else:
            warnings.append(f"{sym}: 入选但可用资金不足 (${cash:.2f}), 跳过")

    # 调仓日标记持久化 (与订单执行结果无关; 执行失败下周照常再调)
    state["last_rebalance"] = today
    with open(a.state, "w") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)
        f.write("\n")

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
    s.add_argument("--config", required=True, help="strategy/momentum.json")
    s.add_argument("--state", required=True, help="state/momentum_positions.json")
    s.add_argument("--bars", nargs="+", required=True,
                   help="日线历史 (integrations.py bars 产出, 需 ≥ 约260 交易日)")
    s.add_argument("--quotes", required=True, help='实时报价 {"SYM": price}')
    s.add_argument("--date", required=True)
    s.add_argument("--portfolio-value", required=True)
    s.add_argument("--buying-power", required=True)
    s.add_argument("--out")
    s.set_defaults(func=cmd_signal)
    a = p.parse_args()
    a.func(a)


if __name__ == "__main__":
    main()
