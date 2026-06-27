#!/usr/bin/env python3
"""
checks.py — deterministic, FLAG-ONLY guardrails for the portfolio snapshot pipeline.

Every function here takes already-fetched/loaded data plus config and returns a
list of findings; NONE of them mutate the snapshot or transfers, and NONE of them
invent a flow to balance the books. The orchestrator runs the fetch → reconcile
stages and then calls these to surface anything unexplained for the user.

A finding is a dict: {"check", "severity", "message", "detail"}.
  severity ∈ {"critical", "warn", "info"}; only the deterministic PROCESS gates
  (coverage, clock) emit "critical" — value divergences are "warn" (advisory).

Everything portfolio-specific is read from config (VENUES / ASSET_MAP / STABLES /
BENCHMARKS / RECURRING / TOL / BASE_CCY). Nothing here hard-codes a ticker, venue,
or amount. The reconciliation math (asset_key, adjustments, cat_netflow) is
imported from reconcile.py — not re-implemented.
"""
from collections import defaultdict

import reconcile


def _finding(check, severity, message, detail=None):
    return {"check": check, "severity": severity, "message": message, "detail": detail or {}}


def _api_venues(config):
    """Venue names whose data is fetched via API (the money-movement channels we own)."""
    return [v for v, spec in config.VENUES.items() if spec.get("method") == "api"]


def _venue_by_role(config, role):
    """The venue name tagged with a semantic role (e.g. 'broker', 'cash'), or None.
    Lets the logic stay venue-name-agnostic. The 'cash' role falls back to the lone
    manual venue when no explicit role is set. First match wins."""
    for v, spec in config.VENUES.items():
        if spec.get("role") == role:
            return v
    if role == "cash":
        manual = [v for v, spec in config.VENUES.items() if spec.get("method") == "manual"]
        if len(manual) == 1:
            return manual[0]
    return None


def _is_stable_asset(name, cat, config):
    """A usd-category line or a known stablecoin code embedded in the display name."""
    if cat == reconcile.STABLE_CAT:
        return True
    up = (name or "").upper()
    return any(s in up for s in config.STABLES)


# ── coverage_gate ────────────────────────────────────────────────────────────
def coverage_gate(manifests, period, config):
    """Enforce a REQUIRED set, not just whatever the manifest happens to list. For
    every API venue, each channel in config.VENUES[v]['required_channels'] MUST be
    present in the manifest, queried cleanly (queried==true, no error), and carry a
    window that fully spans the period (prevSnapDate, currSnapDate]. A missing
    venue, a missing/false/errored required channel, or a window that starts after
    prevSnapDate / ends before currSnapDate => critical. count:0 with queried==true
    (channel ran, no rows) is fine — absence of flows is data, not a gap.

    manifests: {venue_name: {channel: {"queried": bool, "window": [...]|str,
                                       "rows": int, "error"?: ...}}}
    period:    {"prev_date": "YYYY-MM-DD"|None, "curr_date": "YYYY-MM-DD"}
    """
    out = []
    prev = (period.get("prev_date") or "").replace("-", "")
    curr = (period.get("curr_date") or "").replace("-", "")
    for venue in _api_venues(config):
        required = config.VENUES[venue].get("required_channels") or []
        man = manifests.get(venue)
        if not man:
            out.append(_finding(
                "coverage_gate", "critical",
                f"{venue}: no coverage manifest — its money-movement channels were never queried; "
                f"cannot reconcile on incomplete data.",
                {"venue": venue, "required_channels": required}))
            continue
        for ch in required:
            m = man.get(ch)
            if not m:
                out.append(_finding(
                    "coverage_gate", "critical",
                    f"{venue}/{ch}: required money-movement channel absent from the manifest — "
                    f"it was never queried; flows from this channel are missing.",
                    {"venue": venue, "channel": ch, "required_channels": required}))
                continue
            if not m.get("queried") or m.get("error"):
                out.append(_finding(
                    "coverage_gate", "critical",
                    f"{venue}/{ch}: channel not cleanly queried (queried={m.get('queried')}, "
                    f"error={m.get('error')}) — flows may be truncated.",
                    {"venue": venue, "channel": ch, "manifest": m}))
                continue
            span = _window_span(m.get("window"))
            if span and prev and curr:
                w_from, w_to = span
                if (w_from and w_from > prev) or (w_to and w_to < curr):
                    out.append(_finding(
                        "coverage_gate", "critical",
                        f"{venue}/{ch}: window {span} does not span the period "
                        f"({prev or '-'}, {curr}] — partial pull, flows may be lost.",
                        {"venue": venue, "channel": ch, "window": m.get("window"),
                         "period": [prev, curr]}))
        # Non-required channels in the manifest (e.g. balance pulls: spot/funding/earn)
        # that errored or didn't query cleanly understate the SNAPSHOT, not a flow — warn,
        # don't gate. A balance channel carries no period window, so only its query status
        # matters here.
        for ch, m in (man or {}).items():
            if ch in required or not isinstance(m, dict):
                continue
            if not m.get("queried") or m.get("error"):
                out.append(_finding(
                    "coverage_gate", "warn",
                    f"{venue}/{ch}: balance channel not cleanly pulled (queried={m.get('queried')}, "
                    f"error={m.get('error')}) — the snapshot value for this wallet may be understated.",
                    {"venue": venue, "channel": ch, "manifest": m}))
    if not any(f["severity"] == "critical" for f in out):
        out.append(_finding(
            "coverage_gate", "info",
            f"coverage OK: all required channels on API venues {sorted(_api_venues(config))} "
            f"queried across the period.",
            {"venues": sorted(_api_venues(config))}))
    return out


