#!/usr/bin/env python3
"""
外部数据源自诊断 (Alpaca paper / FRED)。

用法: python3 scripts/integrations.py status
每日 Routine 在前置检查时运行, 把输出记入 journal。
三项全 ok 之前, 这些数据源不参与任何交易决策。
密钥从环境变量读取 (ALPACA_API_KEY_ID / ALPACA_API_SECRET_KEY / FRED_API_KEY), 不入库。
"""
import json
import os
import sys
import urllib.request


def _get(url, headers=None, timeout=15):
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def status():
    out = {}

    fred_key = os.environ.get("FRED_API_KEY")
    if not fred_key:
        out["fred"] = {"ok": False, "reason": "FRED_API_KEY 未设置"}
    else:
        try:
            j = _get("https://api.stlouisfed.org/fred/series/observations"
                     f"?series_id=VIXCLS&api_key={fred_key}&file_type=json&sort_order=desc&limit=1")
            obs = j["observations"][0]
            out["fred"] = {"ok": True, "vix": obs["value"], "date": obs["date"]}
        except Exception as e:
            out["fred"] = {"ok": False, "reason": str(e)[:120]}

    ak, asec = os.environ.get("ALPACA_API_KEY_ID"), os.environ.get("ALPACA_API_SECRET_KEY")
    if not (ak and asec):
        out["alpaca"] = {"ok": False, "reason": "ALPACA_API_KEY_ID/SECRET 未设置"}
    else:
        hdrs = {"APCA-API-KEY-ID": ak, "APCA-API-SECRET-KEY": asec}
        try:
            clock = _get("https://paper-api.alpaca.markets/v2/clock", hdrs)
            out["alpaca"] = {"ok": True, "market_is_open": clock["is_open"],
                             "next_open": clock["next_open"], "next_close": clock["next_close"]}
        except Exception as e:
            out["alpaca"] = {"ok": False, "reason": str(e)[:120]}

    out["all_ok"] = all(v.get("ok") for k, v in out.items() if k != "all_ok")
    print(json.dumps(out, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] != "status":
        print(__doc__)
        sys.exit(1)
    sys.exit(status())
