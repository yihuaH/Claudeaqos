#!/usr/bin/env python3
"""
Alpaca 纸面账户执行器 — 只用于挑战者参数的影子验证。

  run     执行 signals.py 产出的订单 (先卖后买, 市价, 幂等 client_order_id);
          整仓出场自动改走 close-position 接口全量平掉, 不留小数残渣;
          --allow-queue: 收盘后不拒单, 改用 limit/day 挂至次一开盘 (配合 --queued-out)
  sync    次日回收排队单成交 → fills (供 signals.py apply 回写账本), 幂等
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


class OrderRejected(Exception):
    """单笔下单被券商拒绝。**必须逐单捕获, 绝不允许中断整批** —— 2026-08-18~21 转空事故
    的根因就是这里原先 raise SystemExit, 导致 _run_queued 在写排队清单前死掉,
    已提交的卖单次日无从回收, 账本与券商脱钩后引擎重复出卖单直至卖成空头。"""

    def __init__(self, symbol, side, detail):
        super().__init__(f"{symbol} {side}: {detail}")
        self.symbol, self.side, self.detail = symbol, side, detail


def _submit(order, coid):
    # 默认 market/day; order 可覆盖 type/time_in_force (排队模式用 limit/day)
    body = {"type": "market", "time_in_force": "day", **order, "client_order_id": coid}
    try:
        return _req("POST", "/v2/orders", body)
    except urllib.error.HTTPError as e:
        detail = e.read().decode()[:200]
        if e.code == 422 and "client_order_id" in detail:
            return _get_by_coid(coid)  # 重试幂等: 复用已提交的同名订单
        raise OrderRejected(order.get("symbol"), order.get("side"), f"HTTP {e.code} {detail}")


OCC_RE = re.compile(r"^[A-Z.]{1,6}\d{6}[CP]\d{8}$")  # 期权 OCC 符号, 整张合约交易
QUEUE_BUF = 0.03  # 收盘后排队限价缓冲: 买 est×1.03 / 卖 est×0.97, 保证次开成交、兼作极端跳空保护


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
        raise OrderRejected(sym, "sell", f"平仓失败 HTTP {e.code} {e.read().decode()[:200]}")
    return _wait_fill_by_id(closed["id"], timeout_s)


def _wash_conflicts(plan):
    """同一标的同日既有卖单又有买单 → Alpaca 判 potential wash trade, 403 拒掉后提交的那张
    (挑战者 3% 止损砍仓 + 同日 RSI-2 再入场就会撞上; 实盘无此形态)。
    返回冲突标的集合 —— 调用方**保留卖单 (出场优先), 跳过买单**并显式记录。
    注: 冲突集按**原始 plan** 计算, 故卖单即便随后被防转空闸跳过, 同标的买单仍会被跳过 ——
    偏保守 (账本与券商已脱钩时不再加仓于该标的), 有意为之。"""
    sells = {o["symbol"] for side, o in plan if side == "sell"}
    buys = {o["symbol"] for side, o in plan if side == "buy"}
    return sells & buys


def _is_long_only_sell(side, o):
    """本轨道正股为纯多头; 期权 (OCC) 与备兑开仓 (sell_to_open) 是正当空头, 不受防转空闸约束。"""
    return (side == "sell" and not OCC_RE.match(o["symbol"])
            and o.get("position_intent") != "sell_to_open")


def _clamp_sell_qty(sym, want):
    """防转空闸: 卖量不得超过券商实际持仓, 否则卖穿零变空头 (2026-08-18~21 事故的直接杀伤面)。
    返回 (可卖量, 说明); 可卖量 ≤ 0 表示该卖单整单跳过。账本与券商脱钩时以**券商为准**。"""
    held = _position_qty(sym)
    if held is None:
        return 0.0, "券商无持仓 → 卖单跳过 (账本与券商已脱钩, 防转空)"
    if held <= 0:
        return 0.0, f"券商持仓 {held} ≤ 0 → 卖单跳过 (已是空头, 防继续放大)"
    if want > held + FULL_EXIT_EPS:
        return held, f"卖量 {want} > 券商持仓 {held} → 截断至持仓量 (防转空)"
    return want, ""


def _prepare(side, o, conflicts, notes):
    """两闸前置: wash 冲突买单跳过 / 正股卖单按券商持仓截断。返回 None 表示整单跳过。"""
    sym = o["symbol"]
    if side == "buy" and sym in conflicts:
        notes.append(f"{sym} buy: 同日已有卖单 → wash trade 冲突, 买单跳过 (出场优先)")
        return None
    if _is_long_only_sell(side, o):
        okqty, why = _clamp_sell_qty(sym, float(o["qty"]))
        if why:
            notes.append(f"{sym} sell: {why}")
        if okqty <= 0:
            return None
        if okqty != float(o["qty"]):
            return {**o, "qty": okqty}
    return o


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

    clk = _req("GET", "/v2/clock")
    if not clk["is_open"]:
        if not getattr(a, "allow_queue", False):
            raise SystemExit("Alpaca 时钟显示市场未开盘, 拒绝提交 paper 订单 "
                             "(顺延下一交易日; 加 --allow-queue 可排队至次开)")
        return _run_queued(a, plan, clk)

    conflicts = _wash_conflicts(plan)
    fills, warnings, skipped, failed = [], [], [], []
    for side, o0 in plan:
        o = _prepare(side, o0, conflicts, skipped)
        if o is None:
            continue
        sym = o["symbol"]
        coid = f"{a.coid_prefix}-{a.date}-{sym}-{side}"
        try:
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
        except OrderRejected as e:      # 单笔被拒 → 记录后继续跑完整批
            failed.append({"symbol": sym, "side": side, "detail": e.detail})
            continue
        fq = float(done.get("filled_qty") or 0)
        if fq > 0:
            fills.append({"symbol": sym, "side": side, "qty": fq,
                          "price": float(done["filled_avg_price"]),
                          "bucket": o.get("bucket", "strategy"),
                          "reason": o.get("reason", "")})
        else:
            warnings.append(f"{sym} {side}: 未成交 (status={done['status']})")

    out = {"fills": fills, "warnings": warnings, "skipped": skipped, "failed": failed}
    if a.fills_out:
        with open(a.fills_out, "w") as f:
            json.dump(out, f, indent=2, ensure_ascii=False)
            f.write("\n")
    print(json.dumps(out, indent=2, ensure_ascii=False))


def _queue_limit_body(side, o):
    """收盘后排队用 limit/day 单 (盘后任意时段都能挂至次一开盘; market 单 16:00-20:00 ET 会被拒)。
    限价 = est_price × (1 ± QUEUE_BUF): 买 +3% / 卖 -3%, 次开成交概率高且封顶极端跳空。
    买单转整股 (limit 不支持 notional/分数); 买不起 1 整股 → 返回 None 跳过。"""
    sym = o["symbol"]
    est = float(o.get("est_price") or 0)
    if est <= 0:
        raise SystemExit(f"{sym}: 排队模式需 est_price 定限价, 缺失")
    body = {"symbol": sym, "side": side, "type": "limit", "time_in_force": "day"}
    if o.get("position_intent"):
        body["position_intent"] = o["position_intent"]
    if OCC_RE.match(sym):
        lp = round(est * (1.0 + (QUEUE_BUF if side == "buy" else -QUEUE_BUF)), 2)
        body["limit_price"] = str(lp)
        body["qty"] = str(int(o["qty"]))  # 期权整张
    elif side == "sell":
        wq = int(float(o["qty"]))  # limit 不支持分数, 向下取整; 残渣 (<1股) 留待次日市价扫
        if wq < 1:
            return None
        body["limit_price"] = str(round(est * (1.0 - QUEUE_BUF), 2))
        body["qty"] = str(wq)
    else:  # 买 → 整股
        lp = round(est * (1.0 + QUEUE_BUF), 2)
        qty = int(float(o["dollar_amount"]) // lp)
        if qty < 1:
            return None
        body["limit_price"] = str(lp)
        body["qty"] = str(qty)
    return body


def _run_queued(a, plan, clk):
    """市场未开盘且 --allow-queue: 逐单提交 limit/day 挂至次开, 不等成交。
    幂等 (同 coid 重跑复用已挂单); 排队清单写 --queued-out 供次日 sync 回收。"""
    conflicts = _wash_conflicts(plan)
    queued, skipped, failed = [], [], []
    try:
        for side, o0 in plan:
            o = _prepare(side, o0, conflicts, skipped)
            if o is None:
                continue
            sym = o["symbol"]
            coid = f"{a.coid_prefix}-{a.date}-{sym}-{side}"
            body = _queue_limit_body(side, o)
            if body is None:
                skipped.append(f"{sym} {side}: 排队限价下买不起 1 整股, 跳过")
                continue
            try:
                sub_o = _submit(body, coid)
            except OrderRejected as e:  # 单笔被拒 → 记录后继续跑完整批
                failed.append({"symbol": sym, "side": side, "detail": e.detail})
                continue
            queued.append({"coid": coid, "order_id": sub_o.get("id"), "symbol": sym,
                           "side": side, "status": sub_o.get("status"),
                           "limit_price": body.get("limit_price"), "qty": body.get("qty"),
                           "bucket": o.get("bucket", "strategy"), "reason": o.get("reason", "")})
    finally:
        # ⚠️ 排队清单**无论如何都要落盘** —— 已提交的单子只能靠这份清单在次日 sync 回收;
        # 清单丢失 = 成交回不到账本 = 引擎次日重复出单 (2026-08-18~21 卖成空头事故根因)。
        qstate = {"queued_when": "market_closed", "date": a.date, "coid_prefix": a.coid_prefix,
                  "next_open": clk.get("next_open"), "skipped": skipped,
                  "failed": failed, "orders": queued}
        if a.queued_out:
            with open(a.queued_out, "w") as f:
                json.dump(qstate, f, indent=2, ensure_ascii=False)
                f.write("\n")
    if a.fills_out:  # 今日无成交, 写空 fills 让 signals.py apply 干净空跑
        with open(a.fills_out, "w") as f:
            json.dump({"fills": []}, f, indent=2, ensure_ascii=False)
            f.write("\n")
    print(json.dumps({"market": "closed", "mode": "queued_for_next_open",
                      "queued": len(queued), "skipped": skipped, "failed": failed,
                      "next_open": clk.get("next_open"),
                      "queued_out": a.queued_out, "fills": []},
                     indent=2, ensure_ascii=False))


def cmd_sync(a):
    """次日回收排队单: 按 coid 查各单当前状态; 已成交 → 汇成 fills (供 signals.py apply
    回写账本); 终态未成交 (canceled/rejected/expired) → 记 terminal_no_fill; 仍挂 → 保留。
    幂等可反复跑; --prune 会把已终态单从排队清单剔除, 只留仍挂的。"""
    q = load_json(a.queued)
    fills, still, dead = [], [], []
    for rec in q.get("orders", []):
        o = _get_by_coid(rec["coid"])
        st = o.get("status")
        fq = float(o.get("filled_qty") or 0)
        if fq > 0 and st == "filled":
            fills.append({"symbol": rec["symbol"], "side": rec["side"], "qty": fq,
                          "price": float(o["filled_avg_price"]),
                          "bucket": rec.get("bucket", "strategy"),
                          "reason": rec.get("reason", "")})
        elif st in ("canceled", "rejected", "expired"):
            dead.append({"coid": rec["coid"], "symbol": rec["symbol"], "status": st})
        else:
            still.append(rec)
    if a.fills_out:
        with open(a.fills_out, "w") as f:
            json.dump({"fills": fills}, f, indent=2, ensure_ascii=False)
            f.write("\n")
    if a.prune:
        q["orders"] = still
        with open(a.queued, "w") as f:
            json.dump(q, f, indent=2, ensure_ascii=False)
            f.write("\n")
    print(json.dumps({"filled": fills, "still_pending": len(still),
                      "terminal_no_fill": dead}, indent=2, ensure_ascii=False))


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
    r.add_argument("--allow-queue", action="store_true",
                   help="收盘后不拒单, 改用 limit/day 挂至次一开盘 (需订单带 est_price)")
    r.add_argument("--queued-out", help="排队清单输出 (供次日 sync 回收成交)")
    r.set_defaults(func=cmd_run)

    sy = sub.add_parser("sync", help="次日回收排队单成交 → fills (供 signals.py apply)")
    sy.add_argument("--queued", required=True, help="run --allow-queue 产出的排队清单")
    sy.add_argument("--fills-out", help="已成交汇总输出 (供 signals.py apply 回写账本)")
    sy.add_argument("--prune", action="store_true",
                    help="从排队清单剔除已终态 (成交/取消/拒绝/过期) 单, 只留仍挂的")
    sy.set_defaults(func=cmd_sync)

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
