#!/usr/bin/env python3
"""
simplify_transfers.py — collapse a raw reconstructed transfer list to the minimal,
meaningful set, matching the user's historical granularity.

The app's per-asset and per-category yields divide by startBalance + GROSS inflow,
so what may be collapsed is bounded by which grosses feed a published number:
  - external deposits and withdrawals net SEPARATELY per asset (same-signed rows
    only), so an asset's gross deposit total survives,
  - a move CROSSING a category boundary is merged per asset pair but never netted
    against the opposite direction: 1000 out of crypto and 800 back in is not a
    net 200 inflow — netting it would shrink crypto's yield denominator by 800,
  - only moves with BOTH ends inside ONE bucket are reduced, because no published
    view can see them at all: their nets are paired net-source against net-sink, so
    multi-hop chains collapse and same-bucket round-trips cancel.

A BUCKET is the finest thing the app scores: a category, and inside stocks a
sub-bucket (companies / ETF us / europe / asia — config.stock_sub_bucket mirrors the
app's classifyStockItem). A stocks move ACROSS two sub-buckets is a real withdraw and
a real deposit in the app's per-sub-bucket yields, so it is kept like a cross-category
leg. Pairing at category granularity re-paired AAPL->VOO plus VTI->MSFT into two
intra-sub-bucket legs, and companies and ETF-us each silently lost 1000 of gross in
AND out of their yield denominators.

Same-bucket movers pair DIRECTLY (largest source against largest sink), not through
the bucket's biggest key: routing A->hub->B hands the hub a withdraw AND a deposit of
its own that never happened, inflating the gross denominator of its sub-bucket — the
exact distortion this step exists to avoid. Cross-bucket legs keep their real
endpoints, so no synthetic pair (AAPL->BTC) is ever invented.

Invariants, enforced by reconcile() before anything is written: per-asset and
per-category NET unchanged; per-category and per-stocks-sub-bucket GROSS in/out
across the boundary unchanged; and no asset gains gross flow it did not have in the
raw set.

Net flows are preserved to the cent: nothing carrying real net is dropped (the old
dust dead-band silently lost net in the (dust/2, dust) gap). Only sub-cent rounding
residue is discarded.

Usage:  python3 tools/simplify_transfers.py raw.json [-o out.json]
        python3 tools/simplify_transfers.py --demo
"""
import argparse
import json
import os
import sys
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import config  # noqa: E402

# Amounts are carried at cent precision; anything below this is rounding residue,
# not a real flow. Dropping it cannot change any net at 2-decimal precision.
EPS = 0.005


_warned_no_sub = False


def _sub_bucket_fn():
    """config.stock_sub_bucket, or None on a config.py predating the STOCKS SUB-BUCKETS
    block — config.py is a one-time copy of the template, so it does not gain new
    entries. Callers fall back to category granularity instead of dying."""
    global _warned_no_sub
    fn = getattr(config, "stock_sub_bucket", None)
    if fn is None and not _warned_no_sub:
        _warned_no_sub = True
        print("  !! WARN: config.py lacks stock_sub_bucket/ETF_REGION — copy the STOCKS "
              "SUB-BUCKETS block from config.example.py; stocks sub-bucket yields may be "
              "distorted until then")
    return fn


def _bucket(key):
    """The finest bucket the app scores this key in: its category, and inside stocks
    its sub-bucket. Pairing coarser than this moves a published yield denominator."""
    cat, _source, name = key
    sub = _sub_bucket_fn()
    return sub(name) if cat == "stocks" and sub else cat


