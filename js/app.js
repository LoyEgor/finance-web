/**
 * Investment Portfolio Tracker
 * Main Application Logic
 */

// ===========================================
// CONFIGURATION
// ===========================================
const START_DATE = new Date('2026-01-01');

const MONTH_NAMES = [
    'January', 'February', 'March', 'April', 'May', 'June',
    'July', 'August', 'September', 'October', 'November', 'December'
];

// ===========================================
// GLOBAL STATE
// ===========================================
let portfolioChart = null;
let performanceChart = null;
let chartMode = 'category'; // 'category' or 'source'
let perfViewMode = 'chart'; // 'chart' or 'table'
let alphaBenchmark = 'VOO'; // 'VOO' or 'VT' — α reference for the table
let tableSortKey = null;    // null | 'ytd' | 'vol' | 'alpha' | 'sharpe'
let tableSortDir = 'desc';
let lastPerfStats = null;
// Set of bucket ids visible in the table. Stocks-related on by default.
let enabledTableBuckets = null;
let currentMonthId = null;
let availableMonths = [];
let currentPortfolioData = null;
let currentSnapshot = null;
let previousSnapshot = null;
let currentComparison = null;
let globalCategories = {}; // Unified Category Definitions

// Cross-month context preservation (session only).
// openCategoryIds tracks which <details> the user has expanded so they stay
// expanded after a swipe / arrow navigation. pendingScrollAnchor remembers
// which category was at viewport top + intra-category offset, so the same
// category lands at the same position in the new month's render.
const openCategoryIds = new Set();
let pendingScrollAnchor = null; // { catId, offset } | null

// Source colours come from CSS custom properties (--source-binance, etc.) so
// the donut chart segments and the badge backgrounds stay in sync — changing
// the value in styles.css updates both.
function getSourceColor(source) {
    if (!source) return null;
    const key = '--source-' + source.toLowerCase().replace(/[^a-z0-9]/g, '-');
    const val = getComputedStyle(document.documentElement).getPropertyValue(key).trim();
    return val || null;
}

// ===========================================
// ETF CLASSIFICATION (UI-only subgrouping for stocks)
// ===========================================
// Region naming convention — three coexisting layers (prefixed, never truncated):
//   ETF_REGION values:        'us' / 'europe' / 'asia'
//   classifyStockItem output: 'etf_us' / 'etf_europe' / 'etf_asia'
//   yields/balances keys:     'stocks_etf_us' / 'stocks_etf_europe' / 'stocks_etf_asia'
//   perf-table row ids:       same as classifyStockItem (etf_us / etf_europe / etf_asia)
// Use the full word — never 'eu' for 'europe'.
const ETF_REGION = {
    // US
    VOO: 'us', SPY: 'us', IVV: 'us', SPLG: 'us', VTI: 'us', ITOT: 'us', SCHB: 'us', RSP: 'us',
    QQQ: 'us', QQQM: 'us', DIA: 'us',
    VUG: 'us', IWF: 'us', SCHG: 'us',
    VTV: 'us', IWD: 'us', SCHV: 'us',
    VYM: 'us', SCHD: 'us', DGRO: 'us', HDV: 'us',
    IWM: 'us', VB: 'us', IJR: 'us', VO: 'us', IJH: 'us', SCHM: 'us',
    XLK: 'us', VGT: 'us', SOXX: 'us', SMH: 'us', PPA: 'us', SPMO: 'us',
    // Global (merged into us)
    VT: 'us', ACWI: 'us', VEA: 'us', IEFA: 'us', VWO: 'us', IEMG: 'us', EEM: 'us',
    CSPX: 'us', SXR8: 'us', SWDA: 'us', IWDA: 'us', EUNL: 'us', EIMI: 'us', VWCE: 'us',
    // Europe
    MEUD: 'europe', EXSA: 'europe', IMEU: 'europe', EUNK: 'europe', VGK: 'europe', IEV: 'europe', EZU: 'europe', FEZ: 'europe',
    VUKE: 'europe', ISF: 'europe', CSUK: 'europe', SXR3: 'europe', EWU: 'europe',
    // Asia
    AAXJ: 'asia', VPL: 'asia',
    FXI: 'asia', MCHI: 'asia', KWEB: 'asia', CQQQ: 'asia', FXC: 'asia', CBUK: 'asia',
    EWJ: 'asia', DXJ: 'asia', TPXE: 'asia', SXRZ: 'asia', VJPA: 'asia',
    EWY: 'asia', CSKR: 'asia',
    EWT: 'asia', INDA: 'asia', EPI: 'asia'
};

function classifyStockItem(name) {
    const ticker = (name || '').toUpperCase().trim().replace(/[.\s]/g, '');
    const region = ETF_REGION[ticker];
    if (region === 'us') return 'etf_us';
    if (region === 'europe') return 'etf_europe';
    if (region === 'asia') return 'etf_asia';
    return 'companies';
}

// ===========================================
// UTILITY: Escape HTML
// ===========================================
// Use whenever interpolating user-controlled data (asset names, category titles,
// transfer fields, error messages) into innerHTML template strings. Hardcoded
// HTML fragments and numeric/formatted values don't need it.
const _escapeHtmlMap = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };
function escapeHtml(value) {
    if (value === null || value === undefined) return '';
    return String(value).replace(/[&<>"']/g, ch => _escapeHtmlMap[ch]);
}

// ===========================================
// UTILITY: Format currency
// ===========================================
function formatMoney(value) {
    const sign = value >= 0 ? '' : '-';
    return sign + Math.abs(value).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 }) + ' $';
}

// Short money for compact tiles: "2.8k", "62.8k", "1.23M" (no currency symbol).
// Values under 1k keep 2-decimal precision.
function formatMoneyShort(value) {
    const abs = Math.abs(value);
    const sign = value < 0 ? '-' : '';
    if (abs < 1000) {
        return sign + abs.toLocaleString('en-US', { minimumFractionDigits: 0, maximumFractionDigits: 2 });
    }
    if (abs < 1_000_000) {
        return sign + (abs / 1000).toFixed(1) + 'k';
    }
    if (abs < 1_000_000_000) {
        return sign + (abs / 1_000_000).toFixed(2) + 'M';
    }
    return sign + (abs / 1_000_000_000).toFixed(2) + 'B';
}

// ===========================================
// UTILITY: Get Badge Class
// ===========================================
function getBadgeClass(source) {
    if (!source) return 'b-default';
    // Clean string: "GOOGLe Token" -> "b-GOOGLe-token"
    return 'b-' + source.toLowerCase().replace(/[^a-z0-9]/g, '-');
}

// ===========================================
// UTILITY: Asset Key Generator (Composite Key)
// ===========================================
function getAssetKey(categoryId, source, name) {
    return `${categoryId}_${source}_${name}`.toLowerCase().replace(/\s+/g, '_');
}

// ===========================================
// UTILITY: Build Normalized Snapshot
// ===========================================
function buildSnapshot(portfolioData, transfers = []) {
    const snapshot = {
        total: 0,
        categories: {},
        assetMap: {}
    };

    if (!portfolioData || !portfolioData.portfolio) return snapshot;

    // First pass: build asset map and calculate totals
    portfolioData.portfolio.forEach(cat => {
        const catData = {
            id: cat.id,
            title: cat.title,
            color: cat.color,
            total: 0,
            items: {}
        };

        cat.items.forEach(item => {
            const key = getAssetKey(cat.id, item.source, item.name);
            const assetData = {
                key,
                categoryId: cat.id,
                source: item.source,
                name: item.name,
                val: item.val,
                originalVal: item.originalVal ?? item.val,
                isVirtual: item.isVirtual || false,
                adjustment: item.adjustment || 0
            };

            catData.items[key] = assetData;
            catData.total += item.val;
            snapshot.assetMap[key] = assetData;
        });

        snapshot.categories[cat.id] = catData;
        snapshot.total += catData.total;
    });

    return snapshot;
}

// ===========================================
// UTILITY: Get Adjustments Per Asset from Transfers
// ===========================================
function getAdjustmentsPerAsset(transfers, portfolioData) {
    const adjustments = {}; // key -> { deposits: n, withdraws: n, net: n }

    if (!transfers || !portfolioData) return adjustments;

    transfers.forEach(t => {
        if (t.type === 'deposit') {
            const key = getAssetKey(t.category, t.source, t.name);
            if (!adjustments[key]) adjustments[key] = { deposits: 0, withdraws: 0, net: 0 };
            adjustments[key].deposits += t.amount;
            adjustments[key].net += t.amount;
        } else if (t.type === 'withdraw') {
            const key = getAssetKey(t.category, t.source, t.name);
            if (!adjustments[key]) adjustments[key] = { deposits: 0, withdraws: 0, net: 0 };
            adjustments[key].withdraws += t.amount;
            adjustments[key].net -= t.amount;
        } else if (t.type === 'move') {
            // From = withdraw, To = deposit
            const fromKey = getAssetKey(t.from_category, t.from_source, t.from_name);
            const toKey = getAssetKey(t.to_category, t.to_source, t.to_name);

            if (!adjustments[fromKey]) adjustments[fromKey] = { deposits: 0, withdraws: 0, net: 0 };
            adjustments[fromKey].withdraws += t.amount;
            adjustments[fromKey].net -= t.amount;

            if (!adjustments[toKey]) adjustments[toKey] = { deposits: 0, withdraws: 0, net: 0 };
            adjustments[toKey].deposits += t.amount;
            adjustments[toKey].net += t.amount;
        }
    });

    return adjustments;
}

// ===========================================
// MOM COMPARISON: Calculate Delta with Adjusted Start
// ===========================================
function calculateDelta(currentVal, previousVal, adjustment = 0) {
    // Adjusted Start = Previous + Net Adjustment (deposits add, withdraws subtract)
    const adjustedStart = previousVal + adjustment;
    const delta = currentVal - adjustedStart;

    // Percent change relative to adjusted start
    let percent = 0;
    if (adjustedStart > 0) {
        percent = (delta / adjustedStart) * 100;
    } else if (currentVal > 0 && adjustedStart === 0) {
        // New asset with only new deposits - no real gain
        percent = 0;
    }

    return {
        delta,
        percent,
        adjustedStart,
        previousVal,
        currentVal
    };
}

// ===========================================
// MOM COMPARISON: Compare Two Snapshots
// ===========================================
function compareSnapshots(currentSnapshot, previousSnapshot, adjustments = {}) {
    const comparison = {
        portfolio: { delta: 0, percent: 0 },
        categories: {},
        assets: {}
    };

    // Get all unique asset keys from both snapshots
    const allAssetKeys = new Set([
        ...Object.keys(currentSnapshot.assetMap),
        ...Object.keys(previousSnapshot?.assetMap || {})
    ]);

    // Compare each asset
    allAssetKeys.forEach(key => {
        const current = currentSnapshot.assetMap[key];
        const previous = previousSnapshot?.assetMap?.[key];
        const adj = adjustments[key]?.net || 0;

        const currentVal = current?.val || 0;
        const previousVal = previous?.val || 0;

        const deltaInfo = calculateDelta(currentVal, previousVal, adj);

        // Determine status
        let status = 'normal';
        if (currentVal > 0 && !previous) {
            status = 'new'; // New asset
        } else if (currentVal === 0 && previousVal > 0) {
            status = 'ghost'; // Sold/exited
        } else if (currentVal === 0 && previousVal === 0) {
            status = 'hidden'; // Never existed meaningfully
        }

        comparison.assets[key] = {
            ...deltaInfo,
            status,
            categoryId: current?.categoryId || previous?.categoryId,
            source: current?.source || previous?.source,
            name: current?.name || previous?.name
        };
    });

    // Aggregate by category
    const allCatIds = new Set([
        ...Object.keys(currentSnapshot.categories),
        ...Object.keys(previousSnapshot?.categories || {})
    ]);

    allCatIds.forEach(catId => {
        const currentCat = currentSnapshot.categories[catId];
        const previousCat = previousSnapshot?.categories?.[catId];

        // Sum adjustments for this category
        let catAdjustment = 0;
        Object.entries(adjustments).forEach(([key, adj]) => {
            if (key.startsWith(catId + '_')) {
                catAdjustment += adj.net;
            }
        });

        const currentTotal = currentCat?.total || 0;
        const previousTotal = previousCat?.total || 0;

        comparison.categories[catId] = calculateDelta(currentTotal, previousTotal, catAdjustment);
    });

    // Portfolio level
    const totalAdjustment = Object.values(adjustments).reduce((sum, adj) => sum + adj.net, 0);
    comparison.portfolio = calculateDelta(
        currentSnapshot.total,
        previousSnapshot?.total || 0,
        totalAdjustment
    );

    return comparison;
}

// ===========================================
// TOGGLE FORECAST (Percent <-> Money)
// ===========================================
window.toggleForecast = function (e, el) {
    e.preventDefault();
    e.stopPropagation();

    const currentMode = el.getAttribute('data-mode') || 'percent';
    const newMode = currentMode === 'percent' ? 'money' : 'percent';

    el.setAttribute('data-mode', newMode);

    let displayStr;
    if (newMode === 'money') {
        displayStr = el.getAttribute('data-money');
    } else {
        displayStr = el.getAttribute('data-pct');
    }
    el.innerText = displayStr;

    // Update color based on displayed value sign
    el.classList.remove('perf-positive', 'perf-negative');
    if (displayStr && displayStr.startsWith('+')) el.classList.add('perf-positive');
    else if (displayStr && displayStr.startsWith('-')) el.classList.add('perf-negative');
};

