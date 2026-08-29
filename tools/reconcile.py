#!/usr/bin/env python3
"""
reconcile.py — Stage 1 reconciliation engine for the portfolio tracker.

Runs entirely on the JSON already in data/ (no API, no network). For each
consecutive snapshot pair it:
  1. VALIDATE  — decomposes every asset's change into recorded-flow + residual
                 (the implied market move) and flags inconsistencies.
  2. RECONSTRUCT — ignores the recorded transfers and re-derives the flows it
                 can from snapshot deltas alone (high-confidence for stable/cash
                 assets), then scores itself against the real transfers.
  3. CLASSIFY  — labels each recorded transfer by the source that will feed it
                 in the live process (IBKR API / Binance API / screenshot / cash).

The math mirrors app.js exactly: asset key = `${cat}_${source}_${name}` lowercased
with whitespace runs collapsed to '_'; a transfer belongs to the period
(prevDate, currDate] (open left, closed right) by its file-level meta.date.
"""
import argparse
import glob
import json
import os
import re
import sys
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.normpath(os.path.join(HERE, "..", "data"))

if HERE not in sys.path:
    sys.path.insert(0, HERE)
import config

# usd-category assets (stablecoins, fiat cash) carry ~no market move, so their
# whole delta is a flow — the only class the engine reconstructs with confidence.
STABLE_CAT = "usd"
STABLE_TOL = config.TOL["stable_resid_usd"]  # $; FX cash (EUR/GBP/EURI) can drift a bit more — noted, not special-cased

# Anti-fudge band for the usd-category residual (recorded flows vs actual delta).
# EUR/FX revaluation + accrued interest legitimately live in this residual, and
# they scale with how much cash is held, so the band is %-of-cash, not fixed:
# max(STABLE_TOL, floor, pct × usd-category total). Only a gap past this band is
# advisory-flagged. FLAG-ONLY: the engine never auto-adjusts transfers to close it.
# Sourced from config.TOL via the SAME formula as the checks-layer usd_band so the
# CLI band and the check never diverge — config is the single place for the threshold.
def usd_resid_band(prev_usd_total, curr_usd_total):
    """Per-period usd-category residual band. Sized on the LARGER of the period's
    two usd totals: the drift accrues on the cash held DURING the period, so a
    category drawn down to near zero by the end still earned a month of FX/interest
    on what it held before."""
    return max(STABLE_TOL, config.TOL["usd_band_floor_usd"],
               config.TOL["usd_band_pct"] * max(abs(prev_usd_total), abs(curr_usd_total)))


def explained_tol(value):
    """How much of a vanished/appeared position a recorded flow may leave unexplained
    before it stops counting as an explanation. A stable asset carries no market move,
    so the floor is dust; a market asset's snapshot value differs from the traded
    amount by a real price move, and charging that to "unexplained" would flag every
    ordinary sell-out."""
    return max(config.TOL["dust_usd"], config.TOL["price_anchor_pp"] / 100.0 * abs(value))


def asset_key(cat, source, name):
    return re.sub(r"\s+", "_", f"{cat}_{source}_{name}".lower())


def load_snapshots():
    snaps = []
    for path in glob.glob(os.path.join(DATA_DIR, "20*-*.json")):
        if "transfers" in os.path.basename(path):
            continue
        with open(path) as fh:
            d = json.load(fh)
        month = os.path.splitext(os.path.basename(path))[0]
        # Fallback is the month's UPPER bound: a bare "2026-05" sorts BELOW every
        # "2026-05-DD", so every transfer of that month would shift a period later.
        date = (d.get("meta") or {}).get("date") or f"{month}-31"
        items = {}
        by_cat = defaultdict(float)
        for cat in d.get("portfolio", []):
            cid = cat.get("id")
            for it in cat.get("items", []):
                k = asset_key(cid, it["source"], it["name"])
                items[k] = {"cat": cid, "source": it["source"], "name": it["name"], "val": float(it["val"])}
                by_cat[cid] += float(it["val"])
        snaps.append({
            "month": month, "date": date, "items": items,
            "by_cat": dict(by_cat), "total": sum(by_cat.values()),
        })
    snaps.sort(key=lambda s: s["date"])
    return snaps


