#!/usr/bin/env python3
"""
个股池周度筛选器 — 从全市场确定性地选出 stocks.json 的 universe。

  pool      按 25 日中位数成交额取流动性前 N (默认1000) → 1000股池
  rank      硬过滤 + 打分排序 → 前 ~150 候选 (待查行业)
  finalize  合并行业数据, 行业上限内取前 100, 写 strategy/universe.json 并更新 strategy/stocks.json

设计原则: 纯 stdlib、确定性 (同输入同输出, 并列按字母序打破)、绝不联网。
筛选只决定"能买什么"; 何时买、买多少仍由 signals.py 引擎决定 (红线 2)。
"""
import argparse
import json
import math
import sys


def load_json(path):
    with open(path) as f:
        return json.load(f)


def save_json(path, obj):
    with open(path, "w") as f:
        json.dump(obj, f, indent=2, ensure_ascii=False)
        f.write("\n")


def parse_bars(path):
    """→ {sym: [(date, close, volume), ...] 升序}"""
    out = {}
    for r in load_json(path)["data"]["results"]:
        rows = [(b["begins_at"][:10], float(b["close_price"]), float(b.get("volume") or 0))
                for b in r["bars"] if not b.get("interpolated")]
        out[r["symbol"]] = sorted(rows)
    return out


def median(xs):
    s = sorted(xs)
    n = len(s)
    return (s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2.0) if n else 0.0


def cmd_pool(a):
    cfg = load_json(a.config)
    bars = parse_bars(a.bars)
    dv = {}
    for sym, rows in bars.items():
        recent = rows[-25:]
        if len(recent) < 15:
            continue
        dv[sym] = median([c * v for _, c, v in recent])
    ranked = sorted(dv.items(), key=lambda kv: (-kv[1], kv[0]))[:cfg["pool_size"]]
    save_json(a.out, {"pool": [s for s, _ in ranked],
                      "dollar_volume": {s: round(v) for s, v in ranked}})
    print(json.dumps({"pool_size": len(ranked),
                      "min_dv_in_pool": round(ranked[-1][1]) if ranked else 0}))


def cmd_rank(a):
    cfg = load_json(a.config)
    pool = load_json(a.pool)
    bars = parse_bars(a.bars)
    popular = set(load_json(a.popular)["symbols"]) if a.popular else set()

    stats, rejected = {}, {"history": 0, "price": 0, "dollar_volume": 0, "move": 0, "vol": 0}
    for sym in pool["pool"]:
        rows = bars.get(sym, [])
        if len(rows) < cfg["min_history_days"]:
            rejected["history"] += 1
            continue
        closes = [c for _, c, _ in rows]
        px = closes[-1]
        if px < cfg["min_price"]:
            rejected["price"] += 1
            continue
        dv = median([c * v for _, c, v in rows[-25:]])
        if dv < cfg["min_median_dollar_volume_usd"]:
            rejected["dollar_volume"] += 1
            continue
        moves = [abs(closes[i] / closes[i - 1] - 1) * 100 for i in range(1, len(closes))]
        if max(moves[-60:]) >= cfg["max_abs_daily_move_pct_60d"]:
            rejected["move"] += 1
            continue
        rets = [closes[i] / closes[i - 1] - 1 for i in range(len(closes) - 20, len(closes))]
        mean = sum(rets) / len(rets)
        vol20 = math.sqrt(sum((r - mean) ** 2 for r in rets) / len(rets)) * math.sqrt(252) * 100
        if vol20 > cfg["max_annualized_vol20_pct"]:
            rejected["vol"] += 1
            continue
        sma200 = sum(closes[-200:]) / 200
        stats[sym] = {"dv": dv, "trend": px / sma200, "vol20": vol20, "price": px}

    def pct_rank(key, reverse=False):
        order = sorted(stats, key=lambda s: (stats[s][key], s), reverse=reverse)
        n = max(len(order) - 1, 1)
        return {s: i / n for i, s in enumerate(order)}

    r_dv = pct_rank("dv")                 # 越大越好
    r_trend = pct_rank("trend")           # 越大越好
    r_lowvol = pct_rank("vol20", reverse=True)  # 越小越好
    w = cfg["score_weights"]
    scored = []
    for s in stats:
        score = (w["dollar_volume"] * r_dv[s] + w["trend"] * r_trend[s]
                 + w["low_vol"] * r_lowvol[s]
                 + (cfg["popular_bonus"] if s in popular else 0.0))
        scored.append((round(score, 6), s))
    scored.sort(key=lambda t: (-t[0], t[1]))
    top = scored[:cfg["candidates_for_sectors"]]
    save_json(a.out, {"date": a.date,
                      "candidates": [{"symbol": s, "score": sc,
                                      "popular": s in popular,
                                      **{k: round(v, 4) for k, v in stats[s].items()}}
                                     for sc, s in top],
                      "survivors": len(stats), "rejected": rejected})
    print(json.dumps({"survivors": len(stats), "rejected": rejected,
                      "candidates": len(top)}, ensure_ascii=False))


