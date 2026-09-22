#!/usr/bin/env python3
"""
orchestrator.py — the portfolio-snapshot pipeline runner: fetch → gate → assemble
→ simplify → reconcile → CHECK → flag. Ties config + fetchers + reconcile +
checks together and prints ONE prioritized report. It never writes files on its
own except an optional --out draft (the skill/human confirms before anything
lands in data/).

Modes:
  (default / live)  For each config.VENUES api venue, run its fetcher to build the
                    candidate snapshot + flows + coverage manifest, then reconcile
                    against the previous month and run every guardrail.
  --validate        Self-test offline against the committed golden in data/: take
                    the latest snapshot as "current", its prior-month snapshot as
                    "previous", the recorded transfers for the period as the book,
                    and run the WHOLE pipeline (gate→reconcile→checks→flag) end to
                    end. No live signed Binance/IBKR calls — the BTC price-anchor
                    return is derived from the API venue's own residual so the
                    anchor is exercised without a network round-trip.

All portfolio specifics come from config (VENUES / ASSET_MAP / STABLES /
BENCHMARKS / RECURRING / TOL / BASE_CCY). Nothing here hard-codes a ticker,
venue, or amount.
"""
import argparse
import datetime
import json
import os
import re
import sys
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import config
import reconcile
import checks
import simplify_transfers


# ── snapshot helpers (reuse reconcile's loader, expose its item maps) ─────────
def _items_map(snap):
    """reconcile.load_snapshots already keys items by asset_key with the shape
    checks.py expects ({cat, source, name, val}); pass it straight through."""
    return snap["items"]


def _snapshot_from_items(items, date, curr_month):
    """Build a reconcile-shaped snapshot ({month,date,items,by_cat,total}) from a
    flat list of candidate rows {category, source, name, val} — the live equivalent
    of one entry from reconcile.load_snapshots, so the same reconcile/check math
    applies to a freshly-assembled (not yet committed) snapshot."""
    keyed = {}
    by_cat = defaultdict(float)
    for it in items:
        cid = it["category"]
        k = reconcile.asset_key(cid, it["source"], it["name"])
        keyed[k] = {"cat": cid, "source": it["source"], "name": it["name"], "val": float(it["val"])}
        by_cat[cid] += float(it["val"])
    return {"month": curr_month, "date": date, "items": keyed,
            "by_cat": dict(by_cat), "total": sum(by_cat.values())}


def category_residuals(prev, curr, transfers):
    cats = set(prev["by_cat"]) | set(curr["by_cat"])
    return {c: (curr["by_cat"].get(c, 0.0) - prev["by_cat"].get(c, 0.0)
                - reconcile.cat_netflow(transfers, c)) for c in cats}


def stable_deltas(prev, curr):
    out = []
    union = {**prev["items"], **curr["items"]}
    for k, meta in union.items():
        if meta["cat"] != reconcile.STABLE_CAT:
            continue
        pv = prev["items"].get(k, {}).get("val", 0.0)
        cv = curr["items"].get(k, {}).get("val", 0.0)
        out.append((meta, cv - pv))
    return out


# ── price feed for the cross-venue anchor ────────────────────────────────────
def make_klines_price_fn(prev_date, curr_date):
    """LIVE price_fn: real period return r for an asset code from Binance public
    klines (no key). Falls back to None if unavailable; checks.price_anchor then
    skips that code rather than guessing. Bound to the period dates via closure so
    checks.price_anchor can call price_fn(code, None, None)."""
    cache = {}

    def close_on_or_before(symbol, date_ymd):
        import urllib.request
        tgt = datetime.datetime.strptime(date_ymd, "%Y-%m-%d").replace(tzinfo=datetime.timezone.utc)
        start = int((tgt - datetime.timedelta(days=7)).timestamp() * 1000)
        end = int((tgt + datetime.timedelta(days=1)).timestamp() * 1000)
        url = (f"https://api.binance.com/api/v3/klines?symbol={symbol}"
               f"&interval=1d&startTime={start}&endTime={end}&limit=20")
        with urllib.request.urlopen(url, timeout=60) as r:
            rows = json.load(r)
        last = None
        for k in rows:  # [openTime, open, high, low, close, ...]
            day = datetime.datetime.fromtimestamp(k[0] / 1000, datetime.timezone.utc).strftime("%Y-%m-%d")
            if day <= date_ymd:
                last = float(k[4])
        return last

    def price_fn(code, _a, _b):
        if code in cache:
            return cache[code]
        r = None
        for quote in ("USDT", "FDUSD", "USDC"):
            sym = f"{code}{quote}"
            try:
                p0 = close_on_or_before(sym, prev_date)
                p1 = close_on_or_before(sym, curr_date)
            except Exception:
                continue
            if p0 and p1:
                r = p1 / p0 - 1
                break
        cache[code] = r
        return r

    return price_fn


