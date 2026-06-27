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
import json
import os
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


def _ymd(s):
    # Canonical YYYYMMDD. Flex date fields may arrive dashed (YYYY-MM-DD) or with a
    # time suffix (YYYYMMDD;HHMMSS / YYYY-MM-DD HH:MM:SS) depending on the query's
    # date-format config; strip both so date comparisons never silently mismatch.
    return (s or "").replace("-", "")[:8]


def parse(root):
    stmt = root.find(".//FlexStatement")
    out = {
        "source": "IBKR",
        "meta": {k: stmt.get(k) for k in ("accountId", "fromDate", "toDate", "period", "whenGenerated")} if stmt is not None else {},
        "nav": None, "base_cash": None, "fx": {"USD": 1.0}, "positions": [], "cash": [], "trades": [], "deposits_withdrawals": [], "income": [], "transfers": [],
    }
    if stmt is None:
        return out

    fx_date = {}  # ConversionRates carry a daily series — keep the latest reportDate per currency
    for cr in stmt.findall(".//ConversionRate"):
        cur, d = cr.get("fromCurrency"), cr.get("reportDate") or ""
        if cur and d >= fx_date.get(cur, ""):
            out["fx"][cur] = f(cr, "rate", 1.0)
            fx_date[cur] = d

    navs = stmt.findall(".//EquitySummaryByReportDateInBase")
    if navs:
        out["nav"] = f(navs[-1], "total")  # last report date = end of period

    for p in stmt.findall(".//OpenPosition"):
        fx = out["fx"].get(p.get("currency")) or f(p, "fxRateToBase", 1.0) or 1.0
        out["positions"].append({
            "symbol": p.get("symbol"), "assetCategory": p.get("assetCategory"),
            "currency": p.get("currency"), "quantity": f(p, "position"),
            "value_native": f(p, "positionValue"), "value_usd": round(f(p, "positionValue") * fx, 2),
            "unrealizedPnl": round(f(p, "fifoPnlUnrealized") * fx, 2), "fxRateToBase": fx,
        })

    for c in stmt.findall(".//CashReportCurrency"):
        cur = c.get("currency")
        if cur == "BASE_SUMMARY":
            out["base_cash"] = f(c, "endingCash")  # total cash already in base USD
            continue
        out["cash"].append({"currency": cur, "endingCash": f(c, "endingCash"),
                            "deposits": f(c, "deposits"), "withdrawals": f(c, "withdrawals")})

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


def to_snapshot_items(data):
    """IBKR positions + cash as snapshot rows: {category, source, name, val, perf}."""
    items = []
    for p in data["positions"]:
        cat = "usd" if p["assetCategory"] == "CASH" else "stocks"  # default (missing category) → stocks
        items.append({"category": cat, "source": "IBKR", "name": p["symbol"],
                      "val": p["value_usd"], "qty": p["quantity"], "unrealizedPnl": p["unrealizedPnl"]})
    # USD is the base currency; consolidate ALL IBKR cash (any currency) into one USD line.
    total = data["base_cash"]
    if total is None:
        total = sum(c["endingCash"] * data["fx"].get(c["currency"], 1.0) for c in data["cash"])
    if abs(total) >= 0.005:
        items.append({"category": "usd", "source": "IBKR", "name": "USD Cash", "val": round(total, 2)})
    return items


def flows_manifest(data, since):
    """Coverage manifest built from the REAL parsed statement — NOT a fabricated
    full-span assertion. Per-channel rows are the actual section lengths filtered to
    >= since; the window is the statement's own reporting period (meta.fromDate..toDate),
    so coverage_gate fails CRITICAL when a section is empty/missing or the statement
    period doesn't span the snapshot period. Shared by the --flows CLI and the
    orchestrator so both gate on identical, real data.

    since: period start "YYYY-MM-DD"|"YYYYMMDD" (the prev snapshot date).
    """
    since = _ymd(since)
    meta = data.get("meta") or {}
    frm, to = _ymd(meta.get("fromDate")), _ymd(meta.get("toDate"))
    window = f"{frm}..{to}"  # statement reporting period; coverage_gate checks it spans the snapshot period

    def in_window(rows):
        return [r for r in rows if _ymd(r["date"]) >= since]

    trades_in = in_window(data["trades"])
    cashtx_in = in_window(data["deposits_withdrawals"])
    transfers_in = in_window(data["transfers"])
    income_in = in_window(data["income"])
    return {
        "trades":    {"queried": True, "window": window, "rows": len(trades_in)},
        "cashtx":    {"queried": True, "window": window, "rows": len(cashtx_in)},
        "transfers": {"queried": True, "window": window, "rows": len(transfers_in)},
        "income":    {"queried": True, "window": window, "rows": len(income_in)},
        "positions": {"queried": True, "window": "as-of-end", "rows": len(data["positions"])},
        "cash":      {"queried": True, "window": "as-of-end", "rows": len(data["cash"])},
    }