def _window_span(window):
    """Normalize a manifest window to (from_yyyymmdd|None, to_yyyymmdd|None).

    Fetchers emit windows as epoch-ms pairs (Binance) or "YYYYMMDD.." strings
    (IBKR --flows). 'as-of-end' balance pulls carry no period span -> return None.
    """
    if window is None:
        return None
    if isinstance(window, str):
        if ".." not in window:
            return None  # e.g. "as-of-end"
        a, b = (window.split("..", 1) + [""])[:2]
        return (a[:8] or None, b[:8] or None)
    if isinstance(window, (list, tuple)) and len(window) == 2:
        a, b = window
        return (_ms_to_ymd(a), _ms_to_ymd(b))
    return None


def _ms_to_ymd(v):
    import datetime
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    # Epoch ms vs an already-YYYYMMDD-ish int: treat large values as ms.
    if v > 1e11:
        return datetime.datetime.fromtimestamp(v / 1000, datetime.timezone.utc).strftime("%Y%m%d")
    return str(int(v))[:8]


# ── clock_guard ──────────────────────────────────────────────────────────────
def clock_guard(target_filename, meta_date, today, prior_snapshot_dates, config):
    """Snapshot must be filed in the CURRENT calendar month, dated <= today, never
    a future month/date. Compare-base = the greatest snapshot strictly in a prior
    month. Pure clock+filesystem logic; the only critical-severity value-free gate
    besides coverage.

    target_filename: e.g. "2026-06.json" or "2026-06"
    meta_date:       the candidate snapshot's meta.date ("YYYY-MM-DD")
    today:           system date "YYYY-MM-DD"
    prior_snapshot_dates: meta.date strings of all OTHER existing snapshots
    """
    out = []
    cur_month = today[:7]
    fname_month = target_filename.replace(".json", "")[:7]

    if fname_month > cur_month:
        out.append(_finding(
            "clock_guard", "critical",
            f"target file {target_filename} is a FUTURE month (> {cur_month}) — refuse to file ahead of the clock.",
            {"target": target_filename, "today": today}))
    elif fname_month != cur_month:
        out.append(_finding(
            "clock_guard", "warn",
            f"target file month {fname_month} != current month {cur_month} — back-dated run; confirm this is intentional.",
            {"target": target_filename, "today": today}))

    if meta_date:
        if meta_date > today:
            out.append(_finding(
                "clock_guard", "critical",
                f"meta.date {meta_date} is in the FUTURE (> today {today}).",
                {"meta_date": meta_date, "today": today}))
        elif meta_date[:7] != fname_month:
            out.append(_finding(
                "clock_guard", "warn",
                f"meta.date {meta_date} is not in the target file's month {fname_month}.",
                {"meta_date": meta_date, "file_month": fname_month}))

    priors = sorted(d for d in prior_snapshot_dates if d and d[:7] < cur_month)
    if not priors:
        out.append(_finding(
            "clock_guard", "warn",
            "no prior-month snapshot to compare against — first snapshot, nothing to reconcile.",
            {}))
    else:
        base = priors[-1]
        if meta_date and base >= meta_date:
            out.append(_finding(
                "clock_guard", "critical",
                f"compare-base {base} is not strictly before meta.date {meta_date} — "
                f"would compare against a same-or-later snapshot.",
                {"base": base, "meta_date": meta_date}))
        else:
            out.append(_finding(
                "clock_guard", "info",
                f"compare-base = {base} (greatest prior-month snapshot).",
                {"base": base}))

    if not any(f["severity"] == "critical" for f in out) and not any(f["severity"] == "warn" for f in out):
        out.append(_finding("clock_guard", "info",
                            f"clock OK: {target_filename} dated {meta_date}, current month {cur_month}.", {}))
    return out


