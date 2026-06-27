#!/usr/bin/env python3
"""
fetch_binance.py — read-only Binance balances across all wallets, valued in USD.

Stdlib only (urllib + hmac). Sums per asset across SPOT + Funding + Simple Earn
(flexible + locked), values each via public ticker prices (stablecoins = $1),
and maps to the snapshot categories/names the user already uses.

The COPYTRADING sleeve is NOT here — a copier's copy balance has no API; it comes
from a screenshot. Futures wallet is skipped (needs "Enable Futures"; not granted,
and copytrading is handled separately).

Needs BINANCE_API_KEY / BINANCE_API_SECRET in tools/.env (read-only key).
Run:  python3 tools/fetch_binance.py            # human summary
      python3 tools/fetch_binance.py --raw       # full normalized JSON
      python3 tools/fetch_binance.py --out tools/out/binance.json
"""
import argparse
import hashlib
import hmac
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import config

SPOT = "https://api.binance.com"

DAY_MS = 86400000
# capital deposit/withdraw history accept at most a 90-day [startTime,endTime] span.
CAPITAL_WINDOW_MS = 90 * DAY_MS
# c2c/p2p order history accepts at most a 30-day [startTimestamp,endTimestamp] span.
P2P_WINDOW_MS = 30 * DAY_MS
DEPOSIT_OK = {1}            # capital deposit status int: 1 = success (0 pending, 6 credited-no-withdraw, 7/8 problems)
WITHDRAW_OK = {6}           # capital withdraw status int: 6 = completed (0..5 = in-flight/cancelled/failed)
FIAT_OK = {"Successful", "Finished"}  # fiat order status strings considered final/credited


class BinanceError(Exception):
    def __init__(self, code, msg):
        self.code = code
        super().__init__(f"Binance error {code}: {msg}")


def load_env():
    path = os.path.join(HERE, ".env")
    if not os.path.exists(path):
        return
    for line in open(path):
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, v = s.split("=", 1)
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        os.environ.setdefault(k.strip(), v)


def public_get(path, params=None):
    url = f"{SPOT}{path}" + (("?" + urllib.parse.urlencode(params)) if params else "")
    with urllib.request.urlopen(url, timeout=60) as r:
        return json.loads(r.read())