def make_residual_anchor_price_fn(prev, curr, transfers, config):
    """OFFLINE price_fn for --validate: derive a fungible code's real period return
    r from the API venue holding it — its qty is trusted, so its market move IS r.

        r = (curr_val - prev_val - recordedNetFlow) / prev_val   on the API venue.

    This exercises the SAME price_anchor math as the live klines feed (which venue
    diverges from r) without a network round-trip. The API venue then anchors at
    excess≈0 by construction; only a screenshot venue can diverge — exactly the
    signal we want from the golden self-test."""
    adj, _ = reconcile.adjustments(transfers)
    api_venues = {v for v, s in config.VENUES.items() if s.get("method") == "api"}
    # Which API venue actually produced r, per code: price_anchor charges the flow no
    # period return ONLY on that venue's rows, and two API venues can anchor different
    # codes in the same run.
    anchored_on = {}

    def price_fn(code, _a, _b):
        for k, meta in {**prev["items"], **curr["items"]}.items():
            if meta["source"] not in api_venues:
                continue
            if checks._asset_code(meta["name"], config) != code:
                continue
            pv = prev["items"].get(k, {}).get("val", 0.0)
            cv = curr["items"].get(k, {}).get("val", 0.0)
            flow = adj.get(k, 0.0)
            if abs(pv) > config.TOL["dust_usd"]:
                anchored_on[code] = meta["source"]
                return (cv - pv - flow) / pv
        return None

    price_fn.anchor_venue = anchored_on.get
    return price_fn


# ── fetched-deposit pool for vanished_venue (from API flows) ─────────────────
def deposits_from_flows(binance_flows, ibkr_flows, coin_price_usd=None,
                        binance_venue="Binance", ibkr_venue="IBKR"):
    """Normalize fetched inbound rows from the API venues into the {amount,...}
    pool checks.vanished_venue matches against — all amounts in USD so they compare
    against a vanished venue's ~$ value. Stablecoins (and fiat rows, already in fiat
    amount) pass through at amount; a non-stable coin amount is the COIN quantity, so
    it must be priced to USD or it can never match a vanished ~$USD venue.

    coin_price_usd(coin_code) -> USD price per unit, or None. Pass
    fetch_binance.price_usd bound to a ticker map (or any price_fn). When a non-stable
    coin can't be priced, the row is kept at the raw coin amount and tagged unpriced —
    a no-match is then a coverage gap to surface, not a silent drop.

    ibkr_flows carries its statement's `fx` map so a non-base-currency row is converted
    the same way; without it a 2,000 EUR deposit is matched as 2,000 USD."""
    pool = []
    for d in (binance_flows or {}).get("deposits", []):
        amt = float(d.get("amount", 0.0))
        coin = (d.get("coin") or "").upper()
        unpriced = False
        if coin and coin not in config.STABLES:
            px = coin_price_usd(coin) if coin_price_usd else None
            if px is not None:
                amt = amt * px
            else:
                unpriced = True
        pool.append({"amount": amt, "coin": coin, "venue": binance_venue,
                     "time": d.get("time"), "source": d.get("source"), "unpriced": unpriced})
    fx = (ibkr_flows or {}).get("fx") or {}
    for r in (ibkr_flows or {}).get("deposits", []):
        ccy = r.get("currency")
        amt = float(r.get("amount", 0.0))
        rate = fx.get(ccy)
        unpriced = rate is None
        if not unpriced:
            amt = amt * rate
        pool.append({"amount": amt, "coin": ccy, "venue": ibkr_venue,
                     "time": r.get("date"), "unpriced": unpriced})
    return pool


def salary_line(config):
    """(payout venue, line name) the salary lands on, or None without a payout venue."""
    venue = checks._venue_by_role(config, "payout")
    name = config.RECURRING.get("salary_to")
    return (venue, name) if venue and name else None


def broker_cash_line(config):
    """(broker venue, its base-currency cash line) external funding lands on, or None."""
    venue = checks._venue_by_role(config, "broker")
    return (venue, f"{config.BASE_CCY} Cash") if venue else None


def prebooked_funding(prev_month, config):
    """USD amounts of payout -> broker-cash moves recorded in the PREVIOUS period's
    transfers file. A funding sent after that statement's last day was booked into
    the broker cash by hand then, so its cashtx row this period is already in the
    book and must not be suggested twice."""
    payout, broker = salary_line(config), broker_cash_line(config)
    path = os.path.join(reconcile.DATA_DIR, f"transfers-{prev_month}.json")
    if not (payout and broker and os.path.exists(path)):
        return []
    with open(path, encoding="utf-8") as fh:
        rows = json.load(fh).get("transfers", [])
    return [float(t["amount"]) for t in rows
            if t.get("type") == "move" and (t.get("from_source"), t.get("from_name")) == payout
            and (t.get("to_source"), t.get("to_name")) == broker]


def _line_val(items, line):
    return next((i["val"] for i in items if (i["source"], i["name"]) == line), 0.0)