// ===========================================
// UPDATE FORECAST UI (Forecasting 2.0)
// ===========================================
function updateForecastUI(stats) {
    const section = document.getElementById('forecast-section');

    if (!stats || !section) {
        if (section) section.style.display = 'none';
        return;
    }

    section.style.display = 'block';

    // Format helpers
    const fmtPct = (val) => {
        const sign = val >= 0 ? '+' : '';
        return `${sign}${(val * 100).toFixed(2)}%`;
    };

    const fmtMoney = (val) => {
        const sign = val >= 0 ? '+' : '';
        return `${sign}${formatMoneyShort(val)}`;
    };

    // Update values
    // Assuming HTML has ids: forecast-month, forecast-ytd, forecast-annual
    const monthEl = document.getElementById('forecast-month');
    const ytdEl = document.getElementById('forecast-ytd');
    const annualEl = document.getElementById('forecast-annual');

    const updateEl = (el, pctVal, moneyVal) => {
        if (!el) return;

        // Save values
        const pctStr = fmtPct(pctVal);
        const moneyStr = fmtMoney(moneyVal);

        el.setAttribute('data-pct', pctStr);
        el.setAttribute('data-money', moneyStr);
        el.setAttribute('onclick', 'toggleForecast(event, this)');

        // Maintain current mode if already set, else default to percent
        const mode = el.getAttribute('data-mode') || 'percent';
        el.innerText = mode === 'money' ? moneyStr : pctStr;
        el.style.cursor = 'pointer';
        // el.style.textDecoration = 'underline dotted'; // Removed per user request

        // Colorize (using pctVal logic primarily as money sign follows it usually)
        el.className = 'forecast-value';
        if (pctVal > 0) el.classList.add('perf-positive');
        else if (pctVal < 0) el.classList.add('perf-negative');
    };

    updateEl(monthEl, stats.monthYield, stats.monthProfit);
    updateEl(ytdEl, stats.ytd, stats.ytdProfit);
    updateEl(annualEl, stats.projected, stats.projectedProfit);

    if (monthEl) {
        const invested = stats.currentMonthStart + stats.currentMonthDeposits;
        monthEl.parentElement.title = `Profit relative to invested capital this period\n\n${fmtMoney(stats.monthProfit)} / (${fmtMoney(stats.currentMonthStart)} + ${fmtMoney(stats.currentMonthDeposits)})\n= ${fmtMoney(stats.monthProfit)} / ${fmtMoney(invested)}\n= ${(stats.monthYield * 100).toFixed(2)}%`;
    }
    if (ytdEl) {
        const factors = stats.yields.map(y => `(1 + ${(y * 100).toFixed(2)}%)`).join(' × ');
        ytdEl.parentElement.title = `Compounded return since Jan 1st\n\n${factors} - 1\n= ${(stats.ytd * 100).toFixed(2)}%\nYTD P&L: ${fmtMoney(stats.currentEndBalance)} - ${fmtMoney(stats.accumulatedNetFlow)} = ${fmtMoney(stats.ytdProfit)}`;
    }
    if (annualEl) {
        annualEl.parentElement.title = `Projected annual return if current YTD pace continues\n\n(1 + ${(stats.ytd * 100).toFixed(2)}%)^(12/${stats.monthsPassed}) - 1\n= ${(stats.projected * 100).toFixed(2)}%`;
    }

    // --- Benchmark comparison ---
    const benchDepositEl = document.getElementById('bench-deposit');
    const benchVtEl = document.getElementById('bench-vt');
    const benchVooEl = document.getElementById('bench-voo');

    const updateBench = (el, pctVal, moneyVal) => {
        if (!el) return;
        if (pctVal === null || pctVal === undefined) {
            el.innerText = '—';
            el.className = 'forecast-value';
            el.removeAttribute('onclick');
            el.style.cursor = '';
            el.removeAttribute('data-mode');
            return;
        }

        const pctStr = fmtPct(pctVal);
        const moneyStr = fmtMoney(moneyVal);

        el.setAttribute('data-pct', pctStr);
        el.setAttribute('data-money', moneyStr);
        el.setAttribute('onclick', 'toggleForecast(event, this)');

        const mode = el.getAttribute('data-mode') || 'percent';
        el.setAttribute('data-mode', mode);
        el.innerText = mode === 'money' ? moneyStr : pctStr;
        el.style.cursor = 'pointer';

        el.className = 'forecast-value';
        if (pctVal > 0) el.classList.add('perf-positive');
        else if (pctVal < 0) el.classList.add('perf-negative');
    };

    if (stats.benchmarks) {
        const b = stats.benchmarks;
        updateBench(benchDepositEl, b.deposit, b.depositMoney);
        updateBench(benchVtEl, b.vt, b.vtMoney);
        updateBench(benchVooEl, b.voo, b.vooMoney);

        const investedStr = formatMoney(stats.currentMonthStart + stats.currentMonthDeposits);

        if (benchDepositEl) benchDepositEl.parentElement.title = `Deposit 3.5% yield: ${fmtPct(b.deposit)}\nPotential profit: ${fmtPct(b.deposit)} * ${investedStr} = ${fmtMoney(b.depositMoney)}`;
        if (benchVtEl) benchVtEl.parentElement.title = `VT (Market) yield: ${fmtPct(b.vt)}\nPotential profit: ${fmtPct(b.vt)} * ${investedStr} = ${fmtMoney(b.vtMoney)}`;
        if (benchVooEl) benchVooEl.parentElement.title = `VOO (S&P 500) yield: ${fmtPct(b.voo)}\nPotential profit: ${fmtPct(b.voo)} * ${investedStr} = ${fmtMoney(b.vooMoney)}`;
    } else {
        updateBench(benchDepositEl, null);
        updateBench(benchVtEl, null);
        updateBench(benchVooEl, null);
    }

    // --- Projection row ---
    const projAvgYieldEl = document.getElementById('proj-avg-yield');
    const projAvgNetflowEl = document.getElementById('proj-avg-netflow');
    const projYearEndEl = document.getElementById('proj-year-end');

    const p = stats.projection;
    if (p) {
        // Avg Yield %
        if (projAvgYieldEl) {
            projAvgYieldEl.innerText = fmtPct(p.avgYield);
            projAvgYieldEl.className = 'forecast-value';
            if (p.avgYield > 0) projAvgYieldEl.classList.add('perf-positive');
            else if (p.avgYield < 0) projAvgYieldEl.classList.add('perf-negative');
            const samples = Math.max(0, stats.monthsPassed - 1);
            projAvgYieldEl.parentElement.title = `Average monthly yield across ${samples} month(s) (excluding Jan baseline).\nUsed to project the portfolio forward to year-end.`;
        }

        // Avg Net Flow $
        if (projAvgNetflowEl) {
            projAvgNetflowEl.innerText = fmtMoney(p.avgNetFlow);
            projAvgNetflowEl.className = 'forecast-value';
            if (p.avgNetFlow > 0) projAvgNetflowEl.classList.add('perf-positive');
            else if (p.avgNetFlow < 0) projAvgNetflowEl.classList.add('perf-negative');
            projAvgNetflowEl.parentElement.title = `Average monthly net flow (deposits - withdrawals) excluding Jan baseline.\nApplied each remaining month in the year-end projection.`;
        }

        // Year-End Total: default $ balance, toggle to % growth from now
        if (projYearEndEl) {
            const pctStr = fmtPct(p.projectedGrowthFromNow);
            const moneyStr = formatMoneyShort(p.projectedEndBalance);

            projYearEndEl.setAttribute('data-pct', pctStr);
            projYearEndEl.setAttribute('data-money', moneyStr);
            projYearEndEl.setAttribute('onclick', 'toggleForecast(event, this)');

            const mode = projYearEndEl.getAttribute('data-mode') || 'money';
            projYearEndEl.setAttribute('data-mode', mode);
            projYearEndEl.innerText = mode === 'money' ? moneyStr : pctStr;
            projYearEndEl.style.cursor = 'pointer';

            projYearEndEl.className = 'forecast-value';
            if (p.projectedGrowthFromNow > 0) projYearEndEl.classList.add('perf-positive');
            else if (p.projectedGrowthFromNow < 0) projYearEndEl.classList.add('perf-negative');

            projYearEndEl.parentElement.title = `Projected year-end portfolio value.\n\nStart: ${formatMoney(stats.currentEndBalance)}\n+${p.remainingMonths} month(s) × (yield ${(p.avgYield * 100).toFixed(2)}% + net flow ${fmtMoney(p.avgNetFlow)})\n= ${formatMoney(p.projectedEndBalance)}\n\nGrowth from now: ${fmtPct(p.projectedGrowthFromNow)}\nProjected P&L: ${fmtMoney(p.projectedEndPnL)} (ROI ${fmtPct(p.projectedTotalROI)})`;
        }
    } else {
        [projAvgYieldEl, projAvgNetflowEl, projYearEndEl].forEach(el => {
            if (!el) return;
            el.innerText = '—';
            el.className = 'forecast-value';
            el.removeAttribute('onclick');
            el.style.cursor = '';
        });
    }
}


// 1. Fetch data for all months from Jan to current
async function fetchYearSequence(currentMonthId) {
    const year = currentMonthId.split('-')[0];
    const monthIndex = parseInt(currentMonthId.split('-')[1]);

    // Fetch in parallel
    const promises = [];
    for (let m = 1; m <= monthIndex; m++) {
        const id = `${year}-${String(m).padStart(2, '0')}`;
        promises.push(Promise.all([
            fetchPortfolioData(id),
            fetchTransfersForMonth(id)
        ]).then(([data, transferGroups]) => ({
            id,
            data,
            transferGroups // Preserve groups with dates for date-based filtering
        })));
    }

    const results = await Promise.all(promises);

    // Sort by id ensures Jan -> Feb -> ...
    return results.sort((a, b) => a.id.localeCompare(b.id));
}

// Simple Yield: profit relative to invested capital.
// Yield = (End - (Start + Deposits - Withdraws)) / (Start + Deposits)
function calculateSimpleYield(startBalance, endBalance, transfers) {
    let deposits = 0;
    let withdraws = 0;

    if (transfers) {
        transfers.forEach(t => {
            if (t.type === 'deposit') deposits += t.amount;
            if (t.type === 'withdraw') withdraws += t.amount;
        });
    }

    const netFlow = deposits - withdraws;
    const profit = endBalance - (startBalance + netFlow);
    const investedCapital = startBalance + deposits;

    // Protection against division by zero
    if (investedCapital <= 0.01) {
        // If no capital was invested, but there is profit?
        // Edge case: Air dropped tokens with 0 cost basis.
        // Cap calculation or return 0? 
        if (profit > 0) return 1.0;
        return 0;
    }

    return profit / investedCapital;
}

// 3. YTD Calculation (Compounding)
// YTD = (1 + r1) * (1 + r2) * ... - 1
function calculateYTD(yields) {
    let compounded = 1;
    yields.forEach(r => {
        compounded *= (1 + r);
    });
    return compounded - 1;
}

// 4. Projected Annual
// Annual = (1 + YTD)^(12/n) - 1
// Uses the YTD up to the current month n
function calculateProjectedAnnual(ytd, monthsPassed) {
    if (monthsPassed <= 0) return 0;
    const exponent = 12 / monthsPassed;
    const base = 1 + ytd;

    // Safety check for negative base (total loss > 100%)
    if (base <= 0) return -1;

    return Math.pow(base, exponent) - 1;
}

// Helper: build per-category transfer list (includes moves as deposit/withdraw)
function buildCategoryTransfers(transfers, catId) {
    const result = [];
    transfers.forEach(t => {
        if (t.type === 'deposit' && t.category === catId) {
            result.push({ type: 'deposit', amount: t.amount });
        } else if (t.type === 'withdraw' && t.category === catId) {
            result.push({ type: 'withdraw', amount: t.amount });
        } else if (t.type === 'move') {
            if (t.from_category === catId) result.push({ type: 'withdraw', amount: t.amount });
            if (t.to_category === catId) result.push({ type: 'deposit', amount: t.amount });
        }
    });
    return result;
}

// Sub-bucket key for a stocks item (companies vs ETF region).
function stockSubKeyFor(name) {
    const sub = classifyStockItem(name);
    if (sub === 'companies') return 'stocks_companies';
    if (sub === 'etf_us') return 'stocks_etf_us';
    if (sub === 'etf_europe') return 'stocks_etf_europe';
    if (sub === 'etf_asia') return 'stocks_etf_asia';
    return null;
}

// Build per-sub-bucket transfer lists for the stocks category.
// `stocks_etf` aggregates all three ETF regions.
function buildStockSubTransfersAll(transfers) {
    const out = {
        stocks_companies: [],
        stocks_etf_us: [],
        stocks_etf_europe: [],
        stocks_etf_asia: [],
        stocks_etf: []
    };
    const addTo = (key, type, amount) => {
        if (!out[key]) return;
        out[key].push({ type, amount });
        if (key !== 'stocks_companies') {
            out.stocks_etf.push({ type, amount });
        }
    };
    transfers.forEach(t => {
        if (t.type === 'deposit' && t.category === 'stocks') {
            const key = stockSubKeyFor(t.name);
            if (key) addTo(key, 'deposit', t.amount);
        } else if (t.type === 'withdraw' && t.category === 'stocks') {
            const key = stockSubKeyFor(t.name);
            if (key) addTo(key, 'withdraw', t.amount);
        } else if (t.type === 'move') {
            if (t.from_category === 'stocks') {
                const key = stockSubKeyFor(t.from_name);
                if (key) addTo(key, 'withdraw', t.amount);
            }
            if (t.to_category === 'stocks') {
                const key = stockSubKeyFor(t.to_name);
                if (key) addTo(key, 'deposit', t.amount);
            }
        }
    });
    return out;
}

// Helper: compound a list of monthly yields into cumulative series (null breaks compounding)
function compoundSeries(monthlyYields) {
    const result = [];
    let compound = 1;
    for (const y of monthlyYields) {
        if (y === null || y === undefined) {
            result.push(null);
        } else {
            compound *= (1 + y);
            result.push(compound - 1);
        }
    }
    return result;
}

// Sample standard deviation of a numeric array
function stdDev(arr) {
    if (!arr || arr.length < 2) return null;
    const n = arr.length;
    const mean = arr.reduce((s, v) => s + v, 0) / n;
    const variance = arr.reduce((s, v) => s + (v - mean) ** 2, 0) / (n - 1);
    return Math.sqrt(variance);
}

// Compute YTD / annualised vol / Sharpe-style ratio for a bucket from
// the stored monthly yields and compounded series.
// Risk-free rate: 3.5% annual (matches the Deposit benchmark used elsewhere).
function bucketStats(monthlyYields, compoundedSeries) {
    if (!monthlyYields || monthlyYields.length === 0) {
        return { ytd: 0, vol: null, sharpe: null, monthsTracked: 0 };
    }
    // Skip Jan zero-km baseline (idx=0) and any nulls.
    const real = monthlyYields.slice(1).filter(y => y !== null && y !== undefined && isFinite(y));
    const ytd = (compoundedSeries && compoundedSeries.length)
        ? (compoundedSeries[compoundedSeries.length - 1] ?? 0) : 0;
    const monthsPassed = monthlyYields.length;

    let vol = null;
    let sharpe = null;
    if (real.length >= 2) {
        const std = stdDev(real);
        if (std !== null && isFinite(std)) {
            vol = std * Math.sqrt(12);
            if (vol > 0.0005 && monthsPassed >= 2 && (1 + ytd) > 0) {
                const annualisedReturn = Math.pow(1 + ytd, 12 / monthsPassed) - 1;
                sharpe = (annualisedReturn - 0.035) / vol;
            }
        }
    }

    return { ytd, vol, sharpe, monthsTracked: real.length };
}

// 5. Orchestrator
// Memoization: keyed by currentMonthId, lives for the session. Wiped on reload
// (which is what settings-save triggers, so config changes implicitly invalidate).
// Invariant: `benchmarksData` is treated as immutable per session — if it ever
// becomes dynamic (e.g. user-editable benchmarks), include it in the cache key.
const _yearStatsCache = new Map();