def main():
    ap = argparse.ArgumentParser(description="Pull & normalize an IBKR Activity Flex statement (read-only).")
    ap.add_argument("--raw", action="store_true", help="print full normalized JSON")
    ap.add_argument("--out", help="write normalized JSON to this path")
    ap.add_argument("--flows", metavar="SINCE", help="instead of balances, net trades + cash deposits/withdrawals since YYYY-MM-DD")
    args = ap.parse_args()

    load_env()
    token, qid = os.environ.get("IBKR_FLEX_TOKEN"), os.environ.get("IBKR_FLEX_QUERY_ID")
    if not token or not qid:
        raise SystemExit("set IBKR_FLEX_TOKEN and IBKR_FLEX_QUERY_ID in tools/.env (copy tools/.env.example)")

    data = parse(flex_fetch(token, qid))
    data["snapshot_items"] = to_snapshot_items(data)

    if args.flows:
        since = _ymd(args.flows)  # YYYYMMDD; parse() already canonicalizes row dates, _ymd is belt-and-suspenders
        trades_in = [t for t in data["trades"] if _ymd(t["date"]) >= since]
        cashtx_in = [r for r in data["deposits_withdrawals"] if _ymd(r["date"]) >= since]
        transfers_in = [tr for tr in data["transfers"] if _ymd(tr["date"]) >= since]
        income_in = [r for r in data["income"] if _ymd(r["date"]) >= since]

        net = defaultdict(float)
        for t in trades_in:
            net[t["symbol"]] += t["proceeds"]  # sells +cash, buys -cash
        print(f"IBKR trades since {args.flows} (net proceeds per symbol; + = sold to cash, - = bought):")
        for sym in sorted(net, key=lambda s: net[s]):
            if abs(net[sym]) >= 0.5:
                print(f"  {sym:8} {net[sym]:>+12,.2f}  ({'sold→USD Cash' if net[sym] > 0 else 'USD Cash→bought'})")

        print(f"\nIBKR cash deposits/withdrawals since {args.flows}:")
        for r in cashtx_in:
            print(f"  {r['amount']:>+12,.2f} {r['currency']:4} [{_ymd(r['date'])}] {r['type']}  {r.get('desc') or ''}")

        # Transfers (ACATS / position & cash moves between accounts). direction IN/OUT;
        # positionAmount is the asset value moved, cashAmount the cash leg (if any).
        print(f"\nIBKR transfers since {args.flows}:")
        for tr in transfers_in:
            sign = "+" if (tr.get("direction") or "").upper() == "IN" else "-"
            amt = tr["cashAmount"] if abs(tr.get("cashAmount") or 0) >= 0.005 else tr["amount"]
            print(f"  {sign}{abs(amt):>11,.2f} {tr['currency'] or '':4} [{_ymd(tr['date'])}] "
                  f"{(tr.get('direction') or '?'):3} {tr.get('symbol') or tr.get('type') or ''}")

        # Cash-affecting income: interest credited + dividends paid to cash (not reinvested),
        # net of withholding tax. Performance, not a flow — but it moves the cash balance.
        print(f"\nIBKR cash income since {args.flows} (net of tax):")
        income_by_type = defaultdict(float)
        for r in income_in:
            income_by_type[r["type"]] += r["amount"]
        for typ in sorted(income_by_type):
            if abs(income_by_type[typ]) >= 0.005:
                print(f"  {income_by_type[typ]:>+12,.2f}  {typ}")

        manifest = flows_manifest(data, args.flows)
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