def simplify(transfers):
    ext_in = defaultdict(float)    # gross external deposits per key
    ext_out = defaultdict(float)   # gross external withdrawals per key
    cross = defaultdict(float)     # gross per (from_key, to_key) across a bucket
    intra = defaultdict(float)     # same-bucket move net per key (+in / -out)

    for t in transfers:
        a = float(t["amount"])
        if t["type"] == "deposit":
            ext_in[(t["category"], t["source"], t["name"])] += a
        elif t["type"] == "withdraw":
            ext_out[(t["category"], t["source"], t["name"])] += a
        elif t["type"] == "move":
            fk = (t["from_category"], t["from_source"], t["from_name"])
            tk = (t["to_category"], t["to_source"], t["to_name"])
            if fk[0] == tk[0] and _bucket(fk) == _bucket(tk):
                intra[fk] -= a
                intra[tk] += a
            else:
                cross[(fk, tk)] += a

    out = []
    # A key with real flow on BOTH sides emits BOTH legs: netting them would shrink
    # the gross deposit total the app's yield denominator is built from.
    for k in list(ext_in) + [k for k in ext_out if k not in ext_in]:
        cat, src, nm = k
        for typ, v in (("deposit", ext_in.get(k, 0.0)), ("withdraw", ext_out.get(k, 0.0))):
            if v >= EPS:
                out.append({"type": typ, "amount": round(v, 2),
                            "category": cat, "source": src, "name": nm})

    def emit(fk, tk, amt):
        out.append({"type": "move", "amount": round(amt, 2),
                    "from_category": fk[0], "from_source": fk[1], "from_name": fk[2],
                    "to_category": tk[0], "to_source": tk[1], "to_name": tk[2]})

    for (fk, tk), amt in sorted(cross.items()):
        if amt >= EPS:
            emit(fk, tk, amt)

    # A bucket's own nets sum to zero, so its net sources fund its net sinks exactly —
    # no hub and no remainder to route anywhere. Pairing stays INSIDE the bucket, so a
    # leg between two sub-buckets is never invented, and one within a sub-bucket is
    # emitted only where the raw set already moved money inside it.
    by_bucket = defaultdict(dict)
    for k, v in intra.items():
        if abs(v) >= EPS:
            by_bucket[(k[0], _bucket(k))][k] = v
    for bucket in sorted(by_bucket):
        ks = by_bucket[bucket]
        srcs = sorted(((k, -v) for k, v in ks.items() if v < 0), key=lambda x: (-x[1], x[0]))
        sinks = sorted(((k, v) for k, v in ks.items() if v > 0), key=lambda x: (-x[1], x[0]))
        i = j = 0
        while i < len(srcs) and j < len(sinks):
            (sk, sv), (dk, dv) = srcs[i], sinks[j]
            amt = min(sv, dv)
            emit(sk, dk, amt)
            srcs[i], sinks[j] = (sk, sv - amt), (dk, dv - amt)
            if srcs[i][1] < EPS:
                i += 1
            if sinks[j][1] < EPS:
                j += 1
    return out


# Synthetic fixture (NO real data). Exercises every reduction path and every
# regression the invariants exist to catch:
#   - dust: the 0.001 move is below EPS and is discarded
#   - cross-category gross survives netting: BTC->Cash 1000 with Cash->ETH 800 must
#     stay two legs, not a net 200 into crypto
#   - same-category movers pair directly: stocks A->B, never A->C->B through the
#     category's largest key C
#   - two cross-sub-bucket stock rebalances (AAPL->VOO, VTI->MSFT) must NOT be
#     re-paired into AAPL->MSFT + VTI->VOO: companies and ETF-us each keep 1000 of
#     gross in and out
DEMO = [
    {"type": "move", "amount": 1000, "from_category": "stocks", "from_source": "VenueX", "from_name": "AAA", "to_category": "usd", "to_source": "VenueX", "to_name": "Cash"},
    {"type": "deposit", "amount": 1000.00, "category": "usd", "source": "VenueY", "name": "Cash"},
    {"type": "move", "amount": 500, "from_category": "usd", "from_source": "VenueX", "from_name": "Cash", "to_category": "crypto", "to_source": "VenueY", "to_name": "BBB"},
    {"type": "move", "amount": 300, "from_category": "usd", "from_source": "VenueY", "from_name": "Cash", "to_category": "safe", "to_source": "VenueY", "to_name": "CCC"},
    {"type": "move", "amount": 300, "from_category": "safe", "from_source": "VenueY", "from_name": "CCC", "to_category": "usd", "to_source": "VenueY", "to_name": "Cash"},
    {"type": "move", "amount": 500, "from_category": "copy", "from_source": "VenueX", "from_name": "Copy", "to_category": "usd", "to_source": "VenueY", "to_name": "Cash"},
    {"type": "withdraw", "amount": 500, "category": "usd", "source": "Cash", "name": "Cash"},
    {"type": "move", "amount": 0.001, "from_category": "usd", "from_source": "VenueY", "from_name": "Cash", "to_category": "crypto", "to_source": "VenueY", "to_name": "BBB"},
    {"type": "move", "amount": 1000, "from_category": "crypto", "from_source": "VenueY", "from_name": "BTC", "to_category": "usd", "to_source": "VenueY", "to_name": "Cash"},
    {"type": "move", "amount": 800, "from_category": "usd", "from_source": "VenueY", "from_name": "Cash", "to_category": "crypto", "to_source": "VenueY", "to_name": "ETH"},
    {"type": "move", "amount": 1000, "from_category": "stocks", "from_source": "VenueX", "from_name": "A", "to_category": "stocks", "to_source": "VenueX", "to_name": "B"},
    {"type": "move", "amount": 3000, "from_category": "usd", "from_source": "VenueY", "from_name": "Cash", "to_category": "stocks", "to_source": "VenueX", "to_name": "C"},
    {"type": "move", "amount": 1000, "from_category": "stocks", "from_source": "VenueX", "from_name": "AAPL", "to_category": "stocks", "to_source": "VenueX", "to_name": "VOO"},
    {"type": "move", "amount": 1000, "from_category": "stocks", "from_source": "VenueX", "from_name": "VTI", "to_category": "stocks", "to_source": "VenueX", "to_name": "MSFT"},
]


