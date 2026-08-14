#!/usr/bin/env python3
"""
周 call 摩擦实测引擎 (RSI-2 × 深ITM 买入 call) — 仅用于 Alpaca paper 账户。

  signal  由 历史K线 + 报价 + 期权链 + 账本 计算今天的期权订单 (paper.py run 输入格式)
  apply   把成交回写账本; --context 传当日 signal 输出以附带 入场快照/出场快照/skip 记录
  report  账本盯市 + 摩擦统计 (点差/成交vs前收mid/模型偏差) + 验证进度 vs go_bar

策略形态 (2026-08 回测定型, 见 journal/2026-08-04-weekly-calls.md):
- 入场信号与实盘 RSI-2 同形: close>SMA200 且 RSI2<10, 按 RSI2 升序取新仓。
- 合约: 行权价 ≤ moneyness×现价 的最高档 (深ITM delta≈0.8), 到期 8-17 日历日最近档;
  点差 gate ≤ max_spread_pct — 超限跳过并记 skip_log (跳过率是 go/no-go 输入)。
- 出场跟正股同形规则 (强势反弹/正股止损/时间止损), **无期权级止损**; DTE≤1 强制平仓防行权。

本轨道唯一目的是实测真实摩擦 vs 回测模型; 纸面盈亏绝不驱动实盘订单 (红线7)。
设计原则: 纯 stdlib、确定性、绝不联网。数据由会话按 playbook §7C 搬运。
"""
import argparse
import json
import math
import statistics
import sys
from datetime import date as _date

import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from signals import (load_json, save_json, parse_historicals, parse_quotes,  # noqa: E402
                     sma, rsi, trading_days_since)
from options_overlay import parse_occ, dte  # noqa: E402

BUCKET = "weekly_calls"

# near_signals 预警门槛 (2026-08-06 用 7 只 × ~242 天实测转化率校准):
# 1 日档 (再跌1%即触发) 转化率 44% — 全收; 2 日档随当前 RSI2 衰减 (<25:28% / 25-40:22% / ≥40:17%),
# 故 2 日档只在 RSI2 < 25 时预警。避免中性标的 (RSI2≈50) 白扣买入力。
NEAR_2D_RSI2_MAX = 25.0
# 预警保留额上限 = 实时 BP 的此比例 (保住立足点, 不让不确定的机会饿死股票策略)
NEAR_RESERVE_BP_CAP = 0.35


# ---------- pricing helpers (仅用于记录模型偏差, 不参与下单决策) ----------

def _ncdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs_call(S, K, T, sig, r=0.04):
    if T <= 1e-9 or sig <= 1e-9:
        return max(S - K, 0.0)
    d1 = (math.log(S / K) + (r + 0.5 * sig * sig) * T) / (sig * math.sqrt(T))
    d2 = d1 - sig * math.sqrt(T)
    return S * _ncdf(d1) - K * math.exp(-r * T) * _ncdf(d2)


def bs_put(S, K, T, sig, r=0.04):
    if T <= 1e-9 or sig <= 1e-9:
        return max(K - S, 0.0)
    d1 = (math.log(S / K) + (r + 0.5 * sig * sig) * T) / (sig * math.sqrt(T))
    d2 = d1 - sig * math.sqrt(T)
    return K * math.exp(-r * T) * _ncdf(-d2) - S * _ncdf(-d1)


def realized_vol(closes, window):
    if len(closes) < window + 1:
        return None
    rets = [math.log(closes[i] / closes[i - 1]) for i in range(-window, 0)]
    return statistics.pstdev(rets) * math.sqrt(252)


def _mid(q):
    b, a = q.get("bid"), q.get("ask")
    if b is None or a is None or b <= 0 or a <= 0 or a < b:
        return None
    return (b + a) / 2.0


def _spread_pct(q):
    m = _mid(q)
    if not m:
        return None
    return (q["ask"] - q["bid"]) / m * 100.0


# ---------- signal ----------

def _pick_contract(sym, spot, chain, cc, today):
    """确定性合约选择: 到期升序 → 同到期行权价降序, 取首个
    [floor_m..moneyness]×spot 区间内 spread/bid/premium 全合规的 call。
    返回 (order_dict, skip_reason)。"""
    target_k = cc["moneyness"] * spot
    floor_k = cc["floor_moneyness"] * spot
    cands = []
    for occ, q in chain.items():
        meta = parse_occ(occ)
        if not meta or meta["type"] != "C":
            continue
        d = dte(meta["expiry"], today)
        if not (cc["min_dte_calendar"] <= d <= cc["max_dte_calendar"]):
            continue
        if not (floor_k <= meta["strike"] <= target_k):
            continue
        cands.append((d, -meta["strike"], occ, meta, q))
    if not cands:
        return None, "no_contract_in_window"
    cands.sort()
    best_spread = None
    for d, _negk, occ, meta, q in cands:
        m = _mid(q)
        if m is None or q["bid"] < cc["min_bid"]:
            continue
        sp = _spread_pct(q)
        best_spread = sp if best_spread is None else min(best_spread, sp)
        if sp > cc["max_spread_pct"]:
            continue
        if m * 100.0 > cc["max_premium_per_contract_usd"]:
            return None, f"premium_too_large({m * 100:.0f})"
        return {"occ": occ, "meta": meta, "quote": q, "mid": m,
                "spread_pct": round(sp, 3), "dte": d}, None
    if best_spread is not None:
        return None, f"spread_too_wide(best={best_spread:.2f}%)"
    return None, "no_valid_quote"


