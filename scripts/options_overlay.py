#!/usr/bin/env python3
"""
备兑开仓 (covered call) 确定性引擎 — 仅用于 Alpaca paper 账户的实验轨道。

  signal  由 账本持仓 + 当日正股订单 + 期权链 + 报价 计算今天的期权订单
          (平仓单与开仓单分开输出: 平仓必须在正股卖出前执行, 开仓在正股交易后执行)
  apply   把期权成交回写到 paper 账本 (option_positions + trades, bucket=options)

规则 (全部确定性, 参数在 strategy/options.json):
- 开仓: 账本内策略持仓满 100 股整手、当日不出场、且无未平 call → 卖出
  到期 min_dte..max_dte 天、行权价 ≥ 现价×(1+otm_pct%)、bid ≥ min_premium_bid 的最近月最低合规行权价 call,
  每 100 股 1 张。
- 平仓: 正股当日有卖出订单 → 先买回其 call; 到期剩 ≤ close_when_dte_lte 天 → 强制买回 (避免行权)。
- 早指派 (罕见): 买回单被拒说明合约已被指派, 按红线 6 记异常、通知用户、人工对账。

设计原则: 纯 stdlib、确定性、绝不联网。期权盈亏 bucket=options 单独记账,
不污染挑战者参数验证 (learn.py record 使用 equity_ex_options)。
"""
import argparse
import json
import math
import re
import sys
from datetime import date as _date

OCC_RE = re.compile(r"^([A-Z.]{1,6})(\d{6})([CP])(\d{8})$")


def load_json(path):
    with open(path) as f:
        return json.load(f)


def save_json(path, obj):
    with open(path, "w") as f:
        json.dump(obj, f, indent=2, ensure_ascii=False)
        f.write("\n")


def parse_occ(sym):
    m = OCC_RE.match(sym)
    if not m:
        return None
    u, d, cp, k = m.groups()
    return {"underlying": u, "expiry": f"20{d[:2]}-{d[2:4]}-{d[4:6]}",
            "type": cp, "strike": int(k) / 1000.0}


def dte(expiry, today):
    return (_date.fromisoformat(expiry) - _date.fromisoformat(today)).days


def cmd_signal(a):
    cfg = load_json(a.config)
    ledger = load_json(a.ledger)
    quotes = {k: float(v) for k, v in load_json(a.quotes).items()}
    chains = load_json(a.chains) if a.chains else {}
    orders = load_json(a.orders) if a.orders else {}
    cc = cfg["covered_call"]
    today = a.date
    closes, opens, warnings = [], [], []

    empty = {"closes": [], "opens": [], "warnings": []}
    if not cfg.get("enabled"):
        _emit(empty, a)
        return

    exiting = {s["symbol"] for s in orders.get("sells", [])}
    opos = ledger.get("option_positions", {})
    spos = ledger.get("strategy_positions", {})

    # --- 平仓 ---
    for occ, pos in sorted(opos.items()):
        meta = parse_occ(occ)
        if not meta:
            warnings.append(f"{occ}: 无法解析 OCC 符号, 跳过 (人工检查)")
            continue
        d = dte(meta["expiry"], today)
        reason = None
        if meta["underlying"] in exiting:
            reason = "underlying_exit"
        elif d <= cc["close_when_dte_lte"]:
            reason = "expiry_close"
        if reason:
            closes.append({"symbol": occ, "qty": int(pos["contracts"]),
                           "position_intent": "buy_to_close",
                           "bucket": "options", "reason": reason})

    closing_underlyings = {parse_occ(c["symbol"])["underlying"] for c in closes}
    covered = {parse_occ(o)["underlying"] for o in opos}

    # --- 开仓 ---
    for sym in sorted(spos):
        if sym in exiting or sym in covered or sym in closing_underlyings:
            continue
        contracts = math.floor(float(spos[sym]["qty"]) / 100)
        if contracts < 1:
            continue
        px = quotes.get(sym)
        if px is None:
            warnings.append(f"{sym}: 无正股报价, 跳过备兑开仓")
            continue
        chain = chains.get(sym, {})
        min_strike = px * (1 + cc["otm_pct"] / 100.0)
        cands = []
        for occ, q in chain.items():
            meta = parse_occ(occ)
            if not meta or meta["type"] != "C":
                continue
            d = dte(meta["expiry"], today)
            bid = float(q.get("bid") or 0)
            if cc["min_dte"] <= d <= cc["max_dte"] and meta["strike"] >= min_strike \
               and bid >= cc["min_premium_bid"]:
                cands.append((meta["expiry"], meta["strike"], occ, bid))
        if not cands:
            warnings.append(f"{sym}: 链上无满足条件的 call (现价 {px}, 最低行权价 {min_strike:.2f})")
            continue
        cands.sort()
        expiry, strike, occ, bid = cands[0]
        opens.append({"symbol": occ, "qty": contracts,
                      "position_intent": "sell_to_open",
                      "bucket": "options", "reason": "covered_call",
                      "underlying": sym, "strike": strike, "expiry": expiry,
                      "est_premium": bid})

    _emit({"date": today, "closes": closes, "opens": opens, "warnings": warnings}, a)


