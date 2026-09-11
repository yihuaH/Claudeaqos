#!/usr/bin/env python3
"""
Claudeaqos 每日主跑驱动器 — 把 playbook 中所有"可脚本化"的步骤合并成一条命令。

  python3 scripts/daily.py --date 2026-08-06 \
      --portfolio-value 6171.42 --buying-power 1485.31 \
      --positions <MCP持仓映射.json> --earnings <MCP财报.json> \
      --workdir <scratchpad> --out <plan.json>

会话侧只剩三件必须走 MCP 的事 (本脚本碰不到, 也不应碰):
  1. 跑本脚本**前**: get_portfolio / get_equity_positions / 财报 → 存成 --positions/--earnings 输入;
  2. 跑本脚本**后**: 按 plan.json 的 place_now (4A 卖单 + 4D 期权出场) 逐单 review→place;
     plan.json 的 to_pending (买单) 写 pending 文件待用户「执行」(红线9, 绝不无人值守下单);
  3. 写 journal (用 plan.json 的 journal_facts) + commit/push。

本脚本**只调用各确定性引擎, 绝不重新实现任何决策逻辑** (红线2):
  signals.py / weekly_calls.py / momentum.py / overnight.py / learn.py / paper.py / integrations.py
每条命令与其输出摘要都记入 plan.json 的 command_log, 可审计、可复现。

任何预期外错误 → 立即停止该阶段, 记入 plan.json 的 anomalies, 不吞异常 (红线6)。
纸面轨道 (paper) 失败不影响实盘阶段的产出。
"""
import argparse
import glob
import json
import os
import re
import subprocess
import sys
import time
from datetime import date as _date, datetime, timedelta, timezone

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = sys.executable


def load(path, default=None):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        if default is not None:
            return default
        raise


def save(path, obj):
    with open(path, "w") as f:
        json.dump(obj, f, indent=2, ensure_ascii=False)
        f.write("\n")


class Runner:
    """跑子命令并记录审计日志; 失败按 critical 决定是否中止本阶段。"""

    def __init__(self):
        self.log = []
        self.anomalies = []
        self.t0 = time.monotonic()

    def elapsed(self):
        return round(time.monotonic() - self.t0, 1)

    def run(self, args, label, critical=True, timeout=900):
        cmd = [PY] + args if args[0].endswith(".py") else args
        _t = time.monotonic()
        r = subprocess.run(cmd, capture_output=True, text=True, cwd=REPO, timeout=timeout)
        # 逐步耗时 (2026-09-11 加): 收盘前关键路径的真实用时是当初退役的核心未知数,
        # 也是用户跳过影子验证后唯一没有数据的一项 —— 让它首日自动产出, 不靠人掐表。
        entry = {"label": label, "cmd": " ".join(args), "rc": r.returncode,
                 "seconds": round(time.monotonic() - _t, 1),
                 "at_seconds": round(_t - self.t0, 1)}
        if r.returncode != 0:
            entry["stderr"] = (r.stderr or "")[-800:]
            self.log.append(entry)
            msg = f"{label} 失败 (rc={r.returncode}): {(r.stderr or '')[-300:]}"
            self.anomalies.append(msg)
            if critical:
                raise RuntimeError(msg)
            return None
        out = (r.stdout or "").strip()
        entry["stdout_tail"] = out[-400:] if out else ""
        self.log.append(entry)
        return out


def parse_json_out(text):
    """引擎多为 stdout 打印 JSON; 取最后一个完整 JSON 对象。"""
    if not text:
        return None
    s = text.strip()
    i = s.find("{")
    if i < 0:
        return None
    try:
        return json.loads(s[i:])
    except ValueError:
        for line in reversed(s.splitlines()):
            line = line.strip()
            if line.startswith("{"):
                try:
                    return json.loads(line)
                except ValueError:
                    continue
    return None


# ---------- 符号收集 ----------

def collect_symbols(cfg, uni, plan):
    """一次性收齐所有引擎需要的标的 — 含持仓中已被剔出股池者 (2026-08-05 教训)。"""
    groups = {}
    groups["etf"] = list(cfg.get("etf_universe", []))
    groups["stock_pool"] = list(uni.get("symbols", []))
    live = load(f"{REPO}/state/positions.json", {})
    groups["live_holdings"] = sorted(set(live.get("strategy_positions", {}))
                                     | set(live.get("legacy_positions", {})))
    paper = load(f"{REPO}/state/paper_positions.json", {})
    groups["paper_holdings"] = sorted(paper.get("strategy_positions", {}))
    mom = load(f"{REPO}/strategy/momentum.json", {})
    groups["momentum"] = list(mom.get("universe", [])) if mom.get("enabled") else []
    ov = load(f"{REPO}/state/overnight_positions.json", {})
    groups["overnight_holdings"] = sorted(ov.get("strategy_positions", {}))

    opt = []
    for c in ("weekly_calls.json", "weekly_calls_live.json"):
        wc = load(f"{REPO}/strategy/{c}", {})
        if wc.get("enabled"):
            opt += list(wc.get("universe", []))
    for led in ("weekly_call_positions.json", "weekly_call_live_positions.json"):
        L = load(f"{REPO}/state/{led}", {})
        opt += [p["underlying"] for p in (L.get("positions") or {}).values()]
    groups["option"] = sorted(set(opt))

    allsyms = sorted({s for g in groups.values() for s in g})
    plan["symbol_groups"] = {k: len(v) for k, v in groups.items()}
    plan["symbols_total"] = len(allsyms)
    return allsyms, groups


# ---------- 阶段 ----------