def suggest_transfers(period, ibkr, binance, fetched_items, coin_price_usd, config, prebooked=()):
    """Draft rows in the data/transfers-*.json schema from what the channels DO see:
    broker trades and cash movements, exchange external legs, and the rent/salary
    rules. Internal moves no channel reports (copytrading <-> spot, exchange <->
    exchange) are the skill's job from the residual menu, so a draft is never the
    whole book. A broker deposit is booked as a move FROM the payout wallet's salary
    line — the only place external funding comes from (config role "payout") — unless
    `prebooked` (see prebooked_funding) already holds it from the previous period."""
    out = []
    since, until = period
    month = until[:7]
    dust = config.TOL["dust_usd"]
    cat_of = {(i["source"], i["name"]): i["category"] for i in fetched_items}
    payout = salary_line(config)
    prebooked = list(prebooked)
    salary = config.RECURRING.get("salary_usd_approx")
    if payout and salary:  # one salary per snapshot, whatever day it landed
        out.append({"type": "deposit", "amount": float(salary), "category": "usd",
                    "source": payout[0], "name": payout[1], "note": "salary"})
    if ibkr:
        venue, mod, data, since, until = ibkr
        fx = data.get("fx") or {}
        lo, hi = mod._ymd(since), mod._ymd(until)
        net = {}
        for t in mod.in_window(data["trades"], lo, hi):
            rate = fx.get(t["currency"])
            if rate is None:
                continue  # already reported as unconverted by the fetcher
            net[t["symbol"]] = net.get(t["symbol"], 0.0) + t["netCash"] * rate
        cash = {"category": "usd", "source": venue, "name": "USD Cash"}
        for sym, amt in sorted(net.items()):
            if abs(amt) < dust:
                continue
            stock = {"category": cat_of.get((venue, sym), "stocks"), "source": venue, "name": sym}
            src, dst = (cash, stock) if amt < 0 else (stock, cash)
            out.append({"type": "move", "amount": round(abs(amt), 2),
                        "from_category": src["category"], "from_source": src["source"], "from_name": src["name"],
                        "to_category": dst["category"], "to_source": dst["source"], "to_name": dst["name"]})
        for r in mod.in_window(data["deposits_withdrawals"], lo, hi):
            rate = fx.get(r["currency"])
            if rate is None or abs(r["amount"] * rate) < dust:
                continue
            amt = r["amount"] * rate
            note = f"{abs(r['amount']):,.2f} {r['currency']} {r.get('desc') or ''}".strip()
            if amt > 0 and payout:
                # FX drifts between the hand-booked month and the settlement month.
                hit = next((p for p in prebooked if abs(p - amt) <= max(config.TOL["match_usd"], 0.02 * amt)), None)
                if hit is not None:
                    prebooked.remove(hit)
                    print(f"  {venue} funding {amt:,.2f} ({note}) was booked last period as {hit:,.2f} — not suggested again.")
                    continue
                out.append({"type": "move", "amount": round(amt, 2), "note": note,
                            "from_category": "usd", "from_source": payout[0], "from_name": payout[1],
                            "to_category": cash["category"], "to_source": cash["source"], "to_name": cash["name"]})
                continue
            out.append({"type": "deposit" if amt > 0 else "withdraw", "amount": round(abs(amt), 2), **cash,
                        "note": note})
    if binance:
        venue, flows = binance
        flows = flows or {}
        converts = list(flows.get("converts", []))
        usdt = {"category": "usd", "source": venue, "name": "USDT"}

        def usd_value(row):
            coin = (row.get("coin") or row.get("fiatCurrency") or "").upper()
            amt = float(row.get("amount", 0.0))
            if coin in config.STABLES:
                return amt, f"{amt:,.2f} {coin}"
            # A fiat leg is worth the stable amount converted into it, not the fiat face value.
            for c in converts:
                if c["to"] == coin and abs(c["toAmt"] - amt) < 0.01 and c["from"] in config.STABLES:
                    converts.remove(c)
                    return c["fromAmt"], f"{amt:,.2f} {coin} via {c['fromAmt']:,.2f} {c['from']}"
            px = coin_price_usd(coin) if coin_price_usd else None
            if px is None:
                return None, f"{amt} {coin} (unpriced)"
            return amt * px, f"{amt} {coin}"

        for kind, bucket in (("withdraw", "withdrawals"), ("deposit", "deposits")):
            for r in flows.get(bucket, []):
                val, label = usd_value(r)
                if val is None:
                    continue
                if kind == "withdraw" and (r.get("coin") or "").upper() in config.STABLES:  # fiat fee is inside the convert
                    val += float(r.get("fee") or 0.0)
                if val < dust:
                    continue
                out.append({"type": kind, "amount": round(val, 2), **usdt,
                            "note": f"{r.get('source') or ''} {label}".strip()})
    rent = checks.rent_amount(config, month)
    cash_venue = checks._venue_by_role(config, "cash")
    rent_from = config.RECURRING.get("rent_from")
    if rent and cash_venue and rent_from:
        out.append({"type": "withdraw", "amount": rent, "category": "usd", "source": cash_venue,
                    "name": rent_from, "note": "rent"})
    return out


def living_withdraw(prev_items, candidate_items, suggested, manual_keys, config):
    """The payout line's residual rule: prev + salary in - moves out - stated balance =
    living spend (wallet fees included), booked as a withdraw. Only computable when
    the user STATED the balance (--manual); a carried-forward value would book
    salary - moves as spend. Returns (row or None, warning or None)."""
    payout = salary_line(config)
    dust = config.TOL["dust_usd"]
    if not payout or payout not in manual_keys:
        return None, None
    prev_val, curr_val = _line_val(prev_items, payout), _line_val(candidate_items, payout)
    inflow = sum(t["amount"] for t in suggested
                 if t["type"] == "deposit" and (t["source"], t["name"]) == payout)
    outflow = sum(t["amount"] for t in suggested
                  if t["type"] == "move" and (t["from_source"], t["from_name"]) == payout)
    living = round(prev_val + inflow - outflow - curr_val, 2)
    if living < -dust:
        return None, (f"{payout[0]}/{payout[1]}: balance grew by {-living:,.2f} beyond salary - moves "
                      f"({prev_val:,.2f} + {inflow:,.2f} - {outflow:,.2f} -> {curr_val:,.2f}); "
                      "an unrecorded inflow — ask, never book it.")
    if living < dust:
        return None, None
    return {"type": "withdraw", "amount": living, "category": "usd", "source": payout[0],
            "name": payout[1], "note": "living (payout residual rule)"}, None


