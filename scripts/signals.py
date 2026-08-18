#!/usr/bin/env python3
"""
Claudeaqos 每日自动交易 — 确定性信号引擎 (RSI-2 均值回归 + 长期趋势过滤)

子命令:
  signal  由 历史数据 + 实时报价 + 持仓状态 + 配置 计算出今天的订单清单
  apply   把实际成交回写到 state/positions.json

设计原则: 纯 stdlib、确定性(同样输入永远产出同样订单)、绝不联网。
执行层(Claude 会话)只负责搬运数据和按输出下单, 不得自行修改订单。
"""
import argparse
import json
import sys
from datetime import date as _date


def floor6(qty):
    """卖单数量取 6 位小数且绝不向上进位 — round() 半数进位会超出券商可用数量导致拒单。"""
    r = round(qty, 6)
    return r if r <= qty + 1e-12 else round(r - 1e-6, 6)


# ---------- IO helpers ----------

def load_json(path):
    with open(path) as f:
        return json.load(f)


def save_json(path, obj):
    with open(path, "w") as f:
        json.dump(obj, f, indent=2, ensure_ascii=False)
        f.write("\n")


def parse_historicals(paths):
    """接受 get_equity_historicals 原始输出文件, 返回 {SYM: [(date, close), ...] 升序}"""
    out = {}
    for p in paths:
        raw = load_json(p)
        for r in raw["data"]["results"]:
            bars = {}
            for b in r["bars"]:
                if b.get("interpolated"):
                    continue
                if b.get("session") not in (None, "reg"):
                    continue
                bars[b["begins_at"][:10]] = float(b["close_price"])
            merged = out.setdefault(r["symbol"], {})
            merged.update(bars)
    return {sym: sorted(d.items()) for sym, d in out.items()}


def parse_quotes(path):
    """接受 简单映射 {"SYM": price} 或 get_equity_quotes 原始输出。返回 {SYM: price}"""
    raw = load_json(path)
    if "data" in raw:
        return {r["quote"]["symbol"]: float(r["quote"]["last_trade_price"])
                for r in raw["data"]["results"]
                if r["quote"].get("state") == "active"}
    return {k: float(v) for k, v in raw.items()}


def parse_positions(path):
    """接受 简单映射 {"SYM": {qty, available, intraday}} 或 get_equity_positions 原始输出。"""
    raw = load_json(path)
    if "data" in raw:
        return {p["symbol"]: {
                    "qty": float(p["quantity"]),
                    "available": float(p["shares_available_for_sells"]),
                    "intraday": float(p.get("intraday_quantity", 0)),
                } for p in raw["data"]["positions"]}
    return {k: {"qty": float(v["qty"]),
                "available": float(v.get("available", v["qty"])),
                "intraday": float(v.get("intraday", 0))}
            for k, v in raw.items()}


# ---------- indicators ----------

def sma(closes, n):
    if len(closes) < n:
        return None
    return sum(closes[-n:]) / n