def normalize_quotes(path):
    """把券商 get_equity_quotes 原始输出归一成 {"SYM": price} —— 口径与 signals.parse_quotes 一致
    (取 quote.last_trade_price, 只收 state=active)。momentum/options_overlay/price_check 只认简单
    映射, 故在驱动器这一层统一归一, 下游引擎一行都不用改。"""
    raw = load(path)
    if "data" in raw:
        return {r["quote"]["symbol"]: float(r["quote"]["last_trade_price"])
                for r in raw["data"]["results"]
                if r["quote"].get("state") == "active"
                and r["quote"].get("last_trade_price") is not None}
    return {k: float(v) for k, v in raw.items()}


PRECLOSE_MARKER = "state/preclose_status.json"

# 协议常量集中在驱动器里, **由会话照抄, 不得凭记忆composing** —— 2026-09-10 事故:
# 4C 换代当晚主跑会话 fetch 到新代码但沿用对话记忆里的旧口径, 把 valid_until 写成已退役的
# 09:25 ET (commit 79a49ad 事后修正)。散文契约靠会话自觉, 机器可抄的字段才靠得住。
PENDING_TEMPLATES = {
    # 收盘前阶段: 清单当日有效, 用户在本次跑完后到收盘前执行 (出场卖单已即时成交, 回款已到账)
    "preclose": {
        "valid_until": "当日 15:55 ET (执行窗 15:30-15:55, 收盘前主跑产出)",
        "order_valid_until": "same_session_1555_et",
        "exec_window_et": "15:30-15:55",
        "exec_day": "same_day",
        "funding_note": "出场卖单已在本阶段即时成交, 回款即时可用 (limited margin); "
                        "执行时仍取实时 min(buying_power, cash) 为上限, 超出整单跳过不缩量",
    },
    # 收盘后阶段 (含 wrapup 回退): 清单次日有效, 必须在 09:30 开盘之后执行
    "full": {
        "valid_until": "次一交易日 15:55 ET (执行窗 09:45-15:55, 推荐锚 10:45 晨检)",
        "order_valid_until": "next_session_1555_et",
        "exec_window_et": "09:45-15:55",
        "exec_day": "next_session",
        "funding_note": "**必须在 09:30 开盘之后执行** —— 出场回款次日开盘才到账; "
                        "执行时取实时 min(buying_power, cash), 不得用 buying_power_at_generation",
    },
}


# 期权待执行清单的时效 —— 与股票分开, 因为期权有自己的点差纪律 (playbook 4D:
# 避开开盘头 15 分钟与**收盘前 15 分钟**的极端点差), 且开仓走手动通道 (agentic 不支持多腿 place)。
OPTION_PENDING_TEMPLATES = {
    # 收盘前产出 (2026-09-11 用户「期权也可以当日马上执行」): 当日执行, 但窗口比股票**更早收口** ——
    # 15:45 后是 4D 明令避开的收盘前极端点差区, 不因为求快就破这条纪律。
    "preclose": {
        "valid_until": "当日 15:45 ET (执行窗 15:30-15:45, 收盘前主跑产出)",
        "order_valid_until": "same_session_1545_et",
        "exec_window_et": "15:30-15:45",
        "exec_day": "same_day",
        "channel_note": "开仓仍走**手动通道** (agentic 暂不支持多腿 place, playbook 4D 注记): "
                        "会话在对话给出 App 参数, 用户手动下单。窗口仅 15 分钟, 会话须"
                        "**先发期权参数再发股票清单** (期权窗口更窄)",
        "spread_note": "15:45 后不得下单 —— 收盘前 15 分钟点差极端 (4D 既有纪律)。"
                       "窗口内未成交 → 次日主跑以新数据重评, 绝不追价 (红线2)",
    },
    # 收盘后产出 (含 wrapup 回退): 沿用 2026-08-04 用户批准的次日 10:30 窗口
    "full": {
        "valid_until": "次一交易日 10:30 ET (推荐执行窗 09:45-10:30, 等开盘点差收窄)",
        "order_valid_until": "next_session_1030_et",
        "exec_window_et": "09:45-10:30",
        "exec_day": "next_session",
        "channel_note": "开仓走手动通道 (同上)",
        "spread_note": "避开开盘头 15 分钟的极端点差; 未成交按 4D 处置 (撤单, 绝不改限价追单)",
    },
}


def option_pending_template(eff_phase):
    """会话写 pending_option_orders.json 时逐字段照抄 (playbook 4D)。"""
    if eff_phase == "wrapup":
        return {"applicable": False,
                "valid_until": "不适用 — 本阶段不产出期权 pending",
                "exec_window_et": "不适用", "exec_day": "不适用",
                "_note": "当日期权清单由 --phase preclose 产出"}
    t = dict(OPTION_PENDING_TEMPLATES["preclose" if eff_phase == "preclose" else "full"])
    t["_note"] = "由 daily.py 按 effective_phase 产出, 会话**照抄不改写**"
    return t


def pending_template(eff_phase):
    """会话写 pending_orders.json 时必须逐字段照抄本模板 (playbook 4B)。"""
    if eff_phase == "wrapup":
        # 正常 wrapup 不产出任何 pending (当日清单已由 preclose 产出并可能已执行);
        # 这里**不能**回落到次日模板, 否则会话会误以为要再写一份 (2026-09-10 09:25 事故同型)
        return {"applicable": False,
                "valid_until": "不适用 — 本阶段不产出 pending",
                "exec_window_et": "不适用",
                "exec_day": "不适用",
                "_note": "当日 pending 由 --phase preclose 产出; wrapup 只跑纸面轨道与行情核对。"
                         "若本阶段 fail-safe 退化为 full, effective_phase 会是 full 并给出次日模板"}
    t = dict(PENDING_TEMPLATES["preclose" if eff_phase == "preclose" else "full"])
    t["_note"] = ("由 daily.py 按 effective_phase 产出, 会话**照抄不改写**; "
                  "隔夜轨道买单另有 same_day_1555_et, 见 playbook 4B")
    return t