def _net_by_asset(transfers):
    """Net flow per asset key, matching the app's getAssetKey semantics:
    deposit +a, withdraw -a, move -a on from-key and +a on to-key."""
    net = defaultdict(float)
    for t in transfers:
        a = float(t["amount"])
        if t["type"] == "deposit":
            net[(t["category"], t["source"], t["name"])] += a
        elif t["type"] == "withdraw":
            net[(t["category"], t["source"], t["name"])] -= a
        elif t["type"] == "move":
            net[(t["from_category"], t["from_source"], t["from_name"])] -= a
            net[(t["to_category"], t["to_source"], t["to_name"])] += a
    return net


def _net_by_category(transfers):
    """Net flow per category, matching buildCategoryTransfers: a move is a
    withdraw on from_category and a deposit on to_category."""
    net = defaultdict(float)
    for t in transfers:
        a = float(t["amount"])
        if t["type"] == "deposit":
            net[t["category"]] += a
        elif t["type"] == "withdraw":
            net[t["category"]] -= a
        elif t["type"] == "move":
            net[t["from_category"]] -= a
            net[t["to_category"]] += a
    return net


def _gross_by_category(transfers):
    """Gross inflow/outflow per category ACROSS its boundary, matching
    buildCategoryTransfers: a same-category move is invisible to the category view,
    a cross-category one is a withdraw on from_category and a deposit on to_category.
    Returns (gross_in, gross_out)."""
    gin, gout = defaultdict(float), defaultdict(float)
    for t in transfers:
        a = float(t["amount"])
        if t["type"] == "deposit":
            gin[t["category"]] += a
        elif t["type"] == "withdraw":
            gout[t["category"]] += a
        elif t["type"] == "move" and t["from_category"] != t["to_category"]:
            gout[t["from_category"]] += a
            gin[t["to_category"]] += a
    return gin, gout


def _gross_by_stock_sub(transfers):
    """Gross inflow/outflow per stocks sub-bucket, matching buildStockSubTransfersAll:
    a move with both legs in ONE sub-bucket is invisible there, a move between two of
    them is a withdraw on one and a deposit on the other (and, when it is not
    ETF-to-ETF, on the stocks_etf aggregate the app also renders).
    Returns (gross_in, gross_out) keyed by sub-bucket."""
    gin, gout = defaultdict(float), defaultdict(float)
    classify = _sub_bucket_fn()
    if classify is None:
        return gin, gout  # no sub-buckets to check: _bucket pairs at category granularity

    def add(side, sub, a, aggregate):
        side[sub] += a
        if aggregate and sub != "companies":
            side["etf"] += a

    for t in transfers:
        a = float(t["amount"])
        if t["type"] in ("deposit", "withdraw") and t["category"] == "stocks":
            add(gin if t["type"] == "deposit" else gout, classify(t["name"]), a, True)
        elif t["type"] == "move":
            fs = classify(t["from_name"]) if t["from_category"] == "stocks" else None
            ts = classify(t["to_name"]) if t["to_category"] == "stocks" else None
            if fs and fs == ts:
                continue
            both_etf = bool(fs and ts) and "companies" not in (fs, ts)
            if fs:
                add(gout, fs, a, not both_etf)
            if ts:
                add(gin, ts, a, not both_etf)
    return gin, gout


def _gross_by_asset(transfers):
    """Gross inflow/outflow per asset key, no netting: every leg counts on the key
    it touches. Returns (gross_in, gross_out)."""
    gin, gout = defaultdict(float), defaultdict(float)
    for t in transfers:
        a = float(t["amount"])
        if t["type"] == "deposit":
            gin[(t["category"], t["source"], t["name"])] += a
        elif t["type"] == "withdraw":
            gout[(t["category"], t["source"], t["name"])] += a
        elif t["type"] == "move":
            gout[(t["from_category"], t["from_source"], t["from_name"])] += a
            gin[(t["to_category"], t["to_source"], t["to_name"])] += a
    return gin, gout


