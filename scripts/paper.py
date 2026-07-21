#!/usr/bin/env python3
"""
Alpaca 纸面账户执行器 — 只用于挑战者参数的影子验证。

  run     执行 signals.py 产出的订单 (先卖后买, 市价, 幂等 client_order_id);
          整仓出场自动改走 close-position 接口全量平掉, 不留小数残渣
  equity  按本地账本 + 实时报价计算挑战者净值/现金 (不受 paper 账户里其他持仓干扰)
  account 打印 paper 账户基本状态

  positions  列出 paper 账户全部持仓 (股票 + 期权)
  liquidate  清仓 (--all 或 --symbols A,B), 含期权; 需开市

硬约束:
- BASE 硬编码为 paper-api.alpaca.markets, 绝不接入 Alpaca 实盘接口。
- paper 账户内股票与期权均可交易、全部持仓均可处置 (用户 2026-07-15 授权)。
- 挑战者的持仓/盈亏以本地账本 (state/paper_positions.json) 为准, Alpaca 提供真实成交价。
密钥从环境变量读取 (ALPACA_API_KEY_ID / ALPACA_API_SECRET_KEY), 不入库。
"""
import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from signals import load_json, parse_quotes  # noqa: E402

BASE = "https://paper-api.alpaca.markets"  # 硬编码: 只允许纸面环境


def _headers():
    ak, asec = os.environ.get("ALPACA_API_KEY_ID"), os.environ.get("ALPACA_API_SECRET_KEY")
    if not (ak and asec):
        raise SystemExit("ALPACA_API_KEY_ID/SECRET 未设置")
    return {"APCA-API-KEY-ID": ak, "APCA-API-SECRET-KEY": asec,
            "Content-Type": "application/json"}


def _req(method, path, body=None, timeout=20):
    req = urllib.request.Request(BASE + path, method=method, headers=_headers(),
                                 data=json.dumps(body).encode() if body else None)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def _get_by_coid(coid):
    return _req("GET", f"/v2/orders:by_client_order_id?client_order_id={coid}")


def _submit(order, coid):
    try:
        return _req("POST", "/v2/orders", {**order, "type": "market",
                                           "time_in_force": "day",
                                           "client_order_id": coid})
    except urllib.error.HTTPError as e:
        detail = e.read().decode()[:200]
        if e.code == 422 and "client_order_id" in detail:
            return _get_by_coid(coid)  # 重试幂等: 复用已提交的同名订单
        raise SystemExit(f"下单失败 {order.get('symbol')}: HTTP {e.code} {detail}")


OCC_RE = re.compile(r"^[A-Z.]{1,6}\d{6}[CP]\d{8}$")  # 期权 OCC 符号, 整张合约交易


def _wait_fill(coid, timeout_s):
    deadline = time.time() + timeout_s
    while True:
        o = _get_by_coid(coid)
        if o["status"] in ("filled", "canceled", "rejected", "expired") or time.time() > deadline:
            return o
        time.sleep(3)


def _wait_fill_by_id(oid, timeout_s):
    deadline = time.time() + timeout_s
    while True:
        o = _req("GET", f"/v2/orders/{oid}")
        if o["status"] in ("filled", "canceled", "rejected", "expired") or time.time() > deadline:
            return o
        time.sleep(3)


# 整仓出场判定: 卖量与账户全仓之差在 FULL_EXIT_EPS 股内、且覆盖 ≥FULL_EXIT_RATIO 持仓。
# 比例下限保护共享账户: 多本账本同持一标的时 (如个股实验与隔夜账本都持 CAT),
# 单本账本的出场卖量远小于账户全仓, 必须走普通限量卖单, 绝不能整仓平掉。
FULL_EXIT_EPS = 0.001
FULL_EXIT_RATIO = 0.999


def _position_qty(sym):
    try:
        return float(_req("GET", f"/v2/positions/{sym}")["qty"])
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise


def _latest_filled_sell(sym, date):
    orders = _req("GET", f"/v2/orders?status=closed&symbols={sym}"
                         f"&after={date}T00:00:00Z&direction=desc&limit=10")
    for o in orders:
        if o["side"] == "sell" and o["status"] == "filled":
            return o
    return None


