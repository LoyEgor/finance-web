#!/usr/bin/env python3
"""
fetch_benchmarks.py — fetch the config.BENCHMARKS closing prices for every snapshot
date and write them into data/benchmarks.json. Public source (Yahoo Finance), no key.

Uses ADJUSTED closes: the portfolio these benchmark against reinvests everything
implicitly (dividends land in the snapshot balances), so a raw close understates
the index by its full dividend yield and inflates every bucket's alpha.

The adjusted series is re-based by each dividend/split, so a file mixing yesterday's
adjusted values with today's would report fake returns across the boundary. Every
run therefore REBUILDS the whole file: one chart request per symbol spanning all
snapshot dates, re-priced from scratch. The file is written only once every symbol
resolved for every date — a partial benchmarks.json renders as a fabricated 0.0% YTD
in the app.

Keys the price under the EXACT snapshot date string the app expects. If that day is
a weekend/holiday or its close isn't published yet, uses the most recent completed
trading day on/before it. Today's bar is never used: it is still moving, and the
price it happens to print would never be corrected.

Run:  python3 tools/fetch_benchmarks.py            # every snapshot date in data/
      python3 tools/fetch_benchmarks.py 2026-06-27 # ...plus this extra date
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


def snapshot_dates():
    dates = set()
    for f in glob.glob(os.path.join(DATA, "20*-*.json")):
        if "transfers" in os.path.basename(f):
            continue
        d = (json.load(open(f)).get("meta") or {}).get("date")
        if d:
            dates.add(d)
    return sorted(dates)


def adjclose_series(ticker, first, last):
    """{YYYY-MM-DD: adjusted close} covering [first, last]."""
    # Anchor the request window to the dates asked for (not NOW) so any historical
    # date works; range=3mo is relative to now and returned nothing when backfilling.
    lo = datetime.datetime.strptime(first, "%Y-%m-%d").replace(tzinfo=datetime.timezone.utc)
    hi = datetime.datetime.strptime(last, "%Y-%m-%d").replace(tzinfo=datetime.timezone.utc)
    period1 = int((lo - datetime.timedelta(days=10)).timestamp())
    period2 = int((hi + datetime.timedelta(days=1)).timestamp())
    url = (
        f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
        f"?interval=1d&period1={period1}&period2={period2}&events=div%7Csplit"
    )
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=60) as r:
        res = json.load(r)["chart"]["result"][0]
    ts = res.get("timestamp") or []
    adj = (res.get("indicators", {}).get("adjclose") or [{}])[0].get("adjclose")
    if adj is None:
        # Falling back to raw `close` here would silently reintroduce the
        # dividend-yield understatement this module exists to avoid.
        raise SystemExit(f"{ticker}: Yahoo returned no adjclose series — refusing to use raw closes")
    today = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
    series = {}
    for t, cl in zip(ts, adj):
        day = datetime.datetime.fromtimestamp(t, datetime.timezone.utc).strftime("%Y-%m-%d")
        if cl is None or day >= today:  # today's session is still open — not a settled close
            continue
        series[day] = round(float(cl), 2)
    return series


def close_on_or_before(series, target):
    days = [d for d in series if d <= target]
    if not days:
        return (None, None)
    day = max(days)
    return day, series[day]


def main():
    dates = snapshot_dates()
    if len(sys.argv) > 1:
        dates = sorted(set(dates) | {sys.argv[1]})
    if not dates:
        raise SystemExit("no snapshot date found; pass one e.g. 2026-06-27")

    bench = {}
    missing = []
    for key, sym in SYMBOLS.items():
        series = adjclose_series(sym, dates[0], dates[-1])
        print(f"{key} ({sym}, adjusted):")
        for target in dates:
            day, price = close_on_or_before(series, target)
            if price is None:
                print(f"  {target}: NOT FOUND (check symbol/source)")
                missing.append(f"{key}@{target}")
                continue
            bench.setdefault(key, {})[target] = price
            note = "" if day == target else f"  (close from {day} — {target} not a settled trading day)"
            print(f"  {target}: {price:.2f}{note}")

    if missing:
        raise SystemExit(f"no price resolved for: {', '.join(missing)} — benchmarks.json left untouched")

    path = os.path.join(DATA, "benchmarks.json")
    with open(path, "w") as fh:
        json.dump(bench, fh, ensure_ascii=False, indent=4)
    print(f"\nrewrote {path}  ({len(SYMBOLS)} symbol(s) × {len(dates)} date(s))")


if __name__ == "__main__":
    main()
