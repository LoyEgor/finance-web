"""
config.example.py — TEMPLATE. Copy to config.py and fill in your real values:

    cp config.example.py config.py and fill in

config.py is gitignored (it holds your venues, real amounts, and the absolute path
to your private data repo). This template ships in the public repo with PLACEHOLDER
values so the structure is documented without leaking anything.

config.py is THE SINGLE PLACE for everything portfolio-specific. Edit ONLY config.py
when your situation changes (new venue, dropped a coin, switched benchmarks, salary
changed, etc.). The orchestrator, checks, and fetchers read from here and contain NO
hard-coded tickers, venues, or amounts. Nothing below is load-bearing for the LOGIC —
it is data the logic consumes (the one exception is stock_sub_bucket, the canonical
mirror of the app's ETF table — it lives with the table it reads). If a value here
is stale, the logic still runs; it just flags against the wrong expectation, so
keep it current.
"""

# ── VENUES ──────────────────────────────────────────────────────────────────
# How each venue's data is obtained. "api" = fetched & trusted; "screenshot" =
# user pastes an image (no usable API); "manual" = user states it. Drop a venue
# or add one freely — the logic iterates this dict, it doesn't assume names.
#
# role: a semantic tag the LOGIC keys off instead of a literal venue name, so
#   checks.py stays venue-agnostic. "broker" = the venue whose Flex NAV is checked
#   and that the payout wallet is forwarded to; "payout" = the wallet salary lands
#   in (its residual after forwarding is living spend — see RECURRING); "cash" =
#   physical home/office cash the rent withdraw comes from. At most one venue per
#   role (first match wins).
# required_channels: for "api" venues, the EXACT money-movement manifest keys the
#   fetcher emits (fetch_binance.flows() / fetch_ibkr --flows) that MUST be queried
#   and span the period or coverage_gate fails CRITICAL. These are channel-key
#   strings, not display names — keep them in lockstep with the fetchers.
VENUES = {
    "BrokerA":   {"method": "api",        "tool": "fetch_ibkr", "role": "broker",
                  "required_channels": ["trades", "cashtx", "transfers"]},
    "ExchangeB": {"method": "api",        "tool": "fetch_binance",
                  "screenshot_sleeves": ["copytrading"],   # copier copytrading has no API
                  "required_channels": ["capital-deposit", "capital-withdraw",
                                        "fiat-deposit", "fiat-withdraw", "p2p-sell", "p2p-buy",
                                        "convert"]},
    "ExchangeC": {"method": "screenshot"},
    "ExchangeD": {"method": "screenshot"},
    "Cash":      {"method": "manual",     "role": "cash",
                  "note": "physical cash; carry forward unless user states a change"},
    "Payout":    {"method": "manual",     "role": "payout",
                  "note": "balance from the payout-wallet screenshot"},
}

# Reporting/base currency. All snapshot values are stored in this currency.
BASE_CCY = "USD"

# ── PUBLISH TARGETS ───────────────────────────────────────────────────────────
# Absolute path to the PRIVATE data repo. It TRACKS all data/*.json and is the
# source the app fetches at runtime. publish.py syncs this repo's data/{YYYY-MM}
# snapshot files here — read from config, never hard-coded.
PRIVATE_DATA_REPO = "/path/to/your/private-data-repo"

# Downstream projects whose source of truth IS this monthly snapshot. publish.py
# copies data/{month}.json into each inbox right after the private commit, because
# publishing is the moment the numbers became verified — a consumer reading a stale
# book silently analyses last month's portfolio. Each entry:
#   path    absolute repo root (skipped with a warning if absent)
#   inbox   repo-relative dir receiving {month}.json verbatim, same filename
#   refresh argv run in `path` to rebuild derived views from it, or None
#   derived repo-relative paths `refresh` regenerates OUTSIDE the inbox; listed so
#           the report shows every file the sync touched, not just the copy
# publish.py never commits in a consumer: these are shared checkouts that routinely
# hold other agents' work, so it syncs + reports and leaves the commit to the owner.
# Leave the list empty if nothing downstream consumes the snapshot.
SNAPSHOT_CONSUMERS = [
    {
        "name": "example-analysis-project",
        "path": "/path/to/your/analysis-project",
        "inbox": "inputs/portfolio",
        "refresh": ["./example-cli", "portfolio"],
        "derived": ["inputs/portfolio.md"],
    },
]

# ── ASSET DEFAULTS ──────────────────────────────────────────────────────────
# Hints mapping an exchange asset code -> (category id, display name as it should
# appear in the snapshot). category ids live in data/categories.json. This is a
# convenience default; a never-before-seen asset is allowed (the orchestrator
# asks/guesses its category). NOT an allow-list — do not assume these exist.
ASSET_MAP = {
    "PAXG":  ("safe",   "PAXG (Gold)"),
    "BTC":   ("crypto", "BTC (Bitcoin)"),
    "WBETH": ("crypto", "WBETH"),
    "BNB":   ("crypto", "BNB"),
    "ETH":   ("crypto", "ETH"),
    "SOL":   ("crypto", "SOL"),
    "USDT":  ("usd",    "USDT"),
    "USDC":  ("usd",    "USDC"),
    "EURI":  ("usd",    "EURI (Euro)"),
}
# Stablecoins / cash-equivalents: assumed ~0 market move (their delta == flow).
STABLES = {"USDT", "USDC", "FDUSD", "TUSD", "BUSD", "DAI", "USDP", "RWUSD"}

