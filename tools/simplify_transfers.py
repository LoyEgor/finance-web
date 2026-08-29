#!/usr/bin/env python3
"""
simplify_transfers.py — collapse a raw reconstructed transfer list to the minimal,
meaningful set, matching the user's historical granularity.

Only the NET flow of each asset between snapshots matters (it's what isolates an
asset's clean performance). Intermediate hops and round-trips don't. So this:
  - nets external deposits and withdrawals SEPARATELY per asset (same-signed rows
    only), so an asset's GROSS deposit total survives — the app's yield divides by
    startBalance + GROSS deposits, so collapsing a deposit against a withdraw on
    the same asset would move the published number,
  - collapses multi-hop chains A->B->C into A->C (an asset that nets to ~0 drops out),
  - cancels round-trips (USDT->BTC->USDT),
  - reduces internal moves to one leg per asset against a cash hub — two-level, so
    no synthetic leg ever crosses a category boundary.

Internal moves are NOT re-paired asset-to-asset: matching arbitrary net sources to
net sinks invents semantically-false venue pairs (e.g. AAPL->BTC, MSFT->BTC that
never happened). Instead each category pairs its own movers against its LOCAL hub
(its largest |net| key) — every level-1 leg stays inside the category — and only
the category's RESIDUAL net crosses a boundary, routed between that category's hub
and the single GLOBAL cash hub. Routing every move through one global hub instead
(the earlier design) inflated that hub category's GROSS flows with moves that never
touched it, diluting its yield. With one move per asset key and each hub absorbing
its own remainder, per-asset AND per-category net flows are preserved EXACTLY, so
the app's clean per-asset / per-category performance is unchanged.

Net flows are preserved to the cent: nothing carrying real net is dropped (the old
dust dead-band silently lost net in the (dust/2, dust) gap). Only sub-cent rounding
residue is discarded.

Usage:  python3 tools/simplify_transfers.py raw.json [-o out.json]
        python3 tools/simplify_transfers.py --demo
"""
import argparse
import json
from collections import defaultdict

# Amounts are carried at cent precision; anything below this is rounding residue,
# not a real flow. Dropping it cannot change any net at 2-decimal precision.
EPS = 0.005


def simplify(transfers):
    ext_in = defaultdict(float)    # gross external deposits per key
    ext_out = defaultdict(float)   # gross external withdrawals per key
    internal = defaultdict(float)  # internal move net per key (+in / -out)

    for t in transfers:
        a = float(t["amount"])
        if t["type"] == "deposit":
            ext_in[(t["category"], t["source"], t["name"])] += a
        elif t["type"] == "withdraw":
            ext_out[(t["category"], t["source"], t["name"])] += a
        elif t["type"] == "move":
            internal[(t["from_category"], t["from_source"], t["from_name"])] -= a
            internal[(t["to_category"], t["to_source"], t["to_name"])] += a

    out = []
    # A key with real flow on BOTH sides emits BOTH legs: netting them would shrink
    # the gross deposit total the app's yield denominator is built from.
    for k in list(ext_in) + [k for k in ext_out if k not in ext_in]:
        cat, src, nm = k
        for typ, v in (("deposit", ext_in.get(k, 0.0)), ("withdraw", ext_out.get(k, 0.0))):
            if v >= EPS:
                out.append({"type": typ, "amount": round(v, 2),
                            "category": cat, "source": src, "name": nm})

    moving = {k: v for k, v in internal.items() if abs(v) >= EPS}
    if moving:
        def emit(fk, tk, amt):
            out.append({"type": "move", "amount": round(amt, 2),
                        "from_category": fk[0], "from_source": fk[1], "from_name": fk[2],
                        "to_category": tk[0], "to_source": tk[1], "to_name": tk[2]})

        by_cat = defaultdict(dict)
        for k, v in moving.items():
            by_cat[k[0]][k] = v
        hubs = {c: max(ks, key=lambda k: abs(ks[k])) for c, ks in by_cat.items()}

        # Level 1 — inside a category, every other mover pairs against that
        # category's own hub, so a same-category move can never plant a leg in
        # another category (which would inflate that category's gross flows).
        for c, ks in by_cat.items():
            for k, v in ks.items():
                if k == hubs[c]:
                    continue
                if v < 0:      # net source -> drains into its category hub
                    emit(k, hubs[c], -v)
                else:          # net sink <- funded from its category hub
                    emit(hubs[c], k, v)

        # Level 2 — only a category's RESIDUAL net crosses a boundary, routed
        # between its hub and the global cash hub (the largest |net| usd key, else
        # the largest category hub). Each hub then lands on exactly its own net,
        # because all internal nets — and so all category residuals — sum to zero.
        global_hub = hubs.get("usd") or max(hubs.values(), key=lambda k: abs(moving[k]))
        for c, h in hubs.items():
            if h == global_hub:
                continue
            resid = sum(by_cat[c].values())
            if abs(resid) < EPS:
                continue       # category rebalanced internally — nothing left it
            if resid < 0:
                emit(h, global_hub, -resid)
            else:
                emit(global_hub, h, resid)
    return out


