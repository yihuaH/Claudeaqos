#!/usr/bin/env python3
"""
Claudeaqos 参数学习器 — 冠军/挑战者 (champion-challenger) 自学习闭环

  search            walk-forward 网格搜索, 产出挑战者参数并初始化 paper 账本
  challenger-config 生成挑战者的合并配置 (供 signals.py 对 paper 账本跑信号)
  record            记录当日 实盘/paper 净值到 equity_history
  evaluate          按 learning.json 标准评估挑战者 (insufficient_data/extend/pass/fail)
  status            打印学习状态摘要
  promote           evaluate 判 pass 后把挑战者参数写入 strategy/config.json
  reject            否决挑战者, 归档原因

边界 (硬约束, 与 CLAUDE.md 红线一致):
- 只能修改 strategy/learning.json learnable_bounds 列出的 entry/exit 参数, 且必须在边界内。
- sizing / circuit_breaker / macro / legacy 等风控参数永不参与学习。
- 晋级必须先通过 Alpaca paper 验证期; promote 内部会重新 evaluate, 非 pass 拒绝执行。
- 纯 stdlib、确定性: 同样输入永远产出同样结果, 绝不联网。
"""
import argparse
import itertools
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from signals import load_json, save_json, parse_historicals  # noqa: E402

LEARNABLE = ["entry.rsi2_max", "exit.rsi2_min", "exit.stop_loss_pct", "exit.max_holding_days"]


def get_path(d, path):
    for k in path.split("."):
        d = d[k]
    return d


def set_path(d, path, v):
    ks = path.split(".")
    for k in ks[:-1]:
        d = d[k]
    d[ks[-1]] = v


def check_bounds(params, bounds):
    for k, v in params.items():
        if k not in bounds:
            raise SystemExit(f"拒绝: 参数 {k} 不在 learnable_bounds 白名单内")
        lo, hi = bounds[k]
        if not (lo <= float(v) <= hi):
            raise SystemExit(f"拒绝: {k}={v} 超出边界 [{lo}, {hi}]")


# ---------- 指标预计算 (与 signals.py 的 sma/rsi 语义一致, 增量算法) ----------

def precompute(hist):
    """hist: {sym: [(date, close)...]} → {sym: {dates, idx, close, rsi2, sma5, sma200}}"""
    out = {}
    for sym, bars in hist.items():
        dates = [d for d, _ in bars]
        close = [c for _, c in bars]
        n = len(close)
        pref = [0.0]
        for c in close:
            pref.append(pref[-1] + c)

        def sma_at(i, w):
            return (pref[i + 1] - pref[i + 1 - w]) / w if i + 1 >= w else None

        rsi2 = [None] * n
        period = 2
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
            rsi2[i] = 100.0 if al == 0 else 100.0 - 100.0 / (1.0 + ag / al)

        out[sym] = {
            "dates": dates,
            "idx": {d: i for i, d in enumerate(dates)},
            "close": close,
            "rsi2": rsi2,
            "sma5": [sma_at(i, 5) for i in range(n)],
            "sma200": [sma_at(i, 200) for i in range(n)],
        }
    return out


# ---------- 回测模拟 (只模拟策略桶, 复刻 signals.py 的入出场规则) ----------