async function calculateYearStats(currentMonthId, benchmarksData) {
    if (_yearStatsCache.has(currentMonthId)) {
        return _yearStatsCache.get(currentMonthId);
    }

    // 1. Fetch sequence of months from Jan up to currentMonthId
    const sequence = await fetchYearSequence(currentMonthId);

    const yields = [];
    const netFlows = []; // per-month net flow for averaging
    let currentMonthYield = 0;
    let currentMonthProfit = 0;
    let currentMonthStart = 0;
    let currentMonthEnd = 0;
    let currentMonthDeposits = 0;
    let currentMonthPrevDate = null;
    let currentMonthCurrDate = null;

    // Initialize Start Balance for Jan 1st as 0 (Assumption: Portfolio starts fresh or rollover not tracked yet)
    // In a real system, we would need the Dec 31st snapshot of previous year.
    let prevBalance = 0;
    let accumulatedNetFlow = 0; // To track invested capital for YTD profit

    // --- Series tracking ---
    const monthLabels = [];
    const categoryYields = {}; // catId -> [monthly yields]
    const benchmarkYields = { vt: [], voo: [], deposit: [] };
    // Stocks sub-buckets: companies / ETF regions / ETF total
    const SUB_STOCK_KEYS = ['stocks_companies', 'stocks_etf_us', 'stocks_etf_europe', 'stocks_etf_asia', 'stocks_etf'];
    const subStocksYields = {};
    SUB_STOCK_KEYS.forEach(k => { subStocksYields[k] = []; });
    let prevSubStocksBalances = {};
    SUB_STOCK_KEYS.forEach(k => { prevSubStocksBalances[k] = 0; });
    let prevCategoryBalances = {};

    // Collect ALL transfer groups across all months for date-based filtering
    const allTransferGroups = sequence.flatMap(item => item.transferGroups || []);

    // Iterate sequentially through months to build the chain
    let prevSnapshotDate = null;
    for (let idx = 0; idx < sequence.length; idx++) {
        const item = sequence[idx];

        // Month label (Jan, Feb, ...)
        const mIdx = parseInt(item.id.split('-')[1]) - 1;
        monthLabels.push(MONTH_NAMES[mIdx].substring(0, 3));

        if (!item.data) {
            yields.push(0);
            netFlows.push(0);
            Object.keys(categoryYields).forEach(catId => categoryYields[catId].push(0));
            SUB_STOCK_KEYS.forEach(k => subStocksYields[k].push(0));
            benchmarkYields.vt.push(null);
            benchmarkYields.voo.push(null);
            benchmarkYields.deposit.push(0);
            continue;
        }

        const endBalance = calculateTotalBalance(item.data);
        const startBalance = prevBalance;
        const currentSnapshotDate = item.data.meta?.date || item.id;

        // Date-based transfer filtering:
        // Keep transfers whose date falls in the current period
        // (after previous snapshot date, up to current snapshot date)
        const periodGroups = allTransferGroups.filter(g => {
            if (!prevSnapshotDate) return g.date <= currentSnapshotDate;
            return g.date > prevSnapshotDate && g.date <= currentSnapshotDate;
        });
        const monthTransfers = periodGroups.flatMap(g => g.transfers);

        // Calc Net Flow for this month
        let mDeposits = 0;
        let mWithdraws = 0;
        monthTransfers.forEach(t => {
            if (t.type === 'deposit') mDeposits += t.amount;
            if (t.type === 'withdraw') mWithdraws += t.amount;
        });
        const mNetFlow = mDeposits - mWithdraws;

        // Build current category balances
        const currCategoryBalances = {};
        item.data.portfolio.forEach(cat => {
            currCategoryBalances[cat.id] = cat.items.reduce((s, it) => s + it.val, 0);
        });

        // Build current stocks sub-bucket balances (companies / ETF regions / ETF total)
        const currSubStocksBalances = {
            stocks_companies: 0,
            stocks_etf_us: 0,
            stocks_etf_europe: 0,
            stocks_etf_asia: 0
        };
        const stocksCat = item.data.portfolio.find(c => c.id === 'stocks');
        if (stocksCat) {
            stocksCat.items.forEach(it => {
                const subKey = stockSubKeyFor(it.name);
                if (subKey) currSubStocksBalances[subKey] += it.val;
            });
        }
        currSubStocksBalances.stocks_etf =
            currSubStocksBalances.stocks_etf_us +
            currSubStocksBalances.stocks_etf_europe +
            currSubStocksBalances.stocks_etf_asia;

        // Ensure all seen categories have a series (zero-fill past months)
        const allCatIds = new Set([
            ...Object.keys(prevCategoryBalances),
            ...Object.keys(currCategoryBalances)
        ]);
        allCatIds.forEach(catId => {
            if (!categoryYields[catId]) {
                categoryYields[catId] = new Array(idx).fill(0);
            }
        });

        // Calculate Yield for this specific month
        let yieldVal = 0;
        let profitVal = 0;

        // ZERO KILOMETER LOGIC FOR FORECAST
        if (idx === 0) {
            yieldVal = 0;
            profitVal = 0;
            accumulatedNetFlow += endBalance;
            allCatIds.forEach(catId => categoryYields[catId].push(0));
            SUB_STOCK_KEYS.forEach(k => subStocksYields[k].push(0));
        } else {
            yieldVal = calculateSimpleYield(startBalance, endBalance, monthTransfers);
            profitVal = endBalance - (startBalance + mNetFlow);
            accumulatedNetFlow += mNetFlow;

            // Per-category yields
            allCatIds.forEach(catId => {
                const catStart = prevCategoryBalances[catId] || 0;
                const catEnd = currCategoryBalances[catId] || 0;
                if (catStart === 0 && catEnd === 0) {
                    categoryYields[catId].push(0);
                    return;
                }
                const catTransfers = buildCategoryTransfers(monthTransfers, catId);
                // If category appears fresh without tracked inflow, treat as 0 to avoid wild 100% spike
                const catDeposits = catTransfers.reduce((s, t) => s + (t.type === 'deposit' ? t.amount : 0), 0);
                if (catStart === 0 && catDeposits === 0 && catEnd > 0) {
                    categoryYields[catId].push(0);
                    return;
                }
                categoryYields[catId].push(calculateSimpleYield(catStart, catEnd, catTransfers));
            });

            // Per stocks sub-bucket yields (companies / ETF regions / ETF total)
            const subTransfersByKey = buildStockSubTransfersAll(monthTransfers);
            SUB_STOCK_KEYS.forEach(subKey => {
                const subStart = prevSubStocksBalances[subKey] || 0;
                const subEnd = currSubStocksBalances[subKey] || 0;
                if (subStart === 0 && subEnd === 0) {
                    subStocksYields[subKey].push(0);
                    return;
                }
                const subTrans = subTransfersByKey[subKey] || [];
                const subDeposits = subTrans.reduce((s, t) => s + (t.type === 'deposit' ? t.amount : 0), 0);
                if (subStart === 0 && subDeposits === 0 && subEnd > 0) {
                    subStocksYields[subKey].push(0);
                    return;
                }
                subStocksYields[subKey].push(calculateSimpleYield(subStart, subEnd, subTrans));
            });
        }

        // Benchmark monthly yields
        if (idx === 0 || !prevSnapshotDate) {
            benchmarkYields.vt.push(idx === 0 ? 0 : null);
            benchmarkYields.voo.push(idx === 0 ? 0 : null);
            benchmarkYields.deposit.push(0);
        } else {
            const d0 = new Date(prevSnapshotDate);
            const d1 = new Date(currentSnapshotDate);
            const daysDiff = (d1 - d0) / (1000 * 60 * 60 * 24);
            const depositMonth = Math.pow(1 + 0.035, daysDiff / 365) - 1;
            benchmarkYields.deposit.push(depositMonth);

            const vtPrices = benchmarksData?.['VT'] || {};
            const vooPrices = benchmarksData?.['VOO'] || {};
            const vtPrev = vtPrices[prevSnapshotDate];
            const vtCurr = vtPrices[currentSnapshotDate];
            const vooPrev = vooPrices[prevSnapshotDate];
            const vooCurr = vooPrices[currentSnapshotDate];
            benchmarkYields.vt.push((vtPrev && vtCurr) ? (vtCurr / vtPrev - 1) : null);
            benchmarkYields.voo.push((vooPrev && vooCurr) ? (vooCurr / vooPrev - 1) : null);
        }

        yields.push(yieldVal);
        netFlows.push(idx === 0 ? 0 : mNetFlow);

        if (item.id === currentMonthId) {
            currentMonthYield = yieldVal;
            currentMonthProfit = profitVal;
            currentMonthStart = startBalance;
            currentMonthEnd = endBalance;
            currentMonthDeposits = mDeposits;
            currentMonthPrevDate = prevSnapshotDate;
            currentMonthCurrDate = currentSnapshotDate;
        }

        // Prepare for next month
        prevSnapshotDate = currentSnapshotDate;
        prevBalance = endBalance;
        prevCategoryBalances = currCategoryBalances;
        prevSubStocksBalances = currSubStocksBalances;
    }

    // YTD is compounded yield of ALL months up to current
    const ytd = calculateYTD(yields);

    // YTD Profit: Current Balance - Invested Capital
    // Invested Capital = Accumulated Net Flow (using our Zero KM logic)
    const currentEndBalance = prevBalance;
    const ytdProfit = currentEndBalance - accumulatedNetFlow;

    // Projected Annual based on this YTD and the number of months passed
    const monthsPassed = sequence.length;
    const projected = calculateProjectedAnnual(ytd, monthsPassed);

    // Projected Profit: Extra money on top of current balance
    const projectedProfit = currentEndBalance * projected;

    // --- Projection (average-based, to end of year) ---
    // Skip idx=0 (zero kilometer baseline)
    const realYields = yields.slice(1);
    const realNetFlows = netFlows.slice(1);
    const avgYield = realYields.length > 0
        ? realYields.reduce((s, y) => s + y, 0) / realYields.length : 0;
    const avgNetFlow = realNetFlows.length > 0
        ? realNetFlows.reduce((s, n) => s + n, 0) / realNetFlows.length : 0;

    const remainingMonths = Math.max(0, 12 - monthsPassed);

    let projectedEndBalance = currentEndBalance;
    for (let m = 0; m < remainingMonths; m++) {
        projectedEndBalance = projectedEndBalance * (1 + avgYield) + avgNetFlow;
    }
    const projectedTotalInvested = accumulatedNetFlow + avgNetFlow * remainingMonths;
    const projectedEndPnL = projectedEndBalance - projectedTotalInvested;
    const projectedGrowthFromNow = currentEndBalance > 0
        ? (projectedEndBalance - currentEndBalance) / currentEndBalance : 0;
    const projectedTotalROI = projectedTotalInvested > 0
        ? projectedEndPnL / projectedTotalInvested : 0;

    const projection = {
        avgYield,
        avgNetFlow,
        remainingMonths,
        projectedEndBalance,
        projectedEndPnL,
        projectedGrowthFromNow,
        projectedTotalROI,
        projectedTotalInvested
    };

    // --- Cumulative series (per-month compound) ---
    const totalSeries = compoundSeries(yields);
    const categorySeries = {};
    Object.keys(categoryYields).forEach(catId => {
        categorySeries[catId] = compoundSeries(categoryYields[catId]);
    });
    const benchmarkSeries = {
        vt: compoundSeries(benchmarkYields.vt),
        voo: compoundSeries(benchmarkYields.voo),
        deposit: compoundSeries(benchmarkYields.deposit)
    };
    const subStocksSeries = {};
    SUB_STOCK_KEYS.forEach(k => {
        subStocksSeries[k] = compoundSeries(subStocksYields[k]);
    });

    // --- Performance comparison table data ---
    const performanceTable = {
        companies:    bucketStats(subStocksYields.stocks_companies,   subStocksSeries.stocks_companies),
        etf_total:    bucketStats(subStocksYields.stocks_etf,         subStocksSeries.stocks_etf),
        etf_us:       bucketStats(subStocksYields.stocks_etf_us,      subStocksSeries.stocks_etf_us),
        etf_europe:   bucketStats(subStocksYields.stocks_etf_europe,  subStocksSeries.stocks_etf_europe),
        etf_asia:     bucketStats(subStocksYields.stocks_etf_asia,    subStocksSeries.stocks_etf_asia),
        stocks_total: bucketStats(categoryYields.stocks || [],        categorySeries.stocks || []),
        vt:           bucketStats(benchmarkYields.vt,                 benchmarkSeries.vt),
        voo:          bucketStats(benchmarkYields.voo,                benchmarkSeries.voo)
    };
    // Other categories (Safe, Cash/USD, Crypto, Copytrading) — for the toggleable
    // "compare-with-anything" rows in the table.
    ['safe', 'usd', 'crypto', 'copy'].forEach(catId => {
        if (categoryYields[catId]) {
            performanceTable[catId] = bucketStats(categoryYields[catId], categorySeries[catId]);
        }
    });

    // Allocation within stocks (last-month balances)
    const lastStocksTotal = (prevCategoryBalances?.stocks) || 0;
    const allocations = lastStocksTotal > 0 ? {
        companies:    (prevSubStocksBalances.stocks_companies   || 0) / lastStocksTotal,
        etf_total:    (prevSubStocksBalances.stocks_etf         || 0) / lastStocksTotal,
        etf_us:       (prevSubStocksBalances.stocks_etf_us      || 0) / lastStocksTotal,
        etf_europe:   (prevSubStocksBalances.stocks_etf_europe  || 0) / lastStocksTotal,
        etf_asia:     (prevSubStocksBalances.stocks_etf_asia    || 0) / lastStocksTotal,
        stocks_total: 1
    } : null;

    // --- Benchmark calculations (monthly period) ---
    let benchmarks = null;
    if (benchmarksData && currentMonthPrevDate && currentMonthCurrDate) {
        // Deposit 3.5% annual: pro-rata for this period
        const d0 = new Date(currentMonthPrevDate);
        const d1 = new Date(currentMonthCurrDate);
        const daysDiff = (d1 - d0) / (1000 * 60 * 60 * 24);
        const depositMonth = Math.pow(1 + 0.035, daysDiff / 365) - 1;

        // ETF returns: price change between prev and current snapshot
        const vtPrices = benchmarksData['VT'] || {};
        const vooPrices = benchmarksData['VOO'] || {};

        const vtPrev = vtPrices[currentMonthPrevDate];
        const vtCurr = vtPrices[currentMonthCurrDate];
        const vooPrev = vooPrices[currentMonthPrevDate];
        const vooCurr = vooPrices[currentMonthCurrDate];

        const vtMonth = (vtPrev && vtCurr) ? (vtCurr / vtPrev - 1) : null;
        const vooMonth = (vooPrev && vooCurr) ? (vooCurr / vooPrev - 1) : null;

        const invested = currentMonthStart + currentMonthDeposits;

        benchmarks = {
            deposit: depositMonth,
            depositMoney: depositMonth * invested,
            vt: vtMonth,
            vtMoney: vtMonth !== null ? vtMonth * invested : null,
            voo: vooMonth,
            vooMoney: vooMonth !== null ? vooMonth * invested : null
        };
    }

    const result = {
        monthYield: currentMonthYield,
        monthProfit: currentMonthProfit,
        ytd: ytd,
        ytdProfit: ytdProfit,
        projected: projected,
        projectedProfit: projectedProfit,
        monthsPassed: monthsPassed,
        yields: yields,
        currentEndBalance: currentEndBalance,
        accumulatedNetFlow: accumulatedNetFlow,
        currentMonthStart: currentMonthStart,
        currentMonthEnd: currentMonthEnd,
        currentMonthDeposits: currentMonthDeposits,
        benchmarks: benchmarks,
        // Series for chart (real data only, no forecast extension)
        monthLabels: monthLabels,
        totalSeries: totalSeries,
        categorySeries: categorySeries,
        benchmarkSeries: benchmarkSeries,
        subStocksSeries: subStocksSeries,
        // Stocks comparison table data
        performanceTable: performanceTable,
        allocations: allocations,
        // Projection (used by forecast tiles, not by chart)
        projection: projection
    };
    _yearStatsCache.set(currentMonthId, result);
    return result;
}

// ===========================================
// GENERATE MONTH LIST
// ===========================================
// ===========================================
// GENERATE CANDIDATE MONTHS
// ===========================================
function generateCandidateMonths() {
    const months = [];
    const current = new Date(START_DATE.getFullYear(), START_DATE.getMonth(), 1);
    // Generate months until the end of the current year (December 31st)
    const endOfYear = new Date(START_DATE.getFullYear(), 11, 31); // Dec 31st

    while (current <= endOfYear) {
        const year = current.getFullYear();
        const month = current.getMonth();
        const id = `${year}-${String(month + 1).padStart(2, '0')}`;
        const label = `${MONTH_NAMES[month]} ${year}`;
        months.push({ id, label });
        current.setMonth(current.getMonth() + 1);
    }

    return months;
}

// ===========================================
// GET AVAILABLE MONTHS (VIA DATA SERVICE)
// ===========================================
async function getAvailableMonths() {
    try {
        return await dataService.getAvailableMonths(generateCandidateMonths);
    } catch (e) {
        console.error('Error getting available months:', e);
        showError('Failed to fetch the list of months. Check data source settings.');
        return [];
    }
}

// ===========================================
// TOGGLE PERCENT <-> MONEY
// ===========================================
window.toggleValue = function (e, btn) {
    e.preventDefault();
    e.stopPropagation();

    const currentMode = btn.getAttribute('data-mode');
    const newMode = currentMode === 'percent' ? 'money' : 'percent';

    btn.setAttribute('data-mode', newMode);
    btn.innerText = btn.getAttribute(newMode === 'money' ? 'data-money' : 'data-pct');
    // Background colour is driven from CSS via the data-mode attribute selector
    // so it follows the theme palette automatically.
};

// ===========================================
// TOGGLE DELTA (Percent <-> Money)
// ===========================================
window.toggleDelta = function (e, btn) {
    if (e) {
        e.preventDefault();
        e.stopPropagation();
    }

    const currentMode = btn.getAttribute('data-mode') || 'percent';
    const newMode = currentMode === 'percent' ? 'money' : 'percent';

    // Find container (category block)
    const container = btn.closest('.category-block');
    if (!container) return;

    // Find all deltas in this block (both header and items)
    const allDeltas = container.querySelectorAll('.delta');

    allDeltas.forEach(el => {
        // Update mode
        el.setAttribute('data-mode', newMode);

        // Update text
        if (newMode === 'money') {
            el.innerText = el.getAttribute('data-money-delta');
        } else {
            el.innerText = el.getAttribute('data-pct-delta');
        }
    });
};

