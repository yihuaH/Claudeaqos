#!/usr/bin/env python3
"""
持仓一致性交叉核对 (2026-08-11 用户「做」批准)。

起因: 2026-08-11 MNST 发生 2:1 正股拆股。券商持仓变成 12 股 @ $45.63, 而
`state/positions.json` 仍是 6 股 @ $91.26。因为 `integrations.py` 拉日线用
`adjustment=split`, **引擎的价格历史是拆股调整后的、账本的 entry_price 不是** ——
当晚主跑会算出 MNST 回撤 ≈ −50% → 触发 7% 止损 → 产出一张莫须有的卖单。
而出场卖单属自动类 (不受 semi_auto 约束), **会在无人干预下直接成交**。
当日靠人工晨检发现; 本脚本把这道核对变成确定性闸门。

覆盖的公司行动: 正/反向拆股、股息再投 (DRIP)、并购换股、以及任何"账本漏记成交"。

**只核对、只报告 —— 绝不改账本、绝不产出任何订单** (红线2)。
发现不一致 → daily.py 记 anomaly 并按红线6 停跑, 由用户判断后手工修正。

用法:
  python3 scripts/position_check.py --broker POSITIONS.json \\
      --state state/positions.json [--qty-tol 1e-6] --out CHECK.json

POSITIONS.json 即主跑已有的 `--positions` 输入 (会话从 get_equity_positions 取回),
两种格式都吃:
  - 原始输出 {"data": {"positions": [{symbol, quantity, average_buy_price, ...}]}}
    (**推荐**: 带均价, 可交叉验证成本基是否守恒 —— 这是区分"拆股"与"漏记成交"的关键)
  - 简单映射 {"SYM": {"qty": 12.0, ...}} (降级: 无均价, 只比份额)

判定分类 (仅供人判读, 不驱动任何自动动作):
  split_suspected        份额比接近简单整数比 且 成本基守恒 → 极可能是拆股
  unapplied_fill         差额恰等于券商 intraday_quantity → 当日成交尚未回写
  qty_mismatch           其他份额不符
  broker_only            券商有、账本无
  ledger_only            账本有、券商无
"""
import argparse
import json
import sys


def load(path):
    with open(path) as f:
        return json.load(f)


def parse_broker(path):
    """{SYM: {"qty": float, "avg": float|None, "intraday": float}}"""
    raw = load(path)
    if "data" in raw:
        out = {}
        for p in raw["data"]["positions"]:
            q = float(p["quantity"])
            if abs(q) < 1e-12:
                continue
            avg = p.get("average_buy_price")
            out[p["symbol"]] = {
                "qty": q,
                "avg": float(avg) if avg not in (None, "") else None,
                "intraday": float(p.get("intraday_quantity") or 0),
            }
        return out
    out = {}
    for k, v in raw.items():
        q = float(v["qty"])
        if abs(q) < 1e-12:
            continue
        avg = v.get("avg", v.get("average_buy_price"))
        out[k] = {"qty": q,
                  "avg": float(avg) if avg not in (None, "") else None,
                  "intraday": float(v.get("intraday") or 0)}
    return out


def parse_ledger(path):
    """券商不区分策略仓/存量仓, 故按标的合并两桶。"""
    st = load(path)
    out = {}
    for bucket in ("strategy_positions", "legacy_positions"):
        for sym, p in (st.get(bucket) or {}).items():
            e = out.setdefault(sym, {"qty": 0.0, "cost": 0.0, "buckets": []})
            e["qty"] += float(p["qty"])
            e["cost"] += float(p.get("cost") or 0.0)
            e["buckets"].append(bucket.replace("_positions", ""))
    return out


def simple_ratio(r, max_den=20, tol=2e-3):
    """把份额比 r 认成简单整数比 p/q (2:1 拆股 / 1:10 反向拆股 / 3:2 …)。
    返回 (p, q) 或 None。取 p+q 最小的那个, 避免 199/100 这种伪命中。"""
    if r <= 0:
        return None
    best = None
    for q in range(1, max_den + 1):
        p = round(r * q)
        if p < 1 or p > max_den * max_den:
            continue
        if abs(r - p / q) <= tol * max(1.0, r):
            g = _gcd(p, q)
            p2, q2 = p // g, q // g
            if (p2, q2) == (1, 1):
                continue
            if best is None or (p2 + q2) < (best[0] + best[1]):
                best = (p2, q2)
    return best