def simulate(pre, universe, window_dates, params, sizing, start_capital=1000.0):
    rsi2_max = float(params["entry.rsi2_max"])
    rsi2_min = float(params["exit.rsi2_min"])
    stop_pct = float(params["exit.stop_loss_pct"]) / 100.0
    max_hold = int(params["exit.max_holding_days"])
    pct = sizing["position_pct_of_portfolio"] / 100.0
    max_pos = sizing["max_strategy_positions"]
    max_new = sizing["max_new_entries_per_day"]
    reserve = sizing["min_cash_reserve_usd"]
    min_order = sizing["min_order_usd"]

    cash = start_capital
    positions = {}   # sym → {qty, entry_px, entry_i}
    last_px = {}
    trades, wins = 0, 0
    peak, maxdd = start_capital, 0.0

    for d in window_dates:
        # 出场 (先卖后买, 与实盘引擎一致)
        for sym in list(positions):
            i = pre[sym]["idx"].get(d)
            if i is None:
                continue
            p = pre[sym]
            px, pos = p["close"][i], positions[sym]
            reason = None
            if px <= pos["entry_px"] * (1 - stop_pct):
                reason = "stop"
            elif (p["sma5"][i] is not None and px > p["sma5"][i]) or \
                 (p["rsi2"][i] is not None and p["rsi2"][i] >= rsi2_min):
                reason = "strength"
            elif i - pos["entry_i"] >= max_hold:
                reason = "time"
            if reason:
                cash += pos["qty"] * px
                trades += 1
                if px > pos["entry_px"]:
                    wins += 1
                del positions[sym]

        # 净值盯市
        equity = cash
        for sym, pos in positions.items():
            i = pre[sym]["idx"].get(d)
            if i is not None:
                last_px[sym] = pre[sym]["close"][i]
            equity += pos["qty"] * last_px[sym]

        # 入场
        cands = []
        for sym in universe:
            i = pre[sym]["idx"].get(d)
            if i is None or sym in positions:
                continue
            p = pre[sym]
            if p["sma200"][i] is None or p["rsi2"][i] is None:
                continue
            if p["close"][i] > p["sma200"][i] and p["rsi2"][i] < rsi2_max:
                cands.append((p["rsi2"][i], sym, i))
        cands.sort()
        slots = max(0, min(max_pos - len(positions), max_new))
        pos_usd = equity * pct
        for _, sym, i in cands[:slots]:
            px = pre[sym]["close"][i]
            amt = min(pos_usd, cash - reserve)
            if amt < min_order:
                continue
            positions[sym] = {"qty": amt / px, "entry_px": px, "entry_i": i}
            last_px[sym] = px
            cash -= amt

        peak = max(peak, equity)
        maxdd = max(maxdd, (peak - equity) / peak * 100.0)

    ret = (equity / start_capital - 1.0) * 100.0
    return {"return_pct": round(ret, 3), "maxdd_pct": round(maxdd, 3),
            "trades": trades, "wins": wins}


def score_candidate(pre, universe, folds, params, sizing):
    results = [simulate(pre, universe, w, params, sizing) for w in folds]
    total_trades = sum(r["trades"] for r in results)
    score = sum(r["return_pct"] / max(r["maxdd_pct"], 1.0) for r in results) / len(results)
    return round(score, 4), total_trades, results


# ---------- 子命令 ----------

def cmd_search(a):
    learning = load_json(a.learning)
    sl = load_json(a.state_learn)
    if not learning.get("enabled"):
        raise SystemExit("learning.enabled=false, 不搜索")
    if sl.get("challenger"):
        raise SystemExit("已有活跃挑战者, 先 promote/reject 再搜索")
    cfg = load_json(a.config)
    universe = cfg["etf_universe"]
    sizing = cfg["sizing"]
    hist = parse_historicals(a.historicals)
    missing = [s for s in universe if s not in hist]
    if missing:
        raise SystemExit(f"历史数据缺少: {missing}")
    pre = precompute({s: hist[s] for s in universe})

    wf = learning["walk_forward"]
    all_dates = sorted({d for s in universe for d in pre[s]["dates"] if d < a.date})
    need = wf["folds"] * wf["test_days"]
    if len(all_dates) < 210 + need:
        raise SystemExit(f"历史数据不足: 有 {len(all_dates)} 个交易日, 需要 ≥ {210 + need}")
    folds = [all_dates[len(all_dates) - (f + 1) * wf["test_days"]:
                       len(all_dates) - f * wf["test_days"]]
             for f in range(wf["folds"])]

    grid_keys = list(learning["search_grid"].keys())
    champion = sl["champion"]["params"]
    champ_score, champ_trades, champ_folds = score_candidate(pre, universe, folds, champion, sizing)

    best = None
    for combo in itertools.product(*(learning["search_grid"][k] for k in grid_keys)):
        params = dict(zip(grid_keys, combo))
        score, total_trades, results = score_candidate(pre, universe, folds, params, sizing)
        if total_trades < wf["min_trades_per_candidate"]:
            continue
        if best is None or score > best[0]:
            best = (score, params, total_trades, results)

    if best is None:
        raise SystemExit("网格内无满足最小交易数的候选")
    score, params, total_trades, results = best
    check_bounds(params, learning["learnable_bounds"])

    out = {"date": a.date, "champion": {"params": champion, "score": champ_score,
                                        "trades": champ_trades, "folds": champ_folds},
           "best": {"params": params, "score": score, "trades": total_trades, "folds": results}}

    if params == {k: champion.get(k) for k in params}:
        out["decision"] = "champion_optimal"
        print(json.dumps(out, indent=2, ensure_ascii=False))
        return

    sl["challenger"] = {"params": params, "started": a.date, "status": "validating",
                        "sim": {"score": score, "champion_score": champ_score,
                                "trades": total_trades}}
    save_json(a.state_learn, sl)
    ledger = {"start_capital": float(a.start_capital), "challenger_started": a.date,
              "high_water_mark": float(a.start_capital), "halted": False,
              "strategy_positions": {}, "legacy_positions": {}, "trades": []}
    save_json(a.paper_ledger, ledger)
    out["decision"] = "new_challenger"
    print(json.dumps(out, indent=2, ensure_ascii=False))


