# Investment Portfolio Tracker

A lightweight web application for tracking an investment portfolio. Designed for hosting on **GitHub Pages** (or any other static hosting) with no backend required.

---

## 📈 Key Features

### 1. Month-over-Month Comparison
The primary purpose is to show how the portfolio has changed compared to the previous month.
*   **Delta Indicators:** Shows the change for each asset and category (`+500$`, `-2%`).
*   **Toggle Mode (`% / $`):** Clicking the indicator toggles between percentage and dollar display.
*   **Ghost Rows:** If an asset was sold (balance 0), it is displayed in gray to preserve context. **Dev note:** Assets are identified by a composite key `categoryId_source_name` for correct MoM comparison and "ghost" row display.
*   **NEW Badge:** New assets are marked with a special badge.

### 2. Transfers
The application can apply transactions to snapshots on the fly. Transfers **do not recalculate** asset balances (Snapshot is the Source of Truth); they only serve for PnL calculations and UI annotations.
*   **Virtual Balances:** If you add a transaction in `transfers-*.json`, the app will show a "virtual" balance (what the amount would be accounting for this operation) and mark the value with an asterisk `*`. The `annotateTransfers` function adds UI indicators to existing assets in memory but does not create artificial duplicates.
*   **Operation types:**
    *   `deposit` — Funds deposited (increases the investment base).
    *   `withdraw` — Funds withdrawn.
    *   `move` — Transfer between categories (does not affect total capital).

### 3. Forecasting
The "Performance & Forecast" section calculates honest investment returns (Return on Invested Capital).
*   **Simple Return:** `Profit / (Start Balance + Deposits)`.
*   **Zero Kilometer:** The first month always shows 0% return and $0 profit. The entire balance is treated as a deposit.
*   **YTD & Annual Proj:** Compound cumulative return since the beginning of the year and annual projection.
*   **Toggle:** Click toggles display between `%` (return) and `$` (actual profit).

---

## 🏗 Data Structure

### 1. Categories (`data/categories.json`)
A global file defining categories (names, colors, order).
*   Allows changing colors and category names across the entire portfolio history.
*   Defines the sort order on the chart and in the list (Order 1 -> 5).

### 2. Monthly Snapshots (`data/YYYY-MM.json`)
A portfolio snapshot at the end of the month. Asset balances are fixed in these files and serve as the absolute source of truth.
```json
{
  "portfolio": [
    { "id": "crypto", "items": [{ "name": "BTC", "val": 50000 }] }
  ]
}
```

### 3. Transfers (`data/transfers-*.json`)
Splitting into multiple files is supported (e.g., `transfers-2026-02.json`, `transfers-2026-02-15.json`, etc.).
**Dev note:** Transfer assignment to a month is based strictly on the date inside the file (`meta.date`), not on file names. There are **two filtering modes**: annotations (`*`) and MoM comparisons use the interval between snapshot dates, while the top stats bar (P&L, Net Flow, Deposits, Withdrawals) filters transfers by **calendar month** (`YYYY-MM-01` — `YYYY-MM-31`).

---

## 🛠 Technical Details
*   **Zero Dependencies:** Vanilla JS + Chart.js (CDN) only.
*   **Hot-reload:** Data is loaded via `fetch` with cache-busting (to prevent caching of old JSON files).
*   **GitHub Pages Ready:** Just push the code to a repository, and it will work.
*   **Code Strictness:** Auto-formatting (Prettier/ESLint) is **prohibited** in this project. Any code changes must be made with the smallest possible diff, without altering the existing style.

## 🚀 Running Locally

A local web server is required for `fetch` to work:

```bash
# Python
python3 -m http.server 8006

# Node.js
npx http-server -p 8006
```

Open `http://localhost:8006`.

## 🔒 Private Data Source (GitHub API)

You can deploy the frontend to public GitHub Pages while storing data in a **separate private repository**.

### Setup
1.  **Create a private repository** (e.g., `finances-data`).
2.  Move the `data/` folder there (the structure must be preserved).
3.  **Create a Personal Access Token (Classic):**
    *   Settings -> Developer Settings -> Personal access tokens -> Tokens (classic).
    *   Generate new token.
    *   Scope: `repo` (Full control of private repositories).
    *   **Important:** Copy the token — it is shown only once.
4.  **In the application:**
    *   Click the **Settings** ⚙️ icon.
    *   Select **Remote (GitHub)**.
    *   Enter the token, Owner (your username), Repo (`finances-data`), Branch (`main`), and Path (`data`).
    *   Click **Save**.

⚠️ **Security:** The token is stored **only in localStorage** of your browser. It is not transmitted anywhere except to the GitHub API. Use this only on personal devices.