def load_groups():
    groups = []
    for path in glob.glob(os.path.join(DATA_DIR, "transfers-*.json")):
        with open(path) as fh:
            d = json.load(fh)
        if isinstance(d, list):
            meta, trs = {}, d
        else:
            meta, trs = d.get("meta") or {}, d.get("transfers") or []
        if not trs:
            continue
        month = re.match(r"transfers-(\d{4}-\d{2})", os.path.basename(path))
        date = meta.get("date") or (month.group(1) if month else "")
        groups.append({"date": date, "file": os.path.basename(path), "transfers": trs})
    groups.sort(key=lambda g: g["date"])
    return groups


def transfers_for_period(groups, prev_date, curr_date):
    out = []
    for g in groups:
        d = g["date"]
        if (d > prev_date if prev_date else True) and d <= curr_date:
            out.extend(g["transfers"])
    return out


def adjustments(transfers):
    """Net flow per asset key, plus per-key signed legs (for tooltips/debug)."""
    adj = defaultdict(float)
    legs = defaultdict(list)
    for t in transfers:
        amt = float(t["amount"])
        if t["type"] == "deposit":
            k = asset_key(t["category"], t["source"], t["name"])
            adj[k] += amt
            legs[k].append(("+", amt, "deposit"))
        elif t["type"] == "withdraw":
            k = asset_key(t["category"], t["source"], t["name"])
            adj[k] -= amt
            legs[k].append(("-", amt, "withdraw"))
        elif t["type"] == "move":
            fk = asset_key(t["from_category"], t["from_source"], t["from_name"])
            tk = asset_key(t["to_category"], t["to_source"], t["to_name"])
            adj[fk] -= amt
            adj[tk] += amt
            legs[fk].append(("-", amt, "move-out"))
            legs[tk].append(("+", amt, "move-in"))
    return adj, legs


def cat_netflow(transfers, cat):
    """Net recorded flow into a category (deposits/move-in +, withdraws/move-out -)."""
    net = 0.0
    for t in transfers:
        if t["type"] == "deposit" and t["category"] == cat:
            net += float(t["amount"])
        elif t["type"] == "withdraw" and t["category"] == cat:
            net -= float(t["amount"])
        elif t["type"] == "move":
            if t["to_category"] == cat:
                net += float(t["amount"])
            if t["from_category"] == cat:
                net -= float(t["amount"])
    return net


COPY_CAT = "copy"  # category id of the no-API copytrading sleeve (the app's category, not the channel name)


def leg_provider(cat, source):
    """Label a transfer leg by the data source that will FEED it live, derived from
    config.VENUES (method) — never from a literal venue name. An api venue is trusted
    ("<venue> API") except its copy-category sleeve, which has no API and comes from a
    screenshot; a manual venue is hand-stated; everything else is a screenshot venue."""
    spec = config.VENUES.get(source, {})
    method = spec.get("method")
    if method == "api":
        if cat == COPY_CAT and spec.get("screenshot_sleeves"):
            return f"{source} copy (screenshot)"
        return f"{source} API"
    if method == "manual":
        return "manual cash"
    return "screenshot/inference"  # screenshot venues + any unknown source


def classify_transfer(t):
    if t["type"] == "move":
        provs = {leg_provider(t["from_category"], t["from_source"]),
                 leg_provider(t["to_category"], t["to_source"])}
    else:
        provs = {leg_provider(t["category"], t["source"])}
    if len(provs) == 1:
        return next(iter(provs))
    return "partial: " + " + ".join(sorted(provs))


def fmt(v):
    return f"{v:>12,.2f}"


