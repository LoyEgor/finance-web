#!/usr/bin/env python3
"""
fetch_ibkr.py — pull an IBKR Activity Flex statement (read-only) and normalize it.

Stdlib only. Two-step Flex Web Service flow (SendRequest -> reference code ->
GetStatement, polling while the statement is still generating). Outputs:
  - balances ready for the snapshot (positions in USD-equiv via fxRateToBase, cash per currency)
  - per-position realized+unrealized P&L (the broker-reported "clean performance")
  - flows for the period: trades (cash<->position moves), deposits/withdrawals, transfers

Needs IBKR_FLEX_TOKEN and IBKR_FLEX_QUERY_ID in tools/.env (see .env.example).
Run:  python3 tools/fetch_ibkr.py            # human summary
      python3 tools/fetch_ibkr.py --raw      # full normalized JSON to stdout
      python3 tools/fetch_ibkr.py --out tools/out/ibkr.json
"""
import argparse
import datetime
import json
import os
import re
import sys
import time
import urllib.request
import xml.etree.ElementTree as ET
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
SEND = "https://ndcdyn.interactivebrokers.com/AccountManagement/FlexWebService/SendRequest?t={t}&q={q}&v=3"
GET = "https://gdcdyn.interactivebrokers.com/AccountManagement/FlexWebService/GetStatement?t={t}&q={q}&v=3"
RETRY_SLEEP = {"1009": 5, "1019": 5, "1018": 10, "1001": 5, "1004": 5, "1005": 5, "1006": 5, "1007": 5, "1008": 5}

# assetCategory -> our category id (cash is taken from CashReport, not positions)
STOCK_CATS = {"STK", "ETF", "FUND", "BOND", "IND", "WAR", "OPT", "FUT"}

# Manifest channel -> the Flex container element whose PRESENCE proves the query
# includes that section. An absent container is a coverage gap; a present-but-empty
# one is data ("nothing happened in the period").
SECTION_TAGS = {
    "trades": "Trades", "cashtx": "CashTransactions", "transfers": "Transfers",
    "income": "CashTransactions", "positions": "OpenPositions", "cash": "CashReport",
}