def parse_amount(spec, fx, config):
    """'AMOUNT[CCY]' -> (usd value, label), non-base currency via the statement fx map."""
    m = re.fullmatch(r"\s*([-+]?[\d,]*\.?\d+)\s*([A-Za-z]{3})?\s*", spec or "")
    if not m:
        raise SystemExit(f"{spec!r}: expected AMOUNT[CCY] (e.g. 3690EUR)")
    amount, ccy = float(m.group(1).replace(",", "")), (m.group(2) or config.BASE_CCY).upper()
    if ccy == config.BASE_CCY:
        return round(amount, 2), f"{amount:,.2f} {ccy}"
    rate = (fx or {}).get(ccy)
    if rate is None:
        raise SystemExit(f"{spec!r}: no {ccy} rate in the broker statement fx map — state it in {config.BASE_CCY}")
    return round(amount * rate, 2), f"{amount:,.2f} {ccy} x {rate}"


def parse_manual(specs, fx, config):
    """--manual 'Source/Name=AMOUNT[CCY]' -> {(source, name): (usd value, label)}.
    A non-base currency is converted with the broker statement's fx map, so a
    payout-wallet EUR balance is stated as read off the screenshot."""
    out = {}
    for spec in specs or []:
        m = re.fullmatch(r"\s*([^/=]+?)\s*/\s*([^=]+?)\s*=\s*([-+]?[\d,]*\.?\d+)\s*([A-Za-z]{3})?\s*", spec)
        if not m:
            raise SystemExit(f"--manual {spec!r}: expected 'Source/Name=AMOUNT[CCY]' (e.g. 'Zen/EUR Cash (Zen)=4295.52EUR')")
        source, name, amount, ccy = m.group(1), m.group(2), float(m.group(3).replace(",", "")), (m.group(4) or config.BASE_CCY).upper()
        if source not in config.VENUES:
            raise SystemExit(f"--manual {spec!r}: unknown venue {source!r} (config.VENUES)")
        if ccy == config.BASE_CCY:
            out[(source, name)] = (round(amount, 2), f"{amount:,.2f} {ccy}")
            continue
        rate = (fx or {}).get(ccy)
        if rate is None:
            raise SystemExit(f"--manual {spec!r}: no {ccy} rate in the broker statement fx map — state it in {config.BASE_CCY}")
        out[(source, name)] = (round(amount * rate, 2), f"{amount:,.2f} {ccy} x {rate}")
    return out


# ── reporting ────────────────────────────────────────────────────────────────
SEV_ORDER = {"critical": 0, "warn": 1, "info": 2}
SEV_TAG = {"critical": "!! CRITICAL", "warn": " ! FLAG    ", "info": "   ok      "}


def print_report(prev, curr, transfers, findings, needed_inputs):
    print(f"\n{'='*78}\nPORTFOLIO SNAPSHOT — {curr['month']}  ({prev['date']} -> {curr['date']})")
    print(f"  portfolio total: {prev['total']:,.0f} -> {curr['total']:,.0f}  ({curr['total']-prev['total']:+,.0f})")

    print("\n  snapshot by category:")
    for cid in sorted(set(prev['by_cat']) | set(curr['by_cat'])):
        pv, cv = prev['by_cat'].get(cid, 0), curr['by_cat'].get(cid, 0)
        print(f"    {cid:8} {pv:>12,.0f} -> {cv:>12,.0f}  ({cv-pv:+,.0f})")

    types = defaultdict(int)
    for t in transfers:
        types[t["type"]] += 1
    print(f"\n  transfers (simplified): {len(transfers)}  {dict(types) if transfers else '(none)'}")
    for t in transfers:
        if t["type"] == "move":
            print(f"    move     {t['amount']:>10,.2f}  {t['from_category']}/{t['from_source']}/{t['from_name']}"
                  f"  ->  {t['to_category']}/{t['to_source']}/{t['to_name']}")
        else:
            print(f"    {t['type']:8} {t['amount']:>10,.2f}  {t['category']}/{t['source']}/{t['name']}")

    crit = [f for f in findings if f["severity"] == "critical"]
    warn = [f for f in findings if f["severity"] == "warn"]
    info = [f for f in findings if f["severity"] == "info"]
    print(f"\n  FLAGS  (critical {len(crit)} / warn {len(warn)} / info {len(info)}):")
    for f in sorted(findings, key=lambda x: SEV_ORDER[x["severity"]]):
        print(f"    [{SEV_TAG[f['severity']]}] {f['check']:16} {f['message']}")

    if needed_inputs:
        print("\n  STILL NEEDED FROM USER (no API / no source):")
        for n in needed_inputs:
            print(f"    - {n}")

    if crit:
        print(f"\n  >>> {len(crit)} CRITICAL gate(s) failed — do NOT write the snapshot until resolved.")
    else:
        print("\n  >>> no critical gates; review the warn flags, then confirm to write.")


