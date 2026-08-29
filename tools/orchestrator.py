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
                return (cv - pv - flow) / pv
        return None

    return price_fn


# ── fetched-deposit pool for vanished_venue (from API flows) ─────────────────
def deposits_from_flows(binance_flows, ibkr_flows, coin_price_usd=None):
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
        pool.append({"amount": amt, "coin": coin, "venue": "Binance",
                     "time": d.get("time"), "source": d.get("source"), "unpriced": unpriced})
    fx = (ibkr_flows or {}).get("fx") or {}
    for r in (ibkr_flows or {}).get("deposits", []):
        ccy = r.get("currency")
        amt = float(r.get("amount", 0.0))
        rate = fx.get(ccy)
        unpriced = rate is None
        if not unpriced:
            amt = amt * rate
        pool.append({"amount": amt, "coin": ccy, "venue": "IBKR",
                     "time": r.get("date"), "unpriced": unpriced})
    return pool


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
            out.append(f"{venue}: state value{note}")
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


def run_live(out_path=None, as_of=None):
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
            fetched_items += mod.to_snapshot_items(data)
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
            if 0 < lag <= 4:
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
        start = int(datetime.datetime.strptime(since, "%Y-%m-%d")
                    .replace(tzinfo=datetime.timezone.utc).timestamp() * 1000)
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
        for f in cov:
            print(f"    [{SEV_TAG[f['severity']]}] {f['check']:16} {f['message']}")
        return cov

    # Assemble the candidate snapshot: fetched API rows + the non-API (screenshot /
    # manual) venues. A pure-code run cannot OCR screenshots or read physical cash;
    # those are carried forward from the previous snapshot as a PLACEHOLDER so the
    # full check layer below runs on a complete-shaped candidate. The skill replaces
    # these carried rows with the user's real screenshot/cash values before writing.
    needed = needed_inputs_from_config()
    api_venues = {v for v, s in config.VENUES.items() if s.get("method") == "api"}
    candidate_items = list(fetched_items)
    for k, meta in prev["items"].items():
        spec = config.VENUES.get(meta["source"], {})
        # An api venue's no-API sleeve (e.g. copytrading) is never in fetched_items,
        # so it must be carried forward too or the sleeve silently drops to zero.
        sleeve = (meta["cat"] == reconcile.COPY_CAT and spec.get("screenshot_sleeves"))
        if meta["source"] in api_venues and not sleeve:
            continue
        candidate_items.append({"category": meta["cat"], "source": meta["source"],
                                "name": meta["name"], "val": meta["val"]})

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

    deposits = deposits_from_flows(binance_flows, ibkr_flows, coin_price_usd)
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

    if out_path:
        draft = {"meta": {"date": today}, "fetched_items": fetched_items,
                 "candidate_items": candidate_items, "manifests": manifests,
                 "transfers": transfers, "findings": findings}
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
    args = ap.parse_args()

    if args.validate:
        run_validate(args.out)
    else:
        run_live(args.out, as_of=args.as_of)


if __name__ == "__main__":
    main()