_MONTHS = {m: f"{i:02d}" for i, m in enumerate(
    ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"), 1)}


def load_env():
    path = os.path.join(HERE, ".env")
    if not os.path.exists(path):
        return
    for line in open(path):
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        os.environ.setdefault(k.strip(), v)


def http_get(url):
    with urllib.request.urlopen(url, timeout=60) as r:
        return r.read().decode("utf-8")


def flex_fetch(token, query_id, max_tries=8):
    root = ET.fromstring(http_get(SEND.format(t=token, q=query_id)))
    if root.findtext("Status") != "Success":
        raise SystemExit(f"SendRequest failed: {root.findtext('ErrorCode')} {root.findtext('ErrorMessage')}")
    ref = root.findtext("ReferenceCode")
    for _ in range(max_tries):
        xml = http_get(GET.format(t=token, q=ref))
        root = ET.fromstring(xml)
        if root.tag == "FlexQueryResponse":
            return root
        code = root.findtext("ErrorCode") or ""
        if code in RETRY_SLEEP:
            time.sleep(RETRY_SLEEP[code])
            continue
        raise SystemExit(f"GetStatement failed: {code} {root.findtext('ErrorMessage')}")
    raise SystemExit("statement not ready after retries")


def f(el, attr, default=0.0):
    v = el.get(attr)
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


_DATE_PREFIX = re.compile(
    r"^(\d{4}-\d{2}-\d{2}|\d{2}/\d{2}/\d{4}|\d{8}|\d{1,2}-[A-Za-z]{3}-(?:\d{4}|\d{2}))")


def _ymd(s):
    """Canonical YYYYMMDD from any date format a Flex query can be configured to emit
    (YYYYMMDD, YYYY-MM-DD, MM/dd/yyyy, dd-MMM-yy), with an optional time suffix.

    An unrecognised format RAISES rather than passing the input through: a passthrough
    compares lexicographically against YYYYMMDD bounds, so every row silently falls
    out of the window and the statement reads as an empty period.
    """
    raw = (s or "").strip()
    if not raw:
        return ""
    # A time suffix is dropped only when what precedes it is a COMPLETE date and the
    # separator is ';', whitespace or 'T'. Anchoring on the whole date is what keeps the
    # 'T' inside the month tokens OCT and DEC from splitting 15-OCT-2026, without
    # assuming the time itself is punctuated (Flex emits HHmmss just as happily).
    m = _DATE_PREFIX.match(raw)
    s = m.group(1) if m and (m.end() == len(raw) or raw[m.end()] in "T;" or raw[m.end()].isspace()) else raw
    digits = s.replace("-", "").replace("/", "")
    # >= 8: the "no separator" Flex setting emits YYYYMMDDhhmmss as one digit run.
    if len(digits) >= 8 and digits.isdigit():
        head = digits[:8]
        return head[4:] + head[:4] if "/" in s else head  # MMddyyyy -> yyyyMMdd
    m = re.fullmatch(r"(\d{1,2})-([A-Za-z]{3})-(\d{2}|\d{4})", s)
    if m and m.group(2).upper() in _MONTHS:
        day, mon, year = m.group(1), _MONTHS[m.group(2).upper()], m.group(3)
        return f"{year if len(year) == 4 else '20' + year}{mon}{int(day):02d}"
    raise ValueError(f"unrecognised Flex date format: {raw!r}")


def in_window(rows, since, until):
    """Rows inside the reconciliation period (since, until] — open left, closed right.
    Open left because `since` is the PREVIOUS snapshot date, whose rows were already
    counted in that period; closed right because the statement's reporting period is
    routinely wider than the snapshot period."""
    return [r for r in rows if since < _ymd(r["date"]) <= until]


def parse(root):
    stmt = root.find(".//FlexStatement")
    out = {
        "source": "IBKR",
        "meta": {k: stmt.get(k) for k in ("accountId", "fromDate", "toDate", "period", "whenGenerated")} if stmt is not None else {},
        "nav": None, "base_cash": None, "fx": {"USD": 1.0}, "positions": [], "cash": [], "trades": [], "deposits_withdrawals": [], "income": [], "transfers": [],
        "sections": {k: False for k in SECTION_TAGS},
    }
    if stmt is None:
        return out
    out["sections"] = {k: stmt.find(f".//{tag}") is not None for k, tag in SECTION_TAGS.items()}

    fx_date = {}  # ConversionRates carry a daily series — keep the latest reportDate per currency
    for cr in stmt.findall(".//ConversionRate"):
        cur, d = cr.get("fromCurrency"), cr.get("reportDate") or ""
        # A missing / unparsable / non-positive rate is an ABSENT rate, never 1.0:
        # defaulting it makes the row look like a usable rate and folds 12,000 HUF in as
        # 12,000 USD — the very case the refusals below exist to catch.
        rate = f(cr, "rate", 0.0)
        if cur and rate > 0 and d >= fx_date.get(cur, ""):
            out["fx"][cur] = rate
            fx_date[cur] = d

    navs = stmt.findall(".//EquitySummaryByReportDateInBase")
    if navs:
        out["nav"] = f(navs[-1], "total")  # last report date = end of period

    for p in stmt.findall(".//OpenPosition"):
        fx = out["fx"].get(p.get("currency")) or f(p, "fxRateToBase", 0.0)
        val, pnl = f(p, "positionValue"), f(p, "fifoPnlUnrealized")
        if fx <= 0:
            # Same refusal as the cash lines in to_snapshot_items, on the LARGER rows: a
            # 10,000 EUR holding folded at 1:1 enters the snapshot as $10,000.
            if abs(val) >= 0.005 or abs(pnl) >= 0.005:
                raise ValueError(
                    f"position {p.get('symbol')} {val:,.2f} {p.get('currency')} has no "
                    f"ConversionRate row and no usable fxRateToBase — refusing to value it "
                    f"at 1:1. Enable Conversion Rates in the Flex query and re-run.")
            fx = 1.0
        out["positions"].append({
            "symbol": p.get("symbol"), "assetCategory": p.get("assetCategory"),
            "currency": p.get("currency"), "quantity": f(p, "position"),
            "value_native": val, "value_usd": round(val * fx, 2),
            "unrealizedPnl": round(pnl * fx, 2), "fxRateToBase": fx,
        })

    for c in stmt.findall(".//CashReportCurrency"):
        cur = c.get("currency")
        if cur == "BASE_SUMMARY":
            out["base_cash"] = f(c, "endingCash")  # total cash already in base USD
            continue
        out["cash"].append({"currency": cur, "endingCash": f(c, "endingCash"),
                            "deposits": f(c, "deposits"), "withdrawals": f(c, "withdrawals"),
                            "fxRateToBase": f(c, "fxRateToBase", 0.0)})

    for t in stmt.findall(".//Trade"):
        out["trades"].append({
            "symbol": t.get("symbol"), "assetCategory": t.get("assetCategory"), "buySell": t.get("buySell"),
            "currency": t.get("currency"), "quantity": f(t, "quantity"), "tradePrice": f(t, "tradePrice"),
            "proceeds": f(t, "proceeds"), "netCash": f(t, "netCash"), "realizedPnl": f(t, "fifoPnlRealized"),
            "date": _ymd(t.get("tradeDate")),
        })

    for ct in stmt.findall(".//CashTransaction"):
        typ = (ct.get("type") or "")
        row = {"type": typ, "currency": ct.get("currency"), "amount": f(ct, "amount"),
               "date": _ymd(ct.get("dateTime") or ct.get("reportDate")), "desc": ct.get("description")}
        if "Deposit" in typ or "Withdrawal" in typ:
            out["deposits_withdrawals"].append(row)
        else:
            out["income"].append(row)  # dividends / interest / fees — performance, not a flow

    for tr in stmt.findall(".//Transfer"):
        out["transfers"].append({
            "type": tr.get("type"), "direction": tr.get("direction"), "symbol": tr.get("symbol"),
            "assetCategory": tr.get("assetCategory"), "currency": tr.get("currency"),
            "quantity": f(tr, "quantity"), "amount": f(tr, "positionAmount"),
            "cashAmount": f(tr, "cashTransfer"), "date": _ymd(tr.get("dateTime") or tr.get("reportDate")),
        })
    return out


def to_snapshot_items(data, venue="IBKR"):
    """Broker positions + cash as snapshot rows: {category, source, name, val, perf}.

    `venue` must be the name this broker carries in config.VENUES: a row tagged with
    anything else is not recognised as an api venue, so the orchestrator ALSO carries
    the previous month's broker rows forward and every broker total is counted twice.
    """
    items = []
    for p in data["positions"]:
        if p["assetCategory"] == "CASH":
            continue  # cash is taken from CashReport below; emitting both double-counts it
        items.append({"category": "stocks", "source": venue, "name": p["symbol"],  # missing category → stocks
                      "val": p["value_usd"], "qty": p["quantity"], "unrealizedPnl": p["unrealizedPnl"]})
    # USD is the base currency; consolidate ALL broker cash (any currency) into one USD line.
    total = data["base_cash"]
    if total is None:
        total = 0.0
        for c in data["cash"]:
            # No rate and no BASE_SUMMARY total: folding at 1:1 would put e.g. 12,000 HUF
            # into the snapshot as 12,000 USD, and a snapshot with an unvalued cash line
            # cannot be trusted at all — so this fails loudly instead of warning.
            rate = data["fx"].get(c["currency"]) or c.get("fxRateToBase")
            if not rate:
                if abs(c["endingCash"]) < 0.005:
                    continue
                raise ValueError(
                    f"{venue}: cash {c['endingCash']:,.2f} {c['currency']} has no ConversionRate row "
                    f"and the statement carries no BASE_SUMMARY total — refusing to value it at 1:1. "
                    f"Enable Conversion Rates in the Flex query and re-run.")
            total += c["endingCash"] * rate
    if abs(total) >= 0.005:
        items.append({"category": "usd", "source": venue, "name": "USD Cash", "val": round(total, 2)})
    return items


def flows_manifest(data, since, until):
    """Coverage manifest built from the REAL parsed statement — NOT a fabricated
    full-span assertion. Per-channel rows are the actual section lengths inside
    (since, until]; the window is the statement's own reporting period
    (meta.fromDate..toDate), so coverage_gate fails CRITICAL when the statement
    period doesn't span the snapshot period. `queried` comes from the SECTION's
    presence in the statement, so a Flex query that lost a section reads as
    never-queried instead of "queried, nothing happened". Shared by the --flows CLI
    and the orchestrator so both gate on identical, real data.

    since: period start "YYYY-MM-DD"|"YYYYMMDD" (the prev snapshot date).
    until: period end, i.e. the snapshot date being written.
    """
    since, until = _ymd(since), _ymd(until)
    meta = data.get("meta") or {}
    frm, to = _ymd(meta.get("fromDate")), _ymd(meta.get("toDate"))
    window = f"{frm}..{to}"  # statement reporting period; coverage_gate checks it spans the snapshot period
    sections = data.get("sections") or {}

    def ch(name, window, rows):
        if sections.get(name):
            return {"queried": True, "window": window, "rows": rows}
        return {"queried": False, "window": window, "rows": 0,
                "error": f"{SECTION_TAGS[name]} section absent from the statement — the Flex query does not include it"}

    return {
        "trades":    ch("trades", window, len(in_window(data["trades"], since, until))),
        "cashtx":    ch("cashtx", window, len(in_window(data["deposits_withdrawals"], since, until))),
        "transfers": ch("transfers", window, len(in_window(data["transfers"], since, until))),
        "income":    ch("income", window, len(in_window(data["income"], since, until))),
        "positions": ch("positions", "as-of-end", len(data["positions"])),
        "cash":      ch("cash", "as-of-end", len(data["cash"])),
    }


def main():
    ap = argparse.ArgumentParser(description="Pull & normalize an IBKR Activity Flex statement (read-only).")
    ap.add_argument("--raw", action="store_true", help="print full normalized JSON")
    ap.add_argument("--out", help="write normalized JSON to this path")
    ap.add_argument("--flows", metavar="SINCE", help="instead of balances, net trades + cash deposits/withdrawals in (SINCE, UNTIL]")
    ap.add_argument("--until", metavar="UNTIL", help="upper bound for --flows (YYYY-MM-DD, inclusive; default: today)")
    args = ap.parse_args()

    load_env()
    token, qid = os.environ.get("IBKR_FLEX_TOKEN"), os.environ.get("IBKR_FLEX_QUERY_ID")
    if not token or not qid:
        raise SystemExit("set IBKR_FLEX_TOKEN and IBKR_FLEX_QUERY_ID in tools/.env (copy tools/.env.example)")

    data = parse(flex_fetch(token, qid))
    data["snapshot_items"] = to_snapshot_items(data)

    if args.flows:
        since = _ymd(args.flows)  # YYYYMMDD; parse() already canonicalizes row dates, _ymd is belt-and-suspenders
        until = _ymd(args.until) if args.until else datetime.date.today().strftime("%Y%m%d")
        win = f"({args.flows}, {until}]"
        trades_in = in_window(data["trades"], since, until)
        cashtx_in = in_window(data["deposits_withdrawals"], since, until)
        transfers_in = in_window(data["transfers"], since, until)
        income_in = in_window(data["income"], since, until)

        net = defaultdict(float)
        unconverted = []
        for t in trades_in:
            # netCash is after commission and tax (proceeds is not). A currency with no
            # ConversionRate row cannot enter the base-ccy sum: at 1:1 it would silently
            # distort the total, which is the very failure this conversion exists to avoid.
            rate = data["fx"].get(t["currency"])
            if rate is None:
                unconverted.append(t)
                continue
            net[t["symbol"]] += t["netCash"] * rate
        print(f"IBKR trades in {win} (net cash per symbol, base ccy; + = sold to cash, - = bought):")
        for sym in sorted(net, key=lambda s: net[s]):
            if abs(net[sym]) >= 0.5:
                print(f"  {sym:8} {net[sym]:>+12,.2f}  ({'sold→USD Cash' if net[sym] > 0 else 'USD Cash→bought'})")
        if unconverted:
            print(f"  !! WARN: {len(unconverted)} trade(s) in a currency with NO ConversionRate row — "
                  f"EXCLUDED from the net above (never assumed 1:1); convert them by hand:")
            for t in unconverted:
                print(f"     {t['symbol']:8} {t['netCash']:>+12,.2f} {t['currency'] or '?':4} [{_ymd(t['date'])}]")

        print(f"\nIBKR cash deposits/withdrawals in {win}:")
        for r in cashtx_in:
            print(f"  {r['amount']:>+12,.2f} {r['currency']:4} [{_ymd(r['date'])}] {r['type']}  {r.get('desc') or ''}")

        # Transfers (ACATS / position & cash moves between accounts). direction IN/OUT;
        # positionAmount is the asset value moved, cashAmount the cash leg (if any).
        print(f"\nIBKR transfers in {win}:")
        for tr in transfers_in:
            sign = "+" if (tr.get("direction") or "").upper() == "IN" else "-"
            amt = tr["cashAmount"] if abs(tr.get("cashAmount") or 0) >= 0.005 else tr["amount"]
            print(f"  {sign}{abs(amt):>11,.2f} {tr['currency'] or '':4} [{_ymd(tr['date'])}] "
                  f"{(tr.get('direction') or '?'):3} {tr.get('symbol') or tr.get('type') or ''}")

        # Cash-affecting income: interest credited + dividends paid to cash (not reinvested),
        # net of withholding tax. Performance, not a flow — but it moves the cash balance.
        print(f"\nIBKR cash income in {win} (net of tax):")
        income_by_type = defaultdict(float)
        for r in income_in:
            income_by_type[r["type"]] += r["amount"]
        for typ in sorted(income_by_type):
            if abs(income_by_type[typ]) >= 0.005:
                print(f"  {income_by_type[typ]:>+12,.2f}  {typ}")

        manifest = flows_manifest(data, args.flows, until)
        print(f"\nmanifest: {json.dumps(manifest)}")
        return

    if args.raw:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return
    if args.out:
        os.makedirs(os.path.dirname(args.out), exist_ok=True)
        with open(args.out, "w") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
        print(f"wrote {args.out}")

    m = data["meta"]
    nav = f"{data['nav']:,.2f}" if data["nav"] is not None else "n/a"
    print(f"IBKR account {m.get('accountId')}  period {m.get('fromDate')}–{m.get('toDate')}  NAV(base) {nav}")
    print(f"\npositions ({len(data['positions'])}):  symbol      USD value   unrealized P&L")
    for it in sorted(data["snapshot_items"], key=lambda x: -x["val"]):
        if "qty" in it:
            print(f"  {it['name']:10} {it['val']:>12,.2f}   {it['unrealizedPnl']:>+12,.2f}")
    print("cash (all IBKR cash consolidated to USD):")
    for it in data["snapshot_items"]:
        if "qty" not in it:
            print(f"  {it['name']:12} {it['val']:>12,.2f}")
    bd = "  ".join(f"{c['currency']} {c['endingCash']:,.2f}" for c in data["cash"] if abs(c["endingCash"]) >= 0.005)
    if bd:
        print(f"  (native: {bd})")
    print(f"\nflows in period:  trades {len(data['trades'])}   "
          f"deposits/withdrawals {len(data['deposits_withdrawals'])}   "
          f"income(div/int/fee) {len(data['income'])}   transfers {len(data['transfers'])}")
    dep = sum(r["amount"] for r in data["deposits_withdrawals"] if r["amount"] > 0)
    wd = sum(-r["amount"] for r in data["deposits_withdrawals"] if r["amount"] < 0)
    print(f"  external deposits +{dep:,.2f}   withdrawals -{wd:,.2f}")


if __name__ == "__main__":
    main()