def needed_inputs_from_config():
    """What the orchestrator must ask the user for: screenshot venues, no-API
    sleeves, and manual (cash) lines — read straight from config.VENUES."""
    out = []
    for venue, spec in config.VENUES.items():
        if spec.get("method") == "screenshot":
            note = f"  ({spec['note']})" if spec.get("note") else ""
            out.append(f"{venue}: paste balance screenshot{note}")
        elif spec.get("method") == "manual":
            note = f"  ({spec['note']})" if spec.get("note") else ""
            out.append(f"{venue}: state value via --manual '{venue}/<line>=<amount><CCY>'{note}")
        for sleeve in spec.get("screenshot_sleeves", []):
            out.append(f"{venue} {sleeve}: no-API sleeve — paste screenshot total")
    return out


# ── pipeline cores ───────────────────────────────────────────────────────────
def run_validate(out_path=None):
    """Self-test: run the full pipeline against the committed golden in data/."""
    snaps = reconcile.load_snapshots()
    if len(snaps) < 2:
        raise SystemExit("need >=2 committed snapshots in data/ for --validate")
    curr, prev = snaps[-1], snaps[-2]
    groups = reconcile.load_groups()
    raw = reconcile.transfers_for_period(groups, prev["date"], curr["date"])

    # mandatory final pipeline step: collapse to NET flows before matching checks
    transfers = simplify_transfers.simplify(raw)
    ok, lines = simplify_transfers.reconcile(raw, transfers)
    if not ok:
        print("  !! simplify changed net flows:")
        for ln in lines:
            print(ln)

    today = datetime.date.today().isoformat()
    target_filename = f"{curr['month']}.json"

    # Synthetic manifests for the offline self-test: assert the committed data was
    # "queried" across the full period so coverage_gate is exercised and PASSES on
    # the golden (a real run gets these from the fetchers). Required channels come
    # from config.VENUES, not a hard-coded list, so the self-test tracks config.
    period = {"prev_date": prev["date"], "curr_date": curr["date"]}
    full_window = f"{prev['date'].replace('-','')}..{curr['date'].replace('-','')}"
    manifests = {
        v: {c: {"queried": True, "window": full_window, "rows": 0}
            for c in spec.get("required_channels", [])}
        for v, spec in config.VENUES.items() if spec.get("method") == "api"
    }

    price_fn = make_residual_anchor_price_fn(prev, curr, transfers, config)

    ctx = {
        "manifests": manifests,
        "period": period,
        "target_filename": target_filename,
        "meta_date": curr["date"],
        "today": today,
        "prior_snapshot_dates": [s["date"] for s in snaps[:-1]],
        "transfers": transfers,
        "raw_transfers": raw,
        "prev_items": _items_map(prev),
        "cur_items": _items_map(curr),
        "price_fn": price_fn,
        "anchor_venue": price_fn.anchor_venue,  # r comes from this venue's own residual, not a price feed
        "deposits": [],  # offline: no fetched deposit pool; vanished_venue reports 0 candidates
        "category_residuals": category_residuals(prev, curr, transfers),
        "stable_deltas": stable_deltas(prev, curr),
        "usd_total": curr["by_cat"].get(reconcile.STABLE_CAT, 0.0),
        "prev_usd_total": prev["by_cat"].get(reconcile.STABLE_CAT, 0.0),
        "snapshot_items": [{"category": m["cat"], "source": m["source"], "name": m["name"], "val": m["val"]}
                           for m in curr["items"].values()],
        "flex_nav": None,  # offline: no Flex pull -> ibkr_nav skips (info)
    }
    findings = checks.run_all(ctx, config)

    print_report(prev, curr, transfers, findings, needed_inputs_from_config())

    if out_path:
        draft = {"meta": {"date": curr["date"], "validate": True}, "transfers": transfers,
                 "findings": findings}
        os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
        with open(out_path, "w") as fh:
            json.dump(draft, fh, ensure_ascii=False, indent=2)
        print(f"\n  wrote draft {out_path}")

    return findings