def phase_preflight(a, R, plan):
    out = R.run(["scripts/integrations.py", "status"], "integrations.status")
    st = parse_json_out(out) or {}
    plan["data_sources"] = st
    if not st.get("all_ok"):
        R.anomalies.append(f"数据源自诊断未全 ok: {json.dumps(st, ensure_ascii=False)}")
        if not a.force:
            raise RuntimeError("数据源不可用, 按红线6 停止 (--force 可强制继续)")
    plan["macro_vix"] = (st.get("fred") or {}).get("vix")
    # 幂等判定 (2026-09-10 二次修正)。两个独立缺陷:
    #   ① 假阳性 (09-03): 原为对整个文件做子串匹配, 晨检段落误写主跑标记就把当晚主跑判成已跑过
    #      → 改为**锚定行首**, 只认独立成行的 `status: completed`;
    #   ② 假阴性 (09-10 发现): 原只查 `journal/<date>.md`, 但当日哪个窗口先跑就先占这个文件名,
    #      后到的窗口写 `<date>-main.md` 等后缀文件。09-10 主跑的标记就在 `-main.md` 里,
    #      固定文件名的闸门**完全看不到**, 等于主跑幂等一直是失效的 (Routine 重复触发会跑两遍)
    #      → 改为扫当日**全部** `journal/<date>*.md`。
    # 晨检用的是另一个键 `morning_check: completed`, 与本闸互不干扰 (键不同正是为此)。
    # 每个阶段守自己的标记 —— 否则收盘前写完 `status: completed`, 同日 17:45 的 wrapup 会被自己挡住。
    #   --phase preclose      → 认 `preclose: completed`
    #   --phase wrapup / full → 认 `status: completed` (= 当日收尾完成)
    # 晨检的 `morning_check: completed` 与两者都不冲突。
    _key = "preclose" if a.phase == "preclose" else "status"
    jrs = sorted(glob.glob(f"{REPO}/journal/{a.date}*.md"))
    _done_in = [os.path.basename(f) for f in jrs
                if re.search(rf"^\s*{_key}:\s*completed\s*$", open(f).read(), re.M)]
    plan["idempotency"] = {"phase": a.phase, "marker_key": f"{_key}: completed",
                           "scanned": [os.path.basename(f) for f in jrs],
                           "completed_in": _done_in}
    jr = f"{REPO}/journal/{a.date}.md"
    if _done_in and not a.force:
        plan["idempotent_skip"] = True
        raise SystemExit(json.dumps(
            {"idempotent_skip": True,
             "note": f"本阶段 ({a.phase}) 当日已完成 "
                     f"({_key}: completed 见 {', '.join(_done_in)}), 幂等结束",
             "scanned": plan["idempotency"]["scanned"]}, ensure_ascii=False))

    # 持仓一致性闸 (2026-08-11 用户「做」批准, 起因 MNST 2:1 拆股):
    # 引擎日线是拆股调整后的 (integrations.py adjustment=split), 账本 entry_price 不是 —
    # 两者不同步时引擎会算出巨额假回撤并触发止损, 而出场卖单是全自动的 (不受 semi_auto 约束),
    # 会在无人干预下卖掉实际没亏的仓位。故放在 preflight 末尾: 早于昂贵的 bars 阶段就停住。
    # 复用主跑已有的 --positions 输入, 不额外调 MCP。
    if a.positions:
        out = R.run(["scripts/position_check.py", "--broker", a.positions,
                     "--state", f"{REPO}/state/positions.json",
                     "--out", f"{a.workdir}/position_check.json"],
                    "position_check", critical=False)
        pk = parse_json_out(out) or {}
        plan["position_check"] = {k: pk.get(k) for k in
                                  ("verdict", "compared", "matched", "mismatched",
                                   "kinds", "note")}
        plan["position_check"]["anomalies"] = pk.get("anomalies", [])
        if pk.get("verdict") == "fail":
            R.anomalies.append(f"券商持仓与账本不一致: {pk.get('note')} "
                               f"明细 {json.dumps(pk.get('anomalies'), ensure_ascii=False)}")
            if not a.force:
                raise RuntimeError("券商持仓与账本不一致, 按红线6 停止 (--force 可强制继续)")
    else:
        plan["position_check"] = {"verdict": "skipped",
                                  "note": "未提供 --positions, 本次未做持仓一致性核对"}