# Synthetic fixture (NO real data). Exercises every reduction path:
#   - multi-hop collapse: AAA -> cash -> BBB nets each leg, hub absorbs remainder
#   - round-trip cancellation: cash <-> CCC at 300 nets to zero and drops out
#   - dust: the 0.001 move is below EPS and is discarded
DEMO = [
    {"type": "move", "amount": 1000, "from_category": "stocks", "from_source": "VenueX", "from_name": "AAA", "to_category": "usd", "to_source": "VenueX", "to_name": "Cash"},
    {"type": "deposit", "amount": 1000.00, "category": "usd", "source": "VenueY", "name": "Cash"},
    {"type": "move", "amount": 500, "from_category": "usd", "from_source": "VenueX", "from_name": "Cash", "to_category": "crypto", "to_source": "VenueY", "to_name": "BBB"},
    {"type": "move", "amount": 300, "from_category": "usd", "from_source": "VenueY", "from_name": "Cash", "to_category": "safe", "to_source": "VenueY", "to_name": "CCC"},
    {"type": "move", "amount": 300, "from_category": "safe", "from_source": "VenueY", "from_name": "CCC", "to_category": "usd", "to_source": "VenueY", "to_name": "Cash"},
    {"type": "move", "amount": 500, "from_category": "copy", "from_source": "VenueX", "from_name": "Copy", "to_category": "usd", "to_source": "VenueY", "to_name": "Cash"},
    {"type": "withdraw", "amount": 500, "category": "usd", "source": "Cash", "name": "Cash"},
    {"type": "move", "amount": 0.001, "from_category": "usd", "from_source": "VenueY", "from_name": "Cash", "to_category": "crypto", "to_source": "VenueY", "to_name": "BBB"},
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


def reconcile(raw, simple):
    """Assert per-asset and per-category net flows are identical before/after.
    Returns (ok, report_lines)."""
    lines = []
    ok = True
    for label, fn in (("per-asset", _net_by_asset), ("per-category", _net_by_category)):
        before, after = fn(raw), fn(simple)
        keys = set(before) | set(after)
        mismatches = [(k, before.get(k, 0.0), after.get(k, 0.0))
                      for k in keys
                      if round(before.get(k, 0.0), 2) != round(after.get(k, 0.0), 2)]
        if mismatches:
            ok = False
            lines.append(f"  {label}: MISMATCH ({len(mismatches)})")
            for k, b, a in sorted(mismatches, key=lambda x: str(x[0])):
                lines.append(f"      {k}: before {b:+.2f}  !=  after {a:+.2f}")
        else:
            lines.append(f"  {label}: OK  ({len(keys)} keys, all net flows equal)")
    return ok, lines


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

    print("\nreconciliation (net flow before vs after simplify):")
    for ln in lines:
        print(ln)
    if not ok:
        raise SystemExit("RECONCILIATION FAILED: net flows changed — nothing written")


if __name__ == "__main__":
    main()
