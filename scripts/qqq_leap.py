#!/usr/bin/env python3
"""QQQ LEAP call 轨道引擎 (实盘) —— 确定性信号/选档/出场, **只出信号不含会话判断** (红线2)。

参数契约: strategy/qqq_leap.json (每字段的回测来源见该文件注释与
strategy/proposals/qqq-leap-call.md §7/§12.6/§15/§16/§17/§18)。

三个子命令, 职责分离:
  window  —— 判当日有无入场信号, 并**告诉会话该去取哪些期权合约的券商报价** (到期区间 +
             行权价区间, 故意取**超集**)。会话只负责取数, **选哪一张由 signal 决定** (红线2)。
  signal  —— 读 config + 日线 + 当日报价 + 链(含 delta) + 账本, 产出 buys/sells/skips。
  apply   —— 按成交回写账本 (positions / trades / round_trips)。

⚠️ 为什么链必须由会话取券商报价而不是 integrations.py chains:
   integrations.py chains 走 Alpaca `feed=indicative` 且**只落盘 {bid, ask}, 没有 delta**;
   本轨道按 delta 选档 (§7.1③), 而券商 get_option_quotes **返回完整 greeks** (delta/IV/theta…,
   2026-10-19 实测确认)。与正股"当日决策价必须走券商"同一条纪律。
"""
import argparse
import json
import math
import os
import sys
from datetime import date as _date

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from signals import load_json, save_json, parse_historicals, parse_quotes  # noqa: E402
from options_overlay import parse_occ, dte  # noqa: E402

UNDERLYING_DEFAULT = "QQQ"


# ---------- Black-Scholes (仅用于 window 估算行权价取数区间, 不参与任何选档/定价) ----------
def _ncdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _bs_delta(S, K, T, sig, r=0.04, q=0.005):
    if T <= 0 or sig <= 0 or K <= 0:
        return 1.0 if S > K else 0.0
    d1 = (math.log(S / K) + (r - q + 0.5 * sig * sig) * T) / (sig * math.sqrt(T))
    return math.exp(-q * T) * _ncdf(d1)


def _realized_vol(closes, window):
    if len(closes) < window + 1:
        return None
    rets = [math.log(closes[i] / closes[i - 1]) for i in range(len(closes) - window, len(closes))]
    m = sum(rets) / len(rets)
    var = sum((x - m) ** 2 for x in rets) / (len(rets) - 1)
    return math.sqrt(var * 252)


# ---------- 共用: 行情派生量 ----------
def _series(hist_paths, underlying):
    """返回 [(date, close), ...] 升序 (不含当日 —— 日线永远落后一天)。"""
    h = parse_historicals(hist_paths)
    s = h.get(underlying)
    if not s:
        raise SystemExit(f"日线里没有 {underlying}: 检查 --historicals")
    return s


def _derive(cfg, hist_paths, quotes_path, underlying, today):
    """当日价 = 券商实时报价 (日线排除当日, 与 signals.py 同口径)。
    SMA200 口径**逐字段对齐回测** (qqq-leap-backtest.py): 含当日价的 200 点均值。"""
    ser = _series(hist_paths, underlying)
    closes = [c for _d, c in ser]
    dates = [d for d, _c in ser]
    if dates and dates[-1] >= today:
        # 日线已含当日 (收盘后窗口): 砍掉当日, 统一用实时价代表当日
        cut = [i for i, d in enumerate(dates) if d < today]
        closes = [closes[i] for i in cut]
        dates = [dates[i] for i in cut]
    q = parse_quotes(quotes_path)
    if underlying not in q:
        raise SystemExit(f"当日报价里没有 {underlying}: 本轨道拒跑 (不接受用日线代替当日价)")
    px = float(q[underlying])
    prev = closes[-1]
    n = int(cfg["entry"]["sma_period"])
    win = closes[-(n - 1):] + [px]
    sma = sum(win) / len(win) if len(win) == n else None
    rv20, rv60 = _realized_vol(closes + [px], 20), _realized_vol(closes + [px], 60)
    return {"price": px, "prev_close": prev, "prev_close_date": dates[-1],
            "day_return_pct": (px / prev - 1) * 100.0,
            "sma": sma, "sma_points": len(win),
            "above_sma": (sma is not None and px > sma),
            "rv20": rv20, "rv60": rv60,
            "trading_dates": dates}


