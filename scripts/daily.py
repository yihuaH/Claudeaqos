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
import json
import os
import subprocess
import sys
from datetime import date as _date, timedelta

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

    def run(self, args, label, critical=True, timeout=900):
        cmd = [PY] + args if args[0].endswith(".py") else args
        r = subprocess.run(cmd, capture_output=True, text=True, cwd=REPO, timeout=timeout)
        entry = {"label": label, "cmd": " ".join(args), "rc": r.returncode}
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

def phase_preflight(a, R, plan):
    out = R.run(["scripts/integrations.py", "status"], "integrations.status")
    st = parse_json_out(out) or {}
    plan["data_sources"] = st
    if not st.get("all_ok"):
        R.anomalies.append(f"数据源自诊断未全 ok: {json.dumps(st, ensure_ascii=False)}")
        if not a.force:
            raise RuntimeError("数据源不可用, 按红线6 停止 (--force 可强制继续)")
    plan["macro_vix"] = (st.get("fred") or {}).get("vix")
    jr = f"{REPO}/journal/{a.date}.md"
    if os.path.exists(jr) and "status: completed" in open(jr).read() and not a.force:
        plan["idempotent_skip"] = True
        raise SystemExit(json.dumps({"idempotent_skip": True,
                                     "note": f"journal/{a.date}.md 已 completed, 幂等结束"},
                                    ensure_ascii=False))


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
    # quotes 分批 (integrations.py quotes 走 --symbols)
    qall = {}
    for i in range(0, len(allsyms), 100):
        chunk = allsyms[i:i + 100]
        p = f"{W}/quotes_{i}.json"
        R.run(["scripts/integrations.py", "quotes", "--symbols", ",".join(chunk), "--out", p],
              f"quotes[{i}]")
        qall.update(load(p, {}))
    save(f"{W}/quotes.json", qall)
    plan["quotes_symbols"] = len(qall)
    missing = [s for s in allsyms if s not in qall]
    if missing:
        R.anomalies.append(f"无报价标的 {len(missing)}: {missing[:10]}")


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
    R.run(["scripts/integrations.py", "chains", "--underlyings", ",".join(unis),
           "--date", a.date, "--dte-max", str(dte_max), "--out", f"{W}/chains.json"],
          "option.chains", critical=False, timeout=900)
    if not os.path.exists(f"{W}/chains.json"):
        plan["options"] = {"error": "期权链拉取失败, 本日期权轨道跳过"}
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
    p.add_argument("--workdir", required=True, help="临时数据目录 (scratchpad)")
    p.add_argument("--out", help="计划输出路径 (缺省 <workdir>/plan.json)")
    p.add_argument("--bars-days", type=int, default=450, help="历史K线回溯天数 (SMA200 需 ≥300)")
    p.add_argument("--skip-paper", action="store_true", help="跳过纸面轨道 (调试用)")
    p.add_argument("--force", action="store_true", help="忽略幂等/数据源告警强制跑")
    p.add_argument("--plan-only", action="store_true",
                   help="只算信号不写账本/不排纸面单 (干预览与测试; 输出改写 workdir)")
    a = p.parse_args()

    os.makedirs(a.workdir, exist_ok=True)
    out_path = a.out or f"{a.workdir}/plan.json"
    R = Runner()
    plan = {"date": a.date, "generated_by": "scripts/daily.py",
            "portfolio_value": a.portfolio_value, "buying_power": a.buying_power,
            "place_now": {}, "to_pending": {}}

    try:
        phase_preflight(a, R, plan)
        cfg = load(f"{REPO}/strategy/config.json")
        uni = load(f"{REPO}/strategy/universe.json", {})
        allsyms, _ = collect_symbols(cfg, uni, plan)
        phase_data(a, R, plan, allsyms)
        o = phase_stock_signal(a, R, plan)
        if o.get("halted") or o.get("circuit_breaker_triggered"):
            plan["stopped"] = "halted/熔断触发 — 只读结束, 通知用户"
        else:
            phase_options(a, R, plan)
            if not a.skip_paper:
                phase_paper(a, R, plan)
    except SystemExit:
        raise
    except Exception as e:
        plan["fatal"] = str(e)

    plan["anomalies"] = R.anomalies
    plan["command_log"] = R.log
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
    print(json.dumps({
        "plan": out_path,
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
