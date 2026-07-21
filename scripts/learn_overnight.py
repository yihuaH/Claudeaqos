#!/usr/bin/env python3
"""
隔夜策略参数学习器 — 与 learn.py 同构的冠军/挑战者闭环 (IBS 隔夜均值回归)。

  search            walk-forward 网格搜索 → 产出挑战者, 初始化 挑战者/冠军孪生 两本纸面账
  challenger-config 生成挑战者的合并 overnight 配置 (冠军孪生直接用 strategy/overnight.json)
  record / evaluate / promote / reject / status — 语义同 learn.py
                    (equity_history 的 live 字段 = 冠军孪生纸面净值, 构成干净 A/B)

边界: 只学 strategy/learning_overnight.json learnable 列出的三个形状参数;
仓位/频率/防御/熔断永不自学习。promote 内部重跑 evaluate, 非 pass 拒绝。
纯 stdlib、确定性、绝不联网。
"""
import argparse
import itertools
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from signals import load_json, save_json  # noqa: E402
from learn import get_path, set_path, _evaluate, cmd_record, cmd_reject, cmd_status  # noqa: E402

LEARNABLE = ["entry.ibs_max", "exit.stop_loss_pct", "exit.window"]


def check_bounds_mixed(params, learnable):
    for k, v in params.items():
        if k not in learnable:
            raise SystemExit(f"拒绝: 参数 {k} 不在可学白名单内")
        spec = learnable[k]
        if "choices" in spec:
            if v not in spec["choices"]:
                raise SystemExit(f"拒绝: {k}={v} 不在允许取值 {spec['choices']} 内")
        else:
            lo, hi = spec["bounds"]
            if not (lo <= float(v) <= hi):
                raise SystemExit(f"拒绝: {k}={v} 超出边界 [{lo}, {hi}]")


# ---------- 数据与预计算 ----------

def parse_ohlc(path):
    out = {}
    for r in load_json(path)["data"]["results"]:
        rows = []
        for b in r["bars"]:
            if b.get("open") is None or b.get("high") is None or b.get("low") is None:
                continue
            rows.append((b["begins_at"][:10], float(b["open"]), float(b["high"]),
                         float(b["low"]), float(b["close_price"])))
        out[r["symbol"]] = sorted(rows)
    return out


def precompute(ohlc, move_pct, move_look):
    pre = {}
    for sym, rows in ohlc.items():
        n = len(rows)
        dates = [r[0] for r in rows]
        op = [r[1] for r in rows]
        cl = [r[4] for r in rows]
        ibs = [None] * n
        for i in range(n):
            h, l, c = rows[i][2], rows[i][3], rows[i][4]
            if h > l:
                ibs[i] = (c - l) / (h - l)
        pref = [0.0]
        for c in cl:
            pref.append(pref[-1] + c)
        sma200 = [None if i + 1 < 200 else (pref[i + 1] - pref[i - 199]) / 200 for i in range(n)]
        blocked = [False] * n
        for i in range(1, n):
            if cl[i - 1] and abs(cl[i] / cl[i - 1] - 1) * 100.0 >= move_pct:
                for j in range(i, min(n, i + move_look + 1)):
                    blocked[j] = True
        pre[sym] = {"dates": dates, "idx": {d: i for i, d in enumerate(dates)},
                    "open": op, "close": cl, "ibs": ibs, "sma200": sma200, "blocked": blocked}
    return pre


# ---------- 模拟 (复刻 overnight.py 规则) ----------

