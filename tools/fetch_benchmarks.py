#!/usr/bin/env python3
"""
fetch_benchmarks.py — fetch VT and VOO (S&P 500) closing prices for a snapshot
date and write them into data/benchmarks.json. Public source (Yahoo Finance), no key.

Keys the price under the EXACT snapshot date string the app expects. If that day
is a weekend/holiday or its close isn't published yet, uses the most recent
completed trading day on/before it.

Run:  python3 tools/fetch_benchmarks.py            # uses latest snapshot's meta.date
      python3 tools/fetch_benchmarks.py 2026-06-27
"""
import datetime
import json
import os
import sys
import glob
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import config

DATA = os.path.normpath(os.path.join(HERE, "..", "data"))
SYMBOLS = {s: s for s in config.BENCHMARKS}  # app key -> Yahoo ticker


def latest_snapshot_date():
    dates = []
    for f in glob.glob(os.path.join(DATA, "20*-*.json")):
        if "transfers" in os.path.basename(f):
            continue
        d = (json.load(open(f)).get("meta") or {}).get("date")
        if d:
            dates.append(d)
    return max(dates) if dates else None


def close_on_or_before(ticker, target):
    # Anchor the request window to `target` (not NOW) so any historical date works.
    # range=3mo is relative to now, so backfilling an old snapshot returned nothing.
    tgt = datetime.datetime.strptime(target, "%Y-%m-%d").replace(tzinfo=datetime.timezone.utc)
    period1 = int((tgt - datetime.timedelta(days=10)).timestamp())
    period2 = int((tgt + datetime.timedelta(days=1)).timestamp())
    url = (
        f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
        f"?interval=1d&period1={period1}&period2={period2}"
    )
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=60) as r:
        res = json.load(r)["chart"]["result"][0]
    ts = res.get("timestamp") or []
    closes = (res.get("indicators", {}).get("quote") or [{}])[0].get("close") or []
    last = None  # ascending; take last completed (non-null) close on/before target
    for t, cl in zip(ts, closes):
        day = datetime.datetime.fromtimestamp(t, datetime.timezone.utc).strftime("%Y-%m-%d")
        if cl is not None and day <= target:
            last = (day, round(float(cl), 2))
    return last if last else (None, None)


def main():
    target = sys.argv[1] if len(sys.argv) > 1 else latest_snapshot_date()
    if not target:
        raise SystemExit("no snapshot date found; pass one e.g. 2026-06-27")

    path = os.path.join(DATA, "benchmarks.json")
    bench = json.load(open(path)) if os.path.exists(path) else {}

    print(f"benchmarks for snapshot date {target}:")
    missing = []
    for key, sym in SYMBOLS.items():
        day, price = close_on_or_before(sym, target)
        if price is None:
            print(f"  {key}: NOT FOUND (check symbol/source)")
            missing.append(key)
            continue
        bench.setdefault(key, {})[target] = price
        note = "" if day == target else f"  (close from {day} — {target} not a trading day)"
        print(f"  {key}: {price:.2f}{note}")

    with open(path, "w") as fh:
        json.dump(bench, fh, ensure_ascii=False, indent=4)
    print(f"\nupdated {path}")

    if missing:
        raise SystemExit(f"no price resolved for: {', '.join(missing)}")


if __name__ == "__main__":
    main()