def _entry_signal(cfg, d):
    """§7.1 入场: 当日跌幅 ≥2% (日收益 ≤ −2.0%) 且 close > SMA200 (牛市过滤)。"""
    e = cfg["entry"]
    reasons = []
    if d["sma"] is None:
        reasons.append(f"sma200_unavailable(points={d['sma_points']})")
    ok_drop = d["day_return_pct"] <= float(e["daily_return_pct_max"])
    if not ok_drop:
        reasons.append(f"drop_too_small({d['day_return_pct']:+.2f}%)")
    ok_bull = (not e.get("require_close_above_sma200")) or d["above_sma"]
    if not ok_bull:
        reasons.append("below_sma200")
    return (not reasons), reasons


def _held_trading_days(trading_dates, entry_date, today):
    """交易日计数 (回测口径 held = j − i, **不是日历日**)。
    trading_dates 不含当日, 故当日单独 +1。入场当日 held=0。"""
    if entry_date >= today:
        return 0
    return sum(1 for d in trading_dates if d > entry_date) + 1


def _mid(q):
    b, a = q.get("bid"), q.get("ask")
    if b is None or a is None or b <= 0 or a <= 0 or a < b:
        return None
    return (b + a) / 2.0


def _spread_pct(q):
    m = _mid(q)
    return None if not m else (q["ask"] - q["bid"]) / m * 100.0


# ---------- window ----------
def cmd_window(a):
    cfg = load_json(a.config)
    led = load_json(a.ledger) if os.path.exists(a.ledger) else {}
    u = cfg.get("underlying", UNDERLYING_DEFAULT)
    d = _derive(cfg, a.historicals, a.quotes, u, a.date)
    sig_ok, why = _entry_signal(cfg, d)

    cc = cfg["contract"]
    lo_d, hi_d = cc["dte_band_calendar"]
    t0 = _date.fromisoformat(a.date)
    out = {
        "date": a.date, "underlying": u,
        "price": round(d["price"], 4), "prev_close": d["prev_close"],
        "day_return_pct": round(d["day_return_pct"], 3),
        "sma200": None if d["sma"] is None else round(d["sma"], 4),
        "above_sma200": d["above_sma"],
        "entry_signal": sig_ok, "entry_block_reasons": why,
        "held_occs": sorted((led.get("positions") or {}).keys()),
        "_held_note": "持仓 OCC 必须一并取报价 —— 出场 (止盈/时间止损) 要当前 bid, 没报价就判不了",
    }
    if sig_ok:
        # 用 BS 反推 delta∈[0.20,0.50] 的行权价区间 (比 delta_band 更宽), 再夹到 [1.02S, 1.50S]。
        # ⚠️ 这只决定**会话去取哪些合约的报价**, 不决定选哪张; 真正的 delta 过滤在 signal 里
        # 用券商返回的 delta 做。取超集是故意的: 窗口估偏了最坏只是多取几张报价, 不会错选。
        iv = 0.20
        if d["rv20"] and d["rv60"]:
            iv = max(0.08, min(0.80, 0.5 * d["rv20"] + 0.5 * d["rv60"]))
        S = d["price"]
        rungs = cc.get("ladder") or [None]
        needs = []
        for rung in rungs:
            r_lo, r_hi = (rung["dte_band_calendar"] if rung else cc["dte_band_calendar"])
            T = ((r_lo + r_hi) / 2.0) / 365.0
            ks = []
            k = math.floor(S * 1.00 / 5) * 5
            while k <= S * 1.60:
                if k > 0 and 0.20 <= _bs_delta(S, k, T, iv) <= 0.50:
                    ks.append(k)
                k += 5
            k_lo = (max(math.floor(S * 1.02 / 5) * 5, min(ks) - 25) if ks
                    else math.floor(S * 1.05 / 5) * 5)
            k_hi = (min(math.ceil(S * 1.50 / 5) * 5, max(ks) + 25) if ks
                    else math.ceil(S * 1.35 / 5) * 5)
            needs.append({
                "rung": (f"dte~{rung['target_dte_calendar']}" if rung else "single"),
                "type": "call",
                "expiry_date_gte": _date.fromordinal(t0.toordinal() + r_lo).isoformat(),
                "expiry_date_lte": _date.fromordinal(t0.toordinal() + r_hi).isoformat(),
                "strike_min": k_lo, "strike_max": k_hi, "strike_step": 5,
                "iv_used_for_window": round(iv, 4),
            })
        out["need_chain"] = needs if cc.get("ladder") else needs[0]
        out["_need_chain_note"] = (
            "会话取这些区间内**全部** QQQ call 的券商报价 (get_option_instruments → "
            "get_option_quotes, 必须带 delta), 原样落盘给 signal --chain。"
            "区间是超集, 选档由 signal 用券商 delta 确定性决定 (红线2)。"
            + ("\n⚠️ **梯子模式**: need_chain 是**数组**, 每档一个区间, 三档的报价**全部**"
               "落进同一个 chain 文件即可 (signal 自己按各档 dte_band 分开选)。"
               if cc.get("ladder") else "")
        )
    _emit(out, a.out)
    print(json.dumps({k: out[k] for k in
                      ("date", "entry_signal", "day_return_pct", "above_sma200", "held_occs")},
                     ensure_ascii=False))
    return 0