def _sell_full_or_none(sym, want, date, timeout_s):
    """整仓出场改走 close-position 接口 (DELETE /v2/positions/{sym}), 按账户实际
    持仓 (9位小数) 全量平掉 — 根治买入按 notional 成交 9 位小数、卖出 floor6 截断
    留下百万分之一股残渣的问题。非整仓 (卖量明显小于账户持仓) 返回 None 走原限量
    卖单; 仓位已不存在时复用当日最近已成交卖单 (幂等重跑)。"""
    apos = _position_qty(sym)
    if apos is None:
        return _latest_filled_sell(sym, date) or {"status": "position_missing"}
    if abs(apos - want) > FULL_EXIT_EPS or want < apos * FULL_EXIT_RATIO:
        return None
    try:
        closed = _req("DELETE", f"/v2/positions/{sym}")
    except urllib.error.HTTPError as e:
        raise SystemExit(f"平仓失败 {sym}: HTTP {e.code} {e.read().decode()[:200]}")
    return _wait_fill_by_id(closed["id"], timeout_s)


def cmd_run(a):
    orders = load_json(a.orders)
    if orders.get("halted") or orders.get("circuit_breaker_triggered"):
        raise SystemExit("订单文件带 halted/熔断标记, 拒绝执行")
    plan = ([("sell", o) for o in orders.get("sells", [])]
            + [("buy", o) for o in orders.get("buys", [])])
    if not plan:
        print(json.dumps({"fills": []}))
        if a.fills_out:
            with open(a.fills_out, "w") as f:
                json.dump({"fills": []}, f)
        return

    if a.dry_run:
        print(json.dumps({"dry_run": True, "plan": plan}, indent=2, ensure_ascii=False))
        return

    if not _req("GET", "/v2/clock")["is_open"]:
        raise SystemExit("Alpaca 时钟显示市场未开盘, 拒绝提交 paper 订单 (顺延下一交易日)")

    fills, warnings = [], []
    for side, o in plan:
        sym = o["symbol"]
        coid = f"{a.coid_prefix}-{a.date}-{sym}-{side}"
        done = None
        if side == "sell" and not OCC_RE.match(sym):
            done = _sell_full_or_none(sym, float(o["qty"]), a.date, a.timeout)
        if done is None:
            body = {"symbol": sym, "side": side}
            if o.get("position_intent"):
                body["position_intent"] = o["position_intent"]
            if OCC_RE.match(sym):
                body["qty"] = str(int(o["qty"]))  # 期权只能整张
            elif side == "sell":
                body["qty"] = str(o["qty"])
            else:
                body["notional"] = str(o["dollar_amount"])
            _submit(body, coid)
            done = _wait_fill(coid, a.timeout)
        fq = float(done.get("filled_qty") or 0)
        if fq > 0:
            fills.append({"symbol": sym, "side": side, "qty": fq,
                          "price": float(done["filled_avg_price"]),
                          "bucket": o.get("bucket", "strategy"),
                          "reason": o.get("reason", "")})
        else:
            warnings.append(f"{sym} {side}: 未成交 (status={done['status']})")

    out = {"fills": fills, "warnings": warnings}
    if a.fills_out:
        with open(a.fills_out, "w") as f:
            json.dump(out, f, indent=2, ensure_ascii=False)
            f.write("\n")
    print(json.dumps(out, indent=2, ensure_ascii=False))