def _gcd(a, b):
    while b:
        a, b = b, a % b
    return a


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--broker", required=True, help="券商持仓 JSON (主跑的 --positions 输入)")
    p.add_argument("--state", required=True, help="账本, 通常 state/positions.json")
    p.add_argument("--qty-tol", type=float, default=1e-6,
                   help="份额绝对容差 (券商分数股保留 6 位小数)")
    p.add_argument("--cost-tol-pct", type=float, default=1.0,
                   help="成本基守恒判定的相对容差 %% (拆股应完全守恒, 留 1%% 容四舍五入)")
    p.add_argument("--out")
    a = p.parse_args()

    broker = parse_broker(a.broker)
    ledger = parse_ledger(a.state)

    rows, bad = [], []
    for sym in sorted(set(broker) | set(ledger)):
        b, l = broker.get(sym), ledger.get(sym)
        if b and not l:
            row = {"symbol": sym, "kind": "broker_only", "broker_qty": b["qty"],
                   "ledger_qty": None,
                   "note": "券商有此持仓但账本无 —— 漏记成交? 手工下单? 公司行动派发?"}
            rows.append(row); bad.append(row); continue
        if l and not b:
            row = {"symbol": sym, "kind": "ledger_only", "broker_qty": None,
                   "ledger_qty": l["qty"],
                   "note": "账本有此持仓但券商无 —— 漏记卖出? 已被并购/退市清算?"}
            rows.append(row); bad.append(row); continue

        d = b["qty"] - l["qty"]
        if abs(d) <= a.qty_tol:
            rows.append({"symbol": sym, "kind": "ok", "broker_qty": b["qty"],
                         "ledger_qty": l["qty"]})
            continue

        row = {"symbol": sym, "broker_qty": b["qty"], "ledger_qty": l["qty"],
               "diff": round(d, 6), "broker_avg": b["avg"], "ledger_cost": round(l["cost"], 2),
               "broker_intraday": b["intraday"], "buckets": l["buckets"]}

        # 当日成交尚未回写: 差额恰等于券商 intraday_quantity
        if b["intraday"] and abs(d - b["intraday"]) <= max(a.qty_tol, 1e-6):
            row["kind"] = "unapplied_fill"
            row["note"] = (f"差额 {d:+.6f} 恰等于券商 intraday_quantity —— 当日成交尚未回写账本, "
                           f"先跑 signals.py apply 再重跑")
        else:
            ratio = b["qty"] / l["qty"] if l["qty"] else None
            sr = simple_ratio(ratio) if ratio else None
            cost_ok = None
            if b["avg"] is not None and l["cost"]:
                cost_ok = abs(b["qty"] * b["avg"] - l["cost"]) <= l["cost"] * a.cost_tol_pct / 100.0
            if sr and cost_ok is not False:
                row["kind"] = "split_suspected"
                row["ratio"] = round(ratio, 6)
                row["ratio_simple"] = f"{sr[0]}:{sr[1]}"
                row["cost_basis_preserved"] = cost_ok
                row["suggested_fix"] = {
                    "qty": b["qty"],
                    "entry_price": round(l["cost"] / b["qty"], 4) if b["qty"] else None,
                    "cost": round(l["cost"], 2),
                }
                row["note"] = (f"份额比 {ratio:.4f} ≈ {sr[0]}:{sr[1]}"
                               + (", 成本基守恒" if cost_ok else
                                  ", 成本基未交叉验证 (券商未给均价)" if cost_ok is None else "")
                               + " —— 极可能是拆股。按 suggested_fix 手工修正账本 "
                                 "(份额与均价按比例调整, cost 不变), 核对无误后重跑主跑。"
                                 "**本脚本不会自动改账本**")
            else:
                row["kind"] = "qty_mismatch"
                row["note"] = "份额不符且不像拆股/当日成交 —— 需人工查明原因后再交易"
        rows.append(row); bad.append(row)

    out = {
        "compared": len(rows),
        "matched": sum(1 for r in rows if r["kind"] == "ok"),
        "mismatched": len(bad),
        "kinds": sorted({r["kind"] for r in bad}),
        "anomalies": bad,
        "rows": rows,
    }
    if not rows:
        out["verdict"] = "no_data"
        out["note"] = "券商与账本均无持仓, 无可比对"
    elif bad:
        out["verdict"] = "fail"
        out["note"] = (f"{len(bad)}/{len(rows)} 只标的券商持仓与账本不一致 "
                       f"({', '.join(out['kinds'])}) —— 引擎会基于错误的 entry_price/份额 "
                       f"算出错误的回撤与出场信号, 按红线6 停止交易并通知用户")
    else:
        out["verdict"] = "ok"
        out["note"] = f"{len(rows)}/{len(rows)} 只标的券商持仓与账本完全一致"

    s = json.dumps(out, indent=2, ensure_ascii=False)
    if a.out:
        with open(a.out, "w") as f:
            f.write(s + "\n")
    print(s)
    return 0


if __name__ == "__main__":
    sys.exit(main())
