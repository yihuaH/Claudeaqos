#!/usr/bin/env python3
"""期权点差/IV 采样器 —— **纯取数留痕, 不产生任何订单、不参与任何交易判定** (红线2)。

目的: 为 DTE 选择补上数据。2026-10-22 的 DTE 扫描发现
`qqq-leap-call.md §16.1` 的点差拟合 (n=3, 只在一年期档标定) 把 30 日档的往返点差
高估了 **17 倍** (拟合 12.35% vs 实测 0.74%), 换成实测点差后 91/120/182/273 日档
全部显著优于现行 365 日。但那个结论挂在 **n=5 的单时点测量**上, 太薄不足以改 config。
本采样器就是去攒那个样本。

⚠️ **本脚本不被任何交易判定读取。** `qqq_leap.py` / `signals.py` / `weekly_calls.py`
   都不读 `state/option_spread_samples.json`。它唯一的消费者是人与 dte-sweep 研究脚本。

两段式 (同 qqq_leap.py 的理由: 券商报价只能由会话经 MCP 取, 且 integrations.py chains
无 delta 且是 indicative feed):
  ① `plan`   —— 纯本地: 读历史样本估 delta 目标行权价, 输出该取哪些 (到期, 行权价)
  ② `record` —— 把会话取回的券商报价挑出每个 DTE 档最接近 delta 目标的一张, 追加落盘

用法:
  python3 scripts/spread_probe.py plan --store state/option_spread_samples.json \
      --spot 705.65 --date 2026-10-22 [--underlying QQQ] [--out plan.json]
  python3 scripts/spread_probe.py record --store state/option_spread_samples.json \
      --quotes <券商 get_option_quotes 原始输出> --instruments <get_option_instruments 原始输出> \
      --spot 705.65 --day-return-pct -2.696 --date 2026-10-22 --window wrapup
"""
import argparse, json, math, os, datetime as dt

DTE_BUCKETS = [30, 90, 180, 365]     # 采样档位 (日历日)
DELTA_TGT   = 0.30                   # 与 qqq_leap.json contract.delta_target 一致
BRACKET     = 3                      # 每个到期取的行权价个数 (围绕估算的 delta 目标)
STRIKE_STEP = 5.0
DEFAULT_IV  = 0.21                   # 无历史样本时的起始猜测 (仅用于估行权价, 不进任何判定)


def load(p, d=None):
    if p and os.path.exists(p):
        with open(p) as f: return json.load(f)
    return d if d is not None else {}


def save(p, obj):
    os.makedirs(os.path.dirname(p) or '.', exist_ok=True)
    with open(p, 'w') as f:
        json.dump(obj, f, ensure_ascii=False, indent=1); f.write('\n')


def _nd(x): return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def strike_for_delta(S, T, sig, tgt=DELTA_TGT, q=0.005, r=0.04):
    """反解使 BS delta ≈ tgt 的行权价, 再对齐到 $5 网格。纯估算, 只为缩小取数范围。"""
    if T <= 0 or sig <= 0: return round(S / STRIKE_STEP) * STRIKE_STEP
    lo, hi = S * 0.5, S * 2.5
    for _ in range(60):
        mid = (lo + hi) / 2
        v = sig * math.sqrt(T)
        d = math.exp(-q * T) * _nd((math.log(S / mid) + (r - q + 0.5 * sig * sig) * T) / v)
        if d > tgt: lo = mid
        else:       hi = mid
    return round((lo + hi) / 2 / STRIKE_STEP) * STRIKE_STEP


def third_friday(y, m):
    d = dt.date(y, m, 1)
    d += dt.timedelta(days=(4 - d.weekday()) % 7)   # 第一个周五
    return d + dt.timedelta(days=14)