def _emit(obj, path):
    if path:
        save_json(path, obj)


# ---------- signal ----------
def _is_standard_monthly(expiry):
    """标准月度到期 = 当月第三个周五 (§4 实测该档点差明显更紧, 约紧一半)。"""
    y, m, dd = (int(x) for x in expiry.split("-"))
    d = _date(y, m, 1)
    fridays = [x for x in range(1, 32)
               if x <= (_date(y + (m == 12), (m % 12) + 1, 1).toordinal() - d.toordinal())
               and _date(y, m, x).weekday() == 4]
    return len(fridays) >= 3 and dd == fridays[2]


def _pick(chain, cfg, spot, today, rung=None):
    """确定性选档: 只认 call, dte 在带内, **delta 在带内**, 点差/最小买价合规。

    `rung` 非空时用该档自己的 `target_dte_calendar` / `dte_band_calendar` 覆盖全局值
    (梯子模式, 用户 2026-10-23「3 个期限各 1 张」); delta 带、点差闸、min_bid 仍是全局的。
    排序键 (全部确定性, 无会话判断):
      ① |delta − target| 最小  ② 标准月度优先 (prefer_standard_monthly, §4 点差更紧)
      ③ 点差更窄            ④ dte 更接近目标  ⑤ OCC 字典序 (兜底保证可复现)
    返回 (pick, skip_reason, diag)。"""
    cc = cfg["contract"]
    dlo, dhi = cc["delta_band"]
    tgt = float(cc["delta_target"])
    lo_d, hi_d = (rung["dte_band_calendar"] if rung else cc["dte_band_calendar"])
    dte_tgt = int(rung["target_dte_calendar"] if rung else cc["dte_target_calendar"])
    cands, diag = [], {"n_call": 0, "in_dte": 0, "in_delta": 0, "has_quote": 0, "pass_spread": 0}
    best_spread = None
    for occ, q in chain.items():
        meta = parse_occ(occ)
        if not meta or meta["type"] != "C":
            continue
        diag["n_call"] += 1
        d = dte(meta["expiry"], today)
        if not (lo_d <= d <= hi_d):
            continue
        diag["in_dte"] += 1
        dl = q.get("delta")
        if dl is None:
            continue
        dl = float(dl)
        if not (dlo <= dl <= dhi):
            continue
        diag["in_delta"] += 1
        m = _mid(q)
        if m is None or float(q["bid"]) < float(cc["min_bid"]):
            continue
        diag["has_quote"] += 1
        sp = _spread_pct(q)
        best_spread = sp if best_spread is None else min(best_spread, sp)
        if sp > float(cc["max_spread_pct"]):
            continue
        diag["pass_spread"] += 1
        monthly_rank = 0 if (cc.get("prefer_standard_monthly")
                             and _is_standard_monthly(meta["expiry"])) else 1
        cands.append((round(abs(dl - tgt), 6), monthly_rank, sp,
                      abs(d - dte_tgt), occ, meta, q, dl, m))
    if not cands:
        if diag["in_delta"] and best_spread is not None:
            return None, f"spread_too_wide(best={best_spread:.2f}%>{cc['max_spread_pct']}%)", diag
        if diag["in_dte"]:
            return None, "no_contract_in_delta_band", diag
        if diag["n_call"]:
            return None, "no_contract_in_dte_band", diag
        return None, "chain_empty_or_no_calls", diag
    cands.sort()
    _dd, _mo, sp, _dr, occ, meta, q, dl, m = cands[0]
    return {"occ": occ, "meta": meta, "quote": q, "delta": round(dl, 4), "mid": m,
            "ask": float(q["ask"]), "bid": float(q["bid"]),
            "spread_pct": round(sp, 3), "dte": dte(meta["expiry"], today),
            "per_contract_usd": round(float(q["ask"]) * 100.0, 2)}, None, diag


