---
name: portfolio-snapshot
description: >
  Build this month's portfolio snapshot + transactions for the finances-web tracker:
  fetch what has an API (per tools/config.py), ingest screenshots for the rest,
  reconcile, and FLAG anything unexplained — never fabricate. Trigger when the user
  wants to make/update a snapshot ("сделай снапшот", "посчитай месяц", "обнови портфель"),
  attaches exchange screenshots for the tracker, or asks to assemble the monthly data.
---

# Portfolio snapshot & transactions

Produces `data/YYYY-MM.json` (balances = **source of truth**), `data/transfers-YYYY-MM.json`
(flows that isolate each asset's clean market performance), and updates
`data/benchmarks.json` — with the least possible input from the user.

**Read `tools/config.py` FIRST.** Every portfolio specific (venues, asset map, benchmark
symbols, recurring amounts, tolerances) lives there. Do NOT hard-code tickers, venues, or
amounts in reasoning or code — read them from config so a changed portfolio needs only a
config edit. This skill encodes METHODS (a class of checks), not the specific bugs we once hit.

## Inputs
- A fresh-chat request, plus screenshots for venues whose `config.VENUES[...]["method"]`
  is `"screenshot"`, and for no-API sleeves (e.g. `screenshot_sleeves`).
- Consume from the user ONLY what no API/source can provide. Everything else is fetched.

## Pipeline
Run `tools/orchestrator.py` (it wires fetch → gate → reconcile → checks → flag). Steps:
1. **Period & filing.** The snapshot is this CALENDAR month's → `data/{current-month}.json`,
   dated today (or the last settled day). Compare to the previous month's snapshot. Never
   write a future-month file or a future date.
2. **Fetch all channels.** For every `"api"` venue, run its fetcher; each emits a coverage
   manifest (every money-movement channel queried across the full period). If any required
   channel is missing/partial → **stop and flag**, don't reconcile on incomplete data.
3. **Field-drift check.** On first run or unexpected shapes, sanity-check field names against
   the live payload (APIs rename fields).
4. **Ingest screenshots / manual.** OCR screenshot-venue balances and no-API sleeve totals.
   Physical cash: carry forward from last snapshot unless the user states a change — except
   the rent line (`config.RECURRING.rent_from`), which the orchestrator already lowered by
   the month's rent (`rent_by_month_parity`); never ask about rent or cash.
   Exchange overview screenshots include no-API sleeve wallets in their totals: a copytrading
   sleeve held in a stable = overview total of that stable − the API's spot/funding/earn figure.
   Salary lands on the payout-role venue's line (`config.RECURRING.salary_to`, Zen) as a
   `deposit` of `salary_usd_approx` once per snapshot, whatever day it landed; a broker funding
   is a `move` from that line (its cash channel reports it in the period it settles); the
   line's residual (prev + salary − moves − stated balance) is living spend, booked as a
   `withdraw` (wallet fees included). Salary and rent are MANDATORY every period: `recurring_guard`
   flags a report missing either; a rule set to 0 in config switches it off — never ask about them. Read the wallet balance off the user's Zen screenshot
   and pass it as `--manual 'Zen/EUR Cash (Zen)=<EUR>EUR'`; money the Zen history shows sent to
   the broker but not yet in the broker's EOD statement is broker cash already — pass
   `--funded <EUR>EUR` and the orchestrator adds it to the broker cash line, books the move now,
   and skips that cashtx row next period (`prebooked_funding`); never ask how much was spent.
5. **Assemble snapshot** (per category/source/name, in `config.BASE_CCY`).
6. **Reconstruct transactions.** Start from the orchestrator's `suggested_transfers` (broker
   trades and cash movements, exchange external legs, rent) and add only the internal moves
   no channel reports (copytrading ↔ spot, exchange ↔ exchange) from the delta vs previous
   snapshot; then **simplify** (`tools/simplify_transfers.py`) to the minimal net set.
7. **Reconcile + CHECK layer** (below).
8. **Present** snapshot + transactions + flags for the user's confirmation; write files after,
   then run `tools/fetch_benchmarks.py` to fill the new date. Do not commit.
9. **Publish (only once the user says to commit).** `tools/publish.py --push` is the ONLY
   route: it commits data to the private repo, code to the public one, and then syncs the
   month to every downstream consumer. Never hand-copy data to a consumer, and never
   hand-roll the commits — the guards and the fan-out live in that script.

## Methods (general — solve the class, not the instance)
- **Snapshot is truth:** never alter a balance to make math close.
- **Exact split where qty is known:** API quantities (IBKR shares, exchange coin qty) give
  flow-vs-market exactly (Δqty × price = flow).
- **Cross-venue price anchor:** one asset = one price = the same % market move everywhere.
  Fetch the real price move; if a venue's value diverges (esp. a no-API screenshot venue),
  the gap is a hidden flow (quantity change), not market. Flag — never auto-edit a screenshot.
- **Vanished-venue conservation:** a venue dropping to ~0 must reappear as a fetched deposit
  elsewhere or a stated spend; if neither, flag with candidates.
- **Stable/cash invariant:** stablecoins & cash have ~0 market; a residual there = a missing flow.
- **Anti-fudge reconciliation:** every category must reconcile from REAL fetched/stated flows.

## Hard rules (non-negotiable)
1. **Never fabricate** a deposit/withdrawal/move to force a balance. Unexplained = FLAGGED.
2. **Residual menu:** when something doesn't reconcile, show the user the candidate causes and
   let them attribute — FX on non-base-currency amounts; broker valuation/rounding; **losses on
   no-API sleeves (e.g. copytrading) — always offer this, it's real and expected**; anything
   screenshots/API miss. Close a residual only with a real row or the user's explicit word.
3. **Ask the user only** for what no API/source provides (screenshot balances, no-API sleeve
   totals, physical-cash changes, residual attribution).
4. **One snapshot per calendar month**, dated the day taken, compared to the previous month.
5. **A published month reaches every consumer in the same breath.** Committing IS the user's
   statement that the numbers are verified, and `config.SNAPSHOT_CONSUMERS` lists the projects
   whose source of truth is this snapshot — they analyse a stale book, silently and confidently,
   until they get it. So publishing is not done when the commits land: it is done when the
   consumer report shows every entry synced and refreshed. If a consumer errors or is skipped,
   say so explicitly in the summary; a quiet skip is the failure mode this rule exists to stop.
   Consumers are never committed for the user: they are shared checkouts holding other agents'
   work, so report the pending paths and let the owner commit them.

## Where things live
- Config (the only place for specifics): `tools/config.py`.
- Fetchers: `tools/fetch_ibkr.py`, `tools/fetch_binance.py`, `tools/fetch_benchmarks.py`.
- Engine: `tools/reconcile.py`; collapse: `tools/simplify_transfers.py`;
  orchestration + guardrail checks: `tools/orchestrator.py`, `tools/checks.py`.
- Publish + downstream fan-out: `tools/publish.py` (dry run by default; `--push` applies).
- Secrets: `tools/.env` (read-only keys, never commit). Output: `data/` (gitignored).
