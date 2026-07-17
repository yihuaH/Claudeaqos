#!/usr/bin/env python3
"""
外部数据源自诊断 (Alpaca paper / FRED)。

用法:
  python3 scripts/integrations.py status            # 连通性自诊断 (记入 journal)
  python3 scripts/integrations.py macro --out FILE  # 拉取宏观数据 (VIX) 供引擎 --macro 使用
  python3 scripts/integrations.py bars --symbols SPY,QQQ --start 2021-01-01 --out FILE
                                                    # 拉取 Alpaca 日线 (split 调整, IEX),
                                                    # 输出与 get_equity_historicals 同构, 供 learn.py 搜索
  python3 scripts/integrations.py quotes --symbols AAPL,MSFT --out FILE
                                                    # 最新成交价 (IEX) → {"SYM": price}
  python3 scripts/integrations.py chains --underlyings XLF,XLE --date 2026-07-16 --dte-max 35 --out FILE
                                                    # 拉取 call 期权链快照 (indicative feed),
                                                    # 输出 {underlying: {occ: {bid, ask}}}, 供 options_overlay.py

数据源不可用时优雅降级: macro 失败则引擎不带宏观过滤运行, 交易照常。
密钥从环境变量读取 (ALPACA_API_KEY_ID / ALPACA_API_SECRET_KEY / FRED_API_KEY), 不入库。
"""
import json
import os
import sys
import urllib.request


def _get(url, headers=None, timeout=15):
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def status():
    out = {}

    fred_key = os.environ.get("FRED_API_KEY")
    if not fred_key:
        out["fred"] = {"ok": False, "reason": "FRED_API_KEY 未设置"}
    else:
        try:
            j = _get("https://api.stlouisfed.org/fred/series/observations"
                     f"?series_id=VIXCLS&api_key={fred_key}&file_type=json&sort_order=desc&limit=1")
            obs = j["observations"][0]
            out["fred"] = {"ok": True, "vix": obs["value"], "date": obs["date"]}
        except Exception as e:
            out["fred"] = {"ok": False, "reason": str(e)[:120]}

    ak, asec = os.environ.get("ALPACA_API_KEY_ID"), os.environ.get("ALPACA_API_SECRET_KEY")
    if not (ak and asec):
        out["alpaca"] = {"ok": False, "reason": "ALPACA_API_KEY_ID/SECRET 未设置"}
    else:
        hdrs = {"APCA-API-KEY-ID": ak, "APCA-API-SECRET-KEY": asec}
        try:
            clock = _get("https://paper-api.alpaca.markets/v2/clock", hdrs)
            out["alpaca"] = {"ok": True, "market_is_open": clock["is_open"],
                             "next_open": clock["next_open"], "next_close": clock["next_close"]}
        except Exception as e:
            out["alpaca"] = {"ok": False, "reason": str(e)[:120]}

    out["all_ok"] = all(v.get("ok") for k, v in out.items() if k != "all_ok")
    print(json.dumps(out, indent=2, ensure_ascii=False))
    return 0


