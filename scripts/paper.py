#!/usr/bin/env python3
"""
Alpaca 纸面账户执行器 — 只用于挑战者参数的影子验证。

  run     执行 signals.py 产出的订单 (先卖后买, 市价, 幂等 client_order_id)
  equity  按本地账本 + 实时报价计算挑战者净值/现金 (不受 paper 账户里其他持仓干扰)
  account 打印 paper 账户基本状态

硬约束:
- BASE 硬编码为 paper-api.alpaca.markets, 绝不接入 Alpaca 实盘接口。
- 只交易订单文件里的符号, 绝不动 paper 账户里的其他持仓。
- 挑战者的持仓/盈亏以本地账本 (state/paper_positions.json) 为准, Alpaca 只提供真实成交价。
密钥从环境变量读取 (ALPACA_API_KEY_ID / ALPACA_API_SECRET_KEY), 不入库。
"""
import argparse
import json
import os
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


def _wait_fill(coid, timeout_s):
    deadline = time.time() + timeout_s
    while True:
        o = _get_by_coid(coid)
        if o["status"] in ("filled", "canceled", "rejected", "expired") or time.time() > deadline:
            return o
        time.sleep(3)


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
        coid = f"cq-{a.date}-{sym}-{side}"
        body = {"symbol": sym, "side": side}
        if side == "sell":
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
    cash = float(led["start_capital"])
    for t in led.get("trades", []):
        v = float(t["qty"]) * float(t["price"])
        cash += v if t["side"] == "sell" else -v
    mv, missing = 0.0, []
    for sym, pos in led.get("strategy_positions", {}).items():
        if sym in quotes:
            mv += float(pos["qty"]) * quotes[sym]
        else:
            missing.append(sym)
            mv += float(pos["qty"]) * float(pos["entry_price"])
    out = {"equity": round(cash + mv, 2), "cash": round(cash, 2),
           "positions": {s: round(float(p["qty"]), 6)
                         for s, p in led.get("strategy_positions", {}).items()}}
    if missing:
        out["warning"] = f"无报价, 按成本计: {missing}"
    print(json.dumps(out, ensure_ascii=False))


def cmd_account(a):
    acct = _req("GET", "/v2/account")
    print(json.dumps({k: acct.get(k) for k in
                      ("account_number", "status", "equity", "cash",
                       "buying_power", "trading_blocked")}, indent=2))


def main():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run")
    r.add_argument("--orders", required=True, help="signals.py signal 的输出文件")
    r.add_argument("--date", required=True)
    r.add_argument("--fills-out", help="成交输出 (供 signals.py apply 回写 paper 账本)")
    r.add_argument("--timeout", type=int, default=90, help="单笔订单等待成交秒数")
    r.add_argument("--dry-run", action="store_true")
    r.set_defaults(func=cmd_run)

    e = sub.add_parser("equity")
    e.add_argument("--ledger", required=True, help="state/paper_positions.json")
    e.add_argument("--quotes", required=True)
    e.set_defaults(func=cmd_equity)

    ac = sub.add_parser("account")
    ac.set_defaults(func=cmd_account)

    a = p.parse_args()
    a.func(a)


if __name__ == "__main__":
    main()