def rsi(closes, period=2):
    """Wilder RSI"""
    if len(closes) <= period:
        return None
    gains = losses = 0.0
    for i in range(1, period + 1):
        d = closes[i] - closes[i - 1]
        gains += max(d, 0.0)
        losses += max(-d, 0.0)
    ag, al = gains / period, losses / period
    for i in range(period + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        ag = (ag * (period - 1) + max(d, 0.0)) / period
        al = (al * (period - 1) + max(-d, 0.0)) / period
    if al == 0:
        return 100.0
    return 100.0 - 100.0 / (1.0 + ag / al)


def trading_days_since(dates, since_date):
    return sum(1 for d in dates if d > since_date)


# ---------- signal ----------

def cmd_signal(a):
    cfg = load_json(a.config)
    state = load_json(a.state)
    hist = parse_historicals(a.historicals)
    quotes = parse_quotes(a.quotes)
    broker = parse_positions(a.positions) if a.positions else None
    macro = load_json(a.macro) if a.macro else None
    today = a.date
    warnings = []

    universe = cfg.get("universe", cfg.get("etf_universe", []))
    # 防御层 (个股轨道): 财报回避 / 异动过滤 / 行业上限。cfg 无 defense 时行为与原引擎完全一致。
    defense = cfg.get("defense")
    # 个股池并入 (2026-07-21 用户授权): stock_universe_file 的 symbols 并入候选池, sectors 供行业上限。
    suf = cfg.get("stock_universe_file")
    if suf:
        try:
            su = load_json(suf)
            universe = list(dict.fromkeys(list(universe) + list(su.get("symbols", []))))
            if defense is not None and isinstance(su.get("sectors"), dict):
                defense["sectors"] = {**su["sectors"], **defense.get("sectors", {})}
        except (OSError, ValueError):
            warnings.append(f"股票池文件 {suf} 读取失败, 本次候选仅 ETF 池")
    # defense.exempt_symbols (通常为 ETF 池): 豁免财报回避与异动过滤 — 指数类标的的恐慌大跌
    # 正是均值回归入场点, 不应被单票防御规则挡掉; 行业上限天然不命中 (无 sector 映射)。
    exempt_syms = set((defense or {}).get("exempt_symbols", []))
    allow_unknown_earnings = bool((defense or {}).get("allow_unknown_earnings"))
    earnings = load_json(a.earnings) if a.earnings else None
    no_earnings_data = defense is not None and earnings is None
    if no_earnings_data:
        if allow_unknown_earnings:
            warnings.append("防御: 无财报日数据, 按配置个股仍可入场 (仅失去财报回避保护)")
        else:
            warnings.append("防御: 无财报日数据, 今日跳过全部新入场 (出场照常)")

    def _earnings_next(sym):
        """下次财报日。兼容两种 earnings 格式:
        旧 {"SYM": "YYYY-MM-DD"|null}; 新 {"SYM": {"next": ...|null, "past": [...]}}。"""
        v = earnings.get(sym) if earnings else None
        return v.get("next") if isinstance(v, dict) else v

    def days_to_earnings(sym):
        """返回距下次财报的天数; None = 数据缺失或无近期财报 (值为 null)。"""
        nxt = _earnings_next(sym)
        if not earnings or nxt is None:
            return None
        try:
            return (_date.fromisoformat(nxt) - _date.fromisoformat(today)).days
        except ValueError:
            return None

    def earnings_reaction_days(sym):
        """财报的市场反应交易日集合 = 财报当日 (盘前 am 公布) + 次一交易日 (盘后 pm 公布)。
        仅在 earnings 为新格式且带 past 时非空; 旧格式返回空集 → 豁免自动失效 (向后兼容)。"""
        v = earnings.get(sym) if earnings else None
        if not isinstance(v, dict):
            return set()
        past = v.get("past") or []
        days = series.get(sym, ([], []))[0]
        out = set()
        for ed in past:
            out.add(ed)
            nxt = next((d for d in days if d > ed), None)
            if nxt:
                out.add(nxt)
        return out

    # 用实时报价补上今天的临时收盘价
    series = {}
    for sym, bars in hist.items():
        dates = [d for d, _ in bars if d < today]
        closes = [c for d, c in bars if d < today]
        if sym in quotes:
            dates.append(today)
            closes.append(quotes[sym])
        else:
            warnings.append(f"{sym}: 无实时报价, 使用最后一根历史K线 ({dates[-1] if dates else 'n/a'})")
        series[sym] = (dates, closes)

    ind = {}
    for sym, (dates, closes) in series.items():
        ind[sym] = {
            "close": round(closes[-1], 4) if closes else None,
            "sma5": sma(closes, 5),
            "sma20": sma(closes, 20),
            "sma200": sma(closes, 200),
            "rsi2": rsi(closes, 2),
        }

    pv = float(a.portfolio_value)
    bp = float(a.buying_power)
    hwm = max(float(state.get("high_water_mark", pv)), pv)
    drawdown_pct = (hwm - pv) / hwm * 100.0
    cb_limit = cfg["circuit_breaker"]["max_drawdown_pct_from_hwm"]
    circuit_breaker = drawdown_pct >= cb_limit
    halted = bool(state.get("halted", False))

    out = {
        "date": today,
        "portfolio_value": pv,
        "buying_power": bp,
        "high_water_mark": hwm,
        "drawdown_pct": round(drawdown_pct, 2),
        "halted": halted,
        "circuit_breaker_triggered": circuit_breaker,
        "sells": [],
        "buys": [],
        "indicators": {s: {k: (round(v, 4) if isinstance(v, float) else v)
                           for k, v in i.items()} for s, i in ind.items()},
        "warnings": warnings,
    }

    if halted or circuit_breaker:
        out["note"] = ("熔断触发: 回撤 %.2f%% ≥ %.1f%%。停止一切交易, 等待用户人工决定。"
                       % (drawdown_pct, cb_limit)) if circuit_breaker else "state.halted=true, 跳过交易。"
        _emit(out, a.out)
        return

    strat = state.get("strategy_positions", {})
    legacy = state.get("legacy_positions", {})

    def broker_qty(sym, fallback):
        if broker is None:
            return fallback
        return min(fallback, broker.get(sym, {}).get("available", 0.0))

    def bought_today(sym):
        if broker is None:
            return False
        return broker.get(sym, {}).get("intraday", 0.0) > 0

    # --- 卖出: 策略仓位出场 ---
    exiting = set()
    for sym, pos in strat.items():
        i = ind.get(sym)
        if not i or i["rsi2"] is None:
            warnings.append(f"{sym}: 指标数据不足, 跳过出场检查")
            continue
        px, entry = i["close"], float(pos["entry_price"])
        reason = None
        if defense is not None:
            ed = days_to_earnings(sym)
            if ed is not None and 0 <= ed <= int(defense.get("earnings_exit_days", 1)):
                reason = "earnings_exit"
        if reason is None and px <= entry * (1 - cfg["exit"]["stop_loss_pct"] / 100.0):
            reason = "stop_loss"
        elif (i["sma5"] is not None and px > i["sma5"]) or i["rsi2"] >= cfg["exit"]["rsi2_min"]:
            reason = "exit_strength"
        elif trading_days_since(series[sym][0], pos["entry_date"]) >= cfg["exit"]["max_holding_days"]:
            reason = "time_stop"
        if reason:
            qty = broker_qty(sym, float(pos["qty"]))
            if qty > 0:
                exiting.add(sym)
                out["sells"].append({"symbol": sym, "qty": floor6(qty), "bucket": "strategy",
                                     "reason": reason, "est_price": i["close"]})

    # --- 卖出: 存量持仓保护性止损 + 止盈 (相对纳管基准价) ---
    # 止损: 收盘 ≤ 纳管价×(1-stop%) → 卖 (下行保护)。
    # 止盈 (用户 2026-07-21 设立, 与策略仓同一反弹信号但加盈利门槛): 现价 ≥ 纳管价×(1+take_profit_min%)
    #   [默认 0=breakeven] 且触发均值回归信号 (收盘>SMA5 或 RSI2>rsi2_min) → 卖 (锁定收益)。
    #   盈利门槛确保只卖赢家 (亏损仓留给止损兜底, 不被无差别清仓); 与止损对称, 均属"风险不增"自动执行。
    tp_min_pct = float(cfg["legacy"].get("take_profit_min_pct", 0.0))
    tp_rsi2_min = float(cfg["legacy"].get("take_profit_rsi2_min", cfg["exit"]["rsi2_min"]))
    for sym, pos in legacy.items():
        i = ind.get(sym)
        if not i:
            warnings.append(f"{sym}: 存量持仓无行情数据")
            continue
        if i["close"] is None or i["sma5"] is None or i["rsi2"] is None:
            continue
        ref = float(pos["adoption_ref_price"])
        reason = None
        if i["close"] <= ref * (1 - cfg["legacy"]["protective_stop_pct"] / 100.0):
            reason = "legacy_protective_stop"
        elif (cfg["legacy"].get("take_profit_enabled", True)
              and i["close"] >= ref * (1 + tp_min_pct / 100.0)
              and (i["close"] > i["sma5"] or i["rsi2"] > tp_rsi2_min)):
            reason = "legacy_take_profit"
        if reason:
            if cfg["legacy"]["avoid_selling_same_day_buys"] and bought_today(sym):
                warnings.append(f"{sym}: 触发{reason}但今日有买入(GFV保护), 顺延到明天")
                continue
            qty = broker_qty(sym, float(pos["qty"]))
            if qty > 0:
                exiting.add(sym)
                out["sells"].append({"symbol": sym, "qty": floor6(qty), "bucket": "legacy",
                                     "reason": reason, "est_price": i["close"]})

    # --- 宏观风控: VIX 高位时暂停新开仓 (卖出/止损不受影响) ---
    # FRED 数据有发布延迟; 超过 vix_max_staleness_days 的旧数据不用于风控, 只告警。
    risk_off = False
    if macro and "vix" in macro:
        out["macro"] = macro
        mc = cfg.get("macro", {})
        vix_cap = mc.get("vix_no_new_entries_above")
        max_stale = int(mc.get("vix_max_staleness_days", 5))
        stale = False
        if macro.get("vix_date"):
            try:
                age = (_date.fromisoformat(today) - _date.fromisoformat(macro["vix_date"])).days
            except ValueError:
                age = None
            if age is None or age < 0 or age > max_stale:
                stale = True
                warnings.append(f"宏观: VIX 数据过旧或日期异常 (vix_date={macro['vix_date']}), 本日跳过宏观过滤")
        if not stale and vix_cap is not None and float(macro["vix"]) >= vix_cap:
            risk_off = True
            out["note"] = f"宏观风控: VIX {macro['vix']} ≥ {vix_cap}, 今日暂停新开仓"

    # --- 买入: ETF 入场信号 ---
    held = set(strat) - {s["symbol"] for s in out["sells"] if s["bucket"] == "strategy"}

    # 弱势排序的可卖存量持仓 (价格/SMA20 比值最低 = 最弱); 换仓与加速清理共用
    def weakness(sym):
        i = ind.get(sym) or {}
        if i.get("sma20"):
            return i["close"] / i["sma20"]
        return 1.0

    fundable = sorted(
        [s for s in legacy
         if s not in exiting and s not in cfg["etf_universe"]
         and ind.get(s) and not (cfg["legacy"]["avoid_selling_same_day_buys"] and bought_today(s))],
        key=weakness)

    def accelerated_liquidation():
        # 存量加速清理 (用户 2026-07-21 指示"加速换仓给引擎供血"): 与当日买入需求无关,
        # 每日额外卖出最弱存量最多 N 只, 卖出款 T+1 结算后归入引擎购买力。
        # semi_auto 下与换仓卖单同走 pending_orders.json 待用户「执行」; VIX 风控不拦卖出。
        acc = cfg["legacy"].get("accelerated_liquidation") or {}
        if not acc.get("enabled"):
            return
        n = 0
        while n < int(acc.get("max_sales_per_day", 0)) and fundable:
            lsym = fundable.pop(0)
            lqty = broker_qty(lsym, float(legacy[lsym]["qty"]))
            if lqty <= 0:
                continue
            out["sells"].append({"symbol": lsym, "qty": floor6(lqty), "bucket": "legacy",
                                 "reason": "accelerated_liquidation",
                                 "est_price": ind[lsym]["close"]})
            exiting.add(lsym)
            n += 1

    if risk_off:
        accelerated_liquidation()
        _emit(out, a.out)
        return

    # --- 加仓: 已持策略仓再跌 N% 补一档 (2026-08-07 用户「做加仓」批准) ---
    # 依据: 4352 信号无约束样本 + 217 对配对检验 (同票同入场日, 加仓版 vs 不加仓版):
    #   每笔改善 +2.13pp (bootstrap 95% 区间 +1.91~+2.37, t=18.2), 71% 的情况美元盈亏更好,
    #   2022-2026 分年胜率 63.6~77.8% 全为正。机制 = 反弹时多赚 177 笔小钱, 止损时多赔 21 笔大钱, 净正。
    # 口径 (与回测一致, 改动即失效): 触发价比**加权均价**; 止损同样比加权均价 (等效把首档止损
    #   外移到 ≈-8.4%, 这是加仓收益的一部分); 时间止损时钟**不因加仓重置**, 仍从首次入场起算。
    # 敞口: 单票上限 = position_pct × max_tranches (红线3 的上限, 只能由用户改)。
    # 加仓单同属买入, 受 semi_auto (红线9) 与 VIX 风控约束 —— risk_off 时上面已 return, 不会加仓。
    si = cfg.get("scale_in") or {}
    adds = []
    if si.get("enabled"):
        drop = float(si["trigger_drop_pct"]) / 100.0
        max_tr = int(si["max_tranches"])
        for sym in sorted(held):
            pos = strat[sym]
            i = ind.get(sym)
            if not i or i["close"] is None or i["rsi2"] is None:
                warnings.append(f"{sym}: 指标数据不足, 跳过加仓检查")
                continue
            if int(pos.get("tranches", 1)) >= max_tr:
                continue
            if i["close"] > float(pos["entry_price"]) * (1 - drop):
                continue
            if defense is not None and sym not in exempt_syms:
                ed = days_to_earnings(sym)
                if ed is not None and 0 <= ed <= int(defense.get("earnings_blackout_days", 10)):
                    warnings.append(f"{sym}: 触发加仓但 {ed} 天后财报, 防御性跳过")
                    continue
            adds.append(sym)

    cands = []
    for sym in universe:
        i = ind.get(sym)
        if not i or sym in held or i["sma200"] is None or i["rsi2"] is None:
            continue
        if not (i["close"] > i["sma200"] and i["rsi2"] < cfg["entry"]["rsi2_max"]):
            continue
        if defense is not None and sym not in exempt_syms:
            if no_earnings_data and not allow_unknown_earnings:
                continue
            if not no_earnings_data and sym not in earnings and not allow_unknown_earnings:
                warnings.append(f"{sym}: 财报日未知, 防御性跳过入场")
                continue
            ed = days_to_earnings(sym)
            if ed is not None and 0 <= ed <= int(defense.get("earnings_blackout_days", 10)):
                continue
            look = int(defense.get("move_lookback_days", 20))
            thr = float(defense.get("max_daily_move_pct", 8.0))
            # 财报上涨跳空豁免 (2026-08-17 用户「直接上实盘」授权, 依据
            # journal/2026-08-14-research-defense-move-filter.md: 财报**上涨**跳空后的信号
            # 均值 +1.01%/胜率 66.7% 不输基准 (+0.45%/66.3%), 而财报**下跌**跳空后为 -0.44%/53.3%
            # 明确负期望 —— 故只豁免上涨方向, 下跌异动与非财报异动一律照拦)。
            # 判定用真实财报日 (回测用跳空占比代理, 实盘用 earnings.past 更准), 无 past 数据则自动失效。
            exempt_gap_up = bool(defense.get("earnings_gap_up_exempt"))
            er_days = earnings_reaction_days(sym) if exempt_gap_up else set()
            dts = series[sym][0][-(look + 1):]
            w = series[sym][1][-(look + 1):]
            hit = None
            for j in range(1, len(w)):
                if not w[j - 1]:
                    continue
                chg = (w[j] / w[j - 1] - 1) * 100.0
                if abs(chg) < thr:
                    continue
                if chg > 0 and dts[j] in er_days:
                    continue        # 财报上涨跳空 → 豁免
                hit = (dts[j], chg)
                break
            if hit:
                warnings.append(f"{sym}: 近{look}日有单日异动 ≥{thr}% "
                                f"({hit[0]} {hit[1]:+.1f}%), 防御性跳过入场")
                continue
        cands.append((i["rsi2"], sym))
    cands.sort()

    slots = max(0, min(cfg["sizing"]["max_strategy_positions"] - len(held),
                       cfg["sizing"]["max_new_entries_per_day"]))

    # 行业上限: 已持仓 + 今日已选 计数
    sec_map = (defense or {}).get("sectors", {})
    sec_cap = (defense or {}).get("max_per_sector")
    sec_count = {}
    for s in held:
        sc = sec_map.get(s)
        if sc:
            sec_count[sc] = sec_count.get(sc, 0) + 1
    picked = []
    for _, sym in cands:
        if len(picked) >= slots:
            break
        sc = sec_map.get(sym)
        if sec_cap and sc and sec_count.get(sc, 0) >= sec_cap:
            warnings.append(f"{sym}: 行业 {sc} 已达持仓上限 {sec_cap}, 跳过")
            continue
        picked.append(sym)
        if sc:
            sec_count[sc] = sec_count.get(sc, 0) + 1
    pos_usd = round(pv * cfg["sizing"]["position_pct_of_portfolio"] / 100.0, 2)
    cash = bp - cfg["sizing"]["min_cash_reserve_usd"]
    cash += sum(s["qty"] * s["est_price"] for s in out["sells"])

    # 加仓单排在新开仓单前面吃现金 (回测: 加仓优先 +141.0% vs 新仓优先 +134.7% vs 不加仓 +123.2%;
    # 两种优先级都胜出, 取更优的一档)。加仓不触发存量换仓卖出 —— 只花手头现金, 不为补仓去砍存量。
    funding_sales = 0
    queue = [(s, "rsi2_scale_in") for s in adds] + [(s, "rsi2_entry") for s in picked]
    for sym, why in queue:
        need = pos_usd
        while (why == "rsi2_entry" and cash < need and cfg["legacy"]["funding_sales_allowed"]
               and funding_sales < cfg["legacy"]["max_funding_sales_per_day"] and fundable):
            lsym = fundable.pop(0)
            lqty = broker_qty(lsym, float(legacy[lsym]["qty"]))
            if lqty <= 0:
                continue
            lpx = ind[lsym]["close"]
            out["sells"].append({"symbol": lsym, "qty": floor6(lqty), "bucket": "legacy",
                                 "reason": "funding_rotation", "est_price": lpx})
            exiting.add(lsym)
            cash += lqty * lpx
            funding_sales += 1
        amt = round(min(need, cash), 2)
        if amt >= cfg["sizing"]["min_order_usd"]:
            order = {"symbol": sym, "dollar_amount": amt,
                     "reason": why, "est_price": ind[sym]["close"],
                     "rsi2": round(ind[sym]["rsi2"], 2)}
            if why == "rsi2_scale_in":
                held_pos = strat[sym]
                avg = float(held_pos["entry_price"])
                order["tranche"] = int(held_pos.get("tranches", 1)) + 1
                order["avg_entry_price"] = avg
                order["drawdown_pct"] = round((ind[sym]["close"] / avg - 1) * 100, 2)
            out["buys"].append(order)
            cash -= amt
        else:
            kind = "加仓" if why == "rsi2_scale_in" else "入场"
            warnings.append(f"{sym}: 有{kind}信号但可用资金不足 (${cash:.2f}), 跳过")

    accelerated_liquidation()
    _emit(out, a.out)


def _emit(out, path):
    s = json.dumps(out, indent=2, ensure_ascii=False)
    if path:
        with open(path, "w") as f:
            f.write(s + "\n")
    print(s)


# ---------- apply ----------

def cmd_apply(a):
    state = load_json(a.state)
    fills = load_json(a.fills)
    today = a.date
    for f in fills["fills"]:
        sym, side = f["symbol"], f["side"]
        qty, price = float(f["qty"]), float(f["price"])
        bucket = f.get("bucket", "strategy")
        if side == "buy":
            book = state.setdefault("strategy_positions", {})
            if sym in book:
                # 同标的追加买入 (4C ②腿补零头, 或 scale_in 加仓档): 加权入仓 — 量相加、成本相加、
                # entry_price = 总成本/总量、entry_date 保留原始 (holding-days/time-stop 以首次入场起算)
                old = book[sym]
                old_cost = float(old.get("cost", float(old["qty"]) * float(old["entry_price"])))
                new_qty = round(float(old["qty"]) + qty, 6)
                new_cost = round(old_cost + qty * price, 2)
                # tranches 只在真正的加仓档上 +1; ②腿 (rsi2_*_leg2) 属同一档的余量, 不计数
                book[sym] = {
                    "qty": new_qty,
                    "entry_price": round(new_cost / new_qty, 4) if new_qty else price,
                    "entry_date": old.get("entry_date", today),
                    "cost": new_cost,
                    "tranches": int(old.get("tranches", 1))
                                + (1 if f.get("reason") == "rsi2_scale_in" else 0),
                }
            else:
                book[sym] = {
                    "qty": qty, "entry_price": price, "entry_date": today,
                    "cost": round(qty * price, 2), "tranches": 1,
                }
        else:
            book = state.get("strategy_positions" if bucket == "strategy" else "legacy_positions", {})
            if sym in book:
                remaining = float(book[sym]["qty"]) - qty
                if remaining <= 1e-6:
                    del book[sym]
                else:
                    book[sym]["qty"] = round(remaining, 6)
        state.setdefault("trades", []).append({
            "date": today, "symbol": sym, "side": side, "qty": qty,
            "price": price, "bucket": bucket, "reason": f.get("reason", ""),
        })
    if a.portfolio_value:
        pv = float(a.portfolio_value)
        state["high_water_mark"] = max(float(state.get("high_water_mark", pv)), pv)
    save_json(a.state, state)
    print(f"state updated: {a.state} ({len(fills['fills'])} fills)")


# ---------- main ----------

def main():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("signal", help="计算今天的订单")
    s.add_argument("--config", required=True)
    s.add_argument("--state", required=True)
    s.add_argument("--historicals", nargs="+", required=True,
                   help="get_equity_historicals 原始输出文件(可多个)")
    s.add_argument("--quotes", required=True,
                   help='实时报价: {"SYM": price} 或 get_equity_quotes 原始输出')
    s.add_argument("--positions", help="券商持仓文件(简单映射或原始输出), 用于校准可卖数量/当日买入")
    s.add_argument("--macro", help='宏观数据文件 {"vix": 16.5, ...} (integrations.py macro 产出); 缺省则不做宏观过滤')
    s.add_argument("--earnings", help='财报日映射。新格式 (2026-08-17): '
                                      '{"SYM": {"next": "YYYY-MM-DD"|null, "past": ["YYYY-MM-DD", ...]}}, '
                                      'past = 已发生的财报日 (get_earnings_results 返回的历史季度), '
                                      '供 defense.earnings_gap_up_exempt 判定财报上涨跳空豁免; '
                                      '旧格式 {"SYM": "YYYY-MM-DD"|null} 仍受支持 (豁免自动失效)。'
                                      'config 含 defense 而未提供此文件时, 防御性跳过全部新入场')
    s.add_argument("--date", required=True, help="今天日期 YYYY-MM-DD")
    s.add_argument("--portfolio-value", required=True)
    s.add_argument("--buying-power", required=True)
    s.add_argument("--out", help="订单 JSON 输出路径")
    s.set_defaults(func=cmd_signal)

    ap = sub.add_parser("apply", help="把成交回写进 state")
    ap.add_argument("--state", required=True)
    ap.add_argument("--fills", required=True,
                    help='{"fills": [{symbol, side, qty, price, bucket, reason}]}')
    ap.add_argument("--date", required=True)
    ap.add_argument("--portfolio-value")
    ap.set_defaults(func=cmd_apply)

    a = p.parse_args()
    a.func(a)


if __name__ == "__main__":
    main()