// ===========================================
// SHOW CALCULATION TOOLTIP
// ===========================================
// Singleton tooltip with toggle semantics:
//  - Click anchor → open; click same anchor again → close (toggle).
//  - Click anywhere else (including other clickables that stopPropagation in
//    their onclick) → close. Achieved via a capture-phase document listener
//    so it runs before other handlers can swallow the event.
//  - Click inside the tooltip itself → keep open (so users can copy text).
//  - Scroll → close.
let activeCalcAnchor = null;
let activeCalcTooltip = null;

function closeCalcTooltip() {
    if (activeCalcTooltip) {
        activeCalcTooltip.remove();
        activeCalcTooltip = null;
    }
    activeCalcAnchor = null;
}

// One-time wiring — outside click and scroll handlers live for the lifetime of
// the page; they no-op when nothing is open.
function setupCalcTooltipDismiss() {
    document.addEventListener('click', (event) => {
        if (!activeCalcAnchor) return;
        // Click inside the tooltip → ignore.
        if (activeCalcTooltip && activeCalcTooltip.contains(event.target)) return;
        // Click on the active anchor itself → let its onclick run the toggle.
        if (activeCalcAnchor.contains(event.target)) return;
        // Anything else → close, then let the other element's onclick run.
        closeCalcTooltip();
    }, true);  // capture phase so stopPropagation in other onclicks can't hide the event from us

    window.addEventListener('scroll', () => {
        if (activeCalcAnchor) closeCalcTooltip();
    }, { passive: true });
}

window.showCalcTooltip = function (e, el) {
    e.preventDefault();
    e.stopPropagation();

    // Toggle: clicking the same anchor a second time just closes.
    if (activeCalcAnchor === el) {
        closeCalcTooltip();
        return;
    }
    // Close any other currently-open tooltip before opening this one.
    closeCalcTooltip();

    const calcText = decodeURIComponent(el.getAttribute('data-calc'));
    const tooltip = document.createElement('div');
    tooltip.id = 'calc-tooltip';
    tooltip.textContent = calcText;
    document.body.appendChild(tooltip);

    // Position
    const rect = el.getBoundingClientRect();
    tooltip.style.top = (rect.bottom + 8) + 'px';
    tooltip.style.left = rect.left + 'px';
    const tooltipRect = tooltip.getBoundingClientRect();
    if (tooltipRect.right > window.innerWidth - 8) {
        tooltip.style.left = (window.innerWidth - tooltipRect.width - 8) + 'px';
    }

    activeCalcAnchor = el;
    activeCalcTooltip = tooltip;
};

// ===========================================
// POPULATE MONTH SELECTOR
// ===========================================
// ===========================================
// POPULATE MONTH SELECTOR
// ===========================================
async function populateMonthSelector() {
    const selector = document.getElementById('monthSelector');
    selector.innerHTML = '<option>Loading...</option>'; // Temporary loading state

    // Get filtered list of months
    availableMonths = await getAvailableMonths();

    selector.innerHTML = '';

    availableMonths.forEach(month => {
        const option = document.createElement('option');
        option.value = month.id;
        option.textContent = month.label;
        selector.appendChild(option);
    });

    // Select the last month by default if available
    if (availableMonths.length > 0) {
        const lastMonth = availableMonths[availableMonths.length - 1];
        selector.value = lastMonth.id;
        currentMonthId = lastMonth.id;
        // Load data for the selected month
        await loadMonth(currentMonthId);
    } else {
        selector.innerHTML = '<option>No data available</option>';
    }

    // Add change event listener — animate like an arrow/swipe switch
    selector.addEventListener('change', (e) => {
        navigateToMonthAnimated(e.target.value);
    });
}

// ===========================================
// GET PREVIOUS MONTH ID
// ===========================================
function getPreviousMonthId(currentId) {
    const currentIndex = availableMonths.findIndex(m => m.id === currentId);
    if (currentIndex > 0) {
        return availableMonths[currentIndex - 1].id;
    }
    return null; // No previous month
}

// ===========================================
// CALCULATE TOTAL BALANCE FROM PORTFOLIO
// ===========================================
function calculateTotalBalance(portfolioData) {
    if (!portfolioData || !portfolioData.portfolio) return 0;

    return portfolioData.portfolio.reduce((total, category) => {
        const categoryTotal = category.items.reduce((sum, item) => sum + item.val, 0);
        return total + categoryTotal;
    }, 0);
}

// ===========================================
// CALCULATE PERFORMANCE (PnL)
// ===========================================
// ===========================================
// CALCULATE PERFORMANCE (PnL)
// ===========================================
function calculatePerformance(startBalance, endBalance, transfers, isFirstMonth = false, calendarTransfers = null) {
    // Calculate calendar-month deposits/withdrawals for UI display
    let calDeposits = 0;
    let calWithdraws = 0;
    const depositDetails = [];
    const withdrawDetails = [];
    const calSrc = calendarTransfers || transfers || [];
    calSrc.forEach(t => {
        if (t.type === 'deposit') {
            calDeposits += t.amount;
            depositDetails.push({ name: t.name, source: t.source, amount: t.amount });
        } else if (t.type === 'withdraw') {
            calWithdraws += t.amount;
            withdrawDetails.push({ name: t.name, source: t.source, amount: t.amount });
        }
    });

    // ZERO KILOMETER LOGIC (First Month)
    if (isFirstMonth) {
        return {
            startBalance: 0,
            endBalance,
            totalDeposits: calDeposits,
            totalWithdraws: calWithdraws,
            depositDetails,
            withdrawDetails,
            netFlow: calDeposits - calWithdraws,
            profit: 0, // Forced 0
            yieldPercent: 0 // Forced 0
        };
    }

    // NORMAL LOGIC
    const netFlow = calDeposits - calWithdraws;

    // Profit = EndBalance - (StartBalance + NetFlow)
    const profit = endBalance - (startBalance + netFlow);

    // Yield % = Profit / (StartBalance + Deposits) * 100
    const investedCapital = startBalance + calDeposits;

    let yieldPercent = 0;
    if (investedCapital > 0.01) {
        yieldPercent = (profit / investedCapital) * 100;
    } else if (profit > 0) {
        yieldPercent = 100;
    }

    return {
        startBalance,
        endBalance,
        totalDeposits: calDeposits,
        totalWithdraws: calWithdraws,
        depositDetails,
        withdrawDetails,
        netFlow,
        profit,
        yieldPercent
    };
}

// ===========================================
// ANNOTATE TRANSFERS ON PORTFOLIO (NO VALUE CHANGE)
// ===========================================
function annotateTransfers(portfolioData, transfers) {
    if (!transfers || transfers.length === 0) return;
    if (!portfolioData || !portfolioData.portfolio) return;

    // Reset previous annotations: assets live in dataService._cache, so the
    // same object is returned every time we revisit the month. Without this
    // wipe, adjustmentHistory accumulates one copy of every transfer per visit.
    portfolioData.portfolio.forEach(cat => {
        cat.items.forEach(item => {
            item.adjustmentHistory = [];
            item.adjustment = 0;
            item.isVirtual = false;
        });
    });

    transfers.forEach(t => {
        if (t.type === 'deposit') {
            markAssetTransfer(portfolioData, t.category, t.source, t.name, t.amount);
        } else if (t.type === 'withdraw') {
            markAssetTransfer(portfolioData, t.category, t.source, t.name, -t.amount);
        } else if (t.type === 'move') {
            markAssetTransfer(portfolioData, t.from_category, t.from_source, t.from_name, -t.amount);
            markAssetTransfer(portfolioData, t.to_category, t.to_source, t.to_name, t.amount);
        }
    });
}

function markAssetTransfer(portfolioData, categoryId, source, assetName, amount) {
    const category = portfolioData.portfolio.find(c => c.id === categoryId);
    if (!category) return;

    const asset = category.items.find(i => i.name === assetName && i.source === source);
    if (!asset) return;

    if (!asset.adjustmentHistory) {
        asset.adjustmentHistory = [];
    }
    asset.adjustmentHistory.push(amount);
    asset.isVirtual = true;
    asset.adjustment = (asset.adjustment || 0) + amount;
}

// ===========================================
// UPDATE PERFORMANCE UI
// ===========================================
function updatePerformanceUI(performance, isFirstMonth, hasTransfers) {
    const summaryEl = document.getElementById('performance-summary');
    const pnlTab = document.getElementById('tab-pnl');
    const netflowTab = document.getElementById('tab-netflow');

    // Always show the summary
    summaryEl.style.display = 'flex';

    // Handle netflow tab - disable if no transfers
    if (netflowTab) {
        if (hasTransfers) {
            netflowTab.classList.add('perf-tab');
        } else {
            netflowTab.classList.remove('perf-tab');
        }
    }

    // Deposits (always positive display)
    const depositsEl = document.getElementById('perf-deposits');
    depositsEl.textContent = '+' + formatMoneyShort(performance.totalDeposits);
    depositsEl.parentElement.title = '';
    if (performance.depositDetails && performance.depositDetails.length > 0) {
        const depLines = performance.depositDetails.map(d => `+${formatMoney(d.amount)}`);
        depLines.push(`Total: +${formatMoney(performance.totalDeposits)}`);
        depositsEl.dataset.calc = encodeURIComponent(depLines.join('\n'));
        depositsEl.style.cursor = 'pointer';
        depositsEl.onclick = function(e) { showCalcTooltip(e, this); };
    } else {
        delete depositsEl.dataset.calc;
        depositsEl.style.cursor = '';
        depositsEl.onclick = null;
    }

    // Withdraws (always show as positive number with minus context)
    const withdrawsEl = document.getElementById('perf-withdraws');
    withdrawsEl.textContent = '-' + formatMoneyShort(performance.totalWithdraws);
    withdrawsEl.parentElement.title = '';
    if (performance.withdrawDetails && performance.withdrawDetails.length > 0) {
        const wdLines = performance.withdrawDetails.map(d => `-${formatMoney(d.amount)}`);
        wdLines.push(`Total: -${formatMoney(performance.totalWithdraws)}`);
        withdrawsEl.dataset.calc = encodeURIComponent(wdLines.join('\n'));
        withdrawsEl.style.cursor = 'pointer';
        withdrawsEl.onclick = function(e) { showCalcTooltip(e, this); };
    } else {
        delete withdrawsEl.dataset.calc;
        withdrawsEl.style.cursor = '';
        withdrawsEl.onclick = null;
    }

    // Net Flow (with sign and color)
    const netFlowEl = document.getElementById('perf-netflow');
    const netFlowTabEl = document.getElementById('tab-netflow');
    const netFlowSign = performance.netFlow >= 0 ? '+' : '';
    netFlowEl.textContent = netFlowSign + formatMoneyShort(performance.netFlow);
    netFlowTabEl.title = `+${formatMoney(performance.totalDeposits)} - ${formatMoney(performance.totalWithdraws)} = ${netFlowSign}${formatMoney(performance.netFlow)}`;
    netFlowEl.classList.remove('perf-positive', 'perf-negative');
    if (performance.netFlow > 0) {
        netFlowEl.classList.add('perf-positive');
    } else if (performance.netFlow < 0) {
        netFlowEl.classList.add('perf-negative');
    }

    if (pnlTab) pnlTab.style.display = '';

    // PnL with tooltip
    const pnlEl = document.getElementById('perf-pnl');
    const pnlTabEl = document.getElementById('tab-pnl');
    const pnlSign = performance.profit >= 0 ? '+' : '';
    const yieldStr = performance.yieldPercent.toFixed(2);
    pnlEl.textContent = `${pnlSign}${formatMoneyShort(performance.profit)} (${pnlSign}${yieldStr}%)`;

    // Set tooltip with PnL formula
    const nfSign = performance.netFlow >= 0 ? '+' : '';
    pnlTabEl.title = `${formatMoney(performance.endBalance)} - (${formatMoney(performance.startBalance)} ${nfSign} ${formatMoney(performance.netFlow)}) = ${pnlSign}${formatMoney(performance.profit)}`;

    // Apply color class to PnL
    pnlEl.classList.remove('perf-positive', 'perf-negative');
    if (performance.profit >= 0) {
        pnlEl.classList.add('perf-positive');
    } else {
        pnlEl.classList.add('perf-negative');
    }
}

// ===========================================
// FETCH DATA (via DataService)
// ===========================================
async function fetchPortfolioData(monthId) {
    return await dataService.fetchPortfolioData(monthId);
}

async function fetchTransfersData(filename) {
    return await dataService.fetchTransfersData(filename);
}

async function fetchBenchmarks() {
    return await dataService._fetch('benchmarks.json');
}

// Fetch ALL transfer files for a specific month
// Tries: transfers-YYYY-MM.json and transfers-YYYY-MM-DD.json for days 01-31
async function fetchTransfersForMonth(monthId) {
    // Prefer a directory listing when available (remote mode) so we only fetch files
    // that actually exist. For local mode the listing is null and we fall back to
    // probing every possible daily filename — wasteful but cached after first miss.
    const knownFiles = await dataService.listDataFilenames();
    let filenames;
    if (knownFiles) {
        const prefix = `transfers-${monthId}`;
        filenames = [];
        knownFiles.forEach(name => {
            if (name === `${prefix}.json` || name.startsWith(`${prefix}-`)) {
                filenames.push(name);
            }
        });
    } else {
        filenames = [`transfers-${monthId}.json`];
        for (let day = 1; day <= 31; day++) {
            const dayStr = String(day).padStart(2, '0');
            filenames.push(`transfers-${monthId}-${dayStr}.json`);
        }
    }

    const results = await Promise.all(filenames.map(async (filename) => {
        const data = await fetchTransfersData(filename);
        if (data && data.transfers && data.transfers.length > 0) {
            return { date: data.meta?.date || monthId, transfers: data.transfers };
        }
        return null;
    }));

    return results
        .filter(r => r)
        .sort((a, b) => a.date.localeCompare(b.date));
}

// ===========================================
// LOAD MONTH DATA
// ===========================================
let preloadStarted = false;