def signed(path, key, secret, params=None, method="GET", host=SPOT):
    params = dict(params or {})
    params["timestamp"] = int(time.time() * 1000)
    params["recvWindow"] = 60000
    qs = urllib.parse.urlencode(params)
    qs += "&signature=" + hmac.new(secret.encode(), qs.encode(), hashlib.sha256).hexdigest()
    req = urllib.request.Request(f"{host}{path}?{qs}", headers={"X-MBX-APIKEY": key}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        try:
            err = json.loads(body)
        except ValueError:
            err = {"msg": body}
        raise BinanceError(err.get("code"), err.get("msg"))


def paginate(path, key, secret, base=None):
    rows, cur = [], 1
    while True:
        params = dict(base or {})
        params.update({"current": cur, "size": 100})
        data = signed(path, key, secret, params)
        chunk = data.get("rows", []) if isinstance(data, dict) else []
        rows.extend(chunk)
        if len(chunk) < 100:
            return rows
        cur += 1


def windows(start_ms, end_ms, span_ms):
    """Yield [from,to] sub-windows of at most span_ms covering [start_ms,end_ms]."""
    win = start_ms
    while win <= end_ms:
        yield win, min(win + span_ms, end_ms)
        win = min(win + span_ms, end_ms) + 1


def price_usd(asset, px):
    if asset in config.STABLES:
        return 1.0
    for q in ("USDT", "FDUSD", "USDC"):
        if asset + q in px:
            return px[asset + q]
    if asset + "BTC" in px and "BTCUSDT" in px:
        return px[asset + "BTC"] * px["BTCUSDT"]
    if asset + "ETH" in px and "ETHUSDT" in px:
        return px[asset + "ETH"] * px["ETHUSDT"]
    return None


def classify(asset):
    if asset in config.ASSET_MAP:
        return config.ASSET_MAP[asset]
    return ("usd", asset) if asset in config.STABLES else ("crypto", asset)


def collect(key, secret):
    qty = defaultdict(float)
    wallets = defaultdict(lambda: defaultdict(float))  # asset -> wallet -> qty (for transparency)
    manifest = {}

    def channel(name, fn):
        manifest[name] = {"queried": False, "rows": 0}
        try:
            n = fn()
            manifest[name].update({"queried": True, "rows": n})
        except (BinanceError, urllib.error.URLError) as e:
            manifest[name]["error"] = str(e)
            print(f"  ({name} wallet skipped: {e})")

    def do_spot():
        acct = signed("/api/v3/account", key, secret, {"omitZeroBalances": "true"})
        n = 0
        for b in acct.get("balances", []):
            a = float(b["free"]) + float(b["locked"])
            if a > 0:
                qty[b["asset"]] += a
                wallets[b["asset"]]["spot"] += a
                n += 1
        return n

    def do_funding():
        n = 0
        for r in signed("/sapi/v1/asset/get-funding-asset", key, secret, method="POST"):
            a = float(r["free"]) + float(r["locked"]) + float(r.get("freeze", 0))
            if a > 0:
                qty[r["asset"]] += a
                wallets[r["asset"]]["funding"] += a
                n += 1
        return n

    def do_earn_flex():
        n = 0
        for r in paginate("/sapi/v1/simple-earn/flexible/position", key, secret):
            a = float(r.get("totalAmount", 0))
            if a > 0:
                qty[r["asset"]] += a
                wallets[r["asset"]]["earn-flex"] += a
                n += 1
        return n

    def do_earn_locked():
        n = 0
        for r in paginate("/sapi/v1/simple-earn/locked/position", key, secret):
            a = float(r.get("amount", 0))
            if a > 0:
                qty[r["asset"]] += a
                wallets[r["asset"]]["earn-locked"] += a
                n += 1
        return n

    channel("spot", do_spot)
    channel("funding", do_funding)
    channel("earn-flex", do_earn_flex)
    channel("earn-locked", do_earn_locked)

    return qty, wallets, manifest


def flows(key, secret, start_ms, end_ms):
    out = {"deposits": [], "withdrawals": [], "converts": [], "dropped": [], "manifest": {}}

    def mark(name, window):
        out["manifest"][name] = {"queried": False, "window": window, "rows": 0}

    def ok(name, rows):
        # queried=True means fully + cleanly fetched; if any window/page errored, leave it False
        # so a completeness gate treats the channel as incomplete (rows may be truncated).
        out["manifest"][name]["rows"] = rows
        out["manifest"][name]["queried"] = "error" not in out["manifest"][name]

    def err(name, e):
        out["manifest"][name].setdefault("error", []).append(str(e))
        print(f"  ({name} skipped: {e})")

    # --- capital (on-chain) deposits: status==1 (success); chunk <=90d, paginate offset ---
    mark("capital-deposit", [start_ms, end_ms])
    n = 0
    for w_from, w_to in windows(start_ms, end_ms, CAPITAL_WINDOW_MS):
        offset = 0
        while True:
            try:
                page = signed("/sapi/v1/capital/deposit/hisrec", key, secret,
                              {"startTime": w_from, "endTime": w_to, "offset": offset, "limit": 1000})
            except (BinanceError, urllib.error.URLError) as e:
                err("capital-deposit", e)
                break
            if not page:
                break
            for d in page:
                row = {"coin": d["coin"], "amount": float(d["amount"]), "source": "capital",
                       "time": d.get("insertTime"), "network": d.get("network"), "txId": d.get("txId"),
                       "status": d.get("status")}
                if d.get("status") in DEPOSIT_OK:
                    out["deposits"].append(row)
                    n += 1
                else:
                    out["dropped"].append({**row, "channel": "capital-deposit", "reason": "non-final-status"})
            if len(page) < 1000:
                break
            offset += 1000
    ok("capital-deposit", n)

    # --- capital (on-chain) withdrawals: status==6 (completed); chunk <=90d, paginate offset ---
    mark("capital-withdraw", [start_ms, end_ms])
    n = 0
    for w_from, w_to in windows(start_ms, end_ms, CAPITAL_WINDOW_MS):
        offset = 0
        while True:
            try:
                page = signed("/sapi/v1/capital/withdraw/history", key, secret,
                              {"startTime": w_from, "endTime": w_to, "offset": offset, "limit": 1000})
            except (BinanceError, urllib.error.URLError) as e:
                err("capital-withdraw", e)
                break
            if not page:
                break
            for w in page:
                row = {"coin": w["coin"], "amount": float(w["amount"]), "source": "capital",
                       "fee": float(w.get("transactionFee", 0)), "time": w.get("applyTime"),
                       "address": w.get("address"), "network": w.get("network"), "status": w.get("status")}
                if w.get("status") in WITHDRAW_OK:
                    out["withdrawals"].append(row)
                    n += 1
                else:
                    out["dropped"].append({**row, "channel": "capital-withdraw", "reason": "non-final-status"})
            if len(page) < 1000:
                break
            offset += 1000
    ok("capital-withdraw", n)

    # --- fiat rail (card/bank): /sapi/v1/fiat/orders, transactionType 0=deposit 1=withdraw ---
    # response: {"code","message","data":[{fiatCurrency,amount,indicatedAmount,totalFee,status,...}],"total"}
    # status strings; only {Successful,Finished} are credited/debited for real.
    for tx_type, bucket, name in ((0, "deposits", "fiat-deposit"), (1, "withdrawals", "fiat-withdraw")):
        mark(name, [start_ms, end_ms])
        n = 0
        for w_from, w_to in windows(start_ms, end_ms, CAPITAL_WINDOW_MS):
            page = 1
            while True:
                try:
                    resp = signed("/sapi/v1/fiat/orders", key, secret,
                                  {"transactionType": tx_type, "beginTime": w_from, "endTime": w_to,
                                   "page": page, "rows": 500})
                except (BinanceError, urllib.error.URLError) as e:
                    err(name, e)
                    break
                data = resp.get("data") or [] if isinstance(resp, dict) else []
                for o in data:
                    row = {"fiatCurrency": o.get("fiatCurrency"), "amount": float(o.get("amount", 0)),
                           "indicatedAmount": float(o.get("indicatedAmount", 0)),
                           "fee": float(o.get("totalFee", 0)), "method": o.get("method"),
                           "time": o.get("createTime") or o.get("updateTime"),
                           "orderNo": o.get("orderNo"), "status": o.get("status"), "source": "fiat"}
                    if o.get("status") in FIAT_OK:
                        out[bucket].append(row)
                        n += 1
                    else:
                        out["dropped"].append({**row, "channel": name, "reason": "non-final-status"})
                if len(data) < 500:
                    break
                page += 1
        ok(name, n)

    # --- P2P (c2c) SELL = crypto out / fiat in the user's pocket -> treat as a withdrawal ---
    # IMPORTANT: params are startTimestamp/endTimestamp + page/rows. Using startTime/endTime
    # returns nothing silently. Window <=30d. resp: {"code","data":[{orderStatus,asset,amount,
    # totalPrice,fiat,unitPrice,createTime,...}],"total"}.
    mark("p2p-sell", [start_ms, end_ms])
    n = 0
    for w_from, w_to in windows(start_ms, end_ms, P2P_WINDOW_MS):
        page = 1
        while True:
            try:
                resp = signed("/sapi/v1/c2c/orderMatch/listUserOrderHistory", key, secret,
                              {"tradeType": "SELL", "startTimestamp": w_from, "endTimestamp": w_to,
                               "page": page, "rows": 100})
            except (BinanceError, urllib.error.URLError) as e:
                err("p2p-sell", e)
                break
            data = resp.get("data") or [] if isinstance(resp, dict) else []
            for o in data:
                row = {"coin": o.get("asset"), "amount": float(o.get("amount", 0)),
                       "fiatCurrency": o.get("fiat"), "fiatAmount": float(o.get("totalPrice", 0)),
                       "unitPrice": float(o.get("unitPrice", 0)), "time": o.get("createTime"),
                       "orderNo": o.get("orderNumber"), "status": o.get("orderStatus"), "source": "p2p"}
                if o.get("orderStatus") == "COMPLETED":
                    out["withdrawals"].append(row)
                    n += 1
                else:
                    out["dropped"].append({**row, "channel": "p2p-sell", "reason": "non-final-status"})
            if len(data) < 100:
                break
            page += 1
    ok("p2p-sell", n)

    # --- convert history (capped at 30-day windows) ---
    mark("convert", [start_ms, end_ms])
    n = 0
    for w_from, w_to in windows(start_ms, end_ms, P2P_WINDOW_MS):
        try:
            cv = signed("/sapi/v1/convert/tradeFlow", key, secret,
                        {"startTime": w_from, "endTime": w_to, "limit": 1000})
        except (BinanceError, urllib.error.URLError) as e:
            err("convert", e)
            continue
        for c in (cv.get("list") or []):
            out["converts"].append({"from": c["fromAsset"], "fromAmt": float(c["fromAmount"]),
                                    "to": c["toAsset"], "toAmt": float(c["toAmount"]), "time": c.get("createTime")})
            n += 1
    ok("convert", n)

    return out


def main():
    import datetime
    ap = argparse.ArgumentParser(description="Read-only Binance balances → USD (spot+funding+earn).")
    ap.add_argument("--raw", action="store_true")
    ap.add_argument("--out")
    ap.add_argument("--flows", metavar="SINCE", help="instead of balances, list deposits/withdrawals/converts since YYYY-MM-DD")
    args = ap.parse_args()

    load_env()
    key, secret = os.environ.get("BINANCE_API_KEY"), os.environ.get("BINANCE_API_SECRET")
    if not key or not secret:
        raise SystemExit("set BINANCE_API_KEY and BINANCE_API_SECRET in tools/.env")

    if args.flows:
        start = int(datetime.datetime.strptime(args.flows, "%Y-%m-%d").replace(tzinfo=datetime.timezone.utc).timestamp() * 1000)
        end = int(time.time() * 1000)
        fl = flows(key, secret, start, end)
        print(f"Binance flows since {args.flows}:")
        print(f"\n  deposits ({len(fl['deposits'])}):")
        for d in fl["deposits"]:
            unit = d.get("coin") or d.get("fiatCurrency") or "?"
            tag = d.get("source", "")
            print(f"    +{d['amount']:>12,.2f} {unit:6} [{tag}] {d.get('network') or ''}")
        print(f"  withdrawals ({len(fl['withdrawals'])}):")
        for w in fl["withdrawals"]:
            unit = w.get("coin") or w.get("fiatCurrency") or "?"
            tag = w.get("source", "")
            extra = ""
            if w.get("source") == "p2p":
                extra = f"-> {w.get('fiatAmount', 0):,.2f} {w.get('fiatCurrency') or ''}"
            elif w.get("source") == "capital":
                extra = f"fee {w.get('fee', 0):g}  {w.get('network') or ''}  ->{(w.get('address') or '')[:14]}"
            print(f"    -{w['amount']:>12,.2f} {unit:6} [{tag}] {extra}")
        print(f"  converts ({len(fl['converts'])}):")
        for c in fl["converts"]:
            print(f"    {c['fromAmt']:>12,.4f} {c['from']:6} -> {c['toAmt']:>12,.4f} {c['to']}")
        if fl["dropped"]:
            print(f"  dropped non-final rows ({len(fl['dropped'])}):")
            for r in fl["dropped"]:
                unit = r.get("coin") or r.get("fiatCurrency") or "?"
                print(f"    {r.get('channel'):16} {r['amount']:>12,.2f} {unit:6} status={r.get('status')}")
        print("\n  manifest:")
        for ch, m in fl["manifest"].items():
            flag = "" if m.get("queried") and not m.get("error") else "  !! INCOMPLETE"
            print(f"    {ch:16} queried={m['queried']} rows={m['rows']}{flag}")
        return

    px = {d["symbol"]: float(d["price"]) for d in public_get("/api/v3/ticker/price")}
    qty, wallets, manifest = collect(key, secret)

    items, unknown = [], []
    for asset, q in qty.items():
        if asset.startswith("LD") and asset[2:] in qty:
            continue  # flexible-Earn shadow token (LDBTC≈BTC) — already counted via simple-earn endpoint
        p = price_usd(asset, px)
        if p is None:
            unknown.append({"asset": asset, "qty": q, "wallets": dict(wallets[asset])})
            continue
        val = round(q * p, 2)
        if val < config.TOL["dust_usd"]:
            continue
        cat, name = classify(asset)
        items.append({"category": cat, "source": "Binance", "name": name, "asset": asset,
                      "qty": q, "val": val, "wallets": dict(wallets[asset])})

    data = {"source": "Binance", "snapshot_items": items, "unpriced": unknown, "manifest": manifest,
            "note": "copytrading sleeve NOT included (screenshot); futures wallet not read. "
                    "unpriced assets are surfaced, NOT dropped from awareness — value them manually."}

    if args.raw:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return
    if args.out:
        os.makedirs(os.path.dirname(args.out), exist_ok=True)
        with open(args.out, "w") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
        print(f"wrote {args.out}")

    total = sum(it["val"] for it in items)
    by_cat = defaultdict(float)
    for it in items:
        by_cat[it["category"]] += it["val"]
    print(f"Binance balances (USD, ex-copytrading):  total {total:,.2f}")
    for cat in sorted(by_cat):
        print(f"\n  [{cat}]  {by_cat[cat]:,.2f}")
        for it in sorted((x for x in items if x["category"] == cat), key=lambda x: -x["val"]):
            w = ",".join(f"{k}={v:g}" for k, v in it["wallets"].items())
            print(f"    {it['name']:18} {it['val']:>11,.2f}   ({it['asset']} {it['qty']:g}  [{w}])")
    if unknown:
        print("\n  unpriced (no USDT/BTC/ETH pair — NOT in total, value manually):")
        for u in unknown:
            print(f"    {u['asset']}  qty {u['qty']:g}")
    bad = [c for c, m in manifest.items() if not m.get("queried") or m.get("error")]
    if bad:
        print(f"\n  !! incomplete wallet channels (balance may be understated): {', '.join(bad)}")
    print("\n  + copytrading (Binance) — add from screenshot;  futures wallet not read (read-only key)")


if __name__ == "__main__":
    main()