def cmd_challenger_config(a):
    cfg = load_json(a.config)
    sl = load_json(a.state_learn)
    ch = sl.get("challenger")
    if not ch:
        raise SystemExit("无活跃挑战者")
    for k, v in ch["params"].items():
        if k not in LEARNABLE:
            raise SystemExit(f"挑战者参数 {k} 不在可学白名单内")
        set_path(cfg, k, v)
    cfg["_challenger"] = {"started": ch["started"], "params": ch["params"]}
    save_json(a.out, cfg)
    print(f"挑战者配置已生成: {a.out} ({ch['params']})")


def cmd_record(a):
    sl = load_json(a.state_learn)
    entry = {"date": a.date, "live": round(float(a.live_equity), 2),
             "paper": round(float(a.paper_equity), 2)}
    hist = [e for e in sl.get("equity_history", []) if e["date"] != a.date]
    hist.append(entry)
    sl["equity_history"] = sorted(hist, key=lambda e: e["date"])
    save_json(a.state_learn, sl)
    print(json.dumps(entry, ensure_ascii=False))


def _evaluate(learning, sl, ledger, today):
    ch = sl.get("challenger")
    if not ch:
        return {"verdict": "no_challenger"}
    v = learning["validation"]
    since = ch["started"]
    pts = [e for e in sl.get("equity_history", []) if e["date"] >= since and e["date"] <= today]
    days = len(pts)
    trades = [t for t in ledger.get("trades", []) if t["date"] >= since]
    res = {"challenger": ch["params"], "started": since, "days": days,
           "paper_trades": len(trades)}
    if days < 2:
        res["verdict"] = "insufficient_data"
        return res

    live_ret = (pts[-1]["live"] / pts[0]["live"] - 1.0) * 100.0
    paper_ret = (pts[-1]["paper"] / pts[0]["paper"] - 1.0) * 100.0
    peak, maxdd = pts[0]["paper"], 0.0
    for e in pts:
        peak = max(peak, e["paper"])
        maxdd = max(maxdd, (peak - e["paper"]) / peak * 100.0)
    edge = paper_ret - live_ret
    res.update({"live_return_pct": round(live_ret, 3), "paper_return_pct": round(paper_ret, 3),
                "edge_pct": round(edge, 3), "paper_maxdd_pct": round(maxdd, 3)})

    if ledger.get("halted") or maxdd > v["max_paper_drawdown_pct"]:
        res["verdict"] = "fail"
        res["reason"] = f"paper 回撤 {maxdd:.2f}% 超限或账本已熔断"
    elif days < v["min_paper_days"]:
        res["verdict"] = "insufficient_data"
    elif len(trades) >= v["min_paper_trades"] and edge >= v["min_edge_vs_champion_pct"]:
        res["verdict"] = "pass"
    elif days >= v["max_paper_days"]:
        res["verdict"] = "fail"
        res["reason"] = "验证期满仍未同时满足 交易数/超额收益 标准"
    else:
        res["verdict"] = "extend"
    return res