def _pick_vertical(sym, spot, chain, cc, today):
    """牛市价差 (bull call spread, 2026-08-07 用户「直接部署到实盘」授权):
    买腿 = 与单腿同一选法 (深ITM, 过 max_spread_pct gate);
    卖腿 = 同到期、行权价 ≥ short_moneyness×spot 的**最低**档 (最贴近目标), 过 max_short_leg_spread_pct gate
    (虚值腿百分比点差天然更宽, 故单设更松的闸; 回测显示卖腿点差×4 仍优于单腿)。
    净价 = 买腿ask − 卖腿bid (保守: 按最差可成交价估, 实盘组合单通常更好)。
    返回 (order_dict, skip_reason)。"""
    long_pick, skip = _pick_contract(sym, spot, chain, cc, today)
    if long_pick is None:
        return None, skip
    expiry = long_pick["meta"]["expiry"]
    min_short_k = cc["short_moneyness"] * spot
    cands = []
    for occ, q in chain.items():
        meta = parse_occ(occ)
        if not meta or meta["type"] != "C" or meta["expiry"] != expiry:
            continue
        if meta["strike"] < min_short_k or meta["strike"] <= long_pick["meta"]["strike"]:
            continue
        cands.append((meta["strike"], occ, meta, q))
    if not cands:
        return None, f"no_short_leg(需 ≥{min_short_k:.2f} 同到期 {expiry})"
    cands.sort()
    max_sp = float(cc.get("max_short_leg_spread_pct", cc["max_spread_pct"] * 4))
    best_sp = None
    for strike, occ, meta, q in cands:
        m = _mid(q)
        if m is None or q["bid"] < cc["min_bid"]:
            continue
        sp = _spread_pct(q)
        best_sp = sp if best_sp is None else min(best_sp, sp)
        if sp > max_sp:
            continue
        # 净借记: 买腿吃 ask, 卖腿收 bid (最保守口径)
        net = long_pick["quote"]["ask"] - q["bid"]
        if net <= 0:
            continue
        if net * 100.0 > cc["max_premium_per_contract_usd"]:
            return None, f"net_debit_too_large({net * 100:.0f})"
        width = strike - long_pick["meta"]["strike"]
        return {"structure": "vertical_spread", "dte": long_pick["dte"], "expiry": expiry,
                "long": long_pick, "short": {"occ": occ, "meta": meta, "quote": q, "mid": m,
                                             "spread_pct": round(sp, 3)},
                "net_debit": round(net, 2), "mid": round(net, 2),
                "max_value": round(width, 2),  # 到期最大值 = 两腿行权价之差
                "spread_pct": long_pick["spread_pct"]}, None
    if best_sp is not None:
        return None, f"short_leg_spread_too_wide(best={best_sp:.2f}%)"
    return None, "no_valid_short_quote"


def _pick_credit_put(sym, spot, chain, cc, today):
    """牛市看跌信用价差 bull put spread (2026-08-13 用户「直接实盘」授权, 回测依据
    journal/2026-08-13-pcs-bt.md: 同信号卖方 OTM put 价差 6/6 窗口胜买方且回撤减半)。
    卖腿 = 行权价 ≤ short_put_moneyness×spot 的**最高**档 put (贴近目标从下),
      gate: 点差 ≤ max_credit_leg_spread_pct 且 bid ≥ min_bid;
    买腿 (保护) = 同到期、行权价 ≤ long_put_moneyness×spot 的最高档 (< 卖腿),
      gate: 点差 ≤ max_far_leg_spread_pct 且 bid > 0 (远虚值便宜腿, 百分比点差天然更宽)。
    净贷记 = 卖腿bid − 买腿ask (最保守: 卖出吃 bid、买入付 ask)。
    每张风险 (=占用抵押) = 宽度 − 净贷记; 注码/预算/BP 全部按风险计。
    返回 (order_dict, skip_reason)。"""
    target_short = cc["short_put_moneyness"] * spot
    cands = {}
    for occ, q in chain.items():
        meta = parse_occ(occ)
        if not meta or meta["type"] != "P":
            continue
        d = dte(meta["expiry"], today)
        if not (cc["min_dte_calendar"] <= d <= cc["max_dte_calendar"]):
            continue
        cands.setdefault((d, meta["expiry"]), []).append((meta["strike"], occ, meta, q))
    if not cands:
        return None, "no_put_in_window"
    best_sp = None
    for (d, expiry) in sorted(cands):
        rows = sorted(cands[(d, expiry)], reverse=True)   # 行权价降序
        shorts = [r for r in rows if r[0] <= target_short]
        if not shorts:
            continue
        s_strike, s_occ, s_meta, s_q = shorts[0]          # ≤0.97×spot 最高档
        sm = _mid(s_q)
        if sm is None or s_q["bid"] < cc["min_bid"]:
            continue
        ssp = _spread_pct(s_q)
        best_sp = ssp if best_sp is None else min(best_sp, ssp)
        # 双档闸: 百分比 或 绝对点差 (美元/股) 过其一即可 — OTM put 权利金常只有几毛,
        # 纯百分比闸对便宜腿失灵 (8% of $0.35 = 3¢ 不现实); 绝对档 = 回测盈亏假设的口径
        # (pcs-bt 的 ab≤$0.10/股/腿 才稳定为正), 把成交约束进回测验证过的摩擦包络。
        s_abs = s_q["ask"] - s_q["bid"]
        if (ssp > float(cc["max_credit_leg_spread_pct"])
                and s_abs > float(cc.get("max_credit_leg_spread_abs", 0.10))):
            continue
        target_long = cc["long_put_moneyness"] * spot
        longs = [r for r in rows if r[0] <= target_long and r[0] < s_strike]
        max_far = float(cc.get("max_far_leg_spread_pct",
                               cc["max_credit_leg_spread_pct"] * 2))
        for l_strike, l_occ, l_meta, l_q in longs:        # 已降序 = 贴近 0.88 从下
            lm = _mid(l_q)
            if lm is None or (l_q["bid"] or 0) <= 0:
                continue
            lsp = _spread_pct(l_q)
            l_abs = l_q["ask"] - l_q["bid"]
            if lsp > max_far and l_abs > float(cc.get("max_far_leg_spread_abs", 0.10)):
                continue
            credit = s_q["bid"] - l_q["ask"]              # 最保守可成交净贷记
            if credit <= 0:
                continue
            width = s_strike - l_strike
            risk = width - credit
            if risk <= 0:
                continue
            if risk * 100.0 > cc["max_premium_per_contract_usd"]:
                return None, f"risk_too_large({risk * 100:.0f})"
            return {"structure": "credit_put_spread", "dte": d, "expiry": expiry,
                    "short": {"occ": s_occ, "meta": s_meta, "quote": s_q, "mid": sm,
                              "spread_pct": round(ssp, 3)},
                    "long": {"occ": l_occ, "meta": l_meta, "quote": l_q, "mid": lm,
                             "spread_pct": round(lsp, 3)},
                    "net_credit": round(credit, 2),
                    "mid": round(risk, 2),   # mid=每股风险 → cmd_signal 的注码/预算/BP 按风险计
                    "max_value": round(width, 2),
                    "spread_pct": round(ssp, 3)}, None
        # 本到期无合格买腿 → 试更远到期
    if best_sp is not None:
        return None, f"credit_leg_spread_too_wide(best={best_sp:.2f}%)"
    return None, "no_valid_put_quote"