def macro(out_path):
    fred_key = os.environ.get("FRED_API_KEY")
    if not fred_key:
        print("FRED_API_KEY 未设置, 跳过宏观数据", file=sys.stderr)
        return 1
    try:
        j = _get("https://api.stlouisfed.org/fred/series/observations"
                 f"?series_id=VIXCLS&api_key={fred_key}&file_type=json&sort_order=desc&limit=10")
        obs = next(o for o in j["observations"] if o["value"] != ".")
        data = {"vix": float(obs["value"]), "vix_date": obs["date"], "source": "FRED VIXCLS"}
    except Exception as e:
        print(f"宏观数据拉取失败: {e}", file=sys.stderr)
        return 1
    with open(out_path, "w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
    print(json.dumps(data))
    return 0


def bars(symbols, start, out_path, quiet=False):
    ak, asec = os.environ.get("ALPACA_API_KEY_ID"), os.environ.get("ALPACA_API_SECRET_KEY")
    if not (ak and asec):
        print("ALPACA_API_KEY_ID/SECRET 未设置", file=sys.stderr)
        return 1
    hdrs = {"APCA-API-KEY-ID": ak, "APCA-API-SECRET-KEY": asec}
    acc = {}
    for i in range(0, len(symbols), 200):  # 分批, 避免 URL 超长
        chunk = symbols[i:i + 200]
        base = ("https://data.alpaca.markets/v2/stocks/bars"
                f"?symbols={','.join(chunk)}&timeframe=1Day&adjustment=split&feed=iex"
                f"&limit=10000&start={start}T00:00:00Z")
        token = None
        while True:
            j = _get(base + (f"&page_token={token}" if token else ""), hdrs, timeout=90)
            for sym, bs in (j.get("bars") or {}).items():
                acc.setdefault(sym, []).extend(bs)
            token = j.get("next_page_token")
            if not token:
                break
    results = [{"symbol": sym,
                "bars": [{"begins_at": b["t"][:10] + "T00:00:00Z",
                          "close_price": str(b["c"]), "volume": b.get("v"),
                          "open": b.get("o"), "high": b.get("h"), "low": b.get("l"),
                          "session": "reg"} for b in bs]}
               for sym, bs in acc.items()]
    with open(out_path, "w") as f:
        json.dump({"data": {"results": results}}, f)
    if quiet:
        print(json.dumps({"symbols_requested": len(symbols), "symbols_with_data": len(acc)}))
    else:
        print(json.dumps({s: len(b["bars"]) for s, b in zip(acc, results)}, ensure_ascii=False))
        missing = [s for s in symbols if s not in acc]
        if missing:
            print(f"警告: 无数据符号 {missing}", file=sys.stderr)
    return 0


FUND_WORDS = ("ETF", "FUND", "TRUST", "SHARES", "ETN", "INDEX", "BOND",
              "TREASURY", "PORTFOLIO", "PROSHARES", "ISHARES", "VANGUARD",
              "SPDR", "DIREXION", "WISDOMTREE", "INVESCO")


def assets(out_path):
    """Alpaca 全市场可交易正股清单 (按名称启发式剔除基金/ETF/ETN)。"""
    ak, asec = os.environ.get("ALPACA_API_KEY_ID"), os.environ.get("ALPACA_API_SECRET_KEY")
    if not (ak and asec):
        print("ALPACA_API_KEY_ID/SECRET 未设置", file=sys.stderr)
        return 1
    hdrs = {"APCA-API-KEY-ID": ak, "APCA-API-SECRET-KEY": asec}
    j = _get("https://paper-api.alpaca.markets/v2/assets?status=active&asset_class=us_equity",
             hdrs, timeout=120)
    keep = []
    for a in j:
        sym, name = a.get("symbol", ""), (a.get("name") or "").upper()
        if not a.get("tradable") or a.get("exchange") not in ("NYSE", "NASDAQ", "AMEX", "ARCA", "BATS"):
            continue
        if not sym.isalpha() or len(sym) > 5:   # 剔除优先股/权证等带点斜杠的符号
            continue
        if any(w in name for w in FUND_WORDS):  # 名称启发式剔除基金类
            continue
        keep.append(sym)
    keep = sorted(set(keep))
    with open(out_path, "w") as f:
        json.dump({"symbols": keep, "count": len(keep)}, f)
    print(json.dumps({"total_active": len(j), "kept_stocks": len(keep)}))
    return 0


def latest_quotes(symbols, out_path):
    ak, asec = os.environ.get("ALPACA_API_KEY_ID"), os.environ.get("ALPACA_API_SECRET_KEY")
    if not (ak and asec):
        print("ALPACA_API_KEY_ID/SECRET 未设置", file=sys.stderr)
        return 1
    hdrs = {"APCA-API-KEY-ID": ak, "APCA-API-SECRET-KEY": asec}
    j = _get("https://data.alpaca.markets/v2/stocks/trades/latest"
             f"?symbols={','.join(symbols)}&feed=iex", hdrs, timeout=30)
    out = {sym: t["p"] for sym, t in (j.get("trades") or {}).items()}
    with open(out_path, "w") as f:
        json.dump(out, f, indent=2)
        f.write("\n")
    missing = [s for s in symbols if s not in out]
    print(json.dumps({"quotes": len(out), "missing": missing}, ensure_ascii=False))
    return 0


def snapshots(symbols, out_path):
    """当日实时 OHLC (算 IBS 用) → {"SYM": {open, high, low, close}}"""
    ak, asec = os.environ.get("ALPACA_API_KEY_ID"), os.environ.get("ALPACA_API_SECRET_KEY")
    if not (ak and asec):
        print("ALPACA_API_KEY_ID/SECRET 未设置", file=sys.stderr)
        return 1
    hdrs = {"APCA-API-KEY-ID": ak, "APCA-API-SECRET-KEY": asec}
    out = {}
    for i in range(0, len(symbols), 200):
        j = _get("https://data.alpaca.markets/v2/stocks/snapshots"
                 f"?symbols={','.join(symbols[i:i + 200])}&feed=iex", hdrs, timeout=60)
        for sym, v in j.items():
            db = v.get("dailyBar") or {}
            if db:
                out[sym] = {"open": db["o"], "high": db["h"], "low": db["l"],
                            "close": db["c"], "date": db["t"][:10]}
    with open(out_path, "w") as f:
        json.dump(out, f)
    print(json.dumps({"requested": len(symbols), "got": len(out)}))
    return 0


def chains(underlyings, date_str, dte_max, out_path):
    from datetime import date as _d, timedelta
    ak, asec = os.environ.get("ALPACA_API_KEY_ID"), os.environ.get("ALPACA_API_SECRET_KEY")
    if not (ak and asec):
        print("ALPACA_API_KEY_ID/SECRET 未设置", file=sys.stderr)
        return 1
    hdrs = {"APCA-API-KEY-ID": ak, "APCA-API-SECRET-KEY": asec}
    lte = (_d.fromisoformat(date_str) + timedelta(days=int(dte_max))).isoformat()
    out = {}
    for u in underlyings:
        acc = {}
        token = None
        while True:
            url = (f"https://data.alpaca.markets/v1beta1/options/snapshots/{u}"
                   f"?feed=indicative&type=call&limit=1000"
                   f"&expiration_date_gte={date_str}&expiration_date_lte={lte}")
            if token:
                url += f"&page_token={token}"
            j = _get(url, hdrs, timeout=60)
            for occ, s in (j.get("snapshots") or {}).items():
                q = s.get("latestQuote") or {}
                acc[occ] = {"bid": q.get("bp"), "ask": q.get("ap")}
            token = j.get("next_page_token")
            if not token:
                break
        out[u] = acc
    with open(out_path, "w") as f:
        json.dump(out, f)
    print(json.dumps({u: len(c) for u, c in out.items()}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    args = sys.argv[1:]
    if args[:1] == ["status"]:
        sys.exit(status())
    if args[:1] == ["macro"] and "--out" in args:
        sys.exit(macro(args[args.index("--out") + 1]))
    if args[:1] == ["bars"] and "--start" in args and "--out" in args:
        if "--symbols-file" in args:
            syms = json.load(open(args[args.index("--symbols-file") + 1]))["symbols"]
        else:
            syms = args[args.index("--symbols") + 1].split(",")
        sys.exit(bars(syms, args[args.index("--start") + 1],
                      args[args.index("--out") + 1], quiet="--symbols-file" in args))
    if args[:1] == ["assets"] and "--out" in args:
        sys.exit(assets(args[args.index("--out") + 1]))
    if args[:1] == ["snapshots"] and "--out" in args:
        if "--symbols-file" in args:
            ss = json.load(open(args[args.index("--symbols-file") + 1]))["symbols"]
        else:
            ss = args[args.index("--symbols") + 1].split(",")
        sys.exit(snapshots(ss, args[args.index("--out") + 1]))
    if args[:1] == ["quotes"] and "--symbols" in args and "--out" in args:
        sys.exit(latest_quotes(args[args.index("--symbols") + 1].split(","),
                               args[args.index("--out") + 1]))
    if args[:1] == ["chains"] and all(f in args for f in ("--underlyings", "--date", "--dte-max", "--out")):
        sys.exit(chains(args[args.index("--underlyings") + 1].split(","),
                        args[args.index("--date") + 1],
                        args[args.index("--dte-max") + 1],
                        args[args.index("--out") + 1]))
    print(__doc__)
    sys.exit(1)