# ── recurring_guard ──────────────────────────────────────────────────────────
def recurring_guard(transfers, config):
    """Two recurring-spend invariants from config.RECURRING (HINTS only -> flags):
      - rent: > config.RECURRING['rent_per_month'] home-cash withdraws in the
        period => duplicate flag (a withdraw from the cash-role venue, usd category).
      - salary: an external deposit into the broker-role venue far from
        salary_usd_approx => flag (raise/cut/partial month or a mis-entry).

    Venues are resolved by config role markers, not by literal name.
    """
    out = []
    rec = config.RECURRING
    cash_venue = _venue_by_role(config, "cash")
    broker_venue = _venue_by_role(config, "broker")

    rents = [t for t in transfers
             if t.get("type") == "withdraw"
             and cash_venue and t.get("source") == cash_venue
             and t.get("category") == reconcile.STABLE_CAT]
    limit = rec.get("rent_per_month", 1)
    if len(rents) > limit:
        out.append(_finding(
            "recurring_guard", "warn",
            f"duplicate rent: {len(rents)} home-cash withdraws this period (> {limit} expected).",
            {"rows": rents}))
    for r in rents:
        amt = float(r["amount"])
        if rec.get("rent_amounts") and amt not in rec["rent_amounts"]:
            out.append(_finding(
                "recurring_guard", "info",
                f"rent amount {amt:,.0f} outside usual {rec['rent_amounts']} — confirm (legit one-offs happen).",
                {"row": r}))

    # External funding (deposit) into the broker venue's usd line = the salary channel.
    salary = rec.get("salary_usd_approx")
    for t in transfers:
        if t.get("type") != "deposit" or not broker_venue or t.get("source") != broker_venue:
            continue
        if t.get("category") != reconcile.STABLE_CAT:
            continue
        amt = float(t["amount"])
        if salary and abs(amt - salary) > 0.5 * salary:
            out.append(_finding(
                "recurring_guard", "info",
                f"{broker_venue} funding {amt:,.0f} far from expected salary ~{salary:,.0f} — "
                f"raise/cut/partial month or a mis-entry; confirm.",
                {"row": t, "expected": salary}))
    return out