def _pick_structure(sym, spot, chain, cc, today):
    """按配置 contract.structure 分派: 'single' (默认) / 'vertical_spread' / 'credit_put_spread'。"""
    if cc.get("structure") == "vertical_spread":
        return _pick_vertical(sym, spot, chain, cc, today)
    if cc.get("structure") == "credit_put_spread":
        return _pick_credit_put(sym, spot, chain, cc, today)
    return _pick_contract(sym, spot, chain, cc, today)


def cmd_signal(a):
    cfg = load_json(a.config)
    ledger = load_json(a.ledger)
    hist = parse_historicals(a.bars)
    quotes = parse_quotes(a.quotes)
    chains = load_json(a.chains) if a.chains else {}
    earnings = load_json(a.earnings) if a.earnings else None
    today = a.date
    warnings, skips = [], []
    out = {"date": today, "halted": False, "sells": [], "buys": [],
           "skips": skips, "warnings": warnings}

    if not cfg.get("enabled"):
        out["note"] = "weekly_calls.json enabled=false, 空跑"
        return _emit(out, a.out)
    if ledger.get("halted"):
        out["halted"] = True
        out["note"] = "账本 halted=true, 只读"
        return _emit(out, a.out)

    # 与 signals.py 同法: 实时报价补今天的临时收盘
    series = {}
    for sym, bars in hist.items():
        dates = [d for d, _ in bars if d < today]
        closes = [c for d, c in bars if d < today]
        if sym in quotes:
            dates.append(today)
            closes.append(quotes[sym])
        series[sym] = (dates, closes)

    ind = {}
    for sym, (dates, closes) in series.items():
        ind[sym] = {"close": closes[-1] if closes else None,
                    "sma5": sma(closes, 5), "sma200": sma(closes, 200),
                    "rsi2": rsi(closes, 2),
                    "rv": realized_vol(closes, int(cfg["model"]["rv_window_days"]))}

    ex = cfg["exit"]
    cc = cfg["contract"]
    positions = ledger.get("positions", {})

    # --- 出场 (跟正股同形规则, 无期权级止损) ---
    exiting_und = set()
    for occ, pos in sorted(positions.items()):
        u = pos["underlying"]
        i = ind.get(u)
        reason = None
        d = dte(pos["expiry"], today)
        if d < 0:
            warnings.append(f"{occ}: 已过期未平仓 (expiry {pos['expiry']}), "
                            "疑似被自动行权/交割 — 停该仓操作, 人工对账 (红线6)")
            continue
        if d <= int(ex["force_exit_dte_lte"]):
            reason = "expiry_close"
        elif i and i["rsi2"] is not None and i["close"] is not None:
            if i["close"] <= float(pos["entry_underlying"]) * (1 - ex["underlying_stop_loss_pct"] / 100.0):
                reason = "underlying_stop"
            elif (i["sma5"] is not None and i["close"] > i["sma5"]) or i["rsi2"] >= ex["rsi2_min"]:
                reason = "exit_strength"
            elif trading_days_since(series[u][0], pos["entry_date"]) >= int(ex["max_holding_days"]):
                reason = "time_stop"
        else:
            warnings.append(f"{occ}: 底层 {u} 指标数据不足, 跳过出场检查 (DTE={d})")
        if not reason:
            continue
        if pos.get("structure") == "credit_put_spread":
            # 平仓 = 买回卖腿 + 卖出买腿 (direction=debit)。属风险减少的出场类,
            # 沿用 4A/4D 出场全自动语义; 若平台分类器把 buy_to_close 当买入拦截
            # (首个出场将实测), 按红线6 记 journal + PushNotification 用户人工触发。
            sq = (chains.get(u) or {}).get(occ) or {}
            lq = (chains.get(u) or {}).get(pos["long_occ"]) or {}
            sask, lbid = sq.get("ask"), lq.get("bid")
            if sask is None or lbid is None:
                spot_now = (ind.get(u) or {}).get("close") or pos["strike"]
                sask = max(pos["strike"] - spot_now, 0.01) if sask is None else sask
                lbid = max(pos["long_strike"] - spot_now, 0.0) if lbid is None else lbid
                warnings.append(f"{occ}: 信用价差平仓缺腿报价, est 按内在价值兜底")
            net = round(max(float(sask) - float(lbid), 0.01), 2)
            exiting_und.add(u)
            out["sells"].append({
                "structure": "credit_put_spread", "qty": int(pos["contracts"]),
                "bucket": BUCKET, "reason": reason, "direction": "debit",
                "est_price": net, "underlying": u, "expiry": pos["expiry"],
                "legs": [
                    {"symbol": occ, "side": "buy", "position_effect": "close",
                     "strike": pos["strike"], "quote": sq},
                    {"symbol": pos["long_occ"], "side": "sell", "position_effect": "close",
                     "strike": pos["long_strike"], "quote": lq},
                ],
                "exit_quote": {"short_ask": sask, "long_bid": lbid, "net_debit": net,
                               "mid": (round(_mid(sq) - _mid(lq), 4)
                                       if _mid(sq) is not None and _mid(lq) is not None else None),
                               "spread_pct": round(_spread_pct(sq), 3) if _spread_pct(sq) else None,
                               "long_spread_pct": round(_spread_pct(lq), 3) if _spread_pct(lq) else None},
            })
            continue
        q = (chains.get(u) or {}).get(occ) or {}
        m = _mid(q)
        if m is None:  # 无链报价 → 内在价值兜底定限价
            spot = (ind.get(u) or {}).get("close")
            m = max((spot or pos["strike"]) - pos["strike"], 0.01)
            warnings.append(f"{occ}: 无链报价, est_price 按内在价值 {m:.2f} 兜底")
        exiting_und.add(u)
        if pos.get("structure") == "vertical_spread":
            # 价差平仓: 卖出买腿 + 买回卖腿, 一笔组合单 (net credit)
            sq = (chains.get(u) or {}).get(pos["short_occ"]) or {}
            lq = q
            lbid = lq.get("bid")
            sask = sq.get("ask")
            if lbid is None or sask is None:
                spot = (ind.get(u) or {}).get("close") or pos["strike"]
                lbid = max(spot - pos["strike"], 0.01) if lbid is None else lbid
                sask = max(spot - pos["short_strike"], 0.01) if sask is None else sask
                warnings.append(f"{occ}: 价差平仓缺腿报价, est 按内在价值兜底")
            net = round(max(float(lbid) - float(sask), 0.01), 2)
            out["sells"].append({
                "structure": "vertical_spread", "qty": int(pos["contracts"]),
                "bucket": BUCKET, "reason": reason, "direction": "credit",
                "est_price": net, "underlying": u, "expiry": pos["expiry"],
                "legs": [
                    {"symbol": occ, "side": "sell", "position_effect": "close",
                     "strike": pos["strike"], "quote": lq},
                    {"symbol": pos["short_occ"], "side": "buy", "position_effect": "close",
                     "strike": pos["short_strike"], "quote": sq},
                ],
                "exit_quote": {"long_bid": lbid, "short_ask": sask, "net_credit": net,
                               "long_spread_pct": round(_spread_pct(lq), 3) if _spread_pct(lq) else None,
                               "short_spread_pct": round(_spread_pct(sq), 3) if _spread_pct(sq) else None},
            })
            continue
        out["sells"].append({
            "symbol": occ, "qty": int(pos["contracts"]),
            "position_intent": "sell_to_close", "bucket": BUCKET,
            "reason": reason, "est_price": round(m, 2),
            "exit_quote": {"bid": q.get("bid"), "ask": q.get("ask"),
                           "mid": round(m, 4) if _mid(q) else None,
                           "spread_pct": round(_spread_pct(q), 3) if _spread_pct(q) else None},
        })

    # --- 轨道级熔断: 累计已实现亏损超限 → 只出不进 ---
    # 阈值支持绝对额 (usd) 或账户百分比 (pct_of_portfolio, 需 --portfolio-value)
    pv = float(a.portfolio_value) if getattr(a, "portfolio_value", None) is not None else None
    cum_real = sum(rt.get("pnl_usd", 0.0) for rt in ledger.get("round_trips", []))
    cbc = cfg["circuit_breaker"]
    cb = cbc.get("max_cumulative_realized_loss_usd")
    if cb is None and cbc.get("max_cumulative_realized_loss_pct_of_portfolio") is not None:
        if pv is None:
            raise SystemExit("熔断配置为百分比但未传 --portfolio-value")
        cb = pv * float(cbc["max_cumulative_realized_loss_pct_of_portfolio"]) / 100.0
    cb = float(cb)
    if cum_real <= -cb:
        out["note"] = (f"轨道熔断: 累计已实现 {cum_real:.0f} ≤ -{cb:.0f}, "
                       "停止新入场 (出场照常), 等用户决定")
        return _emit(out, a.out)

    # --- 入场 (RSI-2 同形) ---
    def days_to_earnings(sym):
        if not earnings or earnings.get(sym) is None:
            return None
        try:
            return (_date.fromisoformat(earnings[sym]) - _date.fromisoformat(today)).days
        except ValueError:
            return None

    # 实盘硬顶 (红线3, 上限不是建议): budget = 未平仓权利金总额封顶, 支持绝对额 (usd) 或
    # 账户百分比 (pct_of_portfolio × --portfolio-value, 2026-08-04 用户定 40%, 随净值自动伸缩);
    # --buying-power = 实时购买力封顶。paper 配置无 budget 段则不启用。
    bud = cfg.get("budget") or {}
    bud_cap = bud.get("max_open_premium_usd")
    if bud_cap is None and bud.get("max_open_premium_pct_of_portfolio") is not None:
        if pv is None:
            raise SystemExit("budget 配置为百分比但未传 --portfolio-value")
        bud_cap = pv * float(bud["max_open_premium_pct_of_portfolio"]) / 100.0
    # 敞口口径: debit 仓 = 已付权利金; credit 仓 = 每张风险 (宽度−贷记, 即占用抵押)
    open_prem = sum(float(p.get("risk_per_contract") or p["entry_premium"]) * 100
                    * int(p["contracts"]) for p in positions.values())
    bp_left = float(a.buying_power) if getattr(a, "buying_power", None) is not None else None
    spent = 0.0

    held_und = {p["underlying"] for p in positions.values()} - exiting_und
    cands = []
    for sym in cfg["universe"]:
        i = ind.get(sym)
        if not i or sym in held_und or i["sma200"] is None or i["rsi2"] is None:
            continue
        if not (i["close"] > i["sma200"] and i["rsi2"] < cfg["entry"]["rsi2_max"]):
            continue
        ed = days_to_earnings(sym)
        if ed is not None and 0 <= ed <= int(cfg["defense"]["earnings_blackout_days"]):
            skips.append({"symbol": sym, "reason": f"earnings_blackout(dte={ed})",
                          "rsi2": round(i["rsi2"], 2)})
            continue
        cands.append((i["rsi2"], sym))
    cands.sort()

    # 仓数限制可选 (缺省=不限, 2026-08-05 用户指示; 总敞口由 budget+BP 双硬顶管)
    mop = int(cfg["sizing"].get("max_open_positions", 10**6))
    mne = int(cfg["sizing"].get("max_new_entries_per_day", 10**6))
    slots = max(0, min(mop - len(held_und), mne))
    for rsi2v, sym in cands:
        if len(out["buys"]) >= slots:
            break
        spot = ind[sym]["close"]
        pick, skip = _pick_structure(sym, spot, chains.get(sym) or {}, cc, today)
        if pick is None:
            skips.append({"symbol": sym, "reason": skip or "no_chain",
                          "rsi2": round(rsi2v, 2), "spot": round(spot, 2)})
            continue
        # 张数: position_pct_of_portfolio (每信号≈净值×N%, 2026-08-05 D20 回测采纳全凯利档)
        # 优先; 缺省回落到固定 contracts_per_position (paper 摩擦实测用)。
        per_cost = pick["mid"] * 100.0
        pos_pct = (cfg.get("sizing") or {}).get("position_pct_of_portfolio")
        if pos_pct is not None:
            if pv is None:
                raise SystemExit("sizing 配置为百分比但未传 --portfolio-value")
            target = pv * float(pos_pct) / 100.0
            qty = int(target // per_cost)
            if qty < 1:
                if per_cost <= 2 * target:  # 贵档例外: 单张 ≤ 2×目标仓 (如 IWM) 允许 1 张
                    qty = 1
                else:
                    skips.append({"symbol": sym, "reason": f"premium_exceeds_position_cap"
                                  f"(per={per_cost:.0f},target={target:.0f})",
                                  "rsi2": round(rsi2v, 2)})
                    continue
        else:
            qty = int(cfg["sizing"]["contracts_per_position"])
        cost = per_cost * qty
        if bud_cap is not None and open_prem + spent + cost > float(bud_cap):
            skips.append({"symbol": sym, "reason": f"budget_exceeded(cost={cost:.0f},"
                          f"cap={float(bud_cap):.0f},open={open_prem + spent:.0f})",
                          "rsi2": round(rsi2v, 2)})
            continue
        if bp_left is not None and spent + cost > bp_left:
            skips.append({"symbol": sym, "reason": f"insufficient_buying_power"
                          f"(cost={cost:.0f},bp_left={bp_left - spent:.0f})",
                          "rsi2": round(rsi2v, 2)})
            continue
        spent += cost
        rv = ind[sym]["rv"]
        iv = max(0.15, min(1.5, (rv or 0.30) * float(cfg["model"]["iv_rv_mult"])))
        if pick.get("structure") == "credit_put_spread":
            sm_, lm_ = pick["short"]["meta"], pick["long"]["meta"]
            rf = float(cfg["model"]["risk_free"])
            model = (bs_put(spot, sm_["strike"], pick["dte"] / 365.0, iv, rf)
                     - bs_put(spot, lm_["strike"], pick["dte"] / 365.0, iv, rf))
            out["buys"].append({
                "structure": "credit_put_spread", "qty": qty, "bucket": BUCKET,
                "reason": "rsi2_entry_put_credit",
                "legs": [
                    {"symbol": pick["short"]["occ"], "side": "sell", "position_effect": "open",
                     "strike": sm_["strike"], "quote": pick["short"]["quote"],
                     "spread_pct": pick["short"]["spread_pct"]},
                    {"symbol": pick["long"]["occ"], "side": "buy", "position_effect": "open",
                     "strike": lm_["strike"], "quote": pick["long"]["quote"],
                     "spread_pct": pick["long"]["spread_pct"]},
                ],
                "est_price": pick["net_credit"], "direction": "credit",
                "underlying": sym, "expiry": pick["expiry"], "dte": pick["dte"],
                "spot": round(spot, 4), "rsi2": round(rsi2v, 2),
                "model_price": round(model, 4),
                "entry_quote": {"mid": round(pick["short"]["mid"] - pick["long"]["mid"], 4),
                                "spread_pct": pick["short"]["spread_pct"],
                                "long_spread_pct": pick["long"]["spread_pct"],
                                "net_credit_conservative": pick["net_credit"]},
                "risk_per_contract": round(pick["mid"], 2),
                "max_value_per_contract": pick["max_value"],
                "max_loss_usd": round(pick["mid"] * 100 * qty, 2),
                "max_gain_usd": round(pick["net_credit"] * 100 * qty, 2),
            })
            continue
        if pick.get("structure") == "vertical_spread":
            lm, sm = pick["long"]["meta"], pick["short"]["meta"]
            model = (bs_call(spot, lm["strike"], pick["dte"] / 365.0, iv, float(cfg["model"]["risk_free"]))
                     - bs_call(spot, sm["strike"], pick["dte"] / 365.0, iv, float(cfg["model"]["risk_free"])))
            out["buys"].append({
                "structure": "vertical_spread", "qty": qty, "bucket": BUCKET,
                "reason": "rsi2_entry_spread",
                "legs": [
                    {"symbol": pick["long"]["occ"], "side": "buy", "position_effect": "open",
                     "strike": lm["strike"], "quote": pick["long"]["quote"],
                     "spread_pct": pick["long"]["spread_pct"]},
                    {"symbol": pick["short"]["occ"], "side": "sell", "position_effect": "open",
                     "strike": sm["strike"], "quote": pick["short"]["quote"],
                     "spread_pct": pick["short"]["spread_pct"]},
                ],
                "est_price": pick["net_debit"], "direction": "debit",
                "underlying": sym, "expiry": pick["expiry"], "dte": pick["dte"],
                "spot": round(spot, 4), "rsi2": round(rsi2v, 2),
                "model_price": round(model, 4),
                "max_value_per_contract": pick["max_value"],
                "max_loss_usd": round(pick["net_debit"] * 100 * qty, 2),
                "max_gain_usd": round((pick["max_value"] - pick["net_debit"]) * 100 * qty, 2),
            })
            continue
        model = bs_call(spot, pick["meta"]["strike"], pick["dte"] / 365.0, iv,
                        float(cfg["model"]["risk_free"]))
        out["buys"].append({
            "symbol": pick["occ"], "qty": qty,
            "position_intent": "buy_to_open", "bucket": BUCKET,
            "reason": "rsi2_entry_call", "est_price": round(pick["mid"], 2),
            "underlying": sym, "strike": pick["meta"]["strike"],
            "expiry": pick["meta"]["expiry"], "dte": pick["dte"],
            "spot": round(spot, 4), "rsi2": round(rsi2v, 2),
            "model_price": round(model, 4),
            "entry_quote": {"bid": pick["quote"]["bid"], "ask": pick["quote"]["ask"],
                            "mid": round(pick["mid"], 4),
                            "spread_pct": pick["spread_pct"]},
        })

    # --- 期权预警 near_signals (报告级, 2026-08-05 用户批准「条件性弹药预留」) ---
    # 情景规则 (确定性): 明日收跌 1% (或连续两日各跌 ~1%) 将使 RSI2 < rsi2_max 且价格仍在
    # SMA200 上方 → 未来 1-2 天可能出现期权买入信号。仅提示: 4B 据此在股票 pending 写
    # option_alert 建议保留弹药, 4C 执行时按其保留 BP; 绝不改引擎选股/金额 (红线2)。
    held_all = {p["underlying"] for p in positions.values()}
    buy_und = {b["underlying"] for b in out["buys"]}
    near = []
    for sym in cfg["universe"]:
        if sym in held_all or sym in buy_und:
            continue
        i = ind.get(sym)
        if not i or i["rsi2"] is None or i["sma200"] is None or i["close"] is None:
            continue
        if i["rsi2"] < cfg["entry"]["rsi2_max"]:
            continue  # 已是当日信号 (未成单原因见 skips)
        ed = days_to_earnings(sym)
        if ed is not None and 0 <= ed <= int(cfg["defense"]["earnings_blackout_days"]):
            continue
        px = i["close"]
        closes_now = series[sym][1]
        scenario = None
        r1 = rsi(closes_now + [px * 0.99], 2)
        if r1 is not None and r1 < cfg["entry"]["rsi2_max"] and px * 0.99 > i["sma200"]:
            scenario = "1d_-1%"
        elif i["rsi2"] < NEAR_2D_RSI2_MAX:
            # 2 日档转化率随当前 RSI2 急剧衰减 (2026-08-06 实测: <25 → 28%; 25-40 → 22%;
            # ≥40 → 17% 近噪音)。只在已明显偏弱时才预警, 避免中性标的白扣弹药。
            r2d = rsi(closes_now + [px * 0.99, px * 0.9801], 2)
            if r2d is not None and r2d < cfg["entry"]["rsi2_max"] and px * 0.9801 > i["sma200"]:
                scenario = "2d_-1%x2"
        if scenario:
            # 预估单张成本: 用 Black-Scholes + 该标的实测波动率, **按当前配置的形态算**
            # (2026-08-07 修正: 原为固定 10.5%×现价的单腿口径, 切价差后会超额扣弹药 —
            #  价差净借记约为深ITM单腿的一半)。仅用于建议保留额, 不参与下单。
            rvn = i.get("rv")
            ivn = max(0.15, min(1.5, (rvn or 0.30) * float(cfg["model"]["iv_rv_mult"])))
            tn = (int(cc["min_dte_calendar"]) + int(cc["max_dte_calendar"])) / 2.0 / 365.0
            rf = float(cfg["model"]["risk_free"])
            if cc.get("structure") == "credit_put_spread":
                # credit 形态需要预留的是抵押 = 宽度 − 模型净贷记 (每股风险)
                mc = (bs_put(px, cc["short_put_moneyness"] * px, tn, ivn, rf)
                      - bs_put(px, cc["long_put_moneyness"] * px, tn, ivn, rf))
                est = (cc["short_put_moneyness"] - cc["long_put_moneyness"]) * px - mc
            else:
                est = bs_call(px, cc["moneyness"] * px, tn, ivn, rf)
                if cc.get("structure") == "vertical_spread":
                    est -= bs_call(px, cc["short_moneyness"] * px, tn, ivn, rf)
            near.append({"symbol": sym, "rsi2_now": round(i["rsi2"], 2),
                         "scenario": scenario, "spot": round(px, 2),
                         "structure": cc.get("structure", "single"),
                         "est_premium_usd": max(round(est * 100.0), 1)})
    # 建议保留额 (确定性): 最便宜预警标的的**单张**权利金, 且不超过实时 BP 的 NEAR_RESERVE_BP_CAP。
    # 只保 1 张而非整个 D20 仓位 — 预警转化率 28-44%, 为不确定的机会扣住整仓会饿死股票策略;
    # 留住"至少买得起 1 张"的立足点, 信号真来时引擎按实时 BP 自然决定张数。
    near.sort(key=lambda x: x["est_premium_usd"])
    reserve = 0.0
    if near:
        cheapest = float(near[0]["est_premium_usd"])
        cap = float(bp_left) * NEAR_RESERVE_BP_CAP if bp_left else cheapest
        reserve = round(min(cheapest, cap), 2)
    out["near_signals"] = near
    out["suggested_reserve_usd"] = reserve
    _emit(out, a.out)


def _emit(out, path):
    s = json.dumps(out, indent=2, ensure_ascii=False)
    if path:
        with open(path, "w") as f:
            f.write(s + "\n")
    print(s)


# ---------- apply ----------

def cmd_apply(a):
    ledger = load_json(a.ledger)
    fills = load_json(a.fills)
    ctx = load_json(a.context) if a.context else {}
    def _key(o):
        """价差单以**首腿 OCC** 为仓位主键 (vertical=买腿, credit_put=卖腿; 与 fills 约定一致)。"""
        if o.get("structure") in ("vertical_spread", "credit_put_spread"):
            return o["legs"][0]["symbol"]
        return o["symbol"]
    ctx_buys = {_key(o): o for o in ctx.get("buys", [])}
    ctx_sells = {_key(o): o for o in ctx.get("sells", [])}
    positions = ledger.setdefault("positions", {})
    today = a.date

    for f in fills["fills"]:
        occ, side = f["symbol"], f["side"]
        qty, price = int(float(f["qty"])), float(f["price"])
        meta = parse_occ(occ)
        if not meta:
            raise SystemExit(f"非期权成交混入 weekly_calls apply: {occ}")
        if side == "buy":
            c = ctx_buys.get(occ, {})
            rec = {
                "underlying": meta["underlying"], "strike": meta["strike"],
                "expiry": meta["expiry"], "contracts": qty,
                "entry_premium": price, "entry_date": today,
                "entry_underlying": c.get("spot"), "entry_rsi2": c.get("rsi2"),
                "entry_quote": c.get("entry_quote"), "model_price": c.get("model_price"),
            }
            # 价差: price = 净借记; 额外存卖腿, 供出场时组同一笔组合单
            if f.get("structure") == "vertical_spread" or c.get("structure") == "vertical_spread":
                short_occ = f.get("short_symbol") or (c.get("legs") or [{}, {}])[1].get("symbol")
                smeta = parse_occ(short_occ) if short_occ else None
                if not smeta:
                    raise SystemExit(f"价差成交缺卖腿 OCC: {occ} (人工对账)")
                rec.update({"structure": "vertical_spread", "short_occ": short_occ,
                            "short_strike": smeta["strike"],
                            "max_value_per_contract": c.get("max_value_per_contract"),
                            "entry_legs": c.get("legs")})
            # 信用 put 价差: 主键 = 卖腿 OCC, price = 净贷记; 存保护买腿 + 每张风险
            elif (f.get("structure") == "credit_put_spread"
                  or c.get("structure") == "credit_put_spread"):
                long_occ = f.get("long_symbol") or (c.get("legs") or [{}, {}])[1].get("symbol")
                lmeta = parse_occ(long_occ) if long_occ else None
                if not lmeta:
                    raise SystemExit(f"信用价差成交缺保护腿 OCC: {occ} (人工对账)")
                risk_pc = f.get("risk_per_contract") or c.get("risk_per_contract")
                if risk_pc is None:
                    risk_pc = round(meta["strike"] - lmeta["strike"] - price, 2)
                rec.update({"structure": "credit_put_spread", "long_occ": long_occ,
                            "long_strike": lmeta["strike"],
                            "risk_per_contract": float(risk_pc),
                            "max_value_per_contract": c.get("max_value_per_contract"),
                            "entry_legs": c.get("legs")})
            positions[occ] = rec
        else:
            pos = positions.pop(occ, None)
            if pos is None:
                raise SystemExit(f"卖出无持仓的合约: {occ} (人工对账)")
            c = ctx_sells.get(occ, {})
            ep = float(pos["entry_premium"])
            eq = pos.get("entry_quote") or {}
            xq = c.get("exit_quote") or {}
            if pos.get("structure") == "credit_put_spread":
                # credit: 开仓收 ep, 平仓付 price → 盈亏 = ep − price; 百分比按每张风险归一
                rk = float(pos.get("risk_per_contract") or 0)
                pnl_usd = round((ep - price) * 100.0 * qty, 2)
                pnl_pct = round((ep - price) / rk * 100.0, 2) if rk else None
            else:
                pnl_usd = round((price - ep) * 100.0 * qty, 2)
                pnl_pct = round((price / ep - 1) * 100.0, 2) if ep else None
            ledger.setdefault("round_trips", []).append({
                "occ": occ, "structure": pos.get("structure", "single"),
                "short_occ": pos.get("short_occ"), "long_occ": pos.get("long_occ"),
                "underlying": meta["underlying"],
                "entry_date": pos["entry_date"], "exit_date": today,
                "entry_premium": ep, "exit_premium": price,
                "pnl_pct": pnl_pct,
                "pnl_usd": pnl_usd,
                "reason": f.get("reason", c.get("reason", "")),
                "entry_spread_pct": eq.get("spread_pct"),
                "exit_spread_pct": xq.get("spread_pct"),
                "entry_mid": eq.get("mid"), "exit_mid": xq.get("mid"),
                "model_price": pos.get("model_price"),
            })
        ledger.setdefault("trades", []).append({
            "date": today, "symbol": occ, "side": side, "qty": qty,
            "price": price, "bucket": BUCKET, "reason": f.get("reason", ""),
        })

    # skip_log: 摩擦实测的一部分 (被 spread gate 拦掉多少机会)。
    # 幂等: 排队日与次日回收会用同一 context 各 apply 一次, 按 (date,symbol,reason) 去重。
    slog = ledger.setdefault("skip_log", [])
    seen = {(s.get("date"), s.get("symbol"), s.get("reason")) for s in slog}
    for s in ctx.get("skips", []):
        rec = {"date": ctx.get("date", today), **s}
        key = (rec.get("date"), rec.get("symbol"), rec.get("reason"))
        if key not in seen:
            slog.append(rec)
            seen.add(key)

    save_json(a.ledger, ledger)
    print(f"weekly_calls 账本已更新: {len(fills['fills'])} 笔成交, "
          f"{len(ctx.get('skips', []))} 条 skip")


# ---------- report ----------

def _median(xs):
    xs = [x for x in xs if x is not None]
    return round(statistics.median(xs), 3) if xs else None


def cmd_report(a):
    cfg = load_json(a.config)
    ledger = load_json(a.ledger)
    chains = load_json(a.chains) if a.chains else {}
    today = a.date
    rts = ledger.get("round_trips", [])
    positions = ledger.get("positions", {})

    open_mtm = []
    for occ, pos in sorted(positions.items()):
        q = (chains.get(pos["underlying"]) or {}).get(occ) or {}
        if pos.get("structure") == "credit_put_spread":
            # 保守盯市: 平仓成本 = 卖腿ask − 保护腿bid; 浮盈 = 收到贷记 − 平仓成本
            lq = (chains.get(pos["underlying"]) or {}).get(pos["long_occ"]) or {}
            mark = None
            if q.get("ask") is not None and lq.get("bid") is not None:
                mark = round(max(float(q["ask"]) - float(lq["bid"]), 0.0), 2)
            upnl = (round((pos["entry_premium"] - mark) * 100 * pos["contracts"], 2)
                    if mark is not None else None)
            open_mtm.append({"occ": occ, "structure": "credit_put_spread",
                             "entry_premium": pos["entry_premium"],
                             "entry_date": pos["entry_date"], "mark_close_cost": mark,
                             "unrealized_usd": upnl, "dte": dte(pos["expiry"], today)})
            continue
        mark = q.get("bid")  # 保守: 按 bid 盯市
        upnl = round((mark - pos["entry_premium"]) * 100 * pos["contracts"], 2) if mark else None
        open_mtm.append({"occ": occ, "entry_premium": pos["entry_premium"],
                         "entry_date": pos["entry_date"], "mark_bid": mark,
                         "unrealized_usd": upnl, "dte": dte(pos["expiry"], today)})

    pnls = [rt["pnl_pct"] for rt in rts if rt.get("pnl_pct") is not None]
    legs = ([rt.get("entry_spread_pct") for rt in rts]
            + [rt.get("exit_spread_pct") for rt in rts]
            + [(p.get("entry_quote") or {}).get("spread_pct") for p in positions.values()])
    # 成交 vs 信号时 mid: 含隔夜漂移, 只作参考; 模型偏差检验回测 IV 假设
    slip = [round((rt["entry_premium"] / rt["entry_mid"] - 1) * 100, 2)
            for rt in rts if rt.get("entry_mid")]
    mdev = [round((rt["entry_mid"] / rt["model_price"] - 1) * 100, 2)
            for rt in rts if rt.get("model_price") and rt.get("entry_mid")]

    v = cfg["validation"]
    started = v["start_date"]
    days_run = (_date.fromisoformat(today) - _date.fromisoformat(started)).days
    n = len(pnls)
    med_leg = _median(legs)
    med_pnl = _median(pnls)
    mature = n >= int(v["min_round_trips"]) or days_run >= int(v["min_trading_days"]) * 1.5
    verdict = "in_progress"
    if mature and n >= 5:
        ok_spread = med_leg is not None and med_leg <= float(v["go_bar"]["max_median_leg_spread_pct"])
        ok_pnl = med_pnl is not None and med_pnl > float(v["go_bar"]["min_median_trade_pnl_pct"])
        verdict = "GO_candidate" if (ok_spread and ok_pnl) else "NO_GO"

    out = {
        "date": today, "round_trips": n,
        "cum_realized_usd": round(sum(rt.get("pnl_usd", 0) for rt in rts), 2),
        "win_rate_pct": round(100 * sum(1 for x in pnls if x > 0) / n, 1) if n else None,
        "median_pnl_pct": med_pnl, "mean_pnl_pct": _median(pnls) if n < 2 else round(statistics.mean(pnls), 2),
        "median_leg_spread_pct": med_leg,
        "median_fill_vs_prev_mid_pct": _median(slip),
        "median_mid_vs_model_pct": _median(mdev),
        "skips_total": len(ledger.get("skip_log", [])),
        "skips_spread_gate": sum(1 for s in ledger.get("skip_log", [])
                                 if str(s.get("reason", "")).startswith("spread_too_wide")),
        "open_positions": open_mtm,
        "validation": {"started": started, "calendar_days": days_run,
                       "verdict": verdict, "go_bar": v["go_bar"]},
    }
    print(json.dumps(out, indent=2, ensure_ascii=False))


# ---------- main ----------

def main():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("signal")
    s.add_argument("--config", required=True, help="strategy/weekly_calls.json")
    s.add_argument("--ledger", required=True, help="state/weekly_call_positions.json")
    s.add_argument("--bars", nargs="+", required=True, help="integrations.py bars 输出 (universe, ≥450天)")
    s.add_argument("--quotes", required=True, help='{"SYM": price} 或 get_equity_quotes 原始输出')
    s.add_argument("--chains", help="integrations.py chains 输出 (universe + 持仓底层, dte-max 17)")
    s.add_argument("--earnings", help='{"SYM": "YYYY-MM-DD"|null} (复用主跑 earnings.json)')
    s.add_argument("--buying-power", type=float,
                   help="实盘配置用: 实时购买力, 新买入权利金合计不得超过 (paper 可省略)")
    s.add_argument("--portfolio-value", type=float,
                   help="账户净值 (get_portfolio total_value); budget/熔断配置为百分比时必传")
    s.add_argument("--date", required=True)
    s.add_argument("--out", help="订单输出 (paper.py run --orders 输入)")
    s.set_defaults(func=cmd_signal)

    ap = sub.add_parser("apply")
    ap.add_argument("--ledger", required=True)
    ap.add_argument("--fills", required=True)
    ap.add_argument("--context", help="当日 signal 输出文件 (附带入场/出场快照与 skip 记录)")
    ap.add_argument("--date", required=True)
    ap.set_defaults(func=cmd_apply)

    r = sub.add_parser("report")
    r.add_argument("--config", required=True)
    r.add_argument("--ledger", required=True)
    r.add_argument("--chains", help="盯市用链报价 (可省略, 省略则开仓不盯市)")
    r.add_argument("--date", required=True)
    r.set_defaults(func=cmd_report)

    a = p.parse_args()
    a.func(a)


if __name__ == "__main__":
    main()