def run_live(out_path=None, as_of=None, manual=None, funded=None):
    """Live pipeline: iterate config.VENUES api venues, run their fetchers, assemble
    the candidate snapshot, reconcile vs previous month, run checks. Requires keys
    in tools/.env; this path makes signed calls and is NOT exercised in --validate.

    as_of dates the snapshot at the LAST SETTLED day instead of today. Broker
    statements only cover through a prior business day, so on any run day the flow
    window ends before today and coverage_gate correctly refuses to reconcile a
    period the statement doesn't span; the fix is to move the period end back to
    what the statement actually covers, never to loosen the gate. What CANNOT be
    back-dated (exchange wallet quantities, the live ticker map, carried-forward
    screenshot rows) stays a live-now value, so a back-dated run emits a warn
    finding saying exactly that.
    """
    import importlib

    snaps = reconcile.load_snapshots()
    prev = snaps[-1] if snaps else None  # previous = latest committed (prior-month) snapshot
    if prev is None:
        raise SystemExit("no previous snapshot in data/ to compare against")

    real_today = datetime.date.today().isoformat()
    today = as_of or real_today
    since = prev["date"]
    if as_of and as_of <= since:
        raise SystemExit(
            f"--as-of {as_of} is not after the previous snapshot {since}: the period ({since}, {as_of}] "
            f"is empty or inverted, so every flow window comes back empty, the run targets the very "
            f"file it compares against, and the result reads as a clean month. Re-run once the "
            f"statement covers a day past {since}.")

    extra_findings = []
    fetched_items = []
    manifests = {}
    binance_flows = ibkr_flows = None
    flex_nav = None
    coin_price_usd = None  # bound to Binance's live ticker map once it's fetched
    source_windows = {}    # venue -> the window its SOURCE covers (drives the auto as-of)
    ibkr_ctx = binance_ctx = None

    # PASS 1 — balances. Dispatch on the configured TOOL, never on the venue's display
    # name: config.example.py ships placeholder names, so a renamed venue would fall
    # through every branch and yield a snapshot built entirely from carried values.
    for venue, spec in config.VENUES.items():
        if spec.get("method") != "api":
            continue
        tool = spec.get("tool")
        mod = importlib.import_module(tool)
        mod.load_env()
        if tool == "fetch_ibkr":
            token, qid = os.environ.get("IBKR_FLEX_TOKEN"), os.environ.get("IBKR_FLEX_QUERY_ID")
            if not token or not qid:
                raise SystemExit("set IBKR_FLEX_TOKEN / IBKR_FLEX_QUERY_ID in tools/.env")
            data = mod.parse(mod.flex_fetch(token, qid))
            fetched_items += mod.to_snapshot_items(data, venue)
            flex_nav = data.get("nav")
            ibkr_ctx = (venue, mod, data)
            m = data.get("meta") or {}
            source_windows[venue] = f"{mod._ymd(m.get('fromDate'))}..{mod._ymd(m.get('toDate'))}"
        elif tool == "fetch_binance":
            key, secret = os.environ.get("BINANCE_API_KEY"), os.environ.get("BINANCE_API_SECRET")
            if not key or not secret:
                raise SystemExit("set BINANCE_API_KEY / BINANCE_API_SECRET in tools/.env")
            px = {d["symbol"]: float(d["price"]) for d in mod.public_get("/api/v3/ticker/price")}
            coin_price_usd = lambda code, _px=px, _m=mod: _m.price_usd(code, _px)
            qty, wallets, balance_manifest = mod.collect(key, secret)
            unpriced = []
            for asset, q in qty.items():
                if asset.startswith("LD") and asset[2:] in qty:
                    continue
                p = mod.price_usd(asset, px)
                if p is None:
                    unpriced.append({"asset": asset, "qty": q})
                    continue
                val = round(q * p, 2)
                if val < config.TOL["dust_usd"]:
                    continue
                cat, name = mod.classify(asset)
                fetched_items.append({"category": cat, "source": venue, "name": name, "val": val})
            if unpriced:
                # Dropping these silently turns the whole position into a market loss;
                # they stay out of the total, but the operator is told they exist.
                extra_findings.append(checks._finding(
                    "unpriced_asset", "warn",
                    f"{venue}: {len(unpriced)} held asset(s) have no USD/BTC/ETH pair and are NOT in the "
                    f"snapshot total — value them manually or their value reads as a market loss: "
                    + ", ".join(f"{u['asset']} {u['qty']:g}" for u in unpriced),
                    {"venue": venue, "unpriced": unpriced}))
            manifests[venue] = dict(balance_manifest)
            binance_ctx = (venue, mod, key, secret)
        else:
            raise SystemExit(f"{venue}: config.VENUES tool {tool!r} has no adapter in the orchestrator")

    if as_of is None:
        # Broker statements are EOD: on any run day their coverage ends before today,
        # and dating the snapshot past it can only fail coverage_gate. Auto-date at the
        # earliest source window end instead — but only a few days back; a very stale
        # statement must fail the gate loudly, never silently back-date.
        ends = []
        for venue, window in source_windows.items():
            span = checks._window_span(window)
            if span and span[1]:
                ends.append(span[1])
        if ends:
            earliest = min(ends)
            e_date = f"{earliest[:4]}-{earliest[4:6]}-{earliest[6:8]}"
            lag = (datetime.date.fromisoformat(today) - datetime.date.fromisoformat(e_date)).days
            if e_date <= since:
                # Back-dating here would invert the period and silently reconcile a
                # negative span; the gate below must fail on the real dates instead.
                extra_findings.append(checks._finding(
                    "as_of", "critical",
                    f"earliest source coverage ends {e_date}, on or before the previous snapshot "
                    f"{since} — the statement covers no part of this period. NOT back-dating; "
                    f"re-pull once it reaches past {since}.",
                    {"coverage_end": e_date, "prev_date": since, "source_windows": source_windows}))
            elif 0 < lag <= 4:
                print(f"  statement coverage ends {e_date} — dating the snapshot there (auto as-of; was {today}).")
                today = e_date

    if today != real_today:
        extra_findings.append(checks._finding(
            "as_of", "warn",
            f"snapshot dated {today} but built on {real_today}: every value that cannot be queried "
            f"historically (exchange wallet quantities, the ticker price map, carried-forward "
            f"screenshot/cash rows) is a LIVE-NOW figure labelled with the as-of date. Anything that "
            f"moved in between lands in the wrong period — replace those rows with as-of values.",
            {"meta_date": today, "run_date": real_today}))

    period = {"prev_date": since, "curr_date": today}

    # PASS 2 — flows, now that the period END is settled. A window running past the
    # snapshot date pulls the NEXT period's transfers into this one.
    if ibkr_ctx:
        venue, mod, data = ibkr_ctx
        # REAL manifest from the parsed statement (window = its own reporting period,
        # rows = actual section lengths, queried = the section's presence) — not a
        # fabricated full-span assertion, so a missing section or a too-short statement
        # period fails coverage_gate.
        manifests[venue] = mod.flows_manifest(data, since, today)
        deps = mod.in_window(data["deposits_withdrawals"], mod._ymd(since), mod._ymd(today))
        ibkr_flows = {"deposits": [r for r in deps if r["amount"] > 0], "fx": data.get("fx") or {}}
    if binance_ctx:
        venue, mod, key, secret = binance_ctx
        # (since, until] — the period is OPEN on the left: `since` is the previous
        # snapshot date and its flows are already inside that snapshot's balances.
        start = int((datetime.datetime.strptime(since, "%Y-%m-%d")
                     .replace(tzinfo=datetime.timezone.utc)
                     + datetime.timedelta(days=1)).timestamp() * 1000)
        end = int(datetime.datetime.strptime(today, "%Y-%m-%d")
                  .replace(tzinfo=datetime.timezone.utc).timestamp() * 1000) + 86_400_000 - 1
        binance_flows = mod.flows(key, secret, start, end)
        # Flow channels (required, period-windowed) + balance channels (spot/funding/
        # earn — no window) in one manifest so a silently-failed Earn/Funding pull is
        # surfaced by coverage_gate instead of silently understating the snapshot.
        manifests[venue] = {**binance_flows.get("manifest", {}), **manifests.get(venue, {})}

    unmanifested = [v for v, s in config.VENUES.items()
                    if s.get("method") == "api" and not manifests.get(v)]
    if unmanifested:
        raise SystemExit(f"api venue(s) produced no coverage manifest: {', '.join(unmanifested)} — "
                         "nothing was fetched for them, so the snapshot would be carried-forward "
                         "placeholders that coverage_gate cannot even see.")

    # Coverage runs first on the fetched manifests so a missing required channel
    # halts before any screenshot/manual assembly work.
    cov = checks.coverage_gate(manifests, period, config)
    if any(f["severity"] == "critical" for f in cov):
        print("  live fetch complete, but coverage gate FAILED — do not assemble on incomplete flows:")
        for f in cov + extra_findings:
            print(f"    [{SEV_TAG[f['severity']]}] {f['check']:16} {f['message']}")
        return cov + extra_findings

    # Assemble the candidate snapshot: fetched API rows + the non-API (screenshot /
    # manual) venues. A pure-code run cannot OCR screenshots or read physical cash;
    # those are carried forward from the previous snapshot as a PLACEHOLDER so the
    # full check layer below runs on a complete-shaped candidate. The skill replaces
    # these carried rows with the user's real screenshot/cash values before writing.
    needed = needed_inputs_from_config()
    api_venues = {v for v, s in config.VENUES.items() if s.get("method") == "api"}
    candidate_items = list(fetched_items)
    cash_venue = checks._venue_by_role(config, "cash")
    rent_from = config.RECURRING.get("rent_from")
    rent = checks.rent_amount(config, today[:7])
    fx_map = ibkr_ctx[2].get("fx") if ibkr_ctx else None
    stated = parse_manual(manual, fx_map, config)
    manual_keys = set(stated)
    prev_rows = [{"source": m["source"], "name": m["name"], "val": m["val"]} for m in prev["items"].values()]
    suggested = suggest_transfers(
        (since, today), ibkr_ctx and (ibkr_ctx[0], ibkr_ctx[1], ibkr_ctx[2], since, today),
        binance_ctx and (binance_ctx[0], binance_flows), fetched_items, coin_price_usd, config,
        prebooked_funding(prev["date"][:7], config))
    if funded:
        # Money the payout wallet already sent to the broker but the EOD statement has
        # not settled: it belongs to the broker cash NOW, and next period's cashtx row
        # is skipped via prebooked_funding.
        payout, broker = salary_line(config), broker_cash_line(config)
        if not (payout and broker):
            raise SystemExit("--funded needs a payout-role and a broker-role venue in config.VENUES")
        val, label = parse_amount(funded, fx_map, config)
        line = next((i for i in candidate_items if (i["source"], i["name"]) == broker), None)
        if line is None:
            raise SystemExit(f"--funded: no fetched {broker[0]}/{broker[1]} line to add to")
        line["val"] = round(line["val"] + val, 2)
        if flex_nav is not None:
            flex_nav = round(flex_nav + val, 2)  # the NAV identity must include it too
        print(f"  {broker[0]}/{broker[1]}: + {label} = {val:,.2f} sent from {payout[0]}, not in the statement yet (--funded)")
        suggested.append({"type": "move", "amount": val, "note": f"{label} sent, settles in the next statement",
                          "from_category": "usd", "from_source": payout[0], "from_name": payout[1],
                          "to_category": "usd", "to_source": broker[0], "to_name": broker[1]})
    for k, meta in prev["items"].items():
        spec = config.VENUES.get(meta["source"], {})
        # An api venue's no-API sleeve (e.g. copytrading) is never in fetched_items,
        # so it must be carried forward too or the sleeve silently drops to zero.
        sleeve = (meta["cat"] == reconcile.COPY_CAT and spec.get("screenshot_sleeves"))
        if meta["source"] in api_venues and not sleeve:
            continue
        val = meta["val"]
        if (meta["source"], meta["name"]) in stated:
            val, label = stated.pop((meta["source"], meta["name"]))
            print(f"  {meta['source']}/{meta['name']}: {label} = {val:,.2f} (--manual)")
        elif meta["source"] == cash_venue and meta["name"] == rent_from and rent:
            val = round(val - rent, 2)
            print(f"  {rent_from}: {meta['val']:,.0f} - rent {rent:,.0f} = {val:,.0f} "
                  f"(config.RECURRING; say so if the cash differs).")
        candidate_items.append({"category": meta["cat"], "source": meta["source"],
                                "name": meta["name"], "val": val})
    if stated:
        raise SystemExit("--manual names line(s) absent from the previous snapshot: "
                         + ", ".join(f"{s}/{n}" for s, n in stated)
                         + " — a NEW line is added by hand in the snapshot file, not by override.")

    curr = _snapshot_from_items(candidate_items, today, curr_month=today[:7])

    groups = reconcile.load_groups()
    raw = reconcile.transfers_for_period(groups, prev["date"], curr["date"])
    transfers = simplify_transfers.simplify(raw)
    # The CLI path aborts here; the live path must not quietly hand a draft to the
    # skill with a net flow that no longer matches the recorded book.
    sok, slines = simplify_transfers.reconcile(raw, transfers)
    if not sok:
        print("  !! simplify changed net flows:")
        for ln in slines:
            print(ln)
        extra_findings.append(checks._finding(
            "simplify_reconcile", "critical",
            "simplify_transfers changed a net flow — the simplified set no longer matches the "
            "recorded book; do NOT write this snapshot.",
            {"mismatches": slines}))

    deposits = deposits_from_flows(binance_flows, ibkr_flows, coin_price_usd,
                                   binance_venue=binance_ctx[0] if binance_ctx else "Binance",
                                   ibkr_venue=ibkr_ctx[0] if ibkr_ctx else "IBKR")
    price_fn = make_klines_price_fn(prev["date"], curr["date"])

    ctx = {
        "manifests": manifests,
        "period": period,
        "target_filename": f"{today[:7]}.json",
        "meta_date": today,
        "today": real_today,  # the WALL CLOCK, so --as-of can't disarm the future-date gate
        "prior_snapshot_dates": [s["date"] for s in snaps],
        "transfers": transfers,
        "raw_transfers": raw,
        "prev_items": _items_map(prev),
        "cur_items": _items_map(curr),
        "price_fn": price_fn,
        "deposits": deposits,
        "category_residuals": category_residuals(prev, curr, transfers),
        "stable_deltas": stable_deltas(prev, curr),
        "usd_total": curr["by_cat"].get(reconcile.STABLE_CAT, 0.0),
        "prev_usd_total": prev["by_cat"].get(reconcile.STABLE_CAT, 0.0),
        "snapshot_items": candidate_items,
        "flex_nav": flex_nav,
    }
    findings = checks.run_all(ctx, config) + extra_findings

    print_report(prev, curr, transfers, findings, needed)
    print("\n  NOTE: non-API venues above are carried-forward placeholders; the skill "
          "must replace them with the user's screenshot/cash values before writing.")

    living, living_warn = living_withdraw(prev_rows, candidate_items, suggested, manual_keys, config)
    if living:
        suggested.append(living)
    if living_warn:
        print(f"\n  ! {living_warn}")
    elif salary_line(config) and salary_line(config) not in manual_keys:
        print(f"\n  {'/'.join(salary_line(config))}: carried forward — pass --manual to book living spend.")
    print("\n  SUGGESTED TRANSFERS (from fetched flows + rent/salary rules; internal/no-API moves are "
          "NOT here — add those from the residual menu):")
    for t in suggested:
        if t["type"] == "move":
            print(f"    move     {t['amount']:>10,.2f}  {t['from_source']}/{t['from_name']} -> {t['to_source']}/{t['to_name']}")
        else:
            print(f"    {t['type']:8} {t['amount']:>10,.2f}  {t['source']}/{t['name']}  {t.get('note', '')}")
    if not suggested:
        print("    (none)")

    if out_path:
        draft = {"meta": {"date": today}, "fetched_items": fetched_items,
                 "candidate_items": candidate_items, "manifests": manifests,
                 "transfers": transfers, "suggested_transfers": suggested, "findings": findings}
        os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
        with open(out_path, "w") as fh:
            json.dump(draft, fh, ensure_ascii=False, indent=2)
        print(f"\n  wrote draft {out_path}")
    return findings