# ── price_anchor ─────────────────────────────────────────────────────────────
def price_anchor(prev_items, cur_items, transfers, price_fn, config):
    """Cross-venue fungible-asset anchor. For an asset code held on >1 venue, the
    same % market move applies everywhere. We derive the real period return r from
    price_fn (injected: Binance klines / Yahoo) and check each venue's residual:

        excess = curr - prev*(1+r) - recordedNetFlow(this venue's asset)

    An excess beyond config.TOL['price_anchor_pp'] (% of prev) implies a hidden
    flow (a deposit/top-up or an OCR misread) -> flag. FLAG-ONLY: never auto-edit a
    screenshot value (a misread and a real top-up are indistinguishable from value).

    prev_items / cur_items: {asset_key: {"cat","source","name","val"}} (reconcile shape)
    price_fn(asset_code, prev_date, curr_date) -> float period return r, or None.
    """
    out = []
    adj, _ = reconcile.adjustments(transfers)
    pp = config.TOL["price_anchor_pp"]
    dust = config.TOL["dust_usd"]
    match_floor = config.TOL["match_usd"]

    # Group lines by asset CODE (resolved from the display name via ASSET_MAP /
    # STABLES). Anchor ONLY a fungible, priceable market asset: skip stables/cash
    # (zero market move — stable_invariant/usd_band own those) and the no-API copy
    # sleeve (no price feed; copytrading is handled separately, never anchored).
    by_code = defaultdict(list)
    union = {**prev_items, **cur_items}
    for k, meta in union.items():
        if meta["cat"] == "copy" or _is_stable_asset(meta["name"], meta["cat"], config):
            continue
        code = _asset_code(meta["name"], config)
        if not code or code in config.STABLES:
            continue
        by_code[code].append((k, meta))

    api_venues = set(_api_venues(config))
    for code, members in by_code.items():
        venues = {m["source"] for _, m in members}
        # The cross-venue signal only exists when the SAME code sits on an API venue
        # (its qty is ground truth -> r) AND a non-API screenshot venue (the one that
        # can silently diverge). A single-venue or all-API code has nothing to anchor.
        if len(venues) < 2 or not (venues & api_venues) or venues <= api_venues:
            continue
        r = price_fn(code, None, None)  # orchestrator binds the period dates in the closure
        if r is None:
            out.append(_finding(
                "price_anchor", "info",
                f"{code}: held on {sorted(venues)} but no price feed return available — anchor skipped.",
                {"code": code, "venues": sorted(venues)}))
            continue
        for k, meta in members:
            pv = (prev_items.get(k) or {}).get("val", 0.0)
            cv = (cur_items.get(k) or {}).get("val", 0.0)
            flow = adj.get(k, 0.0)
            if max(abs(pv), abs(cv)) < dust:
                continue
            excess = cv - pv * (1 + r) - flow
            base = abs(pv) if abs(pv) > dust else abs(cv)
            excess_pp = (excess / base * 100) if base else 0.0
            if abs(excess) >= match_floor and abs(excess_pp) > pp:
                out.append(_finding(
                    "price_anchor", "warn",
                    f"{meta['source']} {meta['name']}: implied hidden flow {excess:+,.2f} "
                    f"({excess_pp:+.1f}pp) after applying market r={r*100:+.1f}% and recorded flow {flow:+,.2f} "
                    f"— a top-up/withdrawal or OCR misread; confirm (never auto-edited).",
                    {"code": code, "venue": meta["source"], "prev": pv, "curr": cv,
                     "r": r, "recorded_flow": flow, "excess": round(excess, 2)}))
    return out


def _asset_code(name, config):
    """Map a snapshot display name back to its exchange asset code via ASSET_MAP
    (display name -> code), then STABLES, else the leading token of the name."""
    for code, (_cat, disp) in config.ASSET_MAP.items():
        if disp == name:
            return code
    up = (name or "").upper()
    for s in config.STABLES:
        if s in up:
            return s
    # Fall back to the first alnum token (e.g. "BTC (Bitcoin)" -> "BTC").
    tok = "".join(c if c.isalnum() else " " for c in up).split()
    return tok[0] if tok else None


# ── vanished_venue ───────────────────────────────────────────────────────────
def vanished_venue(prev_items, cur_items, transfers, deposits, config):
    """Conservation: an asset present in prev (>dust) and ~0 in curr with NO
    recorded exit at all (the simplified transfers show no net move-out/withdraw)
    AND no matching fetched deposit elsewhere (within config.TOL['match_usd']) ->
    flag with candidate destinations. Run AFTER simplify so multi-hop/round-trips
    collapse to net flows before matching.

    A SUBSTANTIAL recorded exit means the drop is already booked — any leftover is a
    market move on a sold-out position (stocks/crypto) or belongs to the stable
    invariant / usd band, NOT a vanished flow. The copy category is EXCLUDED: a
    copytrading sleeve dropping to ~0 can be a genuine loss (no API), handled
    separately — never required to reappear as a deposit.

    deposits: list of fetched inbound rows on API venues, each with a usd 'amount'
              (and optional 'coin'/'venue'/'time') used only to MATCH, never to book.
    """
    out = []
    adj, _ = reconcile.adjustments(transfers)
    dust = config.TOL["dust_usd"]
    slack = config.TOL["match_usd"]

    for k, meta in prev_items.items():
        if meta["cat"] == "copy":  # no-API sleeve; a drop to 0 may be a real loss
            continue
        pv = meta["val"]
        cv = (cur_items.get(k) or {}).get("val", 0.0)
        if pv <= dust or cv > dust:
            continue
        flow = adj.get(k, 0.0)
        if abs(flow) > dust:  # an exit IS recorded; leftover is market move / stable residual, not vanished
            continue
        candidates = [d for d in (deposits or [])
                      if abs(float(d.get("amount", 0.0)) - pv) <= max(slack, 0.05 * pv)]
        out.append(_finding(
            "vanished_venue", "warn",
            f"{meta['source']} {meta['name']} vanished ({pv:,.2f} -> ~0) with no recorded exit; "
            f"{len(candidates)} matching deposit candidate(s) elsewhere within ±{slack:,.0f}.",
            {"asset": k, "prev": pv, "recorded_flow": flow,
             "candidates": candidates}))
    return out


