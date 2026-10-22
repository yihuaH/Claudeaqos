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


def monthlies_near(target_date, k=3):
    """离 target_date 最近的 k 个标准月 (第三个周五), 由近到远。

    ⚠️ 为什么给多个而不是一个 (2026-10-22 实测踩到): QQQ **并不挂满每个标准月的长期档** ——
    2027-04-16 与 2027-10-15 都不存在, 带内只有 2027-03-19 / 2027-06-17 / 2027-09-17。
    让 plan 只给一个到期, 会把会话送去查一个不存在的合约。给候选集 + 由会话一次查出哪个
    存在, 把"哪些到期挂了"这个问题整个从本阶段移走。
    """
    cands = []
    for off in (-2, -1, 0, 1, 2, 3):
        y, m = target_date.year, target_date.month + off
        while m < 1:  y, m = y - 1, m + 12
        while m > 12: y, m = y + 1, m - 12
        cands.append(third_friday(y, m))
    cands.sort(key=lambda d: abs((d - target_date).days))
    return cands[:k]


def _spot_from(a):
    """现货优先级: --spot > --quotes 里的券商报价 > --historicals 最后一根收盘。
    让 daily.py 只需把两个现成文件传进来, 不必自己解析。"""
    if a.spot:
        return float(a.spot), 'arg'
    q = load(a.quotes, {}) if getattr(a, 'quotes', None) else {}
    if isinstance(q, dict):
        if a.underlying in q and isinstance(q[a.underlying], (int, float)):
            return float(q[a.underlying]), 'quotes'
        for r in (q.get('data', q) or {}).get('results', []) or []:
            qq = r.get('quote', r)
            if qq.get('symbol') == a.underlying and qq.get('last_trade_price'):
                return float(qq['last_trade_price']), 'quotes'
    b = load(a.historicals, {}) if getattr(a, 'historicals', None) else {}
    for e in ((b.get('data', b) or {}).get('results', []) or []):
        if e.get('symbol') == a.underlying and e.get('bars'):
            return float(e['bars'][-1]['close_price']), 'bars'
    return None, None


def cmd_plan(a):
    store = load(a.store, {})
    spot, src = _spot_from(a)
    if spot is None:
        print(json.dumps({"skipped": f"拿不到 {a.underlying} 现货 (--spot / --quotes / --historicals 都没给出)",
                          "_no_trade": "纯取数, 跳过无任何后果"}, ensure_ascii=False))
        return
    a.spot, a.spot_source = spot, src
    ivs = store.get('_last_iv_by_bucket', {})
    today = dt.date.fromisoformat(a.date)
    want, seen = [], store.get('_option_id_cache', {})
    for b in DTE_BUCKETS:
        exps = monthlies_near(today + dt.timedelta(days=b))
        sig = float(ivs.get(str(b), DEFAULT_IV))
        per = []
        for exp in exps:
            dte = (exp - today).days
            if dte <= 0: continue
            k0 = strike_for_delta(a.spot, dte / 365.0, sig)
            ks = [k0 + (i - BRACKET // 2) * STRIKE_STEP for i in range(BRACKET)]
            per.append({"expiry": exp.isoformat(), "actual_dte": dte, "strikes": ks,
                        "cached_ids": {f"{k:.4f}": seen[f"{exp.isoformat()}|{k:.4f}"]
                                       for k in ks if f"{exp.isoformat()}|{k:.4f}" in seen}})
        want.append({"dte_bucket": b, "iv_used": sig, "expiry_candidates": per,
                     "_pick": "会话取**第一个存在的**到期即可; 其余候选是 QQQ 可能没挂该月的兜底"})
    out = {"date": a.date, "underlying": a.underlying, "spot": a.spot,
           "spot_source": getattr(a, 'spot_source', 'arg'), "need": want,
           "_session_action": ("① 每个 dte_bucket 取 expiry_candidates 里**第一个存在的**到期 —— "
                              "一次 get_option_instruments 可同时传多个 expiration_dates + 一个 "
                              "strike_price, 返回哪个就是哪个存在; cached_ids 里已有的可跳过。"
                              "② 对全部 option_id 调 get_option_quotes (**必须含 delta**)。"
                              "③ 两个原始输出都传给 record —— record 按实际 DTE 自行归档, "
                              "不要求与 dte_bucket 精确相等。"),
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
        if name == 'plan':
            s.add_argument('--spot', type=float)          # 可选: 缺则从 quotes/historicals 推
            s.add_argument('--quotes')
            s.add_argument('--historicals')
            s.add_argument('--out')
        if name == 'record':
            s.add_argument('--spot', type=float, required=True)
        if name == 'record':
            s.add_argument('--quotes', required=True)
            s.add_argument('--instruments')
            s.add_argument('--day-return-pct', type=float, dest='day_return_pct')
            s.add_argument('--window', default='wrapup')
        s.set_defaults(func=fn)
    a = p.parse_args(); a.func(a)


if __name__ == '__main__':
    main()