def phase_data(a, R, plan, allsyms):
    W = a.workdir
    R.run(["scripts/integrations.py", "macro", "--out", f"{W}/macro.json"], "macro")
    start = (_date.fromisoformat(a.date) - timedelta(days=a.bars_days)).isoformat()
    save(f"{W}/allsyms.json", {"symbols": allsyms})
    R.run(["scripts/integrations.py", "bars", "--symbols-file", f"{W}/allsyms.json",
           "--start", start, "--out", f"{W}/bars.json"], "bars", timeout=1800)
    got = load(f"{W}/bars.json", {})
    nres = len((got.get("data") or {}).get("results", []))
    plan["bars_symbols"] = nres
    if nres < len(allsyms) * 0.9:
        R.anomalies.append(f"bars 覆盖不足: {nres}/{len(allsyms)}")
    # 当日报价。--quotes 给了就**完全取代** integrations.py quotes —— 绝不混用, 否则会悄悄
    # 掺进延迟 15 分钟的价 (收盘前阶段的信号就错了)。缺的标的宁可让 signals.py warn 出来。
    if a.quotes:
        qall = normalize_quotes(a.quotes)
        plan["quotes_source"] = {"mode": "override", "file": a.quotes, "symbols": len(qall),
                                 "note": "券商实时报价 (get_equity_quotes.last_trade_price), "
                                         "未调用 integrations.py quotes"}
        R.log.append({"label": "quotes[override]", "cmd": f"(read {a.quotes})", "rc": 0,
                      "seconds": 0.0, "at_seconds": R.elapsed(),
                      "stdout_tail": f"{len(qall)} symbols from broker quotes"})
    else:
        if a.phase == "preclose":
            R.anomalies.append(
                "收盘前阶段未传 --quotes: 会退回 integrations.py quotes (delayed_sip 延迟 15 分钟), "
                "当日价失真 → 按红线6 停跑, 补券商实时报价后重跑")
            raise RuntimeError("收盘前阶段必须传 --quotes (券商实时报价)")
        qall = {}
        for i in range(0, len(allsyms), 100):
            chunk = allsyms[i:i + 100]
            p = f"{W}/quotes_{i}.json"
            R.run(["scripts/integrations.py", "quotes", "--symbols", ",".join(chunk), "--out", p],
                  f"quotes[{i}]")
            qall.update(load(p, {}))
        plan["quotes_source"] = {"mode": "integrations", "symbols": len(qall),
                                 "note": "Alpaca delayed_sip; 收盘后阶段用当日收盘价, 延迟无影响"}
    save(f"{W}/quotes.json", qall)
    plan["quotes_symbols"] = len(qall)
    missing = [s for s in allsyms if s not in qall]
    if missing:
        R.anomalies.append(f"无报价标的 {len(missing)}: {missing[:10]}")
    # 收盘前阶段的覆盖率闸: 缺口 >10% 说明报价没取全, 大面积用昨收算信号 = 信号失真 (红线6)
    if a.quotes and len(qall) < len(allsyms) * 0.9:
        R.anomalies.append(
            f"实时报价覆盖不足: {len(qall)}/{len(allsyms)} (<90%) —— 缺的标的会用昨日收盘价算信号, "
            f"当日信号失真, 按红线6 停跑")
        if a.phase == "preclose":
            raise RuntimeError(f"收盘前实时报价覆盖不足 {len(qall)}/{len(allsyms)}")

    # 行情管道交叉核对 (2026-08-08 用户批准): 引擎用的 Alpaca SIP 收盘 vs 券商官方收盘。
    # 两者同源 (Robinhood close.source = sip-list-exchange-close), 应完全一致;
    # 持续偏差 = 管道异常 (口径被改回 iex / 数据源故障 / 分割调整不一致), 必须在下单前发现。
    # --broker-closes 由会话经 MCP 取回后落盘; 未提供则跳过 (不阻断主跑)。
    if a.phase == "preclose":
        plan["price_check"] = {"verdict": "skipped",
                               "note": "收盘前阶段无券商官方收盘, 交叉核对留到 wrapup 阶段"}
    elif a.broker_closes:
        out = R.run(["scripts/price_check.py", "--bars", f"{W}/bars.json",
                     "--quotes", f"{W}/quotes.json", "--broker", a.broker_closes,
                     "--date", a.date, "--out", f"{W}/price_check.json"],
                    "price_check", critical=False)
        pc = parse_json_out(out) or {}
        plan["price_check"] = {k: pc.get(k) for k in
                               ("verdict", "compared", "exact_match", "exact_pct",
                                "mean_abs_dev_bp", "max_abs_dev_bp", "note")}
        plan["price_check"]["warnings"] = pc.get("warnings", [])
        if pc.get("verdict") == "fail":
            R.anomalies.append(f"行情管道交叉核对失败: {pc.get('note')} "
                               f"明细 {json.dumps(pc.get('anomalies'), ensure_ascii=False)}")
        elif pc.get("verdict") == "warn":
            plan.setdefault("soft_warnings", []).append(f"行情核对: {pc.get('note')}")
    else:
        plan["price_check"] = {"verdict": "skipped",
                               "note": "未提供 --broker-closes, 本次未做券商侧交叉核对"}


def phase_stock_signal(a, R, plan):
    W = a.workdir
    args = ["scripts/signals.py", "signal",
            "--config", "strategy/config.json", "--state", "state/positions.json",
            "--historicals", f"{W}/bars.json", "--quotes", f"{W}/quotes.json",
            "--macro", f"{W}/macro.json", "--date", a.date,
            "--portfolio-value", str(a.portfolio_value), "--buying-power", str(a.buying_power),
            "--out", f"{W}/orders.json"]
    if a.positions:
        args += ["--positions", a.positions]
    if a.earnings:
        args += ["--earnings", a.earnings]
    R.run(args, "signals.signal")
    o = load(f"{W}/orders.json")
    plan["stock"] = {
        "halted": o.get("halted"), "circuit_breaker": o.get("circuit_breaker_triggered"),
        "drawdown_pct": o.get("drawdown_pct"), "high_water_mark": o.get("high_water_mark"),
        "note": o.get("note"),
        "warnings": [w for w in o.get("warnings", []) if "无实时报价" not in w][:12],
        "candidates": sorted(
            [(round(v["rsi2"], 2), s) for s, v in (o.get("indicators") or {}).items()
             if v.get("rsi2") is not None and v.get("sma200") and v.get("close")
             and v["rsi2"] < 10 and v["close"] > v["sma200"]])[:15],
    }
    exitish = {"funding_rotation", "accelerated_liquidation"}
    plan["place_now"] = {"equity_sells": [s for s in o.get("sells", [])
                                          if s.get("reason") not in exitish]}
    plan["to_pending"] = {"equity_buys": o.get("buys", []),
                          "equity_rotation_sells": [s for s in o.get("sells", [])
                                                    if s.get("reason") in exitish]}
    return o