async function loadMonth(monthId) {
    const listContainer = document.getElementById('portfolio-list');

    currentMonthId = monthId;

    // Determine previous month
    const prevMonthId = getPreviousMonthId(monthId);
    const isFirstMonth = prevMonthId === null;

    // Fast path: if primary files are already cached, skip "Loading..." flash
    const primaryCached =
        dataService.isCached(`${monthId}.json`) &&
        (!prevMonthId || dataService.isCached(`${prevMonthId}.json`));

    if (!primaryCached) {
        listContainer.innerHTML = '<div class="loading">Loading data...</div>';
    }

    try {
        // Load all data in parallel: Current Portfolio, Previous Portfolio, Current Transfers, Previous Transfers
        const [currentData, prevData, monthTransfers, prevMonthTransfers] = await Promise.all([
            fetchPortfolioData(monthId),
            prevMonthId ? fetchPortfolioData(prevMonthId) : Promise.resolve(null),
            fetchTransfersForMonth(monthId),
            prevMonthId ? fetchTransfersForMonth(prevMonthId) : Promise.resolve([])
        ]);

        const flatTransfers = monthTransfers.flatMap(g => g.transfers);

        // Check if current month data exists
        if (!currentData) {
            const monthInfo = availableMonths.find(m => m.id === monthId);
            const label = monthInfo ? monthInfo.label : monthId;
            showError(`No data found for ${label}.<br>Create file <code>data/${monthId}.json</code>`);
            return;
        }

        // Validate data format
        if (!currentData.portfolio || !Array.isArray(currentData.portfolio)) {
            throw new Error('Invalid data format: missing portfolio array');
        }

        // Store current data globally
        currentPortfolioData = currentData;

        // --- DATE-BASED TRANSFER FILTERING ---
        // Transfers belong to the period between two snapshots based on their meta.date.
        // Combine all transfer groups from prev + current month, then filter by snapshot dates.
        const allTransferGroups = [...prevMonthTransfers, ...monthTransfers];
        const prevSnapshotDate = prevData?.meta?.date || null;
        const currentSnapshotDate = currentData.meta?.date || monthId;

        const periodGroups = allTransferGroups.filter(g => {
            if (!prevSnapshotDate) return g.date <= currentSnapshotDate;
            return g.date > prevSnapshotDate && g.date <= currentSnapshotDate;
        });
        const flowTransfers = periodGroups.flatMap(g => g.transfers);
        // -----------------------------

        // --- CALENDAR-MONTH TRANSFER FILTERING (for top bar stats) ---
        const calMonthStart = monthId + '-01';
        const calMonthEnd = monthId + '-31';
        const calendarGroups = allTransferGroups.filter(g => g.date >= calMonthStart && g.date <= calMonthEnd);
        const calendarTransfers = calendarGroups.flatMap(g => g.transfers);

        // Annotate assets with transfer info (for asterisk display, no value change)
        annotateTransfers(currentPortfolioData, flowTransfers);

        // --- MOM COMPARISON ---
        currentSnapshot = buildSnapshot(currentPortfolioData);
        previousSnapshot = prevData ? buildSnapshot(prevData) : null;

        const adjustments = getAdjustmentsPerAsset(flowTransfers, currentPortfolioData);

        currentComparison = previousSnapshot
            ? compareSnapshots(currentSnapshot, previousSnapshot, adjustments)
            : null;
        // ---------------------

        // Calculate balances
        const endBalance = calculateTotalBalance(currentPortfolioData);
        const startBalance = prevData ? calculateTotalBalance(prevData) : 0;

        // Calculate performance (PnL uses snapshot-filtered, stats use calendar-filtered)
        const performance = calculatePerformance(startBalance, endBalance, flowTransfers, isFirstMonth);

        // --- FORECASTING 2.0 ---
        try {
            const benchmarksData = await fetchBenchmarks();
            const forecastStats = await calculateYearStats(monthId, benchmarksData);
            updateForecastUI(forecastStats);
            lastPerfStats = forecastStats;
            // Render the currently active view; the inactive one re-renders on toggle
            if (perfViewMode === 'table') {
                renderPerformanceTable(forecastStats);
            } else {
                renderPerformanceChart(forecastStats);
            }
        } catch (err) {
            console.error('Forecasting error:', err);
            updateForecastUI(null);
            lastPerfStats = null;
            renderPerformanceChart(null);
            renderPerformanceTable(null);
        }
        // -----------------------

        // Update UI
        updatePerformanceUI(performance, isFirstMonth, flatTransfers.length > 0);
        renderPortfolio(currentData, currentComparison);
        renderTransfers(monthTransfers, currentData);

        // Reset to portfolio view
        switchTab('portfolio');

        // Update arrow disabled states at boundaries
        updateMonthArrows();

        // Warm up cache for all other months in the background so subsequent
        // swipes render instantly. Fire-and-forget; errors are swallowed.
        if (!preloadStarted) {
            preloadStarted = true;
            setTimeout(() => preloadMonthsInBackground(monthId), 300);
        }

    } catch (error) {
        console.error('Failed to load portfolio data:', error);
        showError(`Failed to load data: ${escapeHtml(error.message)}`);
    }
}

// Fire-and-forget: fetch data for every other available month so subsequent
// swipes/selector changes hit the DataService cache instead of the network.
async function preloadMonthsInBackground(skipMonthId) {
    if (!availableMonths || availableMonths.length === 0) return;

    const others = availableMonths.filter(m => m.id !== skipMonthId);
    // Prioritise neighbours of the current month first (user most likely to swipe there)
    const skipIdx = availableMonths.findIndex(m => m.id === skipMonthId);
    others.sort((a, b) => {
        const da = Math.abs(availableMonths.findIndex(m => m.id === a.id) - skipIdx);
        const db = Math.abs(availableMonths.findIndex(m => m.id === b.id) - skipIdx);
        return da - db;
    });

    // Fetch portfolio snapshots first (1 file each), then transfer files.
    for (const month of others) {
        try {
            fetchPortfolioData(month.id).catch(() => {});
            fetchTransfersForMonth(month.id).catch(() => {});
        } catch (e) { /* swallow */ }
    }
}

// ===========================================
// RENDER PORTFOLIO
// ===========================================
function renderPortfolio(data, comparison = null) {
    const listContainer = document.getElementById('portfolio-list');

    // Clear containers
    listContainer.innerHTML = '';

    // Copy categories for calculations (don't mutate original)
    // AND MERGE WITH GLOBAL CATEGORIES
    const categories = data.portfolio.map(cat => {
        const unified = globalCategories[cat.id];
        return {
            ...cat,
            // Override with unified data if available
            title: unified ? unified.title : cat.title,
            color: unified ? unified.color : cat.color,
            // Use unified order implies sorting later, but for now we trust the unified sort? 
            // Or we should map items.
            items: [...cat.items]
        };
    });

    // Add ghost items (items that were in previous month but not in current)
    if (comparison) {
        Object.entries(comparison.assets).forEach(([key, assetComp]) => {
            if (assetComp.status === 'ghost') {
                // Find or create category
                let cat = categories.find(c => c.id === assetComp.categoryId);
                if (cat) {
                    // Add ghost item
                    cat.items.push({
                        name: assetComp.name,
                        source: assetComp.source,
                        val: 0,
                        isGhost: true,
                        key: key
                    });
                }
            }
        });
    }

    // Calculate totals per category and sort items by value descending
    let grandTotal = 0;
    categories.forEach(cat => {
        // Sort items by value (descending) - ghosts go to bottom
        cat.items.sort((a, b) => {
            if (a.isGhost && !b.isGhost) return 1;
            if (!a.isGhost && b.isGhost) return -1;
            return b.val - a.val;
        });
        cat.total = cat.items.reduce((acc, item) => acc + item.val, 0);
        grandTotal += cat.total;
    });

    // Sort categories
    // If we have "order" in unified categories, use it.
    // Otherwise by total descending.
    categories.sort((a, b) => {
        const orderA = globalCategories[a.id]?.order;
        const orderB = globalCategories[b.id]?.order;

        if (orderA !== undefined && orderB !== undefined) {
            return orderA - orderB;
        }

        return b.total - a.total;
    });

    // Helper to format delta badge
    function formatDeltaBadge(deltaInfo) {
        if (!deltaInfo || deltaInfo.percent === 0) return '';
        const sign = deltaInfo.percent >= 0 ? '+' : '';
        const cls = deltaInfo.percent >= 0 ? 'positive' : 'negative';

        // Calculate money delta
        const deltaVal = deltaInfo.delta;
        const deltaMoney = (deltaVal >= 0 ? '+' : '') + formatMoney(deltaVal);
        const percentTxt = `${sign}${deltaInfo.percent.toFixed(1)}%`;

        return `<span class="delta ${cls}" 
                     data-mode="percent" 
                     data-pct-delta="${percentTxt}" 
                     data-money-delta="${deltaMoney}" 
                     onclick="toggleDelta(event, this)">${percentTxt}</span>`;
    }

    // Generate category list HTML
    categories.forEach((cat, index) => {
        const percent = ((cat.total / grandTotal) * 100).toFixed(2) + '%';
        const money = formatMoney(cat.total);

        // Category delta badge
        const catComparison = comparison?.categories?.[cat.id];
        const catDeltaBadge = formatDeltaBadge(catComparison);

        const section = document.createElement('div');
        section.className = 'category-block';
        section.dataset.catId = cat.id;
        const detailsOpenAttr = openCategoryIds.has(cat.id) ? ' open' : '';

        // Helper to render a single item row
        function renderItemRow(item) {
            const key = item.key || getAssetKey(cat.id, item.source, item.name);
            const assetComp = comparison?.assets?.[key];

            // Ghost row styling
            const rowClass = item.isGhost ? 'ghost-row' : '';

            // NEW badge for new assets
            let newBadge = '';
            if (assetComp?.status === 'new') {
                newBadge = '<span class="badge-new">new</span>';
            }

            // Delta badge
            const deltaBadge = formatDeltaBadge(assetComp);

            let displayVal = formatMoney(item.val);

            if (item.isVirtual) {
                // Build transfer breakdown for tooltip
                let calcParts = [];
                if (item.adjustmentHistory) {
                    item.adjustmentHistory.forEach(adj => {
                        const sign = adj >= 0 ? '+' : '';
                        calcParts.push(`${sign}${formatMoney(adj)}`);
                    });
                }
                const net = item.adjustment || 0;
                if (calcParts.length > 1) {
                    const netSign = net >= 0 ? '+' : '';
                    calcParts.push(`Net: ${netSign}${formatMoney(net)}`);
                }
                const tooltipText = calcParts.join('\n');

                // Add red asterisk with click handler to show tooltip
                const itemId = `virtual-${item.name}-${item.source}`.replace(/[^a-z0-9]/gi, '-');
                displayVal = `<span class="virtual-marker" id="${itemId}" data-calc="${encodeURIComponent(tooltipText)}" onclick="showCalcTooltip(event, this)"><span style="color: #e53e3e; font-weight: bold; cursor: pointer; margin-right: 4px;">*</span>${displayVal}</span>`;
            }

            return `
            <tr class="${rowClass}">
                <td>
                    <div class="asset-name">
                        ${escapeHtml(item.name)}
                        <span class="badge ${getBadgeClass(item.source)}">${escapeHtml(item.source)}</span>${newBadge}
                    </div>
                </td>
                <td class="amount">${deltaBadge}${displayVal}</td>
            </tr>
        `;
        }

        let rows;
        if (cat.id === 'stocks') {
            // Partition items into subgroups
            const SUBGROUP_ORDER = [
                { key: 'etf_us', label: 'ETF \u2013 USA' },
                { key: 'etf_europe', label: 'ETF \u2013 Europe' },
                { key: 'etf_asia', label: 'ETF \u2013 Asia' },
                { key: 'companies', label: 'Companies' }
            ];
            const buckets = { etf_us: [], etf_europe: [], etf_asia: [], companies: [] };
            cat.items.forEach(item => {
                const sg = classifyStockItem(item.name);
                buckets[sg].push(item);
            });

            // Compute totals for relative percentages
            const stocksTotal = cat.total;
            const etfUsTotal = buckets.etf_us.reduce((s, i) => s + i.val, 0);
            const etfEuTotal = buckets.etf_europe.reduce((s, i) => s + i.val, 0);
            const etfAsiaTotal = buckets.etf_asia.reduce((s, i) => s + i.val, 0);
            const etfTotal = etfUsTotal + etfEuTotal + etfAsiaTotal;

            // Region base for ETF region percentages (regions sum to 100%)
            const regionBase = etfTotal;

            rows = SUBGROUP_ORDER.map(sg => {
                const items = buckets[sg.key];
                if (items.length === 0) return '';

                // Subgroup total
                const sgTotal = items.reduce((s, i) => s + i.val, 0);
                const sgMoney = formatMoney(sgTotal);

                // Relative percentage: ETF regions use regionBase, companies use stocksTotal
                let sgPct;
                if (sg.key === 'companies') {
                    sgPct = stocksTotal > 0 ? ((sgTotal / stocksTotal) * 100).toFixed(2) + '%' : '0.00%';
                } else {
                    sgPct = regionBase > 0 ? ((sgTotal / regionBase) * 100).toFixed(2) + '%' : '0.00%';
                }

                // Subgroup delta: sum per-asset deltas
                let sgDeltaVal = 0;
                let sgPrevTotal = 0;
                let sgAdj = 0;
                items.forEach(item => {
                    const key = item.key || getAssetKey(cat.id, item.source, item.name);
                    const ac = comparison?.assets?.[key];
                    if (ac) {
                        sgDeltaVal += ac.delta || 0;
                        sgPrevTotal += ac.previousVal || 0;
                        sgAdj += (ac.adjustedStart || 0) - (ac.previousVal || 0);
                    }
                });
                const sgDeltaInfo = calculateDelta(sgTotal, sgPrevTotal, sgAdj);
                const sgDeltaBadge = formatDeltaBadge(sgDeltaInfo);

                const headerRow = `
            <tr class="subgroup-header">
                <td><span class="subgroup-label">${sg.label}</span></td>
                <td class="amount">
                    ${sgDeltaBadge}
                    <div class="toggle-btn"
                         onclick="toggleValue(event, this)"
                         data-mode="percent"
                         data-pct="${sgPct}"
                         data-money="${sgMoney}">
                        ${sgPct}
                    </div>
                </td>
            </tr>`;
                return headerRow + items.map(renderItemRow).join('');
            }).join('');
        } else {
            rows = cat.items.map(renderItemRow).join('');
        }

        section.innerHTML = `
            <details${detailsOpenAttr}>
                <summary>
                    <div class="header-title">
                        <span class="color-dot" style="background-color: ${escapeHtml(cat.color)};"></span>
                        <span>${escapeHtml(cat.title)}</span>
                    </div>
                    
                    <div style="display: flex; align-items: center; margin-left: auto;">
                        ${catDeltaBadge}
                        <div class="toggle-btn" 
                             onclick="toggleValue(event, this)" 
                             data-mode="percent" 
                             data-pct="${percent}" 
                             data-money="${money}">
                            ${percent}
                        </div>
                    </div>
                </summary>
                <div class="details-content">
                    <table>
                        ${rows}
                    </table>
                </div>
            </details>
        `;
        listContainer.appendChild(section);
    });

    // Re-apply scroll anchor captured before the month nav. Runs synchronously
    // after the new sections are in the DOM, so the slide-in animation starts
    // from the correct scroll position with no visible jump.
    restoreScrollAnchor();

    // Store chart data globally for re-rendering on toggle
    lastChartData = { categories, grandTotal };

    renderChart();
}

// ===========================================
// RENDER CHART (Category or Source mode)
// ===========================================
let lastChartData = null;