def _emit(out, a):
    print(json.dumps(out, indent=2, ensure_ascii=False))
    # paper.py run 的输入格式: 平仓=买单(buys), 开仓=卖单(sells); OCC 符号一律按 qty 整张下单
    if a.out_closes:
        save_json(a.out_closes, {"sells": [], "buys": out["closes"]})
    if a.out_opens:
        save_json(a.out_opens, {"sells": out["opens"], "buys": []})


def cmd_apply(a):
    ledger = load_json(a.ledger)
    fills = load_json(a.fills)
    opos = ledger.setdefault("option_positions", {})
    for f in fills["fills"]:
        occ, side = f["symbol"], f["side"]
        qty, price = int(float(f["qty"])), float(f["price"])
        meta = parse_occ(occ)
        if not meta:
            raise SystemExit(f"非期权成交混入 options apply: {occ}")
        if side == "sell":
            p = opos.setdefault(occ, {"underlying": meta["underlying"],
                                      "strike": meta["strike"], "expiry": meta["expiry"],
                                      "contracts": 0, "open_premium": price,
                                      "opened": a.date})
            p["contracts"] += qty
        else:
            if occ in opos:
                opos[occ]["contracts"] -= qty
                if opos[occ]["contracts"] <= 0:
                    del opos[occ]
        ledger.setdefault("trades", []).append({
            "date": a.date, "symbol": occ, "side": side, "qty": qty,
            "price": price, "bucket": "options", "reason": f.get("reason", ""),
        })
    save_json(a.ledger, ledger)
    print(f"paper 账本已更新: {len(fills['fills'])} 笔期权成交")


def main():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("signal")
    s.add_argument("--config", required=True, help="strategy/options.json")
    s.add_argument("--ledger", required=True, help="state/paper_positions.json")
    s.add_argument("--quotes", required=True, help='正股报价 {"SYM": price}')
    s.add_argument("--chains", help='期权链 {underlying: {occ: {bid, ask}}} (integrations.py chains 产出)')
    s.add_argument("--orders", help="当日正股订单文件 (识别出场持仓)")
    s.add_argument("--date", required=True)
    s.add_argument("--out-closes", help="平仓订单输出 (供 paper.py run, 在正股卖出前执行)")
    s.add_argument("--out-opens", help="开仓订单输出 (供 paper.py run, 在正股交易后执行)")
    s.set_defaults(func=cmd_signal)

    ap = sub.add_parser("apply")
    ap.add_argument("--ledger", required=True)
    ap.add_argument("--fills", required=True)
    ap.add_argument("--date", required=True)
    ap.set_defaults(func=cmd_apply)

    a = p.parse_args()
    a.func(a)


if __name__ == "__main__":
    main()