def analyze(prev, curr, transfers, verbose, baseline=False):
    """baseline=True marks the synthetic opening period (no predecessor snapshot).
    Every position is new and the whole opening balance looks like an unrecorded
    inflow there, so the appear/propose/band checks are skipped rather than emitting
    noise the operator learns to scroll past."""
    adj, _ = adjustments(transfers)
    keys = set(prev["items"]) | set(curr["items"]) | set(adj)
    cats = sorted(set(prev["by_cat"]) | set(curr["by_cat"]))

    rows, anomalies, proposed = [], [], []
    for k in sorted(keys):
        p, c = prev["items"].get(k), curr["items"].get(k)
        pv = p["val"] if p else 0.0
        cv = c["val"] if c else 0.0
        a = adj.get(k, 0.0)
        meta = c or p or {"cat": k.split("_")[0], "source": "?", "name": k}
        rows.append((meta, pv, cv, a, cv - pv - a))

        if not p and not c and abs(a) > 0.005:
            anomalies.append(f"[ORPHAN] {meta['cat']}/{meta['source']}/{meta['name']} — transfer references an asset absent in both snapshots (net {a:+.2f})")
        # A recorded flow only EXPLAINS the exit/appearance when it accounts for the
        # whole position; a dust row on the same key must not silence the flag.
        elif p and not c and pv > STABLE_TOL and abs(pv + a) > explained_tol(pv):
            anomalies.append(f"[GHOST?] {meta['cat']}/{meta['source']}/{meta['name']} exited ({pv:,.0f}→0), recorded flow {a:+,.2f} leaves {cv - pv - a:+,.2f} unexplained")
        elif c and not p and cv > STABLE_TOL and abs(cv - a) > explained_tol(cv) and not baseline:
            anomalies.append(f"[NEW?]   {meta['cat']}/{meta['source']}/{meta['name']} appeared ({cv:,.0f}), recorded flow {a:+,.2f} leaves {cv - a:+,.2f} unfunded")

        # RECONSTRUCT: re-derive flow from the snapshot delta alone, then compare
        # to what's already recorded. The residual (cv - pv - a) is the part the
        # recorded flows DON'T explain. Only stable/cash assets are confident here.
        if meta["cat"] == STABLE_CAT and not baseline:
            residual = cv - pv - a
            if abs(residual) > STABLE_TOL:
                if abs(a) < 0.005:
                    # nothing recorded → the whole residual is a candidate flow
                    proposed.append((meta, residual, "candidate"))
                else:
                    # already has recorded flow → leftover is unexplained, NOT a
                    # candidate deposit/withdraw (proposing it would double-count)
                    proposed.append((meta, residual, "unexplained"))

    # category-level reconciliation: for usd (all stable) residual≈0 means flows tie out;
    # for market categories residual is the genuine market move (can't validate without prices).
    usd_resid = (curr["by_cat"].get(STABLE_CAT, 0) - prev["by_cat"].get(STABLE_CAT, 0)
                 - cat_netflow(transfers, STABLE_CAT))
    ext_dep = sum(float(t["amount"]) for t in transfers if t["type"] == "deposit")
    ext_wd = sum(float(t["amount"]) for t in transfers if t["type"] == "withdraw")

    print(f"\n{'='*78}\n{prev['date']} → {curr['date']}   ({curr['month']})"
          f"{'   [BASELINE — opening positions, nothing to reconcile against]' if baseline else ''}")
    print(f"  portfolio total: {prev['total']:,.0f} → {curr['total']:,.0f}   ({curr['total']-prev['total']:+,.0f})")
    types = defaultdict(int)
    for t in transfers:
        types[t["type"]] += 1
    print(f"  recorded transfers: {len(transfers)}  {dict(types) if transfers else '(NONE — nothing recorded for this period)'}")

    print(f"  {'category':10} {'prev':>11} {'curr':>11} {'delta':>10} {'recNetFlow':>11} {'residual':>10}")
    for cid in cats:
        pv, cv = prev["by_cat"].get(cid, 0), curr["by_cat"].get(cid, 0)
        nf = cat_netflow(transfers, cid)
        tag = "  (stable→resid≈0)" if cid == STABLE_CAT else "  (resid=market move)"
        print(f"  {cid:10} {pv:>11,.0f} {cv:>11,.0f} {cv-pv:>+10,.0f} {nf:>+11,.0f} {cv-pv-nf:>+10,.0f}{tag}")
    print(f"  external: deposits {ext_dep:+,.0f}  withdrawals -{ext_wd:,.0f}   |   usd category residual {usd_resid:+,.0f} (FX/interest if small)")

    if verbose:
        print(f"  {'asset':42} {'prev':>12} {'curr':>12} {'recFlow':>12} {'market':>12}")
        for meta, pv, cv, a, res in rows:
            print(f"  {(meta['cat']+'/'+meta['source']+'/'+meta['name'])[:42]:42} {fmt(pv)} {fmt(cv)} {fmt(a)} {fmt(res)}")

    if anomalies:
        print("  flags:")
        for x in anomalies:
            print(f"    - {x}")
    if proposed:
        print("  reconstruct would propose (stable/cash residuals; flowed assets shown as unexplained):")
        for meta, residual, kind in proposed:
            tail = f"  {meta['cat']}/{meta['source']}/{meta['name']}"
            if kind == "candidate":
                print(f"    + {'deposit' if residual > 0 else 'withdraw':18} {abs(residual):>10,.2f}{tail}")
            else:
                print(f"    ? {'unexplained residual':18} {residual:>+10,.2f}{tail}")

    # ANTI-FUDGE band check (FLAG-ONLY): a usd residual past the band means the
    # recorded flows leave more unexplained than FX/interest can account for.
    # Advisory only — never auto-fix, never touch transfers.
    band = usd_resid_band(prev["by_cat"].get(STABLE_CAT, 0.0), curr["by_cat"].get(STABLE_CAT, 0.0))
    if not baseline and abs(usd_resid) > band:
        # Rank by the UNEXPLAINED part: the largest raw delta is usually a move the
        # transfers already cover, which points the operator at the wrong row.
        stable_resids = [(m, res) for (m, pv, cv, a, res) in rows if m["cat"] == STABLE_CAT]
        big = max(stable_resids, key=lambda x: abs(x[1]), default=None)
        culprit = f"  largest unexplained stable residual: {big[0]['cat']}/{big[0]['source']}/{big[0]['name']} ({big[1]:+,.2f})" if big else ""
        print(f"  !! FLAG: usd residual {usd_resid:+,.2f} exceeds band ±{band:,.0f} "
              f"(advisory; transfers untouched){culprit}")

    return {"month": curr["month"], "transfers": transfers, "usd_resid": usd_resid,
            "has_transfers": bool(transfers), "baseline": baseline}