function renderChart() {
    const chartCanvas = document.getElementById('portfolioChart');
    if (!chartCanvas || !lastChartData) return;

    const { categories, grandTotal } = lastChartData;
    const chartLabels = [];
    const chartValues = [];
    const chartColors = [];
    const chartDeltas = []; // MOM delta percentages
    // Parallel to chartLabels but holds the *true* segment name (e.g. "Companies"
    // for the companies sub-bucket, instead of the parent category title that
    // chartLabels uses to keep the legend numbered). Read by the tooltip title
    // callback so hovering shows the actual segment identity.
    const chartSegmentNames = [];
    const chartBorderColors = [];
    const chartBorderWidths = [];

    if (chartMode === 'source') {
        // Aggregate by source
        const sourceMap = {}; // source -> { total, prevTotal }
        categories.forEach(cat => {
            cat.items.forEach(item => {
                if (item.isGhost) return;
                const src = item.source;
                if (!sourceMap[src]) sourceMap[src] = { total: 0, prevTotal: 0 };
                sourceMap[src].total += item.val;
            });
        });

        // Get previous totals by source
        if (previousSnapshot) {
            Object.values(previousSnapshot.assetMap).forEach(asset => {
                const src = asset.source;
                if (!sourceMap[src]) sourceMap[src] = { total: 0, prevTotal: 0 };
                sourceMap[src].prevTotal += asset.val;
            });
        }

        // Sort by total descending
        const sorted = Object.entries(sourceMap).sort((a, b) => b[1].total - a[1].total);
        const defaultColors = ['#6366f1', '#8b5cf6', '#ec4899', '#14b8a6', '#f97316', '#64748b'];
        sorted.forEach(([src, data], i) => {
            chartLabels.push(src);
            chartSegmentNames.push(src);
            chartValues.push(data.total);
            chartColors.push(getSourceColor(src) || defaultColors[i % defaultColors.length]);

            // Calculate delta of share percentage
            const prevGrandTotal = Object.values(sourceMap).reduce((s, d) => s + d.prevTotal, 0);
            if (data.prevTotal > 0 && prevGrandTotal > 0) {
                const prevShare = (data.prevTotal / prevGrandTotal) * 100;
                const curShare = (data.total / grandTotal) * 100;
                chartDeltas.push(curShare - prevShare);
            } else if (data.total > 0) {
                chartDeltas.push(null); // new source
            } else {
                chartDeltas.push(0);
            }
        });
    } else {
        // Category mode – split Stocks into 4 subgroup segments
        categories.forEach(cat => {
            if (cat.id === 'stocks') {
                // Partition into subgroups for chart
                const sgDefs = [
                    { key: 'companies', label: 'Companies' },
                    { key: 'etf_us', label: 'ETF - USA' },
                    { key: 'etf_europe', label: 'ETF - Europe' },
                    { key: 'etf_asia', label: 'ETF - Asia' }
                ];
                const sgBuckets = { etf_us: 0, etf_europe: 0, etf_asia: 0, companies: 0 };
                cat.items.forEach(item => {
                    if (item.isGhost) return;
                    const sg = classifyStockItem(item.name);
                    sgBuckets[sg] += item.val;
                });
                // The donut legend below mirrors the accordion's numbered titles
                // ("1. Safe", "2. Stocks", "3. Cash", ...). The first non-empty
                // stocks sub-bucket therefore gets the parent category title;
                // the rest get their region/companies labels and are hidden from
                // the legend by the filter callback (anything starting with
                // "ETF - " or named "Companies" is suppressed).
                // Borders between stock sub-segments are transparent — the canvas
                // has no own background, so the chart container's bg shows through
                // the gap. Themes via CSS only, no JS involvement.
                let firstSg = true;
                sgDefs.forEach(sg => {
                    if (sgBuckets[sg.key] <= 0) return;
                    chartLabels.push(firstSg ? cat.title : sg.label);
                    chartSegmentNames.push(sg.label);
                    if (firstSg) firstSg = false;
                    chartValues.push(sgBuckets[sg.key]);
                    chartColors.push(cat.color);
                    chartBorderColors.push('transparent');
                    chartBorderWidths.push(2);
                    chartDeltas.push(null);
                });
            } else {
                chartLabels.push(cat.title);
                chartSegmentNames.push(cat.title);
                chartValues.push(cat.total);
                chartColors.push(cat.color);
                chartBorderColors.push('transparent');
                chartBorderWidths.push(0);

                // Calculate delta of share percentage
                if (previousSnapshot && currentComparison?.categories?.[cat.id]) {
                    const prevTotal = Object.values(previousSnapshot.categories).reduce((s, c) => s + c.total, 0);
                    const prevCatTotal = previousSnapshot.categories[cat.id]?.total || 0;
                    const prevShare = prevTotal > 0 ? (prevCatTotal / prevTotal) * 100 : 0;
                    const curShare = grandTotal > 0 ? (cat.total / grandTotal) * 100 : 0;
                    chartDeltas.push(curShare - prevShare);
                } else {
                    chartDeltas.push(null);
                }
            }
        });
    }

    // Grand total string for center. Stored on the chart instance so the plugin
    // (registered once at chart creation) always reads the current value.
    const grandTotalStr = formatMoney(grandTotal);

    const centerTextPlugin = {
        id: 'centerText',
        beforeDraw: function (chart) {
            const text = chart.$grandTotalStr || '';
            if (!text) return;
            const ctx = chart.ctx;
            const { top, bottom, left, right } = chart.chartArea;
            const centerX = (left + right) / 2;
            const centerY = (top + bottom) / 2;

            ctx.save();
            const fontSize = 16;
            ctx.font = `800 ${fontSize}px monospace`;
            // Theme-aware: pulls --color-text from :root so dark mode flips automatically.
            ctx.fillStyle = getComputedStyle(document.documentElement)
                .getPropertyValue('--color-text').trim() || '#2d3748';
            ctx.textAlign = 'center';
            ctx.textBaseline = 'middle';
            ctx.fillText(text, centerX, centerY);
            ctx.restore();
        }
    };

    const datasetCfg = {
        data: chartValues,
        backgroundColor: chartColors,
        borderWidth: chartMode === 'source' ? 0 : chartBorderWidths,
        borderColor: chartMode === 'source' ? 'transparent' : chartBorderColors,
        hoverOffset: 6
    };

    // Reuse existing chart instance when possible — avoids destroy/recreate jank
    // and lets Chart.js animate the segment transitions.
    if (portfolioChart) {
        portfolioChart.$grandTotalStr = grandTotalStr;
        portfolioChart.data.labels = chartLabels;
        Object.assign(portfolioChart.data.datasets[0], datasetCfg);
        // chartSegmentNames / chartDeltas are read by tooltip callbacks via closure
        // captured at creation; refresh the references the callbacks dereference.
        portfolioChart.$chartSegmentNames = chartSegmentNames;
        portfolioChart.$chartDeltas = chartDeltas;
        portfolioChart.$grandTotal = grandTotal;
        // Chart.js animates via canvas/rAF and ignores prefers-reduced-motion;
        // skip the segment transition explicitly when the user opted out.
        portfolioChart.update(prefersReducedMotion() ? 'none' : undefined);
        return;
    }

    const ctx = chartCanvas.getContext('2d');
    portfolioChart = new Chart(ctx, {
        type: 'doughnut',
        data: {
            labels: chartLabels,
            datasets: [datasetCfg]
        },
        plugins: [centerTextPlugin],
        options: {
            responsive: true,
            maintainAspectRatio: false,
            plugins: {
                legend: {
                    position: 'bottom',
                    labels: {
                        usePointStyle: true,
                        padding: 15,
                        font: { size: 11 },
                        filter: function (item) {
                            // Hide stocks sub-bucket labels; only the parent
                            // category title (e.g. "2. Stocks") shows in legend.
                            if (item.text.startsWith('ETF - ')) return false;
                            if (item.text === 'Companies') return false;
                            return true;
                        }
                    },
                    // Legend click toggling is disabled — visual feedback (strikethrough)
                    // doesn't behave correctly for the custom Stocks/ETF subgroup, and the
                    // feature isn't worth the complexity. Leave the legend as a passive key.
                    onClick: () => { /* no-op */ }
                },
                tooltip: {
                    footerColor: '#9ca3af',
                    footerFont: { size: 11, weight: 'normal' },
                    callbacks: {
                        // Override the tooltip title so each segment shows its
                        // *true* name on hover (e.g. "Companies"), even though
                        // the legend below uses the parent category title for
                        // the first stocks sub-bucket ("2. Stocks") to keep
                        // the numbered list consistent with the accordion.
                        title: function (items) {
                            if (!items.length) return '';
                            const names = items[0].chart.$chartSegmentNames || [];
                            return names[items[0].dataIndex] || items[0].label;
                        },
                        label: function (context) {
                            const total = context.chart.$grandTotal || 0;
                            const val = context.raw;
                            const pct = total > 0 ? ((val / total) * 100).toFixed(2) + '%' : '0.00%';
                            return ` ${pct} (${val.toLocaleString('en-US')} $)`;
                        },
                        footer: function (items) {
                            if (!items.length) return '';
                            const deltas = items[0].chart.$chartDeltas || [];
                            const delta = deltas[items[0].dataIndex];
                            if (delta === null || delta === undefined) return '';
                            const sign = delta >= 0 ? '+' : '';
                            return `Share: ${sign}${delta.toFixed(2)}%`;
                        }
                    }
                }
            }
        }
    });
    portfolioChart.$grandTotalStr = grandTotalStr;
    portfolioChart.$chartSegmentNames = chartSegmentNames;
    portfolioChart.$chartDeltas = chartDeltas;
    portfolioChart.$grandTotal = grandTotal;
}

// ===========================================
// RENDER PERFORMANCE CHART (YTD Comparison)
// ===========================================
// Plugin: draw a horizontal line at y=0 so positive vs negative is visible
const zeroLinePlugin = {
    id: 'zeroLine',
    afterDatasetsDraw: (chart) => {
        const yScale = chart.scales.y;
        if (!yScale) return;
        const zeroY = yScale.getPixelForValue(0);
        const { top, bottom, left, right } = chart.chartArea;
        if (zeroY < top - 1 || zeroY > bottom + 1) return;
        const ctx = chart.ctx;
        ctx.save();
        // Theme-aware: dashed baseline reads from --color-text-placeholder.
        ctx.strokeStyle = getComputedStyle(document.documentElement)
            .getPropertyValue('--color-text-placeholder').trim() || '#a0aec0';
        ctx.lineWidth = 1;
        ctx.setLineDash([4, 4]);
        ctx.beginPath();
        ctx.moveTo(left, zeroY);
        ctx.lineTo(right, zeroY);
        ctx.stroke();
        ctx.restore();
    }
};

// On mobile, Chart.js shows the tooltip on tap and leaves it visible until
// another tap on the chart. Tapping anywhere else should clear it.
function setupPerfChartTooltipDismiss() {
    const dismiss = (e) => {
        if (!performanceChart) return;
        const canvas = document.getElementById('performanceChart');
        if (!canvas) return;
        if (e.target === canvas) return; // tap on chart itself — keep tooltip
        const tooltip = performanceChart.tooltip;
        if (!tooltip || typeof tooltip.getActiveElements !== 'function') return;
        if (tooltip.getActiveElements().length === 0) return;
        performanceChart.setActiveElements([]);
        tooltip.setActiveElements([], { x: 0, y: 0 });
        performanceChart.update('none');
    };
    document.addEventListener('click', dismiss);
    document.addEventListener('touchstart', dismiss, { passive: true });
}

function renderPerformanceChart(stats) {
    const section = document.getElementById('performance-chart-section');
    const canvas = document.getElementById('performanceChart');
    if (!section || !canvas) return;

    if (!stats || !stats.monthLabels || stats.monthLabels.length < 2) {
        section.style.display = 'none';
        // Free the chart instance — section is being hidden, no reuse target.
        if (performanceChart) {
            performanceChart.destroy();
            performanceChart = null;
        }
        return;
    }

    section.style.display = 'block';

    const datasets = [];

    // Category lines (use color from globalCategories), sorted by order
    const catIds = Object.keys(stats.categorySeries || {}).sort((a, b) => {
        const oa = globalCategories[a]?.order ?? 999;
        const ob = globalCategories[b]?.order ?? 999;
        return oa - ob;
    });

    // Default-hidden categories (low-volatility noise). Stocks is hidden in favor
    // of the Companies sub-bucket which is more informative.
    const DEFAULT_HIDDEN_CATS = new Set(['usd', 'safe', 'stocks']);

    // Stocks sub-buckets share the purple family of the parent stocks category but
    // step through shades from dark → light so they can be visually compared.
    // All hidden by default; the overall Stocks line stays default-on.
    const SUB_STOCK_DATASETS = [
        { key: 'stocks_companies',  label: 'Companies',  color: '#4c1d95' },
        { key: 'stocks_etf',        label: 'ETF total',  color: '#6d28d9' },
        { key: 'stocks_etf_us',     label: 'ETF USA',    color: '#8b5cf6' },
        { key: 'stocks_etf_europe', label: 'ETF Europe', color: '#a78bfa' },
        { key: 'stocks_etf_asia',   label: 'ETF Asia',   color: '#c4b5fd' }
    ];

    catIds.forEach(catId => {
        const meta = globalCategories[catId];
        const label = meta ? meta.title : catId;
        const color = meta?.color || '#a0aec0';
        datasets.push({
            label,
            data: stats.categorySeries[catId].map(v => v === null ? null : v * 100),
            borderColor: color,
            backgroundColor: color,
            borderWidth: 2,
            pointRadius: 2,
            tension: 0.25,
            spanGaps: true,
            hidden: DEFAULT_HIDDEN_CATS.has(catId)
        });

        // Right after the Stocks category line, insert all stocks sub-buckets so
        // they appear contiguous in the legend.
        if (catId === 'stocks' && stats.subStocksSeries) {
            SUB_STOCK_DATASETS.forEach(def => {
                const series = stats.subStocksSeries[def.key];
                if (!series) return;
                const hasAny = series.some(v => v !== null && v !== undefined && v !== 0);
                if (!hasAny) return;
                datasets.push({
                    label: def.label,
                    data: series.map(v => v === null ? null : v * 100),
                    borderColor: def.color,
                    backgroundColor: def.color,
                    borderWidth: 1.6,
                    pointRadius: 2,
                    tension: 0.25,
                    spanGaps: true,
                    hidden: def.key !== 'stocks_companies' // Companies on by default; other sub-buckets off
                });
            });
        }
    });

    // Total portfolio (black, thick)
    datasets.push({
        label: 'Total',
        data: stats.totalSeries.map(v => v === null ? null : v * 100),
        borderColor: '#1a202c',
        backgroundColor: '#1a202c',
        borderWidth: 3,
        pointRadius: 3,
        tension: 0.25,
        spanGaps: true
    });

    // Benchmarks
    const hasBench = (arr) => Array.isArray(arr) && arr.some(v => v !== null && v !== 0);
    if (hasBench(stats.benchmarkSeries?.vt)) {
        datasets.push({
            label: 'VT (Market)',
            data: stats.benchmarkSeries.vt.map(v => v === null ? null : v * 100),
            borderColor: '#718096',
            backgroundColor: '#718096',
            borderWidth: 1.5,
            pointRadius: 2,
            tension: 0.25,
            spanGaps: true
        });
    }
    if (hasBench(stats.benchmarkSeries?.voo)) {
        datasets.push({
            label: 'VOO (S&P 500)',
            data: stats.benchmarkSeries.voo.map(v => v === null ? null : v * 100),
            borderColor: '#a0aec0',
            backgroundColor: '#a0aec0',
            borderWidth: 1.5,
            pointRadius: 2,
            tension: 0.25,
            spanGaps: true
        });
    }
    if (hasBench(stats.benchmarkSeries?.deposit)) {
        datasets.push({
            label: 'Deposit 3.5%',
            data: stats.benchmarkSeries.deposit.map(v => v === null ? null : v * 100),
            borderColor: '#cbd5e0',
            backgroundColor: '#cbd5e0',
            borderWidth: 1.5,
            pointRadius: 2,
            tension: 0.1,
            spanGaps: true,
            hidden: true // low-volatility baseline — hidden by default
        });
    }

    // Preserve user toggles (hidden datasets) across re-renders. Chart.js stores
    // hidden state on the chart instance; replacing the datasets array would reset
    // it. We map by label to keep flags stable across snapshot/month switches.
    if (performanceChart) {
        const prevHidden = new Map();
        performanceChart.data.datasets.forEach((ds, i) => {
            prevHidden.set(ds.label, performanceChart.getDatasetMeta(i).hidden);
        });
        datasets.forEach(ds => {
            if (prevHidden.has(ds.label)) {
                const wasHidden = prevHidden.get(ds.label);
                if (wasHidden !== null) ds.hidden = wasHidden;
            }
        });
        performanceChart.data.labels = stats.monthLabels;
        performanceChart.data.datasets = datasets;
        performanceChart.update(prefersReducedMotion() ? 'none' : undefined);
        return;
    }

    const ctx = canvas.getContext('2d');
    performanceChart = new Chart(ctx, {
        type: 'line',
        data: {
            labels: stats.monthLabels,
            datasets
        },
        plugins: [zeroLinePlugin],
        options: {
            responsive: true,
            maintainAspectRatio: false,
            interaction: {
                mode: 'index',
                intersect: false
            },
            plugins: {
                legend: {
                    position: 'bottom',
                    labels: {
                        usePointStyle: true,
                        padding: 10,
                        font: { size: 11 }
                    }
                },
                tooltip: {
                    callbacks: {
                        label: function (context) {
                            const v = context.parsed.y;
                            if (v === null || v === undefined) return `${context.dataset.label}: —`;
                            const sign = v >= 0 ? '+' : '';
                            return `${context.dataset.label}: ${sign}${v.toFixed(2)}%`;
                        }
                    }
                }
            },
            scales: {
                x: { display: false },
                y: { display: false }
            }
        }
    });
}

// ===========================================
// RENDER PERFORMANCE TABLE (Stocks comparison)
// ===========================================
// All possible bucket rows. Order here defines the unsorted (default) row order
// and the legend pill order.
function getAllPerfRows() {
    const labelFor = (catId, fallback) => {
        const t = globalCategories[catId]?.title;
        return t ? t.replace(/^\d+\.\s*/, '') : fallback;
    };
    return [
        { id: 'companies',    label: 'Companies',     dot: '#4c1d95' },
        { id: 'etf_total',    label: 'ETF total',     dot: '#6d28d9' },
        { id: 'etf_us',       label: 'ETF USA',       dot: '#8b5cf6' },
        { id: 'etf_europe',   label: 'ETF Europe',    dot: '#a78bfa' },
        { id: 'etf_asia',     label: 'ETF Asia',      dot: '#c4b5fd' },
        { id: 'stocks_total', label: 'Stocks',        dot: globalCategories.stocks?.color || '#9f7aea' },
        { id: 'safe',         label: labelFor('safe',   'Safe'),        dot: globalCategories.safe?.color   || '#ecc94b', isCategory: true },
        { id: 'usd',          label: labelFor('usd',    'Cash'),        dot: globalCategories.usd?.color    || '#48bb78', isCategory: true },
        { id: 'crypto',       label: labelFor('crypto', 'Crypto'),      dot: globalCategories.crypto?.color || '#ed8936', isCategory: true },
        { id: 'copy',         label: labelFor('copy',   'Copytrading'), dot: globalCategories.copy?.color   || '#4299e1', isCategory: true },
        { id: 'vt',           label: 'VT (Market)',   dot: '#718096', isBenchmark: true },
        { id: 'voo',          label: 'VOO (S&P 500)', dot: '#a0aec0', isBenchmark: true }
    ];
}