def cmd_signal(a):
    cfg = load_json(a.config)
    led = load_json(a.ledger) if os.path.exists(a.ledger) else {}
    chain_raw = load_json(a.chain) if a.chain else {}
    chain = chain_raw.get(cfg.get("underlying", UNDERLYING_DEFAULT), chain_raw)
    u = cfg.get("underlying", UNDERLYING_DEFAULT)
    today = a.date
    out = {"date": today, "track": "qqq_leap", "underlying": u,
           "enabled": bool(cfg.get("enabled")), "halted": bool(led.get("halted")),
           "funding_priority": cfg.get("funding_priority"),
           "buys": [], "sells": [], "skips": [], "warnings": []}

    d = _derive(cfg, a.historicals, a.quotes, u, today)
    out["spot"] = round(d["price"], 4)
    out["day_return_pct"] = round(d["day_return_pct"], 3)
    out["sma200"] = None if d["sma"] is None else round(d["sma"], 4)
    out["above_sma200"] = d["above_sma"]

    positions = led.get("positions") or {}

    # --- 红线4: enabled=false / halted=true → 只读, 一张单都不出 (出场也不出) ---
    if not cfg.get("enabled") or led.get("halted"):
        out["warnings"].append(
            "enabled=false 或 halted=true → 红线4: 只允许读数据和写日志, 本轨道本次不产出任何订单 "
            f"(enabled={cfg.get('enabled')}, halted={led.get('halted')})")
        _emit(out, a.out)
        print(json.dumps({"buys": 0, "sells": 0, "halted": True}, ensure_ascii=False))
        return 0

    # --- 熔断: 累计已实现亏损 ≥ 净值×pct ---
    cb = (cfg.get("circuit_breaker") or {}).get("halt_on_cumulative_realized_loss_pct_of_portfolio")
    realized = sum(float(r.get("pnl_usd") or 0) for r in (led.get("round_trips") or []))
    if cb is not None and realized < 0 and abs(realized) >= float(a.portfolio_value) * float(cb) / 100.0:
        out["halted"] = True
        out["warnings"].append(
            f"🔴 熔断触发: 累计已实现亏损 ${realized:.2f} ≥ 净值 ${float(a.portfolio_value):.2f}×{cb}% "
            "→ 本次不产出订单, 并应由 apply 把 halted 写进账本 (红线3: 熔断是上限不是建议)")
        _emit(out, a.out)
        print(json.dumps({"buys": 0, "sells": 0, "halted": True}, ensure_ascii=False))
        return 0

    # ---------------- 出场 (全自动, 优先于入场算资金) ----------------
    ex = cfg["exit"]
    sell_factor = float(ex.get("sell_limit_factor", 0.97))
    for occ, pos in sorted(positions.items()):
        q = chain.get(occ) or {}
        meta = parse_occ(occ)
        held = _held_trading_days(d["trading_dates"], pos["entry_date"], today)
        dleft = dte(meta["expiry"], today) if meta else None
        bid = q.get("bid")
        bid = float(bid) if bid is not None else None
        tp_line = round(float(pos["entry_fill_price"]) * float(ex["take_profit_mult"]), 4)
        base = {"occ": occ, "contracts": int(pos["contracts"]),
                "entry_date": pos["entry_date"], "entry_fill_price": pos["entry_fill_price"],
                "held_trading_days": held, "dte": dleft,
                "take_profit_line": tp_line, "current_bid": bid}
        if bid is None:
            out["warnings"].append(
                f"{occ}: 链里没有当前 bid → **本次无法判定止盈/时间止损**。"
                f"已持 {held} 个交易日 (上限 {ex['max_holding_trading_days']})。"
                "必须补该合约的券商报价后重跑 —— 缺报价会让时间止损静默失效 (红线6)")
            out["skips"].append({**base, "reason": "no_bid_cannot_evaluate_exit"})
            continue
        reason = None
        if bid >= tp_line:
            reason = "take_profit"
        elif held >= int(ex["max_holding_trading_days"]):
            reason = "time_stop"
        elif dleft is not None and dleft <= int(ex["force_exit_dte_lte"]):
            reason = "force_exit_dte"
        if reason:
            out["sells"].append({
                **base, "action": "sell_to_close", "reason": reason,
                "order_type": "limit", "time_in_force": "gfd",
                "est_price": round(bid, 2),
                "limit_price": round(bid * sell_factor, 2),
                "_limit_note": f"limit = 当前 bid × {sell_factor} (出场类, 照常全自动)",
                "pnl_pct_est": round((bid / float(pos["entry_fill_price"]) - 1) * 100, 2)})
        else:
            out["skips"].append({**base, "reason": "hold",
                                 "_why": f"bid {bid:.2f} < 止盈线 {tp_line:.2f}; "
                                         f"已持 {held}/{ex['max_holding_trading_days']} 交易日"})

    # ---------------- 入场 ----------------
    sig_ok, why = _entry_signal(cfg, d)
    out["entry_signal"] = sig_ok
    if not sig_ok:
        out["skips"].append({"stage": "entry", "reason": "no_entry_signal", "detail": why})
        _emit(out, a.out)
        print(json.dumps({"buys": len(out["buys"]), "sells": len(out["sells"]),
                          "entry_signal": False}, ensure_ascii=False))
        return 0

    # ---------- 选档 + 定量 + 资金闸 ----------
    # 梯子 (用户 2026-10-23「以后qqq跌了 买三个期权」, 当日澄清为「3 个期限各 1 张」):
    # `contract.ladder` 非空时**每档各选 1 张、各自成一张独立订单**, 按列表顺序逐档过资金闸。
    # ⚠️ **独立订单是这条设计的关键**: 红线9 的「整单跳过不缩量」是按**单笔**判的, 所以 cap
    #    不足时结果是「能买几张买几张」; 若把三张合成一张 3 contracts 的单, 凑不出全额就会
    #    **一张都不买** —— 那等于把原本成功的买入变成不买。
    # ⚠️ 列表顺序 = 资金优先级, **由 config 声明, 不是引擎自己排的** (红线2)。
    rungs = cfg["contract"].get("ladder") or [None]
    laddered = bool(cfg["contract"].get("ladder"))

    bp = float(a.buying_power)
    cash = float(a.cash) if a.cash is not None else bp
    avail0 = min(bp, cash) - float(a.reserved_usd or 0)
    avail = avail0
    out["funding_gate"] = {
        "buying_power": bp, "cash": cash, "reserved_usd": float(a.reserved_usd or 0),
        "available": round(avail0, 2),
        "_note": "cap = min(buying_power, cash) − reserved; **每档单笔**超 cap 则该档整单跳过"
                 "不缩量, 绝不动用借来的钱; 已出单的档会扣减后续档的 cap",
        "_ladder_order_note": ("列表顺序即资金优先级, 由 contract.ladder 声明"
                               if laddered else None),
    }
    pv = float(a.portfolio_value)
    sz = cfg["sizing"]
    buy_factor = float(cfg["contract"].get("buy_limit_factor", 1.0))
    diags, taken = {}, set()

    for rung in rungs:
        tag = (f"dte~{rung['target_dte_calendar']}" if rung else "single")
        pick, skip, diag = _pick(chain, cfg, d["price"], today, rung)
        diags[tag] = diag
        if pick is None:
            out["skips"].append({"stage": "entry", "rung": tag, "reason": skip,
                                 "chain_diag": diag,
                                 "_why": "有入场信号但该档选不出合规合约; **不放宽带宽** (§7.1③)"})
            continue
        if pick["occ"] in taken:
            # 两档 dte_band 重叠时可能选到同一张。必须拦: apply 的同键合并会把它们并成一仓,
            # 于是「3 个期限各 1 张」静默退化成「某一期限 2 张」—— 与用户要的不是一回事。
            out["skips"].append({"stage": "entry", "rung": tag, "occ": pick["occ"],
                                 "reason": "duplicate_occ_across_rungs",
                                 "_why": "本档选到了前档已取的同一合约 (dte_band 重叠)。跳过, "
                                         "不重复买。要真正并排三个期限, 把 ladder 各档的 "
                                         "dte_band_calendar 调成互不重叠"})
            continue

        per = pick["per_contract_usd"]
        pcap = sz.get("max_premium_per_contract_usd")
        if pcap is not None and per > float(pcap):
            out["skips"].append({"stage": "entry", "rung": tag, "occ": pick["occ"],
                                 "reason": f"premium_too_large({per:.0f}>{pcap})"})
            continue

        tier_note = None
        if laddered:
            # 梯子模式每档固定 1 张 —— 用户要的就是「各 1 张」, 故不走 position_pct 的 floor 逻辑。
            qty = 1
            tier_note = ("梯子模式: 本档固定 1 张 (用户 2026-10-23「3 个期限各 1 张」); "
                         "**sizing.position_pct_of_portfolio 在本模式下不参与定量**")
        else:
            target = pv * float(sz["position_pct_of_portfolio"]) / 100.0
            qty = int(target // per)
            if qty < 1:
                tier = sz.get("max_single_position_pct_of_portfolio", "__UNSET__")
                if tier is None:
                    tier_cap, tier_note = float("inf"), "贵档例外: max_single_position_pct_of_portfolio=null → 不限制, 给 1 张"
                elif tier == "__UNSET__":
                    tier_cap, tier_note = 2 * target, "贵档例外: 字段缺失 → 回落 2×目标仓"
                else:
                    tier_cap = pv * float(tier) / 100.0
                    tier_note = f"贵档例外: 单仓上限 {tier}% 净值 = ${tier_cap:.0f}"
                if per <= tier_cap:
                    qty = 1
                else:
                    out["skips"].append({"stage": "entry", "rung": tag, "occ": pick["occ"],
                                         "reason": f"premium_exceeds_position_cap(per={per:.0f},target={target:.0f})"})
                    continue

        # --- 防杠杆闸 (红线3 / 红线9③): 本档单笔超 cap → 该档整单跳过**不缩量** ---
        cost = qty * per
        if cost > avail:
            out["skips"].append({
                "stage": "entry", "rung": tag, "occ": pick["occ"],
                "reason": f"insufficient_buying_power(cost={cost:.0f},avail={avail:.0f})",
                "funding_gate": dict(out["funding_gate"], available_at_this_rung=round(avail, 2)),
                "_why": "红线3/红线9③: 超出本档可用 cap → 整单跳过, **不缩量**。"
                        "梯子模式下后续档仍各自判 cap (能买几张买几张)"})
            continue

        taken.add(pick["occ"])
        avail -= cost
        out["buys"].append({
            "occ": pick["occ"], "action": "buy_to_open", "structure": "single",
            "rung": tag, "contracts": qty, "underlying": u,
            "strike": pick["meta"]["strike"], "expiry": pick["meta"]["expiry"],
            "delta": pick["delta"], "dte": pick["dte"],
            "spread_pct": pick["spread_pct"],
            "bid": pick["bid"], "ask": pick["ask"], "mid": round(pick["mid"], 4),
            "per_contract_usd": per, "total_cost_usd": round(cost, 2),
            "pct_of_portfolio": round(cost / pv * 100, 2),
            "order_type": "limit", "time_in_force": "gfd",
            "est_price": round(pick["ask"], 2),
            "limit_price": round(pick["ask"] * buy_factor, 2),
            "_limit_note": f"limit = ask × {buy_factor} (单腿; 多腿被 Robinhood API 400 拒)",
            "take_profit_line": round(pick["ask"] * float(ex["take_profit_mult"]), 4),
            "_tp_note": ("止盈判定口径 = **当前 bid ≥ 入场 ask 成交价 × "
                         f"{ex['take_profit_mult']}**, 双边点差已扣的净口径, **不是 mid 对 mid**"),
            "entry_spot": round(d["price"], 4),
            "entry_day_return_pct": round(d["day_return_pct"], 3),
            "sizing_note": tier_note,
            "cap_remaining_after": round(avail, 2),
        })

    out["chain_diag"] = diags if laddered else diags.get("single")
    _emit(out, a.out)
    print(json.dumps({"buys": len(out["buys"]), "sells": len(out["sells"]),
                      "ladder": laddered,
                      "occs": [b["occ"] for b in out["buys"]],
                      "contracts_total": sum(b["contracts"] for b in out["buys"]),
                      "total_cost_usd": round(sum(b["total_cost_usd"] for b in out["buys"]), 2),
                      "cap_left": round(avail, 2),
                      "skips": len(out["skips"])}, ensure_ascii=False))
    return 0


# ---------- apply ----------
def cmd_apply(a):
    led = load_json(a.ledger) if os.path.exists(a.ledger) else {}
    fills = load_json(a.fills)
    ctx = load_json(a.context) if a.context else {}
    ctx_buys = {o["occ"]: o for o in ctx.get("buys", [])}
    ctx_sells = {o["occ"]: o for o in ctx.get("sells", [])}
    positions = led.setdefault("positions", {})
    trades = led.setdefault("trades", [])
    rts = led.setdefault("round_trips", [])
    today = a.date

    opened = [f["symbol"] for f in fills["fills"] if f.get("side") == "buy"]
    if opened and not a.context:
        raise SystemExit(
            "apply 含开仓成交但未传 --context: " + ", ".join(opened) +
            "\n开仓快照 (delta/dte/spread/entry_spot) 会写成 null, 次日出场判定会缺依据。"
            "\n请补 --context 指向当日 signal 输出 (state/qqq_leap_last_orders.json)。")
    missing = [o for o in opened if o not in ctx_buys]
    if missing:
        raise SystemExit("开仓成交在 --context 里找不到对应买单: " + ", ".join(missing) +
                         "\ncontext 提供的买单: " + (", ".join(sorted(ctx_buys)) or "(空)") +
                         "\n多半是 context 传成了别的交易日。人工核对后重跑。")

    for f in fills["fills"]:
        occ, side = f["symbol"], f["side"]
        qty, price = int(float(f["qty"])), float(f["price"])
        meta = parse_occ(occ)
        if not meta:
            raise SystemExit(f"非期权成交混入 qqq_leap apply: {occ}")
        trades.append({"date": today, "symbol": occ, "side": side, "qty": qty,
                       "price": price, "bucket": "qqq_leap",
                       "reason": (ctx_sells.get(occ) or ctx_buys.get(occ) or {}).get("reason",
                                                                                    "rsi2_none")})
        if side == "buy":
            c = ctx_buys.get(occ, {})
            ex = load_json(a.config)["exit"] if a.config else {}
            tp_mult = float(ex["take_profit_mult"]) if ex else None
            old = positions.get(occ)
            if old is None:
                positions[occ] = {
                    "underlying": meta["underlying"], "strike": meta["strike"],
                    "expiry": meta["expiry"], "contracts": qty,
                    "entry_date": today, "entry_fill_price": price,
                    "entry_cost_usd": round(price * 100.0 * qty, 2),
                    "entry_delta": c.get("delta"), "entry_dte": c.get("dte"),
                    "entry_spread_pct": c.get("spread_pct"),
                    "entry_spot": c.get("entry_spot"),
                    "entry_day_return_pct": c.get("entry_day_return_pct"),
                    "take_profit_line": round(price * tp_mult, 4) if tp_mult else None,
                    "_tp_note": "止盈线按**实际成交价**重算 (不是 signal 里按 ask 估的那个)",
                    "_lots": [{"date": today, "contracts": qty, "fill_price": price}],
                }
            else:
                # 同一 OCC 再次买入 → **合并, 不覆写** (用户 2026-10-22 选方案 C)。
                # 原先这里是 `positions[occ] = {...}` 直接赋值, 既不合并也没守卫:
                # 当日实盘撞上同键 (QQQ270917C00830000 早上 @24.74 + 收盘前 @24.39),
                # 照跑会把账本从 2 张变 1 张并丢掉 $2,474 那个成本基准。
                # 详见 journal/2026-10-22-preclose.md §3。
                # ⚠️ **这是语义改动, 不是纯 bug 修复**: 止盈基准从「单笔实际成交价」
                # 变成「加权均价」。用户 2026-10-22 在知情下选择如此。
                n0, n1 = int(old["contracts"]), qty
                p0 = float(old["entry_fill_price"])
                avg = (p0 * n0 + price * n1) / (n0 + n1)
                old["contracts"] = n0 + n1
                old["entry_fill_price"] = round(avg, 6)
                old["entry_cost_usd"] = round(
                    float(old["entry_cost_usd"]) + price * 100.0 * n1, 2)
                old["take_profit_line"] = round(avg * tp_mult, 4) if tp_mult else None
                old.setdefault(
                    "_lots", [{"date": old["entry_date"], "contracts": n0,
                               "fill_price": p0}]).append(
                    {"date": today, "contracts": n1, "fill_price": price})
                old["_tp_note"] = (
                    "⚠️ 本仓位含多笔成交, 止盈线按**加权均价**算 (用户 2026-10-22 选 C); "
                    "逐笔明细见 _lots 与 trades[]。")
                # entry_date / entry_* 快照一律**不动**:
                #   · entry_date 不动 => 20 交易日时间止损时钟**不因加仓重置**
                #     (config exit.holding_clock_resets=false, 与 RSI-2 轨道同一纪律);
                #     设成 true 才按本次成交日重置。
                #   · entry_delta/dte/spread/spot/day_return 是**开仓时**的快照,
                #     加仓不改写它们; 想看第二笔的那组数去 trades[] 与当日 journal。
                if ex.get("holding_clock_resets"):
                    old["entry_date"] = today
        else:
            pos = positions.get(occ)
            if pos is None:
                raise SystemExit(f"卖出无持仓的合约: {occ} (人工对账)")
            held = int(pos["contracts"])
            if qty != held:
                # 合并后仓位可以 >1 张, 于是「卖的张数 != 持有张数」第一次变成现实风险。
                # 原代码无条件 pop, 会让账本静默丢掉剩下那几张。
                raise SystemExit(
                    f"部分卖出未实现: {occ} 持 {held} 张但本次卖 {qty} 张。\n"
                    "引擎出场一律整仓卖 (signal 的 sells 用 contracts=持仓张数), "
                    "故这里不相等就是对账出了问题 —— 人工核对后再跑, "
                    "不要让账本静默丢掉剩下的张数。")
            positions.pop(occ)
            ep = float(pos["entry_fill_price"])
            rts.append({
                "occ": occ, "underlying": pos["underlying"], "structure": "single",
                "entry_date": pos["entry_date"], "exit_date": today,
                "entry_premium": ep, "exit_premium": price,
                "contracts": qty,
                "pnl_pct": round((price / ep - 1) * 100, 2),
                "pnl_usd": round((price - ep) * 100.0 * qty, 2),
                "reason": (ctx_sells.get(occ) or {}).get("reason", "unknown"),
                "held_trading_days": (ctx_sells.get(occ) or {}).get("held_trading_days"),
                "entry_delta": pos.get("entry_delta"),
            })
    led["updated"] = today
    save_json(a.ledger, led)
    print(json.dumps({"positions": len(positions), "trades": len(trades),
                      "round_trips": len(rts)}, ensure_ascii=False))
    return 0


def main():
    ap = argparse.ArgumentParser(description="QQQ LEAP call 轨道引擎 (实盘)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    w = sub.add_parser("window", help="判信号 + 告诉会话该取哪些合约报价")
    s = sub.add_parser("signal", help="产出 buys/sells/skips")
    for p in (w, s):
        p.add_argument("--config", default="strategy/qqq_leap.json")
        p.add_argument("--ledger", default="state/qqq_leap_positions.json")
        p.add_argument("--historicals", nargs="+", required=True)
        p.add_argument("--quotes", required=True, help="券商 get_equity_quotes 原始输出或 {SYM: price}")
        p.add_argument("--date", required=True)
        p.add_argument("--out")
    s.add_argument("--chain", required=True,
                   help='券商期权报价 (必须含 delta): {"QQQ": {OCC: {bid, ask, delta, ...}}} 或扁平 {OCC: {...}}')
    s.add_argument("--portfolio-value", type=float, required=True)
    s.add_argument("--buying-power", type=float, required=True)
    s.add_argument("--cash", type=float, help="缺省同 buying-power; 传了则闸用 min(两者)")
    s.add_argument("--reserved-usd", type=float, default=0.0,
                   help="已被更高优先级轨道占用的资金 (本轨道 funding_priority=1 时通常为 0)")

    ap2 = sub.add_parser("apply", help="按成交回写账本")
    ap2.add_argument("--ledger", default="state/qqq_leap_positions.json")
    ap2.add_argument("--fills", required=True)
    ap2.add_argument("--context", help="当日 signal 输出 (开仓成交必传)")
    ap2.add_argument("--config", default="strategy/qqq_leap.json")
    ap2.add_argument("--date", required=True)

    a = ap.parse_args()
    return {"window": cmd_window, "signal": cmd_signal, "apply": cmd_apply}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