def main():
    ap = argparse.ArgumentParser(description="Stage 1 reconciliation engine (offline, uses data/ only).")
    ap.add_argument("--period", help="focus on one month id, e.g. 2026-05")
    ap.add_argument("--verbose", "-v", action="store_true", help="print full per-asset decomposition")
    args = ap.parse_args()

    snaps = load_snapshots()
    groups = load_groups()
    if len(snaps) < 2:
        print("need at least 2 snapshots")
        return

    results = []

    # Opening period: snaps[0] has no predecessor, so the zip below would never
    # reconcile the transfers dated on/before it (prev_date=None branch). Build a
    # synthetic empty "prev" and reconcile snaps[0] against it so those flows count.
    opening_prev = {
        "month": "(opening)", "date": "", "items": {},
        "by_cat": {}, "total": 0.0,
    }
    if not args.period or snaps[0]["month"] == args.period:
        opening_trs = transfers_for_period(groups, None, snaps[0]["date"])
        results.append(analyze(opening_prev, snaps[0], opening_trs, args.verbose, baseline=True))

    for prev, curr in zip(snaps, snaps[1:]):
        if args.period and curr["month"] != args.period:
            continue
        trs = transfers_for_period(groups, prev["date"], curr["date"])
        results.append(analyze(prev, curr, trs, args.verbose))

    # global summary
    prov_count = defaultdict(int)
    prov_vol = defaultdict(float)
    n_tr = 0
    for r in results:
        for t in r["transfers"]:
            n_tr += 1
            p = classify_transfer(t)
            prov_count[p] += 1
            prov_vol[p] += float(t["amount"])

    print(f"\n{'='*78}\nSUMMARY")
    print(f"  recorded transfers analysed: {n_tr}")
    print("  usd-category reconciliation (recorded flows vs actual delta; small = FX/interest):")
    for r in results:
        if r["baseline"]:
            note = "  ← baseline (opening balance, not a residual)"
        else:
            note = "" if r["has_transfers"] else "  ← no transfers recorded for this period"
        print(f"    {r['month']:10} residual {r['usd_resid']:>+9,.0f}{note}")
    if n_tr:
        print("  recorded transfers by future data source (count / $ volume):")
        for p in sorted(prov_vol, key=lambda x: -prov_vol[x]):
            print(f"    {p:48} {prov_count[p]:>4}   {prov_vol[p]:>12,.2f}")


if __name__ == "__main__":
    main()
