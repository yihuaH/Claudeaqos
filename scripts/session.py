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
  python3 scripts/session.py brief --window preclose|main_run|morning|report
  python3 scripts/session.py brief --json       # 机器可读
"""
import argparse
import json
import re
import glob
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
    if 9 * 60 + 45 <= m < 12 * 60:      # 09:45 = 4C 执行窗开启 (2026-09-10); 推荐锚 10:45
        return "morning"
    if 15 * 60 + 5 <= m < 15 * 60 + 58:  # 收盘前主跑 (2026-09-10 用户「直接实现」): 跑 15:20, 执行 15:40-15:55
        return "preclose"
    if 16 * 60 <= m < 18 * 60 + 30:
        return "main_run"
    if 18 * 60 + 30 <= m < 23 * 60 + 59:
        return "report"
    if 0 <= m < 9 * 60 + 30:
        return "report"            # 盘前归战报窗口: **只读**, 4C 执行须等 09:45 开窗 (2026-09-10)
    return "off_hours"


def exec_window(t):
    """4C 股票买单执行窗 (2026-09-10 用户批准): 交易日 09:45-15:55 ET, 推荐锚 10:45 晨检。
    必须在 09:30 开盘之后 —— 出场回款 09:30 才到账 (原 09:25 截止使其结构性拿不到)。
    与 detect_window 正交: 12:00-15:55 不属任何作业窗口, 但用户说「执行」时仍合法。"""
    m = t.hour * 60 + t.minute
    if t.weekday() >= 5:
        return {"open": False, "reason": "周末"}
    if m < 9 * 60 + 45:
        return {"open": False, "reason": f"未到 09:45 开窗 (现 {t:%H:%M} ET); "
                                         f"开盘前执行会吃不到当日出场回款"}
    if m >= 15 * 60 + 55:
        return {"open": False, "reason": f"已过 15:55 关窗 (现 {t:%H:%M} ET); 清单当日过期, 引擎当晚重算"}
    return {"open": True, "reason": f"09:45-15:55 ET 执行窗开放中 (现 {t:%H:%M} ET)"}


def preclose_state(date):
    """当日收盘前主跑是否完成 —— 决定 17:45 窗口跑 --phase wrapup 还是 fail-safe 的 full。"""
    d = load("state/preclose_status.json") or {}
    return {"exists": bool(d), "date": d.get("date"), "status": d.get("status"),
            "today": d.get("date") == date and d.get("status") == "completed",
            "buys": d.get("equity_buys_to_pending"), "sells": d.get("equity_sells_placed_to_session"),
            "anomalies": d.get("anomalies")}


def pending_exec_state(t):
    """当前生效的 pending 清单自带的执行窗 (收盘前产出=当日 15:30-15:55, 收盘后产出=次日 09:45-15:55)。"""
    d = load("state/pending_orders.json") or {}
    tpl = d.get("pending_template") or {}
    win = tpl.get("exec_window_et") or d.get("exec_window_et")
    day = tpl.get("exec_day") or d.get("exec_day")
    return {"trade_date": d.get("trade_date"), "status": d.get("status"),
            "exec_window_et": win, "exec_day": day,
            "valid_until": d.get("valid_until")}


def journal_state(date):
    """当日主跑是否已完成。扫**全部** journal/<date>*.md 并锚定行首 —— 当日哪个窗口先跑就先占
    `<date>.md`, 后到的窗口写 `-main.md` 等后缀文件 (2026-09-10 实况), 锚死单一文件名会假阴性。
    晨检用另一个键 morning_check: completed, 不参与本判定。"""
    files = sorted(glob.glob(os.path.join(REPO, "journal", f"{date}*.md")))
    done = [os.path.basename(f) for f in files
            if re.search(r"^\s*status:\s*completed\s*$", open(f).read(), re.M)]
    return {"exists": bool(files), "completed": bool(done),
            "completed_in": done, "files": [os.path.basename(f) for f in files],
            "bytes": sum(os.path.getsize(f) for f in files)}


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
            "to_pending.equity_buys (+rotation_sells) → 写 state/pending_orders.json "
            "(valid_until 次日 15:55 ET, 执行窗 09:45-15:55 = 开盘后, 推荐锚 10:45 晨检); "
            "买单标的跑 integrations.py news 红旗预检; near_signals 非空则加 option_alert "
            "(reserve_usd 直接照抄引擎 suggested_reserve_usd, 不得自算)",
            "to_pending.option_buys → 写 state/pending_option_orders.json (valid_until 次日 10:30 ET)",
            "⚠️ 买单一律绝不 place (无人值守会被平台分类器拦)",
        ]),
        ("战报 (2026-09-11 由 18:45 独立窗口并入本窗口)", [
            "组合净值/回撤/信号摘要/成交/告警异常",
            "周call 双轨小节 (实盘持仓盯市/skip 原因/near_signals; paper round_trips/中位点差/verdict)",
            "price_check 结果 (引擎价 vs 券商官方收盘)",
            "带 option_alert 时显著提示预警标的与保留额",
            "⚠️ **pending 提示按当日实际形态写**: 收盘前主跑正常 → 当日清单 15:55 已过期, 战报只做"
            "事后陈述, **不要提示用户「回复执行」** (窗口早关了); 本次若是 fail-safe 退化的完整主跑 "
            "→ 才提示次日 09:45-15:55 执行窗 (推荐锚 10:45 晨检)。看 plan.json 的 effective_phase 判断",
            "次日预览: 加仓线、期权 near_signals、次日是否周一 (股票池 13:00 刷新)",
        ]),
        ("收尾", [
            "用 plan.json 的 journal_facts 写 journal/<今天>.md (status: completed)",
            "实际成交的 4A 卖单 → signals.py apply 回写 state (未成交的不写)",
            "git add -A && commit && push origin Main",
            "PushNotification 通知用户 (附待执行逐笔明细; 提示执行窗为**明日 09:45-15:55 ET**, "
            "推荐 10:45 晨检窗口 — **不要提示开盘前执行**, 出场回款 09:30 才到账)",
        ]),
    ],
    "preclose": [
        ("① 昨日 wrapup 看门狗 (2026-09-11 由 18:45 战报迁入)", [
            "查上一交易日的 journal 有没有 `status: completed` 行 —— 没有 = 昨晚 17:45 wrapup 失败 "
            "(已知失败模式: worker 重启)。后果: 昨日纸面轨道未跑、行情未核对、账本可能未回写",
            "**在交易动作之前**查这一条 (这正是从战报窗迁到这里的理由: 原来是事后一小时才发现)",
            "发现失败 → 先通知用户并在今日 journal 注明; 账本若确未回写, 按 playbook §1 步骤3 "
            "的 position_check 结果处置后再决定是否继续今日交易 (红线6)",
        ]),
        ("⏱ 时段自检 (本窗口唯一硬约束)", [
            "现在必须 < 15:55 ET 且开市中。已过 15:40 → **不要开跑**, 直接等 17:45 wrapup "
            "(fail-safe 会退化成完整主跑, 出场照下), 跑一半更危险",
            "Alpaca 时钟 market_is_open=false → 休市, 写日志结束",
        ]),
        ("MCP 取数 (收盘前专属口径)", [
            "get_portfolio(802095265) → total_value, buying_power",
            "get_equity_positions(802095265) → 存 <wd>/positions.json (原始输出)",
            "① 先拿清单: daily.py --emit-symbols <wd>/allsyms.json (纯读本地无网络, 秒出; "
            "实测 129 只 — 含期权白名单与各账本持仓, 比「ETF+股池」多一截)",
            "② ⚠️ **对该清单全部标的**调 get_equity_quotes (分批), **原样存原始输出**到 "
            "<wd>/rh_quotes.json → 用 --quotes 传入。signals.py 排除当天日线, 当日价只从这里来; "
            "**绝不用 integrations.py quotes** (delayed_sip 延迟 15 分钟)。"
            "驱动器两道硬闸: 未传 --quotes 拒跑 · 覆盖率 <90% 拒跑 (都是 fatal 不是 warn)",
            "财报: 同主跑 (持仓 + RSI2<10 候选, 新格式带 past)",
            "**不取** broker_closes (官方收盘还不存在, 驱动器自动跳过核对)",
        ]),
        ("跑驱动器", [
            "daily.py ... --phase preclose --quotes <wd>/rh_quotes.json (不传 --broker-closes)",
            "fatal / anomalies 非空 → 红线6 停止交易、写日志、通知用户, **不要硬着头皮下单**",
            "position_check fail → preflight 已停跑, 按 playbook §1 步骤3 处理",
        ]),
        ("执行 (playbook §4)", [
            "place_now.equity_sells → 4A: review → place (market+regular_hours), **盘中即时成交**",
            "place_now.option_sells → 4D: limit = 引擎 est×0.97, gfd",
            "to_pending.equity_buys → 写 state/pending_orders.json, 时效字段**逐字段照抄** "
            "plan.json 的 to_pending.pending_template (exec_day=same_day, 执行窗 15:30-15:55 ET)",
            "to_pending.option_buys → 写 state/pending_option_orders.json, 时效字段**逐字段照抄** "
            "plan.json 的 to_pending.option_pending_template (2026-09-11 起当日执行: "
            "执行窗 **15:30-15:45 ET**, 比股票早 10 分钟收口 — 15:45 后是 4D 明令避开的收盘前极端点差区)",
            "⚠️ **先发期权参数, 再发股票清单** — 期权窗口只有 15 分钟且开仓走手动通道 (App 组合单); "
            "两轨对同一时刻实时 BP 算, 按 2D 期权优先 (playbook 4C-2D-0)",
            "⚠️ 买单一律绝不 place (红线9)",
        ]),
        ("通知用户 (本窗口的关键动作)", [
            "commit+push, 然后 PushNotification 附逐笔明细, 明确写两个窗口 (别写混): "
            "**期权 今日 15:30-15:45 ET (12:30-12:45 PT)** · **股票 今日 15:30-15:55 ET "
            "(12:30-12:55 PT)**, 过点作废。期权在前 (窗口更窄且走手动 App 通道)",
            "时间紧 → 先推通知再补 journal (journal 可由 17:45 wrapup 补全)",
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
        ("待执行清单 —— **条件式**, 多数日子本段无事 (2026-09-11 改写)", [
            "先看 pending 的 `pending_template.exec_day`: **`same_day` → 本段跳过** —— 那是昨天"
            "收盘前主跑产出的当日清单, 昨天 15:55 就已消费或过期, 与今晨无关",
            "只有 `exec_day=next_session` 才轮到本窗口 —— 那意味着**昨天走了 fail-safe** "
            "(收盘前主跑没跑成, 17:45 wrapup 退化为完整主跑)。此时本窗口是 4C 的推荐执行锚点 "
            "(09:45-15:55 ET, 出场回款已于 09:30 到账)",
            "**任何情况下仍须用户明确说「执行」** (红线9): 附逐笔明细提醒即可, 绝不自行下买单",
            "同日若有 next_session 形态的 pending_option_orders → 按 4D-2D 期权优先 (窗口 09:45-10:30 更窄)",
            "已过 15:55 ET 仍未执行 → status=expired, journal 注明, commit (引擎当晚重算)",
            "⚠️ 正常日 (收盘前主跑跑成了) 本段应当**什么都不做** —— 若发现有 next_session 清单, "
            "说明昨天出过问题, 顺手核对昨日 journal 与 state/preclose_status.json",
        ]),
        ("收尾", ["有动作则 commit+push 并推送; 休市或无动作无异常 → 静默结束不打扰用户"]),
    ],
    # 2026-09-11: 18:45 独立战报窗口已并入 17:45 wrapup (拆分后战报的 pending 提示与「执行」入口
    # 都随当日清单 15:55 过期而失效, 只剩看门狗, 而看门狗已迁到次日 preclose 的交易动作之前)。
    # 本窗口保留给**手动**调用与盘前/盘后时段的只读核查; 对应 Routine 已 disabled 可回退。
    "report": [
        ("⚠️ 本窗口已并入 17:45 (2026-09-11)", [
            "18:45 独立战报 Routine 已停用 —— 战报现由 17:45 wrapup 一并发出",
            "本窗口只在**手动**调用时有意义: 只读核查, 绝不代跑下单",
        ]),
        ("只读核查", [
            "读当日 journal (status: completed = 主跑成功)",
            "get_equity_orders + get_option_orders 与 journal 核对 (cash_printer 不可用则注明未核对)",
        ]),
        ("战报内容", [
            "组合净值/回撤/信号摘要/成交/告警异常",
            "pending_orders (次日 15:55 ET 前有效, **执行窗 09:45-15:55 = 开盘后**, 推荐 10:45 晨检) 与 "
            "pending_option_orders (次日 10:30 ET 前有效, 推荐执行窗 09:45-10:30 ET) 状态; "
            "待执行则附逐笔明细提醒用户可回复「执行」",
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
            "⚠️ **股票买单必须在 09:30 开盘之后执行** (2026-09-10 起): 本窗口若在盘前/盘后, "
            "告知用户改到次日 09:45-15:55 ET 执行, 不要下单 — 出场回款 09:30 才到账",
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
    ap.add_argument("--window", choices=["preclose", "main_run", "morning", "report"],
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
        "exec_window": exec_window(t),
        "preclose": preclose_state(date),
        "pending_exec": pending_exec_state(t),
    }

    blockers = []
    if cfg.get("enabled") is False:
        blockers.append("config.json enabled=false → 只允许读数据和写日志 (红线4)")
    if st.get("halted"):
        blockers.append("state.halted=true → 只允许读数据和写日志 (红线4)")
    if win == "main_run" and jr["completed"]:
        blockers.append(f"journal/{date}.md 已 status:completed → 幂等, 静默结束")
    if win in ("off_weekend", "off_hours"):
        msg = f"当前不在任何作业窗口 ({win}) → 只读; 如确需执行请用 --window 指定"
        if info["exec_window"]["open"]:
            msg += " (注: 4C 股票买单执行窗仍开放, 用户说「执行」照常按 playbook 4C 处理)"
        blockers.append(msg)
    if ck and ck.get("market_is_open") is False and win == "main_run":
        pass   # 主跑本就在收盘后, 不算 blocker
    info["blockers"] = blockers

    if a.json:
        info["checklist"] = CHECKLIST.get(win, [])
        print(json.dumps(info, ensure_ascii=False, indent=2))
        return 0

    W = {"preclose": "收盘前主跑 (15:20 ET)", "main_run": "收盘后收尾 (wrapup)",
         "morning": "晨间核查", "report": "收盘战报",
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
    print(f"日志: journal/{date}*.md  文件={jr['files'] or '无'}  "
          f"主跑completed={jr['completed']}"
          + (f" (标记在 {', '.join(jr['completed_in'])})" if jr['completed_in'] else ""))
    ew = info["exec_window"]
    print(f"4C 执行窗: {'🟢 开放' if ew['open'] else '🔴 关闭'}  {ew['reason']}")
    pcs = info["preclose"]
    if pcs["exists"]:
        mark = "✅ 今日已完成" if pcs["today"] else f"⚠️ 非今日/未完成 ({pcs['date']}/{pcs['status']})"
        print(f"收盘前主跑: {mark}"
              + (f"  出场 {pcs['sells']} 单 · 买单入 pending {pcs['buys']} 单" if pcs["today"] else ""))
    pe = info["pending_exec"]
    if pe["exec_window_et"]:
        print(f"待执行清单自带执行窗: {pe['exec_window_et']} ET ({pe['exec_day']})"
              f"  清单日 {pe['trade_date']}")
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
        if win == "main_run":
            pcs = info["preclose"]
            if pcs["today"]:
                print("⚠️ 当日收盘前主跑**已完成** → 本次用 `--phase wrapup` "
                      "(只跑纸面轨道 + 行情核对; 正股/期权信号绝不重算, 否则会看到新持仓重复出单)")
            else:
                print("当日无收盘前主跑完成标记 → 用 `--phase wrapup`; daily.py 会自动 fail-safe "
                      "退化为完整主跑 (出场照下, pending 按次日窗口)。不要手动改成 --phase full")
        print(f"""python3 scripts/daily.py --date {date} \\
  --portfolio-value <total_value> --buying-power <BP> \\
  --positions {a.workdir}/positions.json \\
  --earnings {a.workdir}/earnings.json \\
  --broker-closes {a.workdir}/broker_closes.json \\
  --workdir {a.workdir}{' --phase wrapup' if win == 'main_run' else ''}""")
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