def monthly_near(target_date):
    """离 target_date 最近的标准月 (第三个周五)。"""
    cands = []
    for off in (-1, 0, 1, 2):
        y, m = target_date.year, target_date.month + off
        while m < 1:  y, m = y - 1, m + 12
        while m > 12: y, m = y + 1, m - 12
        cands.append(third_friday(y, m))
    return min(cands, key=lambda d: abs((d - target_date).days))


def cmd_plan(a):
    store = load(a.store, {})
    ivs = store.get('_last_iv_by_bucket', {})
    today = dt.date.fromisoformat(a.date)
    want, seen = [], store.get('_option_id_cache', {})
    for b in DTE_BUCKETS:
        exp = monthly_near(today + dt.timedelta(days=b))
        dte = (exp - today).days
        sig = float(ivs.get(str(b), DEFAULT_IV))
        k0 = strike_for_delta(a.spot, dte / 365.0, sig)
        ks = [k0 + (i - BRACKET // 2) * STRIKE_STEP for i in range(BRACKET)]
        want.append({"dte_bucket": b, "expiry": exp.isoformat(), "actual_dte": dte,
                     "iv_used": sig, "strikes": ks,
                     "cached_ids": {f"{k:.4f}": seen.get(f"{exp.isoformat()}|{k:.4f}")
                                    for k in ks if seen.get(f"{exp.isoformat()}|{k:.4f}")}})
    out = {"date": a.date, "underlying": a.underlying, "spot": a.spot, "need": want,
           "_session_action": ("对 need 里每个 (expiry, strikes) 调 get_option_instruments "
                              "(cached_ids 里已有的可跳过), 再对全部 option_id 调 "
                              "get_option_quotes (必须含 delta), 两个原始输出都传给 record。"),
           "_no_trade": "纯取数。本文件与 record 的产出都不被任何交易判定读取 (红线2)。"}
    s = json.dumps(out, ensure_ascii=False, indent=1)
    if a.out: save(a.out, out)
    print(s)


def cmd_record(a):
    store = load(a.store, {})
    samples = store.setdefault('samples', [])
    idcache = store.setdefault('_option_id_cache', {})
    lastiv  = store.setdefault('_last_iv_by_bucket', {})
    store.setdefault('_what', '期权点差/IV 采样 —— 纯取数留痕, 不被任何交易判定读取 (红线2)。')
    store.setdefault('_why', ('为 DTE 选择补数据。2026-10-22 查出 §16.1 点差拟合 (n=3, 只在一年期标定) '
                              '把 30 日档往返点差高估 17 倍; 换实测点差后 91~273 日档显著优于现行 365 日, '
                              '但该结论挂在 n=5 单时点测量上。见 journal/2026-10-22-research-dte-sweep.md。'))
    # 行权价 → option_id (供下次 plan 复用)
    inst = load(a.instruments, {})
    rows = inst.get('data', inst).get('instruments', []) if isinstance(inst, dict) else []
    meta = {}
    for it in rows:
        key = f"{it['expiration_date']}|{float(it['strike_price']):.4f}"
        idcache[key] = it['id']
        meta[it['id']] = (it['expiration_date'], float(it['strike_price']))
    for k, v in idcache.items():
        e, s = k.split('|'); meta.setdefault(v, (e, float(s)))

    q = load(a.quotes, {})
    qrows = q.get('data', q).get('results', []) if isinstance(q, dict) else []
    today = dt.date.fromisoformat(a.date)
    by_bucket = {}
    for r in qrows:
        qq = r.get('quote', r)
        oid = qq.get('instrument_id')
        if oid not in meta:
            continue                                  # 不认识的合约直接跳过, 不猜
        exp, strike = meta[oid]
        d = qq.get('delta')
        bid, ask = qq.get('bid_price'), qq.get('ask_price')
        if d is None or bid is None or ask is None:
            continue
        d, bid, ask = float(d), float(bid), float(ask)
        mid = (bid + ask) / 2
        if mid <= 0:
            continue
        dte = (dt.date.fromisoformat(exp) - today).days
        b = min(DTE_BUCKETS, key=lambda x: abs(x - dte))
        cand = {"date": a.date, "window": a.window, "underlying": a.underlying,
                "dte_bucket": b, "expiry": exp, "actual_dte": dte, "strike": strike,
                "option_id": oid, "delta": round(d, 6),
                "bid": bid, "ask": ask, "mid": round(mid, 4),
                "spread_pct": round((ask - bid) / mid * 100, 4),
                "iv": float(qq['implied_volatility']) if qq.get('implied_volatility') else None,
                "open_interest": qq.get('open_interest'), "volume": qq.get('volume'),
                "spot": a.spot, "day_return_pct": a.day_return_pct}
        prev = by_bucket.get(b)
        if prev is None or abs(d - DELTA_TGT) < abs(prev['delta'] - DELTA_TGT):
            by_bucket[b] = cand

    added = 0
    for b in sorted(by_bucket):
        c = by_bucket[b]
        if any(s['date'] == c['date'] and s['window'] == c['window']
               and s['dte_bucket'] == b and s['underlying'] == c['underlying'] for s in samples):
            continue                                   # 幂等: 同日同窗同档只记一条
        samples.append(c); added += 1
        if c['iv']: lastiv[str(b)] = round(c['iv'], 6)
    store['_updated'] = f"{a.date} {a.window}"
    save(a.store, store)
    print(json.dumps({"added": added, "total_samples": len(samples),
                      "buckets": {str(b): by_bucket[b]['spread_pct'] for b in sorted(by_bucket)}},
                     ensure_ascii=False))


def cmd_report(a):
    store = load(a.store, {})
    s = store.get('samples', [])
    if not s:
        print(json.dumps({"samples": 0, "note": "还没有样本"}, ensure_ascii=False)); return
    import statistics
    out = {"total": len(s), "dates": len({x['date'] for x in s}),
           "span": [min(x['date'] for x in s), max(x['date'] for x in s)], "by_bucket": {}}
    for b in DTE_BUCKETS:
        g = [x for x in s if x['dte_bucket'] == b]
        if not g: continue
        sp = [x['spread_pct'] for x in g]
        dn = [x['spread_pct'] for x in g if (x.get('day_return_pct') or 0) <= -2.0]
        out['by_bucket'][str(b)] = {
            "n": len(g), "spread_pct_median": round(statistics.median(sp), 3),
            "spread_pct_mean": round(statistics.mean(sp), 3),
            "spread_pct_min": round(min(sp), 3), "spread_pct_max": round(max(sp), 3),
            "iv_median": round(statistics.median([x['iv'] for x in g if x['iv']]), 4)
                         if any(x['iv'] for x in g) else None,
            "delta_median": round(statistics.median([x['delta'] for x in g]), 4),
            "n_down_days": len(dn),
            "spread_pct_median_on_down_days": round(statistics.median(dn), 3) if dn else None,
            "_down_day_note": "day_return_pct ≤ -2% 的子集 —— 这才是 LEAP 轨道真正入场的时刻"}
    print(json.dumps(out, ensure_ascii=False, indent=1))


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest='cmd', required=True)
    for name, fn in (('plan', cmd_plan), ('record', cmd_record), ('report', cmd_report)):
        s = sub.add_parser(name)
        s.add_argument('--store', default='state/option_spread_samples.json')
        s.add_argument('--underlying', default='QQQ')
        if name != 'report':
            s.add_argument('--date', required=True)
            s.add_argument('--spot', type=float, required=True)
        if name == 'plan':
            s.add_argument('--out')
        if name == 'record':
            s.add_argument('--quotes', required=True)
            s.add_argument('--instruments')
            s.add_argument('--day-return-pct', type=float, dest='day_return_pct')
            s.add_argument('--window', default='wrapup')
        s.set_defaults(func=fn)
    a = p.parse_args(); a.func(a)


if __name__ == '__main__':
    main()