def simulate(pre, etfs, stocks, sectors, params, sizing, defense, window_dates,
             start_capital=1000.0):
    ibs_max = float(params["entry.ibs_max"])
    stop = float(params["exit.stop_loss_pct"]) / 100.0
    mode = params["exit.window"]
    pct = sizing["position_pct_of_portfolio"] / 100.0
    max_new = sizing["max_new_entries_per_day"]
    reserve = sizing["min_cash_reserve_usd"]
    min_order = sizing["min_order_usd"]
    sec_cap = int(defense["max_per_sector"])
    universe = etfs + [s for s in stocks if s not in etfs]

    cash, positions, last_px = start_capital, {}, {}
    trades = wins = 0
    peak, maxdd = start_capital, 0.0
    equity = start_capital

    for d in window_dates:
        sold_today = set()
        for sym in list(positions):
            pos = positions[sym]
            if pos["entry_d"] == d:
                continue
            p = pre[sym]
            i = p["idx"].get(d)
            if i is None:
                continue
            if mode == "next_open":
                px = p["open"][i]
            else:
                px = p["close"][i]
                if not (px <= pos["entry_px"] * (1 - stop)):
                    v = p["ibs"][i]
                    if v is not None and v < ibs_max and not pos.get("ext"):
                        pos["ext"] = True
                        continue
            cash += pos["qty"] * px
            trades += 1
            if px > pos["entry_px"]:
                wins += 1
            sold_today.add(sym)
            del positions[sym]

        equity = cash
        for sym, pos in positions.items():
            i = pre[sym]["idx"].get(d)
            if i is not None:
                last_px[sym] = pre[sym]["close"][i]
            equity += pos["qty"] * last_px[sym]

        cands = []
        for sym in universe:
            p = pre.get(sym)
            if p is None or sym in positions or sym in sold_today:
                continue
            i = p["idx"].get(d)
            if i is None or p["sma200"][i] is None or p["ibs"][i] is None:
                continue
            if p["close"][i] <= p["sma200"][i] or p["ibs"][i] >= ibs_max:
                continue
            if sym not in etfs and p["blocked"][i]:
                continue
            cands.append((p["ibs"][i], sym))
        cands.sort()

        sec_count = {}
        for s in positions:
            sc = sectors.get(s)
            if sc:
                sec_count[sc] = sec_count.get(sc, 0) + 1
        pos_usd = equity * pct
        placed = 0
        for v, sym in cands:
            if placed >= max_new:
                break
            sc = sectors.get(sym)
            if sym not in etfs and sc and sec_count.get(sc, 0) >= sec_cap:
                continue
            amt = min(pos_usd, cash - reserve)
            if amt < min_order:
                continue
            i = pre[sym]["idx"][d]
            px = pre[sym]["close"][i]
            positions[sym] = {"qty": amt / px, "entry_px": px, "entry_d": d}
            last_px[sym] = px
            cash -= amt
            placed += 1
            if sc:
                sec_count[sc] = sec_count.get(sc, 0) + 1

        peak = max(peak, equity)
        maxdd = max(maxdd, (peak - equity) / peak * 100.0)

    ret = (equity / start_capital - 1.0) * 100.0
    return {"return_pct": round(ret, 3), "maxdd_pct": round(maxdd, 3),
            "trades": trades, "wins": wins}


def score_candidate(pre, etfs, stocks, sectors, folds, params, sizing, defense):
    results = [simulate(pre, etfs, stocks, sectors, params, sizing, defense, w)
               for w in folds]
    total = sum(r["trades"] for r in results)
    score = sum(r["return_pct"] / max(r["maxdd_pct"], 1.0) for r in results) / len(results)
    return round(score, 4), total, results


# ---------- 子命令 ----------

def cmd_search(a):
    learning = load_json(a.learning)
    sl = load_json(a.state_learn)
    if not learning.get("enabled"):
        raise SystemExit("learning_overnight.enabled=false, 不搜索")
    if sl.get("challenger"):
        raise SystemExit("已有活跃挑战者, 先 promote/reject 再搜索")
    cfg = load_json(a.config)
    uni = load_json(a.universe)
    etfs = cfg["universe_etfs"]
    stocks = uni["symbols"]
    sectors = uni.get("sectors", {})
    d = cfg["defense"]
    pre = precompute(parse_ohlc(a.bars), float(d["max_daily_move_pct"]),
                     int(d["move_lookback_days"]))
    missing = [s for s in etfs + stocks if s not in pre]
    if missing:
        print(f"警告: 缺数据符号 {missing[:8]}{'...' if len(missing) > 8 else ''}", file=sys.stderr)

    wf = learning["walk_forward"]
    ref = "SPY" if "SPY" in pre else etfs[0]
    all_dates = [dd for dd in pre[ref]["dates"] if dd < a.date]
    need = wf["folds"] * wf["test_days"]
    if len(all_dates) < 210 + need:
        raise SystemExit(f"历史不足: {len(all_dates)} 交易日, 需 ≥ {210 + need}")
    folds = [all_dates[len(all_dates) - (f + 1) * wf["test_days"]:
                       len(all_dates) - f * wf["test_days"]]
             for f in range(wf["folds"])]

    keys = list(learning["learnable"].keys())
    champ = sl["champion"]["params"]
    c_score, c_trades, c_folds = score_candidate(pre, etfs, stocks, sectors, folds,
                                                 champ, cfg["sizing"], d)
    best = None
    for combo in itertools.product(*(learning["learnable"][k]["grid"] for k in keys)):
        params = dict(zip(keys, combo))
        score, total, results = score_candidate(pre, etfs, stocks, sectors, folds,
                                                params, cfg["sizing"], d)
        if total < wf["min_trades_per_candidate"]:
            continue
        if best is None or score > best[0]:
            best = (score, params, total, results)
    if best is None:
        raise SystemExit("网格内无满足最小交易数的候选")
    score, params, total, results = best
    check_bounds_mixed(params, learning["learnable"])

    out = {"date": a.date,
           "champion": {"params": champ, "score": c_score, "trades": c_trades, "folds": c_folds},
           "best": {"params": params, "score": score, "trades": total, "folds": results}}
    if params == {k: champ.get(k) for k in params}:
        out["decision"] = "champion_optimal"
        print(json.dumps(out, indent=2, ensure_ascii=False))
        return

    sl["challenger"] = {"params": params, "started": a.date, "status": "validating",
                        "sim": {"score": score, "champion_score": c_score, "trades": total}}
    save_json(a.state_learn, sl)
    for path in (a.challenger_ledger, a.champion_ledger):
        save_json(path, {"start_capital": round(float(a.start_capital), 2),
                         "challenger_started": a.date,
                         "high_water_mark": round(float(a.start_capital), 2),
                         "halted": False, "strategy_positions": {},
                         "legacy_positions": {}, "trades": []})
    out["decision"] = "new_challenger"
    print(json.dumps(out, indent=2, ensure_ascii=False))