def phase_options(a, R, plan):
    W = a.workdir
    live_cfg = load(f"{REPO}/strategy/weekly_calls_live.json", {})
    paper_cfg = load(f"{REPO}/strategy/weekly_calls.json", {})
    unis = []
    for c in (live_cfg, paper_cfg):
        if c.get("enabled"):
            unis += c.get("universe", [])
    for led in ("weekly_call_positions.json", "weekly_call_live_positions.json"):
        L = load(f"{REPO}/state/{led}", {})
        unis += [p["underlying"] for p in (L.get("positions") or {}).values()]
    unis = sorted(set(unis))
    if not unis:
        plan["options"] = {"skipped": "两轨均 disabled"}
        return
    dte_max = max(int(c.get("contract", {}).get("max_dte_calendar", 17))
                  for c in (live_cfg, paper_cfg) if c.get("enabled"))
    # 收盘前阶段期权窗只有 15 分钟 (15:30-15:45), 链拉取在关键路径上 —— 原 900s 超时比整个窗口还长,
    # 拖到 15:45 后期权单等于作废, 还顺带挤掉股票的时间。收盘前收紧到 240s, 超时就当日跳过期权轨道
    # (phase_options 在 preclose 下被调用方捕获为 anomaly, 正股不受影响)。
    chains_timeout = 240 if a.phase == "preclose" else 900
    R.run(["scripts/integrations.py", "chains", "--underlyings", ",".join(unis),
           "--date", a.date, "--dte-max", str(dte_max), "--out", f"{W}/chains.json"],
          "option.chains", critical=False, timeout=chains_timeout)
    if not os.path.exists(f"{W}/chains.json"):
        plan["options"] = {"error": f"期权链拉取失败或超时 ({chains_timeout}s), 本日期权轨道跳过"}
        if a.phase == "preclose":
            R.anomalies.append(
                f"收盘前期权链 {chains_timeout}s 内未取回 → 今日期权轨道跳过 (正股不受影响); "
                f"不要为等链而拖过 15:45 期权窗")
        return
    plan["options"] = {}
    # --plan-only: 只算信号, 不碰账本/不排纸面单 (输出改写 workdir)
    po = a.plan_only
    paper_ctx = f"{W}/wc_last_orders.json" if po else "state/weekly_call_last_orders.json"
    live_ctx = f"{W}/wc_live_last_orders.json" if po else "state/weekly_call_live_last_orders.json"

    # paper 轨道: 先回收昨日队列, 再出信号并排队
    if paper_cfg.get("enabled") and not po:
        q = f"{REPO}/state/paper_queued_weekly_calls.json"
        if os.path.exists(q):
            R.run(["scripts/paper.py", "sync", "--queued", q,
                   "--fills-out", f"{W}/wc_sync_fills.json", "--prune"],
                  "wc.paper.sync", critical=False)
            if os.path.exists(f"{W}/wc_sync_fills.json"):
                R.run(["scripts/weekly_calls.py", "apply",
                       "--ledger", "state/weekly_call_positions.json",
                       "--fills", f"{W}/wc_sync_fills.json",
                       "--context", "state/weekly_call_last_orders.json", "--date", a.date],
                      "wc.paper.apply_sync", critical=False)
        args = ["scripts/weekly_calls.py", "signal", "--config", "strategy/weekly_calls.json",
                "--ledger", "state/weekly_call_positions.json",
                "--bars", f"{W}/bars.json", "--quotes", f"{W}/quotes.json",
                "--chains", f"{W}/chains.json", "--date", a.date,
                "--out", paper_ctx]
        if a.earnings:
            args += ["--earnings", a.earnings]
        if R.run(args, "wc.paper.signal", critical=False) is not None:
            o = load(paper_ctx if paper_ctx.startswith("/") else f"{REPO}/{paper_ctx}", {})
            plan["options"]["paper"] = {"buys": len(o.get("buys", [])),
                                        "sells": len(o.get("sells", [])),
                                        "skips": o.get("skips", [])}
            if o.get("buys") or o.get("sells"):
                R.run(["scripts/paper.py", "run", "--orders", paper_ctx,
                       "--date", a.date, "--coid-prefix", "cqw",
                       "--fills-out", f"{W}/wc_fills.json", "--allow-queue",
                       "--queued-out", "state/paper_queued_weekly_calls.json"],
                      "wc.paper.run", critical=False)
            fills = f"{W}/wc_fills.json"
            if not os.path.exists(fills):
                save(fills, {"fills": []})
            R.run(["scripts/weekly_calls.py", "apply", "--ledger", "state/weekly_call_positions.json",
                   "--fills", fills, "--context", paper_ctx,
                   "--date", a.date], "wc.paper.apply", critical=False)

    # 实盘轨道: 出场卖单交会话 place; 买单进 pending
    if live_cfg.get("enabled"):
        args = ["scripts/weekly_calls.py", "signal", "--config", "strategy/weekly_calls_live.json",
                "--ledger", "state/weekly_call_live_positions.json",
                "--bars", f"{W}/bars.json", "--quotes", f"{W}/quotes.json",
                "--chains", f"{W}/chains.json", "--date", a.date,
                "--buying-power", str(a.buying_power),
                "--portfolio-value", str(a.portfolio_value),
                "--out", live_ctx]
        if a.earnings:
            args += ["--earnings", a.earnings]
        if R.run(args, "wc.live.signal", critical=False) is not None:
            o = load(live_ctx if live_ctx.startswith("/") else f"{REPO}/{live_ctx}", {})
            plan["place_now"]["option_sells"] = o.get("sells", [])
            plan["to_pending"]["option_buys"] = o.get("buys", [])
            plan["to_pending"]["near_signals"] = o.get("near_signals", [])
            plan["options"]["live"] = {"buys": len(o.get("buys", [])),
                                       "sells": len(o.get("sells", [])),
                                       "skips": o.get("skips", []),
                                       "near_signals": o.get("near_signals", []),
                                       "warnings": o.get("warnings", [])}
            out = R.run(["scripts/weekly_calls.py", "report",
                         "--config", "strategy/weekly_calls_live.json",
                         "--ledger", "state/weekly_call_live_positions.json",
                         "--chains", f"{W}/chains.json", "--date", a.date],
                        "wc.live.report", critical=False)
            plan["options"]["live_report"] = parse_json_out(out)


