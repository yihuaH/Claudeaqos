#!/usr/bin/env python3
"""
行情管道交叉核对 (2026-08-08 用户「可以」批准)。

比对 **引擎实际使用的收盘价** (Alpaca SIP, 由 integrations.py 落盘) 与
**券商官方收盘价** (Robinhood get_equity_quotes 的 close 字段, source=sip-list-exchange-close,
由会话经 MCP 取回)。两者应同源, 实测 2026-08-06 12 只标的 12/12 完全一致。

任何持续偏差 = 行情管道出了问题 (口径被改回 iex / Alpaca 数据异常 / 分割股息调整不一致),
必须在下单前发现, 而不是等亏了钱才发现。仅核对、不改任何订单 (红线2)。

用法:
  python3 scripts/price_check.py --bars BARS.json --quotes QUOTES.json \\
      --broker BROKER.json --date YYYY-MM-DD [--warn-bp 5] [--fail-bp 25] --out CHECK.json

BROKER.json 由会话写入, 格式 (逐字段照抄 get_equity_quotes 返回, 不得手改价格):
  {"MNST": {"date": "2026-08-07", "price": 90.36, "source": "sip-list-exchange-close"}, ...}

比对逻辑: 以**券商报出的那个日期**为准去引擎侧取同日收盘 —— 券商的 close 字段滚动有延迟
(实测周六仍报周四的收盘), 硬编码"今天"会比错日子。
  - 该日期 < 运行日 → 从 bars 取 (历史日线)
  - 该日期 == 运行日 → 从 quotes 取 (当日临时收盘)
"""
import argparse
import json
import sys


def load(path):
    with open(path) as f:
        return json.load(f)


def bars_closes(path):
    """{SYM: {date: close}}"""
    raw = load(path)
    out = {}
    for r in raw["data"]["results"]:
        d = {}
        for b in r["bars"]:
            if b.get("interpolated") or b.get("session") not in (None, "reg"):
                continue
            d[b["begins_at"][:10]] = float(b["close_price"])
        out[r["symbol"]] = d
    return out


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--bars", required=True)
    p.add_argument("--quotes", required=True)
    p.add_argument("--broker", required=True)
    p.add_argument("--date", required=True)
    p.add_argument("--warn-bp", type=float, default=5.0,
                   help="超过此偏差记 warn (同源应为 0.00bp, 留 5bp 容分割/四舍五入)")
    p.add_argument("--fail-bp", type=float, default=25.0,
                   help="超过此偏差记 anomaly, 触发红线6 停止交易")
    p.add_argument("--min-exact-pct", type=float, default=80.0,
                   help="完全一致率低于此值即 warn (≥5 只时生效)。同源应为 100%%; "
                        "口径悄悄退回 iex 时单只偏差仅几 bp 不触发 bp 闸, 但一致率会从 100%% "
                        "掉到 ~17%% — 这是管道回归最灵敏的探针")
    p.add_argument("--out")
    a = p.parse_args()

    bars = bars_closes(a.bars)
    quotes = load(a.quotes)
    broker = load(a.broker)

    rows, warns, fails, skipped = [], [], [], []
    for sym, info in sorted(broker.items()):
        bpx, bdate = info.get("price"), info.get("date")
        if bpx is None or not bdate:
            skipped.append({"symbol": sym, "reason": "券商未返回 close"})
            continue
        bpx = float(bpx)
        if bdate == a.date:
            ours, src = quotes.get(sym), "quotes"
        else:
            ours, src = (bars.get(sym) or {}).get(bdate), "bars"
        if ours is None:
            skipped.append({"symbol": sym, "reason": f"引擎侧无 {bdate} 收盘 ({src})"})
            continue
        ours = float(ours)
        dev = (ours - bpx) / bpx * 10000.0 if bpx else 0.0
        row = {"symbol": sym, "date": bdate, "broker": round(bpx, 4),
               "engine": round(ours, 4), "dev_bp": round(dev, 2), "src": src,
               "broker_source": info.get("source")}
        rows.append(row)
        if abs(dev) > a.fail_bp:
            fails.append(row)
        elif abs(dev) > a.warn_bp:
            warns.append(row)

    exact = sum(1 for r in rows if abs(r["dev_bp"]) < 0.01)
    devs = [abs(r["dev_bp"]) for r in rows]
    out = {
        "date": a.date,
        "compared": len(rows),
        "exact_match": exact,
        "exact_pct": round(exact / len(rows) * 100, 1) if rows else None,
        "mean_abs_dev_bp": round(sum(devs) / len(devs), 3) if devs else None,
        "max_abs_dev_bp": round(max(devs), 2) if devs else None,
        "warn_bp": a.warn_bp, "fail_bp": a.fail_bp,
        "warnings": warns, "anomalies": fails, "skipped": skipped,
        "rows": rows,
    }
    # 一致率探针: 口径悄悄退回 iex 时单只偏差仅几 bp (不触发 bp 闸), 但一致率会从 100% 崩到 ~17%
    低一致率 = (len(rows) >= 5 and out["exact_pct"] is not None
                and out["exact_pct"] < a.min_exact_pct)
    out["low_exact_rate"] = 低一致率
    if fails:
        out["verdict"] = "fail"
        out["note"] = (f"{len(fails)} 只标的引擎价与券商官方收盘偏差 >{a.fail_bp}bp — "
                       f"行情管道可能异常, 按红线6 停止交易并通知用户")
    elif 低一致率:
        out["verdict"] = "warn"
        out["note"] = (f"完全一致率 {out['exact_pct']}% < {a.min_exact_pct}% "
                       f"({exact}/{len(rows)}) — 引擎与券商可能已非同源 (口径退回 iex? "
                       f"数据源故障?), 平均偏差 {out['mean_abs_dev_bp']}bp。核查 integrations.py "
                       f"的 EQUITY_FEED/EQUITY_RT_FEED 并通知用户")
    elif warns:
        out["verdict"] = "warn"
        out["note"] = f"{len(warns)} 只标的偏差 >{a.warn_bp}bp, 记录观察, 不阻断交易"
    elif rows:
        out["verdict"] = "ok"
        out["note"] = f"{exact}/{len(rows)} 完全一致, 行情管道同源"
    else:
        out["verdict"] = "no_data"
        out["note"] = "无可比对标的 (券商未返回官方收盘, 或引擎侧缺该日数据)"

    s = json.dumps(out, indent=2, ensure_ascii=False)
    if a.out:
        with open(a.out, "w") as f:
            f.write(s + "\n")
    print(s)
    return 0


if __name__ == "__main__":
    sys.exit(main())