# ── stable_invariant ─────────────────────────────────────────────────────────
def stable_invariant(prev_items, cur_items, transfers, config):
    """Stable/cash assets carry ~0 market move, so a per-asset residual
    (curr - prev - recordedNetFlow) above config.TOL['stable_resid_usd'] is an
    unrecorded flow. Emitted as DIAGNOSTIC (info) only: unlogged stable<->stable
    conversions (the user curates, not logs) leave per-asset residue that nets ~0
    across the usd category, so the GATING check is the category-level usd_band —
    not this. Surfaced so the user can see WHERE a category-level breach sits.
    """
    out = []
    adj, _ = reconcile.adjustments(transfers)
    tol = config.TOL["stable_resid_usd"]
    union = {**prev_items, **cur_items}
    for k, meta in union.items():
        if not _is_stable_asset(meta["name"], meta["cat"], config):
            continue
        pv = (prev_items.get(k) or {}).get("val", 0.0)
        cv = (cur_items.get(k) or {}).get("val", 0.0)
        flow = adj.get(k, 0.0)
        resid = cv - pv - flow
        if abs(resid) > tol:
            out.append(_finding(
                "stable_invariant", "info",
                f"{meta['source']} {meta['name']} (stable/cash) per-asset residual {resid:+,.2f} "
                f"(diagnostic; may net out across the usd category — see usd_band for the gate).",
                {"asset": k, "prev": pv, "curr": cv, "recorded_flow": flow,
                 "residual": round(resid, 2)}))
    return out


# ── usd_band ─────────────────────────────────────────────────────────────────
def usd_band(category_residuals, config, stable_deltas=None, usd_total=0.0):
    """Anti-fudge gate on the usd category. A usd-category residual beyond the
    per-period band max(floor, pct × usd-category total) -> flag the signed gap +
    the largest-delta stable asset (so the user sees where to look). The band scales
    with cash held because the residual (FX/conversion/Earn-yield) does too.
    Advisory (warn) — the engine never auto-inserts a balancing flow.

    category_residuals: {cat_id: residual_float} (from reconcile: Δ - cat_netflow)
    stable_deltas:      optional [(meta, delta), ...] for usd-category assets.
    usd_total:          the CURRENT snapshot's usd-category total (sizes the band).
    """
    out = []
    # SAME formula as the reconcile CLI band so the two layers never diverge.
    band = reconcile.usd_resid_band(usd_total)
    resid = category_residuals.get(reconcile.STABLE_CAT, 0.0)
    if abs(resid) > band:
        culprit = None
        if stable_deltas:
            big = max(stable_deltas, key=lambda x: abs(x[1]), default=None)
            if big:
                m = big[0]
                culprit = f"{m['source']}/{m['name']} ({big[1]:+,.2f})"
        out.append(_finding(
            "usd_band", "warn",
            f"usd-category residual {resid:+,.2f} exceeds band ±{band:,.0f} "
            f"(FX + interest live here) — unexplained gap; do NOT fabricate a flow to close it."
            + (f"  largest stable delta: {culprit}" if culprit else ""),
            {"residual": round(resid, 2), "band": band, "largest_stable_delta": culprit}))
    else:
        out.append(_finding(
            "usd_band", "info",
            f"usd-category residual {resid:+,.2f} within band ±{band:,.0f}.",
            {"residual": round(resid, 2), "band": band}))
    return out