function defaultEnabledTableBuckets() {
    // Stocks-related + the two market benchmarks (VT, VOO) ON by default;
    // other categories (Safe / Cash / Crypto / Copytrading) OFF.
    return new Set(['companies', 'etf_total', 'etf_us', 'etf_europe', 'etf_asia', 'stocks_total', 'vt', 'voo']);
}

function renderPerformanceTable(stats) {
    const table = document.getElementById('performanceTable');
    const legendEl = document.getElementById('perfTableLegend');
    if (!table) return;
    if (!stats || !stats.performanceTable) {
        table.innerHTML = '';
        if (legendEl) legendEl.innerHTML = '';
        return;
    }

    if (!enabledTableBuckets) enabledTableBuckets = defaultEnabledTableBuckets();

    const data = stats.performanceTable;
    const ALL = getAllPerfRows();

    // Pick benchmark for α
    const benchKey = alphaBenchmark === 'VOO' ? 'voo' : 'vt';
    const benchYTD = data[benchKey]?.ytd ?? null;

    const buildRow = (def) => {
        const d = data[def.id] || {};
        const ytd = d.ytd != null ? d.ytd : null;
        const vol = d.vol;
        const sharpe = d.sharpe;
        let alpha = null;
        if (def.id !== benchKey && ytd != null && benchYTD != null) {
            alpha = ytd - benchYTD;
        }
        return {
            id: def.id, label: def.label,
            isBenchmark: !!def.isBenchmark,
            ytd, vol, sharpe, alpha
        };
    };

    // --- Legend pills (all rows, active = visible in table) ---
    if (legendEl) {
        legendEl.innerHTML = ALL.map(def => {
            const active = enabledTableBuckets.has(def.id);
            const dot = def.dot ? `<span class="legend-dot" style="background:${escapeHtml(def.dot)}"></span>` : '';
            return `<button type="button" class="perf-table-legend-pill${active ? ' active' : ''}" data-bucket-id="${escapeHtml(def.id)}">${dot}${escapeHtml(def.label)}</button>`;
        }).join('');
        legendEl.querySelectorAll('.perf-table-legend-pill').forEach(el => {
            el.addEventListener('click', () => {
                const id = el.getAttribute('data-bucket-id');
                if (enabledTableBuckets.has(id)) enabledTableBuckets.delete(id);
                else enabledTableBuckets.add(id);
                renderPerformanceTable(stats);
            });
        });
    }

    // --- Rows: filter by enabled, sort if active ---
    let rows = ALL.filter(def => enabledTableBuckets.has(def.id)).map(buildRow);
    if (tableSortKey) {
        rows.sort((a, b) => {
            const va = a[tableSortKey];
            const vb = b[tableSortKey];
            if (va == null && vb == null) return 0;
            if (va == null) return 1;
            if (vb == null) return -1;
            return tableSortDir === 'desc' ? vb - va : va - vb;
        });
    }

    // --- Formatters / colour classes ---
    const fmtPct = (v, signed = false) => {
        if (v == null || !isFinite(v)) return '—';
        const sign = signed ? (v >= 0 ? '+' : '') : '';
        return `${sign}${(v * 100).toFixed(1)}%`;
    };
    const fmtSharpe = (v) => (v == null || !isFinite(v)) ? '—' : v.toFixed(2);
    const cls = (v) => {
        if (v == null || !isFinite(v)) return '';
        if (v > 0.0005) return 'perf-positive';
        if (v < -0.0005) return 'perf-negative';
        return '';
    };
    // Sharpe uses a wider neutral band — tiny values aren't meaningful.
    const sharpeColorVal = (v) => {
        if (v == null || !isFinite(v)) return null;
        if (v > 0.05) return 1;
        if (v < -0.05) return -1;
        return 0;
    };

    const cellsForRow = (r) => `
        <td title="${escapeHtml(r.label)}">${escapeHtml(r.label)}</td>
        <td class="${cls(r.ytd)}">${fmtPct(r.ytd, true)}</td>
        <td>${fmtPct(r.vol)}</td>
        <td class="${cls(r.alpha)}">${fmtPct(r.alpha, true)}</td>
        <td class="${cls(sharpeColorVal(r.sharpe))}">${fmtSharpe(r.sharpe)}</td>
    `;

    // --- Headers ---
    const headers = [
        { key: null,     label: 'Bucket',                          tip: '' },
        { key: 'ytd',    label: 'YTD',                             tip: 'Накопленная доходность с начала года' },
        { key: 'vol',    label: 'σ',                               tip: 'Annualised volatility (std dev × √12) — амплитуда колебаний' },
        { toggleBench: true, label: `α vs ${alphaBenchmark}`,      tip: 'Alpha vs выбранный бенчмарк. Тап — переключение VOO ↔ VT.' },
        { key: 'sharpe', label: 'S',                               tip: 'Sharpe-стиль: (annualised return − 3.5%) / annualised σ — доходность за единицу риска' }
    ];

    const headerHtml = headers.map(h => {
        const titleAttr = h.tip ? `title="${escapeHtml(h.tip)}"` : '';
        if (h.toggleBench) {
            return `<th data-toggle-bench="true" ${titleAttr}>${h.label}</th>`;
        }
        const sortable = h.key !== null;
        const isActive = h.key === tableSortKey;
        const sortClass = isActive ? (tableSortDir === 'desc' ? 'sort-active desc' : 'sort-active') : '';
        const sortAttr = sortable ? `data-sort-key="${h.key}"` : '';
        return `<th class="${sortClass}" ${sortAttr} ${titleAttr}>${h.label}</th>`;
    }).join('');

    const bodyHtml = rows.map(r => {
        const cls = r.isBenchmark ? 'row-benchmark' : '';
        return `<tr class="${cls}">${cellsForRow(r)}</tr>`;
    }).join('');

    table.innerHTML = `
        <thead><tr>${headerHtml}</tr></thead>
        <tbody>${bodyHtml}</tbody>
    `;

    // Wire sort handlers
    table.querySelectorAll('th[data-sort-key]').forEach(th => {
        th.addEventListener('click', () => {
            const key = th.getAttribute('data-sort-key');
            if (tableSortKey === key) {
                if (tableSortDir === 'desc') tableSortDir = 'asc';
                else { tableSortKey = null; tableSortDir = 'desc'; }
            } else {
                tableSortKey = key;
                tableSortDir = 'desc';
            }
            renderPerformanceTable(stats);
        });
    });

    // Wire α benchmark toggle (VOO ↔ VT)
    table.querySelectorAll('th[data-toggle-bench]').forEach(th => {
        th.addEventListener('click', () => {
            alphaBenchmark = alphaBenchmark === 'VOO' ? 'VT' : 'VOO';
            renderPerformanceTable(stats);
        });
    });
}

// ===========================================
// PERFORMANCE VIEW TOGGLE (Chart / Table) + α benchmark toggle
// ===========================================
function setupPerfViewToggle() {
    const toggle = document.getElementById('perfViewToggle');
    const chartView = document.getElementById('perf-view-chart');
    const tableView = document.getElementById('perf-view-table');

    if (toggle && chartView && tableView) {
        toggle.addEventListener('click', (e) => {
            const opt = e.target.closest('.chart-toggle-option');
            if (!opt || opt.classList.contains('active')) return;
            toggle.querySelectorAll('.chart-toggle-option').forEach(o => {
                const active = o === opt;
                o.classList.toggle('active', active);
                o.setAttribute('aria-selected', active ? 'true' : 'false');
            });
            perfViewMode = opt.getAttribute('data-view');
            if (perfViewMode === 'chart') {
                chartView.style.display = '';
                tableView.style.display = 'none';
                if (lastPerfStats) renderPerformanceChart(lastPerfStats);
            } else {
                chartView.style.display = 'none';
                tableView.style.display = '';
                if (lastPerfStats) renderPerformanceTable(lastPerfStats);
            }
        });
    }

}

// ===========================================
// CHART TOGGLE HANDLER
// ===========================================
function setupChartToggle() {
    const toggle = document.getElementById('chartToggle');
    if (!toggle) return;
    toggle.addEventListener('click', (e) => {
        const option = e.target.closest('.chart-toggle-option');
        if (!option || option.classList.contains('active')) return;
        toggle.querySelectorAll('.chart-toggle-option').forEach(o => {
            const active = o === option;
            o.classList.toggle('active', active);
            o.setAttribute('aria-selected', active ? 'true' : 'false');
        });
        chartMode = option.getAttribute('data-mode');
        renderChart();
    });
}

// ===========================================
// SHOW ERROR
// ===========================================
function showError(message) {
    const listContainer = document.getElementById('portfolio-list');
    // Note: HTML markup in `message` (e.g. <br>, <code>) is intentional from callers,
    // so we don't escape the whole string. Callers must escape any user-supplied parts.
    listContainer.innerHTML = `<div class="error">${message}</div>`;
}

// ===========================================
// TAB SWITCHING
// ===========================================
function switchTab(tabName) {
    const portfolioView = document.getElementById('view-portfolio');
    const transfersView = document.getElementById('view-transfers');
    const tabPnl = document.getElementById('tab-pnl');
    const tabNetflow = document.getElementById('tab-netflow');

    if (tabName === 'portfolio') {
        portfolioView.style.display = 'block';
        transfersView.style.display = 'none';
        tabPnl.classList.add('active');
        tabNetflow.classList.remove('active');
    } else if (tabName === 'transfers') {
        portfolioView.style.display = 'none';
        transfersView.style.display = 'block';
        tabPnl.classList.remove('active');
        tabNetflow.classList.add('active');
    }
}

function setupTabHandlers() {
    document.getElementById('tab-pnl').addEventListener('click', () => switchTab('portfolio'));
    document.getElementById('tab-netflow').addEventListener('click', () => switchTab('transfers'));
}

// ===========================================
// RENDER TRANSFERS
// ===========================================

// Helper: format transfer path to string
function formatTransferPath(categoryId, source, name, portfolioData) {
    // Try global categories first
    if (globalCategories[categoryId]) {
        return `${globalCategories[categoryId].title} › ${source} › ${name}`;
    }

    // Fallback to local portfolio data
    const category = portfolioData?.portfolio?.find(c => c.id === categoryId);
    const categoryTitle = category ? category.title : categoryId;
    return `${categoryTitle} › ${source} › ${name}`;
}

// Helper: get category color by id
function getCategoryColor(categoryId, portfolioData) {
    // Try global categories first
    if (globalCategories[categoryId]) {
        return globalCategories[categoryId].color;
    }

    if (!categoryId || !portfolioData || !portfolioData.portfolio) {
        return '#a0aec0'; // default gray
    }

    const category = portfolioData.portfolio.find(c => c.id === categoryId);
    return category ? category.color : '#a0aec0';
}

function renderTransfers(allTransfersData, portfolioData) {
    const container = document.getElementById('transfers-list');

    // allTransfersData is array of {date, transfers}
    if (!allTransfersData || allTransfersData.length === 0) {
        container.innerHTML = '<div class="transfers-empty">No transactions found</div>';
        return;
    }

    // Helper to render a single transfer item
    function renderTransferItem(t) {
        let pathDisplay = '';
        let amountClass = '';
        let amountPrefix = '';
        let categoryColor = '#a0aec0';

        if (t.type === 'deposit') {
            pathDisplay = formatTransferPath(t.category, t.source, t.name, portfolioData);
            amountClass = 'deposit';
            amountPrefix = '+';
            categoryColor = getCategoryColor(t.category, portfolioData);
        } else if (t.type === 'withdraw') {
            pathDisplay = formatTransferPath(t.category, t.source, t.name, portfolioData);
            amountClass = 'withdraw';
            amountPrefix = '-';
            categoryColor = getCategoryColor(t.category, portfolioData);
        } else if (t.type === 'move') {
            const fromStr = formatTransferPath(t.from_category, t.from_source, t.from_name, portfolioData);
            const toStr = formatTransferPath(t.to_category, t.to_source, t.to_name, portfolioData);
            const fromColor = getCategoryColor(t.from_category, portfolioData);
            const toColor = getCategoryColor(t.to_category, portfolioData);

            return `
                <div class="transfer-item">
                    <div class="transfer-content">
                        <div class="transfer-row">
                            <div class="transfer-category-dot" style="background-color: ${escapeHtml(fromColor)};"></div>
                            <div class="transfer-path">${escapeHtml(fromStr)}</div>
                        </div>
                        <div class="transfer-row">
                            <div class="transfer-category-dot" style="background-color: ${escapeHtml(toColor)};"></div>
                            <div class="transfer-path">${escapeHtml(toStr)}</div>
                        </div>
                    </div>
                    <div class="transfer-amount move">${formatMoney(t.amount)}</div>
                </div>
            `;
        }

        return `
            <div class="transfer-item">
                <div class="transfer-content">
                    <div class="transfer-row">
                        <div class="transfer-category-dot" style="background-color: ${escapeHtml(categoryColor)};"></div>
                        <div class="transfer-path">${escapeHtml(pathDisplay)}</div>
                    </div>
                </div>
                <div class="transfer-amount ${amountClass}">${amountPrefix}${formatMoney(t.amount)}</div>
            </div>
        `;
    }

    // Render each date group
    const html = allTransfersData.map(group => {
        const dateHeader = `<div class="transfers-date-header">${escapeHtml(group.date)}</div>`;
        const items = group.transfers.map(t => renderTransferItem(t)).join('');
        return dateHeader + items;
    }).join('');

    container.innerHTML = html;
}

// ===========================================
// INITIALIZATION
// ===========================================
async function fetchCategories() {
    try {
        const data = await dataService.fetchCategories();
        if (data) {
            globalCategories = data;
        } else {
            console.warn('Categories loaded is empty/null');
        }
    } catch (e) {
        console.error('Failed to load categories', e);
    }
}

