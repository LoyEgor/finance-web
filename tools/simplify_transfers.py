#!/usr/bin/env python3
"""
simplify_transfers.py — collapse a raw reconstructed transfer list to the minimal,
meaningful set, matching the user's historical granularity.

Only the NET flow of each asset between snapshots matters (it's what isolates an
asset's clean performance). Intermediate hops and round-trips don't. So this:
  - nets external deposits/withdrawals per asset,
  - collapses multi-hop chains A->B->C into A->C (an asset that nets to ~0 drops out),
  - cancels round-trips (USDT->BTC->USDT),
  - reduces internal moves to one leg per asset against a single canonical cash hub.

Internal moves are NOT re-paired asset-to-asset: matching arbitrary net sources to
net sinks invents semantically-false venue pairs (e.g. AAPL->BTC, MSFT->BTC that
never happened). Instead every asset's net internal flow is expressed as a single
move against one canonical cash hub — the economically real picture (you sell an
asset into cash and buy another out of cash). With one move per asset key and the
hub absorbing the remainder, per-asset AND per-category net flows are preserved
EXACTLY (the hub leg is omitted; its net falls out of the others summing to zero),
so the app's clean per-asset / per-category performance is unchanged.

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
    ext = defaultdict(float)       # external net per key (+deposit / -withdraw)
    internal = defaultdict(float)  # internal move net per key (+in / -out)

    for t in transfers:
        a = float(t["amount"])
        if t["type"] == "deposit":
            ext[(t["category"], t["source"], t["name"])] += a
        elif t["type"] == "withdraw":
            ext[(t["category"], t["source"], t["name"])] -= a
        elif t["type"] == "move":
            internal[(t["from_category"], t["from_source"], t["from_name"])] -= a
            internal[(t["to_category"], t["to_source"], t["to_name"])] += a

    out = []
    for k, v in ext.items():
        if abs(v) < EPS:
            continue
        cat, src, nm = k
        out.append({"type": "deposit" if v > 0 else "withdraw", "amount": round(abs(v), 2),
                    "category": cat, "source": src, "name": nm})

    # Canonical cash hub: the key with the largest |net| inside the usd category
    # (the real cash account everything routes through); fall back to the overall
    # largest |net| if there is no usd flow. The hub leg is never emitted directly —
    # connecting every other key to it gives the hub exactly its own net, because
    # all internal nets sum to zero.
    moving = {k: v for k, v in internal.items() if abs(v) >= EPS}
    if moving:
        usd_keys = {k: v for k, v in moving.items() if k[0] == "usd"}
        pool = usd_keys or moving
        hub = max(pool, key=lambda k: abs(pool[k]))

        def emit(fk, tk, amt):
            out.append({"type": "move", "amount": round(amt, 2),
                        "from_category": fk[0], "from_source": fk[1], "from_name": fk[2],
                        "to_category": tk[0], "to_source": tk[1], "to_name": tk[2]})

        for k, v in moving.items():
            if k == hub:
                continue
            if v < 0:          # net source -> drains into the hub
                emit(k, hub, -v)
            else:              # net sink <- funded from the hub
                emit(hub, k, v)
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

    if args.out:
        json.dump(result, open(args.out, "w"), ensure_ascii=False, indent=2)
        print(f"wrote {args.out}")
    print(f"raw: {len(raw)} transfers  ->  simplified: {len(simple)} transfers\n")
    for t in simple:
        if t["type"] == "move":
            print(f"  move     {t['amount']:>10,.2f}  {t['from_category']}/{t['from_source']}/{t['from_name']}  ->  {t['to_category']}/{t['to_source']}/{t['to_name']}")
        else:
            print(f"  {t['type']:8} {t['amount']:>10,.2f}  {t['category']}/{t['source']}/{t['name']}")

    ok, lines = reconcile(raw, simple)
    print("\nreconciliation (net flow before vs after simplify):")
    for ln in lines:
        print(ln)
    if not ok:
        raise SystemExit("RECONCILIATION FAILED: net flows changed")


if __name__ == "__main__":
    main()