# ── ibkr_nav ─────────────────────────────────────────────────────────────────
def ibkr_nav(snapshot_items, flex_nav, config, tol_abs=25.0, tol_pct=0.001):
    """sum(broker-venue snapshot rows) vs Flex NAV within max(tol_abs, tol_pct*NAV)
    -> flag. Skipped (info) if NAV unavailable or no broker role configured.
    FLAG-only: never auto-overwrite the snapshot from Flex (same-day Flex can be
    the stale side). The broker venue is resolved by config role, not by name.

    snapshot_items: list of candidate snapshot rows {category, source, name, val}.
    flex_nav:       broker Flex EquitySummary NAV in base ccy, or None.
    """
    broker = _venue_by_role(config, "broker")
    if flex_nav is None or broker is None:
        return [_finding("ibkr_nav", "info",
                         f"{broker or 'broker'} Flex NAV unavailable — NAV identity skipped.", {})]
    broker_sum = sum(float(it["val"]) for it in snapshot_items if it.get("source") == broker)
    tol = max(tol_abs, tol_pct * abs(flex_nav))
    gap = broker_sum - flex_nav
    if abs(gap) > tol:
        return [_finding(
            "ibkr_nav", "warn",
            f"{broker} snapshot sum {broker_sum:,.2f} vs Flex NAV {flex_nav:,.2f} differ by {gap:+,.2f} "
            f"(> ±{tol:,.2f}) — dropped/stale position or hand-rounded cash; confirm (snapshot not auto-overwritten).",
            {"broker_sum": round(broker_sum, 2), "flex_nav": round(flex_nav, 2), "gap": round(gap, 2), "tol": tol})]
    return [_finding(
        "ibkr_nav", "info",
        f"{broker} snapshot sum {broker_sum:,.2f} matches Flex NAV {flex_nav:,.2f} (±{tol:,.2f}).",
        {"broker_sum": round(broker_sum, 2), "flex_nav": round(flex_nav, 2)})]


def run_all(ctx, config):
    """Convenience: run every check from a single context dict and return the flat
    finding list. The orchestrator builds ctx; each key is optional and a missing
    one simply skips that check.

    ctx keys:
      manifests, period, target_filename, meta_date, today, prior_snapshot_dates,
      transfers, prev_items, cur_items, price_fn, deposits, category_residuals,
      stable_deltas, usd_total, snapshot_items, flex_nav
    """
    findings = []
    if "manifests" in ctx and "period" in ctx:
        findings += coverage_gate(ctx["manifests"], ctx["period"], config)
    if "target_filename" in ctx and "today" in ctx:
        findings += clock_guard(ctx["target_filename"], ctx.get("meta_date"), ctx["today"],
                                ctx.get("prior_snapshot_dates", []), config)
    if "transfers" in ctx:
        findings += recurring_guard(ctx["transfers"], config)
    if {"prev_items", "cur_items", "transfers", "price_fn"} <= ctx.keys():
        findings += price_anchor(ctx["prev_items"], ctx["cur_items"], ctx["transfers"], ctx["price_fn"], config)
    if {"prev_items", "cur_items", "transfers"} <= ctx.keys():
        findings += vanished_venue(ctx["prev_items"], ctx["cur_items"], ctx["transfers"],
                                   ctx.get("deposits", []), config)
        findings += stable_invariant(ctx["prev_items"], ctx["cur_items"], ctx["transfers"], config)
    if "category_residuals" in ctx:
        findings += usd_band(ctx["category_residuals"], config, ctx.get("stable_deltas"),
                             ctx.get("usd_total", 0.0))
    if "snapshot_items" in ctx:
        findings += ibkr_nav(ctx["snapshot_items"], ctx.get("flex_nav"), config)
    return findings


def _self_test():
    """Run with `python3 tools/checks.py`. Regression guard for the salary
    amount-deviation flag: a deposit at ~salary_usd_approx must NOT flag; one far
    from it (here doubled) MUST flag. Also asserts the usd_band scales with cash."""
    import config

    def _fund(amount):
        return {"type": "deposit", "source": _venue_by_role(config, "broker"),
                "category": reconcile.STABLE_CAT, "amount": amount, "name": "USDT"}

    def _salary_flagged(amount):
        return any(f["check"] == "recurring_guard" and "expected salary" in f["message"]
                   for f in recurring_guard([_fund(amount)], config))

    salary = config.RECURRING["salary_usd_approx"]
    assert not _salary_flagged(salary), "on-target salary -> must NOT flag"
    assert _salary_flagged(salary * 2), "salary far from expected -> MUST flag"

    near_empty = usd_band({reconcile.STABLE_CAT: 100.0}, config, usd_total=0.0)
    big_cash = usd_band({reconcile.STABLE_CAT: 100.0}, config, usd_total=50_000.0)
    assert near_empty[0]["detail"]["band"] < big_cash[0]["detail"]["band"], "band must scale with usd total"
    print("checks.py self-test OK")


if __name__ == "__main__":
    _self_test()
