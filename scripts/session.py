#!/usr/bin/env python3
"""
会话调度器 (2026-08-08 用户「你可以做脚本吗？不用每天一大段 prompt 吧」)。

把原本写在 Routine 唤醒词里的一大段流程搬进仓库: 本脚本判断**当前处于哪个窗口**、
检查幂等与市场状态、读取所有账本/待执行文件, 然后打印**这一次该做的精确清单**
(含要调用的 MCP 与参数)。唤醒词因此可缩到两行。

    cd /home/user/Claudeaqos && git fetch origin Main && git checkout -B Main origin/Main \
      && python3 scripts/session.py brief
    然后照输出的清单执行。规则以 CLAUDE.md 红线 + strategy/playbook.md 为准。

⚠️ 本脚本是**调度器与检查表, 不含任何交易决策** (红线2): 买什么卖什么多少钱一律来自
   signals.py / weekly_calls.py / overnight.py / momentum.py 等确定性引擎。
   这里只回答"现在该做哪一步", 不回答"该买什么"。

用法:
  python3 scripts/session.py brief              # 自动判窗口 (默认)
  python3 scripts/session.py brief --window main_run|morning|report
  python3 scripts/session.py brief --json       # 机器可读
"""
import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ET = timezone(timedelta(hours=-4))          # 美东夏令时 (EDT)


def load(path, default=None):
    try:
        with open(os.path.join(REPO, path)) as f:
            return json.load(f)
    except Exception:
        return default


def now_et():
    return datetime.now(timezone.utc).astimezone(ET)


def clock():
    """Alpaca 市场时钟 (经 integrations.py status)。失败返回 None, 不阻断。"""
    try:
        r = subprocess.run([sys.executable, os.path.join(REPO, "scripts/integrations.py"),
                            "status"], capture_output=True, text=True, timeout=60, cwd=REPO)
        return (json.loads(r.stdout) or {}).get("alpaca")
    except Exception:
        return None


def detect_window(t):
    """按 ET 时刻判断窗口。周末/非交易时段归 off。"""
    m = t.hour * 60 + t.minute
    if t.weekday() >= 5:
        return "off_weekend"
    if 10 * 60 + 15 <= m < 12 * 60:
        return "morning"
    if 16 * 60 <= m < 18 * 60 + 30:
        return "main_run"
    if 18 * 60 + 30 <= m < 23 * 60 + 59:
        return "report"
    if 0 <= m < 9 * 60 + 25:
        return "report"            # 盘前也归战报窗口 (只读 + 可执行 4C)
    return "off_hours"


def journal_state(date):
    p = os.path.join(REPO, "journal", f"{date}.md")
    if not os.path.exists(p):
        return {"exists": False, "completed": False}
    s = open(p).read()
    return {"exists": True, "completed": "status: completed" in s, "bytes": len(s)}


def pending_state(fname, label):
    d = load(f"state/{fname}")
    if not d:
        return {"file": fname, "label": label, "exists": False}
    return {"file": fname, "label": label, "exists": True,
            "trade_date": d.get("trade_date"), "status": d.get("status"),
            "valid_until": d.get("valid_until"),
            "n_orders": len(d.get("orders") or []),
            "option_alert": (d.get("option_alert") or {}).get("reserve_usd"),
            "alert_names": (d.get("option_alert") or {}).get("names")}


def scale_in_watch():
    """加仓线监控 (只读展示, 不产生订单 — 订单由 signals.py 出)。"""
    st = load("state/positions.json", {}) or {}
    cfg = load("strategy/config.json", {}) or {}
    si = cfg.get("scale_in") or {}
    if not si.get("enabled"):
        return []
    drop = float(si.get("trigger_drop_pct", 3)) / 100.0
    mx = int(si.get("max_tranches", 2))
    out = []
    for sym, p in (st.get("strategy_positions") or {}).items():
        e = float(p["entry_price"])
        out.append({"symbol": sym, "avg": round(e, 4), "tranches": int(p.get("tranches", 1)),
                    "trigger_at": round(e * (1 - drop), 2),
                    "maxed": int(p.get("tranches", 1)) >= mx})
    return sorted(out, key=lambda x: x["symbol"])


