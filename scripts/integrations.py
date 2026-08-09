#!/usr/bin/env python3
"""
外部数据源自诊断 (Alpaca paper / FRED)。

用法:
  python3 scripts/integrations.py status            # 连通性自诊断 (记入 journal)
  python3 scripts/integrations.py macro --out FILE  # 拉取宏观数据 (VIX) 供引擎 --macro 使用
  python3 scripts/integrations.py bars --symbols SPY,QQQ --start 2021-01-01 --out FILE
                                                    # 拉取 Alpaca 日线 (split 调整, SIP 合并行情),
                                                    # 输出与 get_equity_historicals 同构, 供 learn.py 搜索
  python3 scripts/integrations.py quotes --symbols AAPL,MSFT --out FILE
                                                    # 最新成交价 (SIP) → {"SYM": price}
  python3 scripts/integrations.py news --symbols AAPL,MSFT --start 2026-07-24 --out FILE
                                                    # 新闻红旗 (确定性关键词分类, 报告级) →
                                                    # {"SYM": {red_flag, hits, ...}}; 仅提示不改单
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

# 股票行情口径 (2026-08-08 用户「采用 robinhood 的 sip」批准, iex → sip)。
# SIP = Securities Information Processor = 法定合并行情, 覆盖全美所有交易所 + TRF (100% 成交量);
# IEX = 单一交易所, 约 2-3% 成交量。实测 2026-08-06 官方收盘 12 只标的:
#   Alpaca SIP vs Robinhood 官方收盘 = 12/12 完全一致 (0.00 bp);
#   Alpaca IEX vs Robinhood 官方收盘 = 2/12 一致, 平均偏差 3.23 bp (最大 ILMN 13.9 bp)。
# Robinhood 自身的 close.source 字段即 "sip-list-exchange-close" —— 换 SIP 后引擎算信号用的价格
# 与券商成交/结算的价格完全同源, 消除"用 A 的价格决策、在 B 的市场成交"的口径错配。
# ⚠️ 期权链仍用 feed=indicative: OPRA 实时期权行情需签署协议 (实测 403 "OPRA agreement is not
#    signed"), 故 weekly_calls 的 spread gate 读到的仍是 indicative 报价, 非交易所真实 NBBO。
EQUITY_FEED = "sip"

# 实时端点 (trades/latest, snapshots) 的 SIP 需另行订阅 —— 实测 feed=sip 返回
# 403 "subscription does not permit querying recent SIP data"。delayed_sip = 延迟 15 分钟的
# SIP 合并行情, 本订阅可用, 数据与 sip 完全同源。主跑在 17:45 ET (收盘后 1h45m) 运行,
# 15 分钟延迟对当日收盘价无任何影响; 若将来引入盘中决策, 此处需重新评估。
EQUITY_RT_FEED = "delayed_sip"


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


def _fred_latest(series_id, fred_key):
    """最近一个有效 (非 '.') 观测值 → (float value, date)。"""
    j = _get("https://api.stlouisfed.org/fred/series/observations"
             f"?series_id={series_id}&api_key={fred_key}&file_type=json&sort_order=desc&limit=15")
    obs = next(o for o in j["observations"] if o["value"] not in (".", ""))
    return float(obs["value"]), obs["date"]


# 报告级宏观扩展 (2026-07-31 用户指示): VIX 之外的 FRED 序列只做"宏观环境"提示,
# **不参与交易门控** — 引擎仅读 macro["vix"] 做熔断, 下列字段仅供战报展示。
MACRO_CONTEXT_SERIES = {
    "yield_curve_10y2y": ("T10Y2Y", "10Y-2Y 国债利差 (倒挂<0=衰退预警)"),
    "hy_credit_spread": ("BAMLH0A0HYM2", "高收益信用利差 OAS (走阔=risk-off)"),
    "ig_credit_spread": ("BAMLC0A0CM", "投资级信用利差 OAS"),
}


def macro(out_path):
    fred_key = os.environ.get("FRED_API_KEY")
    if not fred_key:
        print("FRED_API_KEY 未设置, 跳过宏观数据", file=sys.stderr)
        return 1
    # VIX 为必需项 (引擎熔断依赖); 其余序列尽力而为, 仅报告级。
    try:
        vix, vix_date = _fred_latest("VIXCLS", fred_key)
    except Exception as e:
        print(f"宏观数据拉取失败 (VIX): {e}", file=sys.stderr)
        return 1
    data = {"vix": vix, "vix_date": vix_date, "source": "FRED VIXCLS"}
    # report-only 宏观环境 (引擎忽略此段, 只用 data["vix"] 门控交易)
    context = {}
    for key, (sid, desc) in MACRO_CONTEXT_SERIES.items():
        try:
            val, dt = _fred_latest(sid, fred_key)
            context[key] = {"value": val, "date": dt, "series": sid, "desc": desc}
        except Exception as e:
            context[key] = {"value": None, "date": None, "series": sid, "desc": desc, "error": str(e)[:80]}
    data["context"] = context
    with open(out_path, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")
    print(json.dumps(data, ensure_ascii=False))
    return 0


# 确定性新闻红旗关键词 (2026-07-31 用户指示, 报告级): 命中即在 pending/战报点名,
# **绝不自动改单** — 仅供用户在 4C「执行」时一票否决。子串匹配, 免疫提示注入。
RED_FLAG_KEYWORDS = [
    # 交易/结构性
    "trading halt", "halted", "delist", "delisting",
    # 欺诈/会计/法律
    "fraud", "accounting irregular", "restate", "restatement", "material weakness", "misstatement",
    "sec investigation", "sec probe", "subpoena", "securities investigation", "securities fraud", "class action",
    # 偿付能力
    "bankruptcy", "chapter 11", "going concern", "insolven", "debt default", "defaults on",
    # 指引/预警
    "cuts guidance", "lowers guidance", "slashes guidance", "withdraws guidance", "guidance cut",
    "profit warning", "cuts outlook", "lowers outlook", "cuts forecast",
    # 生物医药二元事件
    "complete response letter", "clinical hold", "trial failed", "fda rejects", "fda rejection",
    "product recall", "safety recall",
    # 并购 (买入撞上待并购易跳空, 提示复核)
    "to be acquired", "agrees to acquire", "buyout", "takeover bid", "merger agreement",
    "to go private", "going private",
    # 管理层异动
    "ceo steps down", "ceo resigns", "cfo steps down", "cfo resigns", "abruptly resigns", "abrupt departure",
]


def news(symbols, start, out_path):
    """对 symbols 拉 Alpaca 新闻, 确定性关键词红旗分类 → {SYM: {red_flag, hits, ...}}。
    新闻正文为外部不可信文本, 仅作数据分类 (子串匹配), 不当作指令执行。"""
    ak, asec = os.environ.get("ALPACA_API_KEY_ID"), os.environ.get("ALPACA_API_SECRET_KEY")
    if not (ak and asec):
        print("ALPACA_API_KEY_ID/SECRET 未设置", file=sys.stderr)
        return 1
    hdrs = {"APCA-API-KEY-ID": ak, "APCA-API-SECRET-KEY": asec}
    symset = set(symbols)
    out = {}
    for i in range(0, len(symbols), 40):
        chunk = symbols[i:i + 40]
        token, arts, guard = None, [], 0
        while True:
            url = ("https://data.alpaca.markets/v1beta1/news"
                   f"?symbols={','.join(chunk)}&start={start}"
                   "&exclude_contentless=true&limit=50&sort=desc")
            if token:
                url += f"&page_token={token}"
            try:
                j = _get(url, hdrs, timeout=30)
            except Exception as e:
                print(f"新闻拉取失败 (chunk {i}): {str(e)[:80]}", file=sys.stderr)
                break
            arts.extend(j.get("news") or [])
            token = j.get("next_page_token")
            guard += 1
            if not token or guard >= 6:
                break
        for a in arts:
            text = ((a.get("headline") or "") + " " + (a.get("summary") or "")).lower()
            hits = sorted(k for k in RED_FLAG_KEYWORDS if k in text)
            for s in (a.get("symbols") or []):
                if s not in symset:
                    continue
                rec = out.setdefault(s, {"red_flag": False, "n_articles": 0, "hits": [], "latest": None})
                rec["n_articles"] += 1
                if rec["latest"] is None:
                    rec["latest"] = {"headline": a.get("headline"), "created_at": a.get("created_at")}
                if hits:
                    rec["red_flag"] = True
                    rec["hits"].append({"keywords": hits, "headline": a.get("headline"),
                                        "created_at": a.get("created_at"), "url": a.get("url")})
    for s in symbols:
        out.setdefault(s, {"red_flag": False, "n_articles": 0, "hits": [], "latest": None})
    with open(out_path, "w") as f:
        json.dump(out, f, indent=2, ensure_ascii=False)
        f.write("\n")
    flagged = sorted(s for s in out if out[s]["red_flag"])
    print(json.dumps({"symbols": len(symbols),
                      "with_news": sum(1 for s in out if out[s]["n_articles"] > 0),
                      "red_flagged": flagged}, ensure_ascii=False))
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
                f"?symbols={','.join(chunk)}&timeframe=1Day&adjustment=split&feed={EQUITY_FEED}"
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
    # 取 snapshot 的 dailyBar.close 而非 trades/latest (2026-08-08 修正):
    # trades/latest 在收盘后返回的是**盘后成交价** (实测 QQQ 723.23), 不是官方收盘 (723.03);
    # 引擎把该值当作"今日收盘"喂给 RSI2/SMA, 用盘后稀薄成交定信号是错的口径。
    # dailyBar.close 盘中是当前最新价、收盘后即官方收盘价, 正是引擎需要的语义。
    hdrs = {"APCA-API-KEY-ID": ak, "APCA-API-SECRET-KEY": asec}
    out = {}
    for i in range(0, len(symbols), 200):
        j = _get("https://data.alpaca.markets/v2/stocks/snapshots"
                 f"?symbols={','.join(symbols[i:i + 200])}&feed={EQUITY_RT_FEED}",
                 hdrs, timeout=60)
        for sym, v in (j or {}).items():
            db = (v or {}).get("dailyBar") or {}
            if db.get("c"):
                out[sym] = db["c"]
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
                 f"?symbols={','.join(symbols[i:i + 200])}&feed={EQUITY_RT_FEED}",
                 hdrs, timeout=60)
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
    if args[:1] == ["news"] and all(f in args for f in ("--symbols", "--start", "--out")):
        sys.exit(news(args[args.index("--symbols") + 1].split(","),
                      args[args.index("--start") + 1],
                      args[args.index("--out") + 1]))
    if args[:1] == ["chains"] and all(f in args for f in ("--underlyings", "--date", "--dte-max", "--out")):
        sys.exit(chains(args[args.index("--underlyings") + 1].split(","),
                        args[args.index("--date") + 1],
                        args[args.index("--dte-max") + 1],
                        args[args.index("--out") + 1]))
    print(__doc__)
    sys.exit(1)