// ===========================================
// SETTINGS UI LOGIC
// ===========================================
function initSettingsUI() {
    const btnOpen = document.getElementById('btn-settings');
    const modal = document.getElementById('modal-settings');
    const btnCancel = document.getElementById('btn-cancel-settings');
    const btnSave = document.getElementById('btn-save-settings');
    const toggles = document.querySelectorAll('.source-toggle-option');
    const remoteFields = document.getElementById('settings-remote-fields');
    const authStatus = document.getElementById('auth-status');

    // Inputs
    const inputToken = document.getElementById('input-token');
    const inputOwner = document.getElementById('input-owner');
    const inputRepo = document.getElementById('input-repo');
    const inputBranch = document.getElementById('input-branch');
    const inputPath = document.getElementById('input-path');

    let currentSource = dataService.config.sourceType;

    // Helper: Update UI State based on source
    const updateUIState = (source) => {
        currentSource = source;
        toggles.forEach(t => {
            const active = t.dataset.source === source;
            t.classList.toggle('active', active);
            t.setAttribute('aria-selected', active ? 'true' : 'false');
        });

        if (source === 'remote') {
            remoteFields.classList.add('visible');
        } else {
            remoteFields.classList.remove('visible');
        }
    };

    let lastFocusedBeforeModal = null;
    const onEscClose = (e) => {
        if (e.key === 'Escape') closeModal();
    };

    // Open Modal
    btnOpen.addEventListener('click', () => {
        // Load current config into fields
        inputToken.value = dataService.config.githubToken || '';
        inputOwner.value = dataService.config.owner || 'loyegor';
        inputRepo.value = dataService.config.repo || 'finances';
        inputBranch.value = dataService.config.branch || 'main';
        inputPath.value = dataService.config.path || 'data';
        updateUIState(dataService.config.sourceType);

        authStatus.style.display = 'none';
        modal.style.display = 'flex';

        lastFocusedBeforeModal = document.activeElement;
        document.addEventListener('keydown', onEscClose);
        // Focus the first toggle so keyboard users land inside the modal
        const firstFocusable = modal.querySelector('.source-toggle-option.active') || btnCancel;
        firstFocusable?.focus();
    });

    // Close Modal
    const closeModal = () => {
        modal.style.display = 'none';
        document.removeEventListener('keydown', onEscClose);
        if (lastFocusedBeforeModal && typeof lastFocusedBeforeModal.focus === 'function') {
            lastFocusedBeforeModal.focus();
        }
    };
    btnCancel.addEventListener('click', closeModal);

    // Click on the dark overlay (outside .modal-content) closes the modal
    modal.addEventListener('click', (e) => {
        if (e.target === modal) closeModal();
    });

    // Toggle Source
    toggles.forEach(t => {
        t.addEventListener('click', () => {
            updateUIState(t.dataset.source);
        });
    });

    // Save Config
    btnSave.addEventListener('click', async () => {
        // 1. Gather values
        const newConfig = {
            sourceType: currentSource,
            githubToken: inputToken.value.trim(),
            owner: inputOwner.value.trim(),
            repo: inputRepo.value.trim(),
            branch: inputBranch.value.trim(),
            path: inputPath.value.trim()
        };

        // 2. If Remote, Validate logic
        if (currentSource === 'remote') {
            authStatus.className = 'auth-status loading';
            authStatus.style.display = 'block';
            authStatus.textContent = 'Checking connection...';

            // Temporarily update service to test
            const oldConfig = { ...dataService.config };
            dataService.config = { ...dataService.config, ...newConfig }; // Temporary apply for test

            const result = await dataService.testConnection();

            if (result.success) {
                authStatus.className = 'auth-status success';
                authStatus.textContent = result.message;

                // Commit save
                dataService.saveConfig(newConfig);
                setTimeout(() => {
                    closeModal();
                    location.reload(); // Refresh app to load new data
                }, 1000);
            } else {
                authStatus.className = 'auth-status error';
                authStatus.textContent = 'Error: ' + result.message;
                // Revert config if test failed (optional, depending on UX. Usually better to let them save anyway? 
                // But user asked for reliable workflow. Let's block save on error or at least warn.)
                // We'll keep the bad config in the service instance (since we modified it) but NOT save to localStorage if we wanted to be strict.
                // However, user might want to save and fix later.
                // Let's allow saving even if error, but warn. 
                // Wait, "Verify connection" is crucial. Let's NOT save to localStorage if it fails completely? 
                // No, let's allow saving but keep modal open.

                // Reverting instance config to be safe so we don't break the app immediately if they cancel
                dataService.config = oldConfig;
            }
        } else {
            // Local mode - just save and reload
            dataService.saveConfig(newConfig);
            closeModal();
            location.reload();
        }
    });
}

// ===========================================
// MONTH NAVIGATION — live-drag swipe + animated transitions (arrows / selector)
// ===========================================
// `canvas` is intentionally NOT in this list — swipe is more common than chart
// taps, and the 8px horizontal threshold already keeps quick taps working.
// When a swipe commits, preventDefault on touchmove suppresses the synthesised
// click so Chart.js doesn't react.
const IGNORE_SELECTOR = 'select, input, textarea, button, .modal-overlay, .toggle-btn, .delta, .forecast-value, .perf-value, .perf-tab';
const DOMINANT_THRESHOLD = 8;   // px to decide horizontal vs vertical intent
const COMMIT_DISTANCE = 60;     // px past which the drag commits
const COMMIT_VELOCITY = 0.5;    // px/ms flick velocity that also commits

let isMonthAnimating = false;

function cardElements() {
    return [
        document.getElementById('performance-summary'),
        document.getElementById('view-portfolio')
    ].filter(Boolean);
}

function applyCardTransform(tx, opacity, withTransition) {
    cardElements().forEach(el => {
        el.style.transition = withTransition
            ? 'transform 0.22s cubic-bezier(0.22,1,0.36,1), opacity 0.22s ease'
            : 'none';
        el.style.transform = tx;
        el.style.opacity = String(opacity);
    });
}

function resetCardInlineStyles() {
    cardElements().forEach(el => {
        el.style.transition = '';
        el.style.transform = '';
        el.style.opacity = '';
    });
}

// Perform a month switch with slide-out → render → slide-in animation.
// direction: 'next' (new month ahead) → card exits left, new enters from right;
// direction: 'prev' (new month before) → card exits right, new enters from left.
function prefersReducedMotion() {
    return window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
}

// Document-relative top of an element (sum of offsetTop up the offsetParent
// chain). Independent of current scroll position and CSS transforms — both
// matter here because renderPortfolio clears innerHTML mid-transition (which
// can momentarily clamp scrollY) and the card is mid-slide-animation.
function getDocumentTop(el) {
    let top = 0;
    while (el) {
        top += el.offsetTop;
        el = el.offsetParent;
    }
    return top;
}

// Snapshot which category is at the top of the viewport so we can restore the
// same vertical context after the new month renders. Called at the very start
// of a month transition (swipe / arrows / selector). If nothing is visible
// (e.g. user is above the portfolio list), clears the anchor so the new month
// renders at top.
function captureScrollAnchor() {
    const cards = document.querySelectorAll('#portfolio-list .category-block[data-cat-id]');
    if (!cards.length) {
        pendingScrollAnchor = null;
        return;
    }
    const scrollTop = window.scrollY || window.pageYOffset || 0;
    let best = null;
    cards.forEach(el => {
        const top = getDocumentTop(el);
        const distance = Math.abs(top - scrollTop);
        if (best === null || distance < best.distance) {
            best = { catId: el.dataset.catId, top, distance };
        }
    });
    pendingScrollAnchor = best
        ? { catId: best.catId, offset: scrollTop - best.top }
        : null;
}

function restoreScrollAnchor() {
    if (!pendingScrollAnchor) return;
    const target = document.querySelector(
        `#portfolio-list .category-block[data-cat-id="${pendingScrollAnchor.catId}"]`
    );
    if (target) {
        const newTop = getDocumentTop(target);
        // Positional scrollTo(x, y) is synchronous and universally supported;
        // the object form with behavior:'instant' is flakey on older mobile Safari.
        window.scrollTo(0, Math.max(0, newTop + pendingScrollAnchor.offset));
    }
    pendingScrollAnchor = null;
}

// Track <details> open/close in a Set so the same accordions reopen on next
// render. `toggle` doesn't bubble, so attach in capture phase on the container.
function setupAccordionStateTracking() {
    const listContainer = document.getElementById('portfolio-list');
    if (!listContainer) return;
    listContainer.addEventListener('toggle', (e) => {
        const details = e.target;
        if (!details || details.tagName !== 'DETAILS') return;
        const block = details.closest('.category-block[data-cat-id]');
        if (!block) return;
        const catId = block.dataset.catId;
        if (details.open) openCategoryIds.add(catId);
        else openCategoryIds.delete(catId);
    }, true);
}

async function animateMonthTransition(nextId, direction, currentDx = 0) {
    if (!nextId || nextId === currentMonthId) {
        snapCardBack();
        return;
    }
    // Capture only when we know we're actually navigating to a different month.
    captureScrollAnchor();
    isMonthAnimating = true;

    // Skip the slide animation entirely if the user prefers reduced motion.
    if (prefersReducedMotion()) {
        resetCardInlineStyles();
        const selector = document.getElementById('monthSelector');
        if (selector) selector.value = nextId;
        await loadMonth(nextId);
        isMonthAnimating = false;
        return;
    }

    const dir = direction === 'next' ? -1 : 1; // sign for exit translation
    const exitX = `${dir * (window.innerWidth + 60)}px`;

    // Slide current content out in gesture direction
    applyCardTransform(`translateX(${exitX})`, 0, true);

    // Wait for animation to finish (approx transition duration)
    await new Promise(r => setTimeout(r, 220));

    // Keep the selector in sync before the render
    const selector = document.getElementById('monthSelector');
    if (selector) selector.value = nextId;

    // Render the target month (cached → instant)
    await loadMonth(nextId);

    // Place card off-screen on the *opposite* side without transition
    const incomingX = `${-dir * (window.innerWidth + 60)}px`;
    applyCardTransform(`translateX(${incomingX})`, 0, false);

    // Force reflow so the next transition animates from this offset
    void document.body.offsetHeight;

    // Animate in to centre
    applyCardTransform('translateX(0)', 1, true);

    // Clean up inline styles after animation completes
    setTimeout(() => {
        resetCardInlineStyles();
        isMonthAnimating = false;
    }, 260);
}

function snapCardBack() {
    applyCardTransform('translateX(0)', 1, true);
    setTimeout(() => {
        resetCardInlineStyles();
    }, 240);
}

// Drag-during-touch: apply partial transform that follows the finger.
function applyDragTransform(dx) {
    const opacity = Math.max(0.4, 1 - Math.abs(dx) / 700);
    // Gentle resistance so the card slightly lags behind the finger
    const translated = dx * 0.75;
    applyCardTransform(`translateX(${translated}px)`, opacity, false);
}

// Unified entry for month navigation from arrows / selector.
function navigateToMonthAnimated(nextId) {
    if (!availableMonths || !currentMonthId) {
        loadMonth(nextId);
        return;
    }
    if (isMonthAnimating) return;
    const currentIdx = availableMonths.findIndex(m => m.id === currentMonthId);
    const newIdx = availableMonths.findIndex(m => m.id === nextId);
    if (currentIdx === -1 || newIdx === -1 || newIdx === currentIdx) {
        loadMonth(nextId);
        return;
    }
    const direction = newIdx > currentIdx ? 'next' : 'prev';
    animateMonthTransition(nextId, direction);
}

function setupSwipeNavigation() {
    let state = 'idle'; // idle | tracking | committed
    let startX = 0, startY = 0, startTime = 0, startTarget = null;

    document.body.addEventListener('touchstart', (e) => {
        if (e.touches.length !== 1 || isMonthAnimating) {
            state = 'idle';
            return;
        }
        const t = e.touches[0];
        startX = t.clientX;
        startY = t.clientY;
        startTime = Date.now();
        startTarget = e.target;
        state = 'tracking';
    }, { passive: true });

    document.body.addEventListener('touchmove', (e) => {
        if (state === 'idle' || e.touches.length !== 1) return;
        const t = e.touches[0];
        const dx = t.clientX - startX;
        const dy = t.clientY - startY;

        if (state === 'tracking') {
            // Vertical intent dominates → leave alone (native scroll)
            if (Math.abs(dy) > DOMINANT_THRESHOLD && Math.abs(dy) > Math.abs(dx)) {
                state = 'idle';
                return;
            }
            // Horizontal intent confirmed
            if (Math.abs(dx) > DOMINANT_THRESHOLD && Math.abs(dx) > Math.abs(dy)) {
                if (startTarget?.closest?.(IGNORE_SELECTOR)) { state = 'idle'; return; }
                const modal = document.getElementById('modal-settings');
                if (modal && modal.style.display !== 'none') { state = 'idle'; return; }
                if (!availableMonths || availableMonths.length === 0 || !currentMonthId) { state = 'idle'; return; }
                state = 'committed';
            } else {
                return;
            }
        }

        if (state === 'committed') {
            if (e.cancelable) e.preventDefault();
            applyDragTransform(dx);
        }
    }, { passive: false });

    const finishTouch = (e) => {
        if (state !== 'committed') {
            state = 'idle';
            return;
        }
        const t = e.changedTouches[0];
        const dx = t.clientX - startX;
        const dt = Date.now() - startTime;
        const velocity = dt > 0 ? Math.abs(dx) / dt : 0;

        state = 'idle';

        const idx = availableMonths.findIndex(m => m.id === currentMonthId);
        const committed = Math.abs(dx) > COMMIT_DISTANCE || velocity > COMMIT_VELOCITY;

        if (committed) {
            const delta = dx < 0 ? +1 : -1;
            const newIdx = idx + delta;
            if (newIdx >= 0 && newIdx < availableMonths.length) {
                const nextId = availableMonths[newIdx].id;
                const direction = delta > 0 ? 'next' : 'prev';
                const selector = document.getElementById('monthSelector');
                if (selector) selector.value = nextId;
                animateMonthTransition(nextId, direction, dx);
                return;
            }
        }
        snapCardBack();
    };

    document.body.addEventListener('touchend', finishTouch, { passive: true });
    document.body.addEventListener('touchcancel', finishTouch, { passive: true });
}

// ===========================================
// MONTH ARROW BUTTONS
// ===========================================
function setupMonthArrows() {
    const prev = document.getElementById('btn-prev-month');
    const next = document.getElementById('btn-next-month');

    const navBy = (delta) => {
        if (isMonthAnimating) return;
        if (!availableMonths || !currentMonthId) return;
        const idx = availableMonths.findIndex(m => m.id === currentMonthId);
        const newIdx = idx + delta;
        if (newIdx < 0 || newIdx >= availableMonths.length) return;
        navigateToMonthAnimated(availableMonths[newIdx].id);
    };

    prev?.addEventListener('click', () => navBy(-1));
    next?.addEventListener('click', () => navBy(+1));
}

function updateMonthArrows() {
    const prev = document.getElementById('btn-prev-month');
    const next = document.getElementById('btn-next-month');
    if (!availableMonths || !currentMonthId) {
        if (prev) prev.disabled = true;
        if (next) next.disabled = true;
        return;
    }
    const idx = availableMonths.findIndex(m => m.id === currentMonthId);
    if (prev) prev.disabled = idx <= 0;
    if (next) next.disabled = idx >= availableMonths.length - 1;
}

// Pulls the active CSS theme colors and applies them as Chart.js defaults
// (legend labels, axis text, gridlines). Chart.js doesn't auto-respect
// prefers-color-scheme; we wire it explicitly.
function applyChartTheme() {
    if (typeof Chart === 'undefined') return;
    const cs = getComputedStyle(document.documentElement);
    Chart.defaults.color = cs.getPropertyValue('--color-text-muted').trim() || '#4a5568';
    Chart.defaults.borderColor = cs.getPropertyValue('--color-border').trim() || '#e2e8f0';
}

// Theme toggle — session-only manual override of the OS preference.
// No localStorage; reload reverts to system. CSS handles the actual switch via
// the `data-theme` attribute on <html>; we just flip it and re-paint Chart.js.
function getCurrentTheme() {
    const explicit = document.documentElement.getAttribute('data-theme');
    if (explicit) return explicit;
    return window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches
        ? 'dark' : 'light';
}
function setupThemeToggle() {
    const btn = document.getElementById('btn-theme');
    if (!btn) return;
    btn.addEventListener('click', () => {
        const next = getCurrentTheme() === 'dark' ? 'light' : 'dark';
        document.documentElement.setAttribute('data-theme', next);
        applyChartTheme();
        if (portfolioChart) portfolioChart.update('none');
        if (performanceChart) performanceChart.update('none');
    });
}

async function init() {
    // Init Settings UI
    initSettingsUI();
    setupThemeToggle();

    applyChartTheme();
    // Re-theme charts if the OS toggles between light and dark mid-session.
    if (window.matchMedia) {
        window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => {
            applyChartTheme();
            if (portfolioChart) portfolioChart.update('none');
            if (performanceChart) performanceChart.update('none');
        });
    }

    // 0. Load shared categories
    await fetchCategories();

    // 1. Populate month selector
    // Catch initial error if remote is configured but invalid
    try {
        await populateMonthSelector();
    } catch (e) {
        showError('Initialization error. Check settings.');
    }

    // 2. Setup tab handlers
    setupTabHandlers();
    setupChartToggle();
    setupPerfViewToggle();
    setupSwipeNavigation();
    setupMonthArrows();
    setupPerfChartTooltipDismiss();
    setupAccordionStateTracking();
    setupCalcTooltipDismiss();
}

// Start the app
init();