CHECKLIST = {
    "main_run": [
        ("MCP 取数", [
            "get_portfolio(802095265) → total_value, buying_power (用 BP 不用 cash)",
            "get_equity_positions(802095265) → 存 <wd>/positions.json (**建议直接存原始输出**, 带 "
            "average_buy_price 才能验成本基); 与 state/positions.json 的一致性由驱动器 position_check 闸自动核对",
            "财报: 对持仓+RSI2<10 候选逐个 get_earnings_results → **新格式** "
            "{\"SYM\":{\"next\":\"YYYY-MM-DD\"|null,\"past\":[已发生财报日...]}} 存 <wd>/earnings.json "
            "(past 供财报上涨跳空豁免, 缺则豁免静默失效; 不知候选时先跑一次 --plan-only 看 plan.json 的 stock.candidates)",
            "券商官方收盘 (≤20 只: 持仓+买单候选): get_equity_quotes → 取 close 字段 → "
            "{\"SYM\":{date,price,source}} 存 <wd>/broker_closes.json",
        ]),
        ("跑驱动器", ["见下方 CMD 行, 直接复制执行"]),
        ("检查 plan.json", [
            "fatal / anomalies 非空 → 红线6: 停止交易, 写日志, 通知用户",
            "stopped 非空 (halted/熔断) → 只读结束并通知用户",
            "price_check.verdict: fail→已并入 anomalies; warn→写 journal 并通知 (核查 EQUITY_FEED)",
            "position_check.verdict=fail → preflight 已停跑。split_suspected: 按 suggested_fix 手工改账本 "
            "(份额取券商值、均价=原cost÷新份额、cost 不变, trades[] 不动, 写 corporate_actions 留痕) 后重跑; "
            "unapplied_fill: 先 signals.py apply 补回写; 其余分类查明原因前不交易 (playbook §1 步骤3)",
        ]),
        ("执行 (playbook §4)", [
            "place_now.equity_sells → 4A: review_equity_order → place (market+regular_hours), 无需用户确认",
            "place_now.option_sells → 4D: limit = 引擎 est_price×0.97, gfd; 成交后 weekly_calls.py apply 回写",
            "to_pending.equity_buys (+rotation_sells) → 写 state/pending_orders.json (valid_until 次日 09:25 ET); "
            "买单标的跑 integrations.py news 红旗预检; near_signals 非空则加 option_alert "
            "(reserve_usd 直接照抄引擎 suggested_reserve_usd, 不得自算)",
            "to_pending.option_buys → 写 state/pending_option_orders.json (valid_until 次日 10:30 ET)",
            "⚠️ 买单一律绝不 place (无人值守会被平台分类器拦)",
        ]),
        ("收尾", [
            "用 plan.json 的 journal_facts 写 journal/<今天>.md (status: completed)",
            "实际成交的 4A 卖单 → signals.py apply 回写 state (未成交的不写)",
            "git add -A && commit && push origin Main",
            "PushNotification 通知用户 (附待执行逐笔明细)",
        ]),
    ],
    "morning": [
        ("股票残单", [
            "get_equity_orders(802095265, created_at_gte=昨日)",
            "已成交 → signals.py apply 回写 state + journal, 推送报成交",
            "仍 queued/confirmed 且已过目标时段 → cancel_equity_order (失败则任其 gfd 到期, 记录)",
        ]),
        ("期权残单", [
            "get_option_orders(802095265, created_at_gte=昨日)",
            "已成交 → weekly_calls.py apply --ledger state/weekly_call_live_positions.json 回写",
            "未成交且已过 10:30 ET 窗口 → cancel_option_order; pending_option_orders.json 记 "
            "status=cancelled_unfilled + outcome。**绝不改限价追单** (红线2)",
            "pending_option_orders 仍 awaiting_execution 且已过 10:30 ET → status=expired, commit",
        ]),
        ("收尾", ["有动作则 commit+push 并推送; 休市或无动作无异常 → 静默结束不打扰用户"]),
    ],
    "report": [
        ("只读核查", [
            "读当日 journal (status: completed = 主跑成功)",
            "get_equity_orders + get_option_orders 与 journal 核对 (cash_printer 不可用则注明未核对)",
        ]),
        ("战报内容", [
            "组合净值/回撤/信号摘要/成交/告警异常",
            "pending_orders (次日 09:25 ET 前有效) 与 pending_option_orders (次日 10:30 ET 前有效, "
            "推荐执行窗 09:45-10:30 ET) 状态; 待执行则附逐笔明细提醒用户可回复「执行」",
            "带 option_alert 时显著提示预警标的与保留额",
            "周call 双轨小节 (实盘持仓盯市/skip 原因/near_signals; paper round_trips/中位点差/verdict)",
            "price_check 结果 (引擎价 vs 券商官方收盘)",
        ]),
        ("主跑健康检查", [
            "当日 journal 缺失或未 completed → 主跑失败 (已知失败模式: worker 重启)。"
            "send_later ~20 分钟复查; 仍无 → 诊断并明确报告",
            "⚠️ 本窗口只诊断报告, **绝不自行代跑下单** (可跑 daily.py --plan-only 只读预览供报告)",
        ]),
        ("例外: 用户说「执行」", [
            "按 playbook §4C 消费 pending_orders.json / §4D 消费 pending_option_orders.json",
            "必须先回显逐笔明细获确认 (4C-1B); 逐字段照抄引擎输出, 绝不放大/加单/改标的",
            "带 option_alert 时: 股票买单累计 ≤ 实时BP − reserve_usd; 装不下的整单跳过不缩量",
            "用户说「不留了」= 撤销弹药保留, 照常全执行",
        ]),
    ],
}


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", nargs="?", default="brief", choices=["brief"])
    ap.add_argument("--window", choices=["main_run", "morning", "report"],
                    help="强制指定窗口 (缺省按 ET 时刻自动判断)")
    ap.add_argument("--workdir", default=os.environ.get(
        "CLAUDEAQOS_WD", "<scratchpad>"), help="驱动器 workdir")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    t = now_et()
    date = t.strftime("%Y-%m-%d")
    win = a.window or detect_window(t)
    ck = clock()
    jr = journal_state(date)
    st = load("state/positions.json", {}) or {}
    cfg = load("strategy/config.json", {}) or {}

    info = {
        "now_et": t.strftime("%Y-%m-%d %H:%M:%S %a"),
        "date": date, "window": win, "forced_window": bool(a.window),
        "market": ck,
        "journal": jr,
        "enabled": cfg.get("enabled"), "halted": st.get("halted"),
        "high_water_mark": st.get("high_water_mark"),
        "positions": len(st.get("strategy_positions") or {}),
        "legacy_positions": len(st.get("legacy_positions") or {}),
        "pending": [pending_state("pending_orders.json", "股票待执行"),
                    pending_state("pending_option_orders.json", "期权待执行")],
        "scale_in": scale_in_watch(),
    }

    blockers = []
    if cfg.get("enabled") is False:
        blockers.append("config.json enabled=false → 只允许读数据和写日志 (红线4)")
    if st.get("halted"):
        blockers.append("state.halted=true → 只允许读数据和写日志 (红线4)")
    if win == "main_run" and jr["completed"]:
        blockers.append(f"journal/{date}.md 已 status:completed → 幂等, 静默结束")
    if win in ("off_weekend", "off_hours"):
        blockers.append(f"当前不在任何作业窗口 ({win}) → 只读; 如确需执行请用 --window 指定")
    if ck and ck.get("market_is_open") is False and win == "main_run":
        pass   # 主跑本就在收盘后, 不算 blocker
    info["blockers"] = blockers

    if a.json:
        info["checklist"] = CHECKLIST.get(win, [])
        print(json.dumps(info, ensure_ascii=False, indent=2))
        return 0

    W = {"main_run": "收盘后主跑", "morning": "晨间核查", "report": "收盘战报",
         "off_weekend": "周末 (非作业窗口)", "off_hours": "非作业时段"}[win]
    print("=" * 78)
    print(f"  Claudeaqos 会话调度  ·  {info['now_et']} ET  ·  窗口 = {W}"
          + ("  [强制指定]" if info["forced_window"] else ""))
    print("=" * 78)
    if ck:
        print(f"市场: {'开市中' if ck.get('market_is_open') else '休市'}"
              f"   下次开盘 {ck.get('next_open')}")
    else:
        print("市场: ⚠️ Alpaca 时钟不可用 (integrations.py status 失败) — 按红线6 先查数据源")
    print(f"账本: enabled={info['enabled']}  halted={info['halted']}  "
          f"策略仓 {info['positions']} 只  存量仓 {info['legacy_positions']} 只  "
          f"HWM ${info['high_water_mark']}")
    print(f"日志: journal/{date}.md  存在={jr['exists']}  completed={jr['completed']}")
    print()
    for p in info["pending"]:
        if not p["exists"]:
            print(f"  · {p['label']}: 无文件")
            continue
        extra = ""
        if p.get("option_alert"):
            extra = f"   ⚡option_alert {p['alert_names']} 保留 ${p['option_alert']}"
        print(f"  · {p['label']}: {p['trade_date']}  status={p['status']}  "
              f"{p['n_orders']} 单  有效至 {p['valid_until']}{extra}")
    if info["scale_in"]:
        print()
        print("  加仓线监控 (只读; 订单仍由 signals.py 产出):")
        for s in info["scale_in"]:
            tag = " [已满档]" if s["maxed"] else ""
            print(f"    {s['symbol']:<6} 均价 {s['avg']:<10} 第{s['tranches']}档  "
                  f"加仓线 ≤{s['trigger_at']}{tag}")
    if blockers:
        print()
        print("  ⛔ 阻断/注意:")
        for b in blockers:
            print(f"    - {b}")

    print()
    print("-" * 78)
    print(f"  本窗口任务清单 ({W})")
    print("-" * 78)
    for i, (title, steps) in enumerate(CHECKLIST.get(win, []), 1):
        print(f"\n{i}. {title}")
        for s in steps:
            print(f"   · {s}")

    if win == "main_run":
        print()
        print("-" * 78)
        print("  CMD (取数完成后直接执行; 加 --plan-only 可干预览)")
        print("-" * 78)
        print(f"""python3 scripts/daily.py --date {date} \\
  --portfolio-value <total_value> --buying-power <BP> \\
  --positions {a.workdir}/positions.json \\
  --earnings {a.workdir}/earnings.json \\
  --broker-closes {a.workdir}/broker_closes.json \\
  --workdir {a.workdir}""")
        first_monday_note = ""
        if t.weekday() == 0 and t.day <= 7:
            first_monday_note = ("\n⚠️ 今天是本月首个交易周一 → 额外跑 playbook §7C.6B "
                                 "期权池月度复核 (skip_log 30 天通过率 + 候选实测点差收编)")
        if first_monday_note:
            print(first_monday_note)

    print()
    print("规则以 CLAUDE.md 硬性红线 + strategy/playbook.md 为准; 本脚本只调度, 不做交易决策。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