def cmd_challenger_config(a):
    cfg = load_json(a.config)
    cfg.pop("live_entries_paused", None)  # 实盘入场暂停不影响纸面学习账本
    sl = load_json(a.state_learn)
    ch = sl.get("challenger")
    if not ch:
        raise SystemExit("无活跃挑战者")
    for k, v in ch["params"].items():
        if k not in LEARNABLE:
            raise SystemExit(f"挑战者参数 {k} 不在可学白名单内")
        set_path(cfg, k, v)
    cfg["funding"] = {**cfg.get("funding", {}), "allowed": False,
                      "note": "学习账本不做换仓 (不得触碰实盘存量)"}
    cfg["_challenger"] = {"started": ch["started"], "params": ch["params"]}
    save_json(a.out, cfg)
    print(f"隔夜挑战者配置已生成: {a.out} ({ch['params']})")


def cmd_twin_config(a):
    """冠军孪生配置 = 当前 overnight.json + 禁止换仓 (供学习账本影子跑)"""
    cfg = load_json(a.config)
    cfg.pop("live_entries_paused", None)  # 实盘入场暂停不影响纸面学习账本
    cfg["funding"] = {**cfg.get("funding", {}), "allowed": False,
                      "note": "学习账本不做换仓 (不得触碰实盘存量)"}
    cfg["_twin"] = True
    save_json(a.out, cfg)
    print(f"冠军孪生配置已生成: {a.out}")


def cmd_evaluate(a):
    res = _evaluate(load_json(a.learning), load_json(a.state_learn),
                    load_json(a.paper_ledger), a.date)
    res["note"] = "live 字段 = 冠军孪生纸面净值 (干净 A/B)"
    print(json.dumps(res, indent=2, ensure_ascii=False))


def cmd_promote(a):
    learning = load_json(a.learning)
    sl = load_json(a.state_learn)
    ledger = load_json(a.paper_ledger)
    res = _evaluate(learning, sl, ledger, a.date)
    if res.get("verdict") != "pass":
        raise SystemExit(f"拒绝晋级: evaluate={res.get('verdict')} (需要 pass)\n"
                         + json.dumps(res, indent=2, ensure_ascii=False))
    ch = sl["challenger"]
    check_bounds_mixed(ch["params"], learning["learnable"])
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


def main():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("search")
    s.add_argument("--config", required=True, help="strategy/overnight.json")
    s.add_argument("--learning", required=True)
    s.add_argument("--state-learn", required=True)
    s.add_argument("--bars", required=True, help="OHLC 日线 (integrations.py bars, 含 open)")
    s.add_argument("--universe", required=True, help="strategy/universe.json")
    s.add_argument("--date", required=True)
    s.add_argument("--start-capital", required=True)
    s.add_argument("--challenger-ledger", required=True)
    s.add_argument("--champion-ledger", required=True)
    s.set_defaults(func=cmd_search)

    c = sub.add_parser("challenger-config")
    c.add_argument("--config", required=True)
    c.add_argument("--state-learn", required=True)
    c.add_argument("--out", required=True)
    c.set_defaults(func=cmd_challenger_config)

    tw = sub.add_parser("twin-config")
    tw.add_argument("--config", required=True)
    tw.add_argument("--out", required=True)
    tw.set_defaults(func=cmd_twin_config)

    r = sub.add_parser("record")
    r.add_argument("--state-learn", required=True)
    r.add_argument("--date", required=True)
    r.add_argument("--live-equity", required=True, help="冠军孪生纸面净值")
    r.add_argument("--paper-equity", required=True, help="挑战者纸面净值")
    r.set_defaults(func=cmd_record)

    e = sub.add_parser("evaluate")
    e.add_argument("--learning", required=True)
    e.add_argument("--state-learn", required=True)
    e.add_argument("--paper-ledger", required=True, help="挑战者账本")
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