def phase_paper(a, R, plan):
    """挑战者影子验证 + 动量轮动 (全 paper, 失败不影响实盘产出)。"""
    W = a.workdir
    plan["paper"] = {}
    if a.plan_only:
        plan["paper"] = {"skipped": "--plan-only"}
        return
    # --- 挑战者 ---
    lc = load(f"{REPO}/strategy/learning.json", {})
    if lc.get("enabled"):
        try:
            q = f"{REPO}/state/paper_queued_challenger.json"
            if os.path.exists(q):
                R.run(["scripts/paper.py", "sync", "--queued", q,
                       "--fills-out", f"{W}/ch_sync.json", "--prune"], "ch.sync", critical=False)
                if os.path.exists(f"{W}/ch_sync.json"):
                    R.run(["scripts/signals.py", "apply", "--state", "state/paper_positions.json",
                           "--fills", f"{W}/ch_sync.json", "--date", a.date],
                          "ch.apply_sync", critical=False)
            R.run(["scripts/learn.py", "challenger-config", "--config", "strategy/config.json",
                   "--state-learn", "state/learning.json", "--out", f"{W}/ch_config.json"],
                  "ch.config", critical=False)
            eq = R.run(["scripts/paper.py", "equity", "--ledger", "state/paper_positions.json",
                        "--quotes", f"{W}/quotes.json"], "ch.equity", critical=False)
            E = parse_json_out(eq) or {}
            pv = E.get("equity_ex_options")
            plan["paper"]["challenger_equity"] = E
            if pv:
                led = load(f"{REPO}/state/paper_positions.json", {})
                pmap = {k: {"qty": v["qty"], "available": v["qty"], "intraday": 0}
                        for k, v in led.get("strategy_positions", {}).items()}
                save(f"{W}/ch_positions.json", pmap)
                args = ["scripts/signals.py", "signal", "--config", f"{W}/ch_config.json",
                        "--state", "state/paper_positions.json",
                        "--historicals", f"{W}/bars.json", "--quotes", f"{W}/quotes.json",
                        "--positions", f"{W}/ch_positions.json", "--macro", f"{W}/macro.json",
                        "--date", a.date, "--portfolio-value", str(pv),
                        "--buying-power", str(E.get("cash", 0)), "--out", f"{W}/ch_orders.json"]
                if a.earnings:
                    args += ["--earnings", a.earnings]
                if R.run(args, "ch.signal", critical=False) is not None:
                    co = load(f"{W}/ch_orders.json", {})
                    plan["paper"]["challenger"] = {
                        "sells": [(s["symbol"], s["reason"]) for s in co.get("sells", [])],
                        "buys": [(b["symbol"], b["dollar_amount"]) for b in co.get("buys", [])]}
                    if co.get("sells") or co.get("buys"):
                        R.run(["scripts/paper.py", "run", "--orders", f"{W}/ch_orders.json",
                               "--date", a.date, "--coid-prefix", "cq",
                               "--fills-out", f"{W}/ch_fills.json", "--allow-queue",
                               "--queued-out", "state/paper_queued_challenger.json"],
                              "ch.run", critical=False)
                R.run(["scripts/learn.py", "record", "--state-learn", "state/learning.json",
                       "--date", a.date, "--live-equity", str(a.portfolio_value),
                       "--paper-equity", str(pv)], "ch.record", critical=False)
                ev = R.run(["scripts/learn.py", "evaluate", "--learning", "strategy/learning.json",
                            "--state-learn", "state/learning.json",
                            "--paper-ledger", "state/paper_positions.json", "--date", a.date],
                           "ch.evaluate", critical=False)
                plan["paper"]["challenger_evaluate"] = parse_json_out(ev)
        except Exception as e:  # paper 失败不影响实盘
            R.anomalies.append(f"挑战者轨道异常 (不影响实盘): {e}")

    # --- 动量轮动 (仅调仓日) ---
    mc = load(f"{REPO}/strategy/momentum.json", {})
    if mc.get("enabled"):
        try:
            ms = load(f"{REPO}/state/momentum_positions.json", {})
            wd = _date.fromisoformat(a.date).weekday()
            last = ms.get("last_rebalance")
            gap = (_date.fromisoformat(a.date) - _date.fromisoformat(last)).days if last else 99
            due = wd == int(mc["rebalance"].get("weekday", 0)) or \
                gap >= int(mc["rebalance"].get("max_days_between", 8))
            plan["paper"]["momentum"] = {"rebalance_due": due, "last_rebalance": last,
                                         "days_since": gap}
            q = f"{REPO}/state/paper_queued_momentum.json"
            if os.path.exists(q):
                R.run(["scripts/paper.py", "sync", "--queued", q,
                       "--fills-out", f"{W}/mom_sync.json", "--prune"], "mom.sync", critical=False)
                if os.path.exists(f"{W}/mom_sync.json"):
                    R.run(["scripts/signals.py", "apply", "--state", "state/momentum_positions.json",
                           "--fills", f"{W}/mom_sync.json", "--date", a.date],
                          "mom.apply_sync", critical=False)
            if due:
                if R.run(["scripts/momentum.py", "signal", "--config", "strategy/momentum.json",
                          "--state", "state/momentum_positions.json",
                          "--bars", f"{W}/bars.json", "--quotes", f"{W}/quotes.json",
                          "--date", a.date, "--portfolio-value", str(ms.get("start_capital", 25000)),
                          "--buying-power", str(ms.get("start_capital", 25000)),
                          "--out", f"{W}/mom_orders.json"], "mom.signal", critical=False) is not None:
                    mo = load(f"{W}/mom_orders.json", {})
                    plan["paper"]["momentum"]["orders"] = {
                        "sells": [s["symbol"] for s in mo.get("sells", [])],
                        "buys": [b["symbol"] for b in mo.get("buys", [])]}
                    if mo.get("sells") or mo.get("buys"):
                        R.run(["scripts/paper.py", "run", "--orders", f"{W}/mom_orders.json",
                               "--date", a.date, "--coid-prefix", "mom",
                               "--fills-out", f"{W}/mom_fills.json", "--allow-queue",
                               "--queued-out", "state/paper_queued_momentum.json"],
                              "mom.run", critical=False)
        except Exception as e:
            R.anomalies.append(f"动量轨道异常 (不影响实盘): {e}")


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--date", required=True, help="交易日 YYYY-MM-DD")
    p.add_argument("--portfolio-value", required=True, type=float, help="get_portfolio total_value")
    p.add_argument("--buying-power", required=True, type=float, help="实时 buying_power (非 cash)")
    p.add_argument("--positions", help="券商持仓映射 JSON (get_equity_positions 整理)")
    p.add_argument("--earnings", help="财报日映射 JSON")
    p.add_argument("--broker-closes",
                   help='券商官方收盘价 JSON (会话从 get_equity_quotes 的 close 字段整理): '
                        '{"SYM": {"date": "YYYY-MM-DD", "price": 0.0, "source": "..."}}; '
                        '提供则做行情管道交叉核对, 偏差超阈值记 anomaly')
    p.add_argument("--emit-symbols", metavar="PATH",
                   help="只解析并输出驱动器需要报价的全部标的 (纯读本地文件, 无网络), 写 PATH 后立即退出。"
                        "收盘前流程第一步用它拿清单, 再据此调 get_equity_quotes —— 清单含期权白名单与"
                        "各账本持仓, 比「ETF+股池」多出一截, 少取会触发覆盖率闸")
    p.add_argument("--quotes",
                   help='当日实时报价覆盖 (收盘前阶段**必传**): 接受 mcp__cash_printer__get_equity_quotes '
                        '的原始输出, 或简单映射 {"SYM": price}。给了就**完全取代** integrations.py quotes '
                        '(后者走 EQUITY_RT_FEED=delayed_sip 延迟 15 分钟, 盘中决策不可用)。'
                        '归一化后写 <workdir>/quotes.json, 下游引擎无需改动')
    p.add_argument("--workdir", required=True, help="临时数据目录 (scratchpad)")
    p.add_argument("--out", help="计划输出路径 (缺省 <workdir>/plan.json)")
    p.add_argument("--bars-days", type=int, default=450, help="历史K线回溯天数 (SMA200 需 ≥300)")
    p.add_argument("--skip-paper", action="store_true", help="跳过纸面轨道 (调试用)")
    p.add_argument("--force", action="store_true", help="忽略幂等/数据源告警强制跑")
    p.add_argument("--plan-only", action="store_true",
                   help="只算信号不写账本/不排纸面单 (干预览与测试; 输出改写 workdir)")
    p.add_argument("--phase", choices=["full", "preclose", "wrapup"], default="full",
                   help="拆分式收盘前主跑 (2026-09-10 用户「直接实现」批准): "
                        "preclose = 收盘前关键路径 (preflight→取数→正股信号→期权信号), "
                        "产出当日出场卖单与**当日** pending; "
                        "wrapup = 收盘后收尾 (纸面轨道 + 行情核对); 若当日 preclose 未成功完成, "
                        "wrapup **自动回退跑完整主跑** (fail-safe, 次日 pending); "
                        "full = 原收盘后单跑 (缺省, 未启用收盘前时的行为)")
    a = p.parse_args()

    os.makedirs(a.workdir, exist_ok=True)

    if a.emit_symbols:
        cfg = load(f"{REPO}/strategy/config.json")
        uni = load(f"{REPO}/strategy/universe.json", {})
        _p = {}
        allsyms, _ = collect_symbols(cfg, uni, _p)
        save(a.emit_symbols, {"symbols": allsyms, "count": len(allsyms),
                              "groups": _p.get("symbol_groups"),
                              "_note": "由 daily.py --emit-symbols 产出; 收盘前阶段据此调 "
                                       "get_equity_quotes, 原始输出经 --quotes 传回"})
        print(json.dumps({"symbols_file": a.emit_symbols, "count": len(allsyms),
                          "groups": _p.get("symbol_groups")}, ensure_ascii=False, indent=2))
        return 0

    out_path = a.out or f"{a.workdir}/plan.json"
    R = Runner()
    plan = {"date": a.date, "generated_by": "scripts/daily.py",
            "portfolio_value": a.portfolio_value, "buying_power": a.buying_power,
            "place_now": {}, "to_pending": {}}

    # wrapup 的 fail-safe: 当日 preclose 未成功完成 → 退化成完整主跑 (出场+次日 pending)
    eff = a.phase
    if a.phase == "wrapup":
        pc = load(f"{REPO}/{PRECLOSE_MARKER}", {}) or {}
        done = pc.get("date") == a.date and pc.get("status") == "completed"
        plan["preclose_marker"] = pc or None
        if not done:
            eff = "full"
            plan["wrapup_fallback"] = (
                f"当日 preclose 未完成 (marker={pc.get('status') or 'missing'}/"
                f"{pc.get('date') or 'n/a'}) → 本次退化为完整主跑: 出场卖单照下, "
                f"pending 按次日窗口 (fail-safe, 收盘前跑挂不致当日无出场)")
            R.anomalies.append(plan["wrapup_fallback"])
    plan["phase"] = a.phase
    plan["effective_phase"] = eff

    try:
        phase_preflight(a, R, plan)
        cfg = load(f"{REPO}/strategy/config.json")
        uni = load(f"{REPO}/strategy/universe.json", {})
        allsyms, _ = collect_symbols(cfg, uni, plan)
        if eff == "wrapup":
            # 正股/期权信号已由当日 preclose 产出并回写, 这里绝不重算 (会看到新持仓重复出单)
            plan["skipped"] = "wrapup: 正股与期权信号已由当日 preclose 完成, 本阶段只跑纸面轨道与行情核对"
            phase_data(a, R, plan, allsyms)
            if not a.skip_paper:
                phase_paper(a, R, plan)
        else:
            phase_data(a, R, plan, allsyms)
            o = phase_stock_signal(a, R, plan)
            if o.get("halted") or o.get("circuit_breaker_triggered"):
                plan["stopped"] = "halted/熔断触发 — 只读结束, 通知用户"
            else:
                # 收盘前阶段: 期权信号非关键 —— 失败不得拖垮正股关键路径 (红线6 记 anomaly 即可),
                # 但它的最大在险额是股票 cap 的扣减项 (2D 期权优先), 故仍在本阶段跑。
                try:
                    phase_options(a, R, plan)
                except Exception as e:
                    if eff != "preclose":
                        raise
                    R.anomalies.append(f"收盘前期权信号失败 (正股不受影响, 期权今日不出单): {e}")
                if eff == "full" and not a.skip_paper:
                    phase_paper(a, R, plan)
                # preclose 的纸面轨道留到 wrapup (最慢且完全不敏感)
    except SystemExit:
        raise
    except Exception as e:
        plan["fatal"] = str(e)

    plan["to_pending"]["pending_template"] = pending_template(eff)
    plan["to_pending"]["option_pending_template"] = option_pending_template(eff)

    plan["anomalies"] = R.anomalies
    plan["command_log"] = R.log
    _slow = sorted((e for e in R.log if e.get("seconds")),
                   key=lambda e: -e["seconds"])[:5]
    plan["timing"] = {
        "total_seconds": R.elapsed(),
        "slowest": [{"label": e["label"], "seconds": e["seconds"]} for e in _slow],
        "_note": "关键路径耗时。收盘前窗 15:20 起跑、期权 15:45 收口 → "
                 "total_seconds 逼近 900s 就要考虑提前开跑或缩减收盘前阶段",
    }
    if eff == "preclose" and R.elapsed() > 600:
        R.anomalies.append(
            f"收盘前关键路径耗时 {R.elapsed()}s (>10 分钟) —— 15:20 起跑已吃掉期权窗 (15:45 收口), "
            f"下次需提前开跑或缩减收盘前阶段; 本次结果仍有效, 但执行窗可能已所剩无几")
    plan["journal_facts"] = {
        "vix": plan.get("macro_vix"),
        "candidates": (plan.get("stock") or {}).get("candidates"),
        "equity_sells": plan["place_now"].get("equity_sells", []),
        "equity_buys": plan["to_pending"].get("equity_buys", []),
        "option_buys": plan["to_pending"].get("option_buys", []),
        "option_sells": plan["place_now"].get("option_sells", []),
        "near_signals": plan["to_pending"].get("near_signals", []),
        "paper": plan.get("paper"), "options": plan.get("options"),
    }
    save(out_path, plan)

    # preclose 完成标记 —— wrapup 靠它判断要不要 fail-safe 退化成完整主跑
    if eff == "preclose" and not a.plan_only:
        ok = not plan.get("fatal") and not plan.get("stopped")
        save(f"{REPO}/{PRECLOSE_MARKER}", {
            "date": a.date,
            "status": "completed" if ok else "incomplete",
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "fatal": plan.get("fatal"), "stopped": plan.get("stopped"),
            "equity_sells_placed_to_session": len(plan["place_now"].get("equity_sells", [])),
            "equity_buys_to_pending": len(plan["to_pending"].get("equity_buys", [])),
            "anomalies": len(R.anomalies),
            "_note": "由 daily.py --phase preclose 写; wrapup 读它判断是否 fail-safe 回退完整主跑",
        })

    print(json.dumps({
        "plan": out_path,
        "phase": a.phase, "effective_phase": eff,
        "wrapup_fallback": plan.get("wrapup_fallback"),
        "pending_valid_until": plan["to_pending"]["pending_template"]["valid_until"],
        "pending_exec_window_et": plan["to_pending"]["pending_template"]["exec_window_et"],
        "option_pending_valid_until": plan["to_pending"]["option_pending_template"]["valid_until"],
        "option_pending_exec_window_et":
            plan["to_pending"]["option_pending_template"]["exec_window_et"],
        "fatal": plan.get("fatal"),
        "anomalies": len(R.anomalies),
        "equity_sells_to_place": len(plan["place_now"].get("equity_sells", [])),
        "option_sells_to_place": len(plan["place_now"].get("option_sells", [])),
        "equity_buys_to_pending": len(plan["to_pending"].get("equity_buys", [])),
        "option_buys_to_pending": len(plan["to_pending"].get("option_buys", [])),
        "near_signals": [n["symbol"] for n in plan["to_pending"].get("near_signals", [])],
    }, ensure_ascii=False, indent=2))
    return 1 if plan.get("fatal") else 0


if __name__ == "__main__":
    sys.exit(main())