def cmd_equity(a):
    led = load_json(a.ledger)
    quotes = parse_quotes(a.quotes)
    chains = json.load(open(a.chains)) if a.chains else {}
    stock_flow = opt_flow = 0.0
    for t in led.get("trades", []):
        mult = 100.0 if t.get("bucket") == "options" else 1.0
        v = float(t["qty"]) * float(t["price"]) * mult
        if t.get("bucket") == "options":
            opt_flow += v if t["side"] == "sell" else -v
        else:
            stock_flow += v if t["side"] == "sell" else -v
    mv, missing = 0.0, []
    for sym, pos in led.get("strategy_positions", {}).items():
        if sym in quotes:
            mv += float(pos["qty"]) * quotes[sym]
        else:
            missing.append(sym)
            mv += float(pos["qty"]) * float(pos["entry_price"])
    # 未平空头 call 的负债: 有链报价用 ask (平仓成本), 否则按内在价值
    liability = 0.0
    for occ, p in led.get("option_positions", {}).items():
        chain = chains.get(p["underlying"], {})
        ask = (chain.get(occ) or {}).get("ask")
        if ask is None:
            ask = max(0.0, quotes.get(p["underlying"], p["strike"]) - p["strike"])
        liability += float(ask) * 100.0 * p["contracts"]

    start = float(led["start_capital"])
    cash = start + stock_flow + opt_flow
    ex_options = start + stock_flow + mv           # 参数验证用: 剔除期权轨道
    overlay_pnl = opt_flow - liability             # 备兑轨道独立盈亏
    out = {"equity": round(cash + mv - liability, 2), "cash": round(cash, 2),
           "equity_ex_options": round(ex_options, 2),
           "overlay_pnl": round(overlay_pnl, 2),
           "option_liability": round(liability, 2),
           "positions": {s: round(float(p["qty"]), 6)
                         for s, p in led.get("strategy_positions", {}).items()},
           "option_positions": {o: p["contracts"]
                                for o, p in led.get("option_positions", {}).items()}}
    if missing:
        out["warning"] = f"无报价, 按成本计: {missing}"
    print(json.dumps(out, ensure_ascii=False))


def cmd_account(a):
    acct = _req("GET", "/v2/account")
    print(json.dumps({k: acct.get(k) for k in
                      ("account_number", "status", "equity", "cash", "buying_power",
                       "options_trading_level", "options_buying_power",
                       "trading_blocked")}, indent=2))


def cmd_positions(a):
    pos = _req("GET", "/v2/positions")
    print(json.dumps([{k: p.get(k) for k in
                       ("symbol", "asset_class", "qty", "market_value", "unrealized_pl")}
                      for p in pos], indent=2))


def cmd_liquidate(a):
    if not (a.all or a.symbols):
        raise SystemExit("需要 --all 或 --symbols")
    if not _req("GET", "/v2/clock")["is_open"]:
        raise SystemExit("Alpaca 时钟显示市场未开盘, 拒绝清仓 (顺延下一交易日)")
    if a.all:
        res = _req("DELETE", "/v2/positions?cancel_orders=true")
        closed = [r.get("symbol") for r in res]
    else:
        closed = []
        for sym in a.symbols.split(","):
            _req("DELETE", f"/v2/positions/{sym.strip()}")
            closed.append(sym.strip())
    # 等订单落地后汇报剩余持仓
    time.sleep(5)
    left = _req("GET", "/v2/positions")
    print(json.dumps({"close_submitted": closed,
                      "remaining": [p["symbol"] for p in left]},
                     indent=2, ensure_ascii=False))


def main():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run")
    r.add_argument("--orders", required=True, help="signals.py signal 的输出文件")
    r.add_argument("--date", required=True)
    r.add_argument("--fills-out", help="成交输出 (供 signals.py apply 回写 paper 账本)")
    r.add_argument("--timeout", type=int, default=90, help="单笔订单等待成交秒数")
    r.add_argument("--coid-prefix", default="cq",
                   help="幂等ID前缀; 不同 paper 轨道用不同前缀, 避免同日同标的订单冲突")
    r.add_argument("--dry-run", action="store_true")
    r.set_defaults(func=cmd_run)

    e = sub.add_parser("equity")
    e.add_argument("--ledger", required=True, help="state/paper_positions.json")
    e.add_argument("--quotes", required=True)
    e.add_argument("--chains", help="期权链文件, 用于给未平 call 估值 (缺省按内在价值)")
    e.set_defaults(func=cmd_equity)

    ac = sub.add_parser("account")
    ac.set_defaults(func=cmd_account)

    ps = sub.add_parser("positions")
    ps.set_defaults(func=cmd_positions)

    lq = sub.add_parser("liquidate")
    lq.add_argument("--all", action="store_true", help="清空全部持仓 (含期权), 撤销挂单")
    lq.add_argument("--symbols", help="只清指定符号, 逗号分隔")
    lq.set_defaults(func=cmd_liquidate)

    a = p.parse_args()
    a.func(a)


if __name__ == "__main__":
    main()