def cmd_finalize(a):
    cfg = load_json(a.config)
    ranked = load_json(a.ranked)
    sectors = load_json(a.sectors)  # {"SYM": {"sector": ..., "name": ...} 或 null=非个股/未知}
    cap = cfg["max_per_sector_in_universe"]
    picked, sec_count, skipped, seen_names = [], {}, [], set()
    for c in ranked["candidates"]:
        if len(picked) >= cfg["final_size"]:
            break
        sym = c["symbol"]
        meta = sectors.get(sym)
        sec = (meta or {}).get("sector") if isinstance(meta, dict) else None
        name = (meta or {}).get("name") if isinstance(meta, dict) else None
        if not sec:
            skipped.append(f"{sym}(非个股/行业未知)")
            continue
        if name and name in seen_names:  # 双股权类去重 (GOOG/GOOGL), 保留分高的
            skipped.append(f"{sym}(同公司已入选: {name})")
            continue
        if sec_count.get(sec, 0) >= cap:
            skipped.append(f"{sym}(行业{sec}满)")
            continue
        picked.append({"symbol": sym, "sector": sec, "score": c["score"]})
        sec_count[sec] = sec_count.get(sec, 0) + 1
        if name:
            seen_names.add(name)

    universe = {"generated": a.date, "source": "screen.py pipeline",
                "criteria": {k: cfg[k] for k in
                             ("pool_size", "min_price", "min_median_dollar_volume_usd",
                              "max_abs_daily_move_pct_60d", "max_annualized_vol20_pct",
                              "score_weights", "popular_bonus", "max_per_sector_in_universe")},
                "symbols": [p["symbol"] for p in picked],
                "sectors": {p["symbol"]: p["sector"] for p in picked},
                "scores": {p["symbol"]: p["score"] for p in picked},
                "sector_counts": dict(sorted(sec_count.items())),
                "skipped": skipped}
    save_json(a.out, universe)

    if a.apply_stocks:
        stocks = load_json(a.apply_stocks)
        stocks["universe"] = universe["symbols"]
        stocks["defense"]["sectors"] = universe["sectors"]
        stocks["universe_generated"] = a.date
        save_json(a.apply_stocks, stocks)

    print(json.dumps({"final": len(picked), "sector_counts": universe["sector_counts"],
                      "skipped": len(skipped),
                      "applied_to": a.apply_stocks or None}, ensure_ascii=False))


def main():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    po = sub.add_parser("pool")
    po.add_argument("--config", required=True)
    po.add_argument("--bars", required=True, help="全市场 25 日 bars (integrations.py bars)")
    po.add_argument("--out", required=True)
    po.set_defaults(func=cmd_pool)

    r = sub.add_parser("rank")
    r.add_argument("--config", required=True)
    r.add_argument("--pool", required=True)
    r.add_argument("--bars", required=True, help="1000股池 470 日 bars")
    r.add_argument("--popular", help='Robinhood 热门榜 {"symbols": [...]} (加分项)')
    r.add_argument("--date", required=True)
    r.add_argument("--out", required=True)
    r.set_defaults(func=cmd_rank)

    f = sub.add_parser("finalize")
    f.add_argument("--config", required=True)
    f.add_argument("--ranked", required=True)
    f.add_argument("--sectors", required=True, help='{"SYM": "sector"|null} (Robinhood fundamentals)')
    f.add_argument("--date", required=True)
    f.add_argument("--out", required=True, help="strategy/universe.json")
    f.add_argument("--apply-stocks", help="同时把 universe/sectors 写入 strategy/stocks.json")
    f.set_defaults(func=cmd_finalize)

    a = p.parse_args()
    a.func(a)


if __name__ == "__main__":
    main()