# ── STOCKS SUB-BUCKETS ──────────────────────────────────────────────────────
# The app scores the stocks category per sub-bucket (companies + ETF by region), so
# simplify_transfers has to pair transfers at THAT granularity: a leg collapsed
# inside one sub-bucket is invisible to the app, while a leg invented between two
# sub-buckets moves their published yield denominators. Mirrors js/app.js ETF_REGION
# and classifyStockItem — keep the two tables in lockstep.
ETF_REGION = {t: "us" for t in (
    "VOO SPY IVV SPLG VTI ITOT SCHB RSP QQQ QQQM DIA VUG IWF SCHG VTV IWD SCHV "
    "VYM SCHD DGRO HDV IWM VB IJR VO IJH SCHM XLK VGT SOXX SMH DRAM PPA SPMO "
    # global funds are scored with the US sleeve, as in the app
    "VT ACWI VEA IEFA VWO IEMG EEM CSPX SXR8 SWDA IWDA EUNL EIMI VWCE").split()}
ETF_REGION.update({t: "europe" for t in (
    "MEUD EXSA IMEU EUNK VGK IEV EZU FEZ VUKE ISF CSUK SXR3 EWU").split()})
ETF_REGION.update({t: "asia" for t in (
    "AAXJ VPL FXI MCHI KWEB CQQQ FXC CBUK EWJ DXJ TPXE SXRZ VJPA EWY CSKR EWT INDA EPI").split()})


def stock_sub_bucket(name):
    """'companies' | 'etf_us' | 'etf_europe' | 'etf_asia' for a stocks display name,
    resolved exactly like js classifyStockItem (upper-cased, dots/spaces stripped)."""
    ticker = "".join(ch for ch in (name or "").upper() if ch != "." and not ch.isspace())
    region = ETF_REGION.get(ticker)
    return f"etf_{region}" if region else "companies"


# ── BENCHMARKS ──────────────────────────────────────────────────────────────
# Symbols to fetch each snapshot (Yahoo tickers). NOT freely swappable: the app
# reads benchmarksData['VT'] and ['VOO'] by literal key and renders exactly those
# two perf rows, so dropping either leaves its row blank — changing the set needs
# an app change too. The app keys these by snapshot date; fetch_benchmarks fills them.
BENCHMARKS = ["VOO", "VT"]

# ── RECURRING PATTERNS (HINTS ONLY) ─────────────────────────────────────────
# Used ONLY to FLAG deviations for your review — never to force or fabricate a
# value. If a number here is wrong, you get a spurious flag, not a wrong report.
# Keep loose; update when your life changes. Values below are illustrative
# placeholders — replace with your own in config.py.
RECURRING = {
    # Salary is fixed in USD and lands on the payout-role venue's line `salary_to` once
    # per snapshot, whatever the day; whatever is not forwarded to the broker is living
    # spend, so the orchestrator books deposit + moves + a residual withdraw from that line.
    "salary_usd_approx": 0,
    "salary_to": "EUR Cash (Payout)",
    # Rent alternates by calendar-month parity and is paid from the home-cash line,
    # so the orchestrator books it (and lowers that line) without asking.
    "rent_by_month_parity": {"odd": 500, "even": 600},
    "rent_from": "USD Cash (Home)",
    "rent_per_month": 1,                # >1 home-cash rent withdraw in a period = duplicate flag
    "living_p2p_usd_approx": 0,         # exchange P2P stable->local-fiat living cash-out (SELL side)
}

# ── TOLERANCES / BANDS ──────────────────────────────────────────────────────
# Reconciliation thresholds. Tune if you get too many / too few flags.
TOL = {
    "dust_usd": 1.0,                    # ignore positions/flows below this
    "stable_resid_usd": 2.0,            # a stable/cash asset residual above this is suspicious
    "usd_band_pct": 0.015,              # usd-category residual band = max(floor, pct × usd-category total); the residual (FX/conversion/Earn-yield) scales with cash held, so the band is %-of-cash, not fixed; breach -> FLAG
    "usd_band_floor_usd": 50.0,         # floor so a near-empty cash category still has a non-zero band
    "price_anchor_pp": 3.0,             # cross-venue price-move divergence (percentage points) before flagging a hidden flow
    "match_usd": 50.0,                  # cross-venue vanished->deposit amount-match slack
    "ibkr_nav_abs": 25.0,               # broker snapshot-sum vs Flex NAV: absolute floor of the tolerance
    "ibkr_nav_pct": 0.001,              # ...and its %-of-NAV part; tolerance = max(abs, pct × NAV)
}