def _compare(label, before, after, lines, unit):
    mismatches = [(k, before.get(k, 0.0), after.get(k, 0.0))
                  for k in set(before) | set(after)
                  # EPS-dropped dust can shift a sum by less than a cent.
                  if abs(before.get(k, 0.0) - after.get(k, 0.0)) > 2 * EPS]
    if mismatches:
        lines.append(f"  {label}: MISMATCH ({len(mismatches)})")
        for k, b, a in sorted(mismatches, key=lambda x: str(x[0])):
            lines.append(f"      {k}: before {b:+.2f}  !=  after {a:+.2f}")
        return False
    lines.append(f"  {label}: OK  ({len(set(before) | set(after))} {unit})")
    return True


def reconcile(raw, simple):
    """Check the invariants the app's published numbers depend on:
      (1) per-asset and per-category NET flow unchanged;
      (2) per-category and per-stocks-sub-bucket GROSS in/out across the boundary
          unchanged — the yield denominators, which netting an in-leg against an
          out-leg, or re-pairing two rebalances inside their sub-buckets, would move;
      (3) no asset key gained gross flow it did not have in raw (a pass-through leg
          on a hub key inflates that sub-bucket's denominator).
    (1) and (2) gate; (3) only WARNs, because a leg set that satisfies (2) can still
    need a leg on a key whose raw gross was smaller, and (2) owns the published
    denominator. Returns (ok, report_lines).
    """
    lines = []
    ok = True
    for label, fn in (("per-asset net", _net_by_asset), ("per-category net", _net_by_category)):
        ok &= _compare(label, fn(raw), fn(simple), lines, "keys")

    (rin, rout), (sin, sout) = _gross_by_category(raw), _gross_by_category(simple)
    ok &= _compare("per-category gross-in", rin, sin, lines, "categories")
    ok &= _compare("per-category gross-out", rout, sout, lines, "categories")

    (rin, rout), (sin, sout) = _gross_by_stock_sub(raw), _gross_by_stock_sub(simple)
    ok &= _compare("per-stock-sub gross-in", rin, sin, lines, "sub-buckets")
    ok &= _compare("per-stock-sub gross-out", rout, sout, lines, "sub-buckets")

    (kin_r, kout_r), (kin_s, kout_s) = _gross_by_asset(raw), _gross_by_asset(simple)
    inflated = [f"      {k}: gross {side} {r.get(k, 0.0):.2f} -> {s[k]:.2f}"
                for side, r, s in (("in", kin_r, kin_s), ("out", kout_r, kout_s))
                for k in s if s[k] - r.get(k, 0.0) > 2 * EPS]
    if inflated:
        lines.append(f"  per-asset gross: WARN — {len(inflated)} key(s) gained pass-through flow")
        lines += sorted(inflated)
    else:
        lines.append("  per-asset gross: OK  (no key gained flow it did not have)")
    return bool(ok), lines


def main():
    ap = argparse.ArgumentParser(description="Collapse raw transfers to the minimal meaningful set.")
    ap.add_argument("file", nargs="?", help="JSON file: {meta, transfers:[...]} or a bare list")
    ap.add_argument("-o", "--out")
    ap.add_argument("--demo", action="store_true")
    args = ap.parse_args()

    if args.demo:
        raw, meta = DEMO, {"date": "demo"}
    else:
        d = json.load(open(args.file))
        raw = d["transfers"] if isinstance(d, dict) else d
        meta = d.get("meta", {}) if isinstance(d, dict) else {}

    simple = simplify(raw)
    result = {"meta": meta, "transfers": simple}

    # Reconcile BEFORE writing: a corrupt file on disk outlives the traceback, and
    # the operator remembers the "wrote ..." line.
    ok, lines = reconcile(raw, simple)

    if ok and args.out:
        json.dump(result, open(args.out, "w"), ensure_ascii=False, indent=2)
        print(f"wrote {args.out}")
    print(f"raw: {len(raw)} transfers  ->  simplified: {len(simple)} transfers\n")
    for t in simple:
        if t["type"] == "move":
            print(f"  move     {t['amount']:>10,.2f}  {t['from_category']}/{t['from_source']}/{t['from_name']}  ->  {t['to_category']}/{t['to_source']}/{t['to_name']}")
        else:
            print(f"  {t['type']:8} {t['amount']:>10,.2f}  {t['category']}/{t['source']}/{t['name']}")

    print("\nreconciliation (flows before vs after simplify):")
    for ln in lines:
        print(ln)
    if not ok:
        raise SystemExit("RECONCILIATION FAILED: flows changed — nothing written")
    if args.demo and any("WARN" in ln for ln in lines):
        raise SystemExit("DEMO: the fixtures must satisfy every invariant, WARNs included")


if __name__ == "__main__":
    main()