def main():
    ap = argparse.ArgumentParser(description="Portfolio snapshot pipeline: fetch -> gate -> reconcile -> check -> flag.")
    ap.add_argument("--validate", action="store_true",
                    help="offline self-test against the committed golden in data/ (no live signed calls)")
    ap.add_argument("--out", help="write an optional draft (transfers + findings); NEVER touches data/")
    ap.add_argument("--as-of", dest="as_of",
                    help="date the snapshot at this last-settled day (YYYY-MM-DD) instead of today, "
                         "for when the broker statement doesn't cover today yet")
    ap.add_argument("--manual", action="append", metavar="Source/Name=AMOUNT[CCY]",
                    help="stated balance of a manual/screenshot line, e.g. 'Zen/EUR Cash (Zen)=4295.52EUR' "
                         "(non-base currency converted with the broker statement fx); repeatable")
    ap.add_argument("--funded", metavar="AMOUNT[CCY]",
                    help="payout-wallet money already sent to the broker but not in its EOD statement yet "
                         "(e.g. 3690EUR): added to the broker cash line and booked as the move now")
    args = ap.parse_args()

    if args.validate:
        run_validate(args.out)
    else:
        run_live(args.out, as_of=args.as_of, manual=args.manual, funded=args.funded)


if __name__ == "__main__":
    main()