def cmd_evaluate(a):
    res = _evaluate(load_json(a.learning), load_json(a.state_learn),
                    load_json(a.paper_ledger), a.date)
    print(json.dumps(res, indent=2, ensure_ascii=False))


def cmd_promote(a):
    learning = load_json(a.learning)
    sl = load_json(a.state_learn)
    ledger = load_json(a.paper_ledger)
    res = _evaluate(learning, sl, ledger, a.date)
    if res.get("verdict") != "pass":
        raise SystemExit(f"拒绝晋级: evaluate 结果为 {res.get('verdict')} (需要 pass)\n"
                         + json.dumps(res, indent=2, ensure_ascii=False))
    ch = sl["challenger"]
    check_bounds(ch["params"], learning["learnable_bounds"])
    cfg = load_json(a.config)
    old = {k: get_path(cfg, k) for k in ch["params"]}
    for k, v in ch["params"].items():
        set_path(cfg, k, v)
    save_json(a.config, cfg)
    sl["history"].append({"event": "promoted", "date": a.date, "old_params": old,
                          "new_params": ch["params"], "evaluation": res})
    sl["champion"] = {"params": ch["params"], "since": a.date}
    sl["challenger"] = None
    save_json(a.state_learn, sl)
    print(json.dumps({"promoted": ch["params"], "replaced": old, "evaluation": res},
                     indent=2, ensure_ascii=False))


def cmd_reject(a):
    sl = load_json(a.state_learn)
    ch = sl.get("challenger")
    if not ch:
        raise SystemExit("无活跃挑战者")
    sl["history"].append({"event": "rejected", "date": a.date,
                          "params": ch["params"], "reason": a.reason})
    sl["challenger"] = None
    save_json(a.state_learn, sl)
    print(f"挑战者已否决: {ch['params']} ({a.reason})")


def cmd_status(a):
    sl = load_json(a.state_learn)
    print(json.dumps({"champion": sl["champion"], "challenger": sl.get("challenger"),
                      "equity_points": len(sl.get("equity_history", [])),
                      "history_events": len(sl.get("history", []))},
                     indent=2, ensure_ascii=False))


def main():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("search")
    s.add_argument("--config", required=True)
    s.add_argument("--learning", required=True)
    s.add_argument("--state-learn", required=True)
    s.add_argument("--historicals", nargs="+", required=True)
    s.add_argument("--date", required=True)
    s.add_argument("--start-capital", required=True, help="paper 账本起始资金 (= 实盘当前净值)")
    s.add_argument("--paper-ledger", required=True)
    s.set_defaults(func=cmd_search)

    c = sub.add_parser("challenger-config")
    c.add_argument("--config", required=True)
    c.add_argument("--state-learn", required=True)
    c.add_argument("--out", required=True)
    c.set_defaults(func=cmd_challenger_config)

    r = sub.add_parser("record")
    r.add_argument("--state-learn", required=True)
    r.add_argument("--date", required=True)
    r.add_argument("--live-equity", required=True)
    r.add_argument("--paper-equity", required=True)
    r.set_defaults(func=cmd_record)

    e = sub.add_parser("evaluate")
    e.add_argument("--learning", required=True)
    e.add_argument("--state-learn", required=True)
    e.add_argument("--paper-ledger", required=True)
    e.add_argument("--date", required=True)
    e.set_defaults(func=cmd_evaluate)

    pr = sub.add_parser("promote")
    pr.add_argument("--config", required=True)
    pr.add_argument("--learning", required=True)
    pr.add_argument("--state-learn", required=True)
    pr.add_argument("--paper-ledger", required=True)
    pr.add_argument("--date", required=True)
    pr.set_defaults(func=cmd_promote)

    rj = sub.add_parser("reject")
    rj.add_argument("--state-learn", required=True)
    rj.add_argument("--date", required=True)
    rj.add_argument("--reason", required=True)
    rj.set_defaults(func=cmd_reject)

    st = sub.add_parser("status")
    st.add_argument("--state-learn", required=True)
    st.set_defaults(func=cmd_status)

    a = p.parse_args()
    a.func(a)


if __name__ == "__main__":
    main()
