/**
 * Investment Portfolio Tracker
 * Main Application Logic
 */

// ===========================================
// CONFIGURATION
// ===========================================
const START_DATE = new Date('2026-01-01');

const MONTH_NAMES = [
    'Январь', 'Февраль', 'Март', 'Апрель', 'Май', 'Июнь',
    'Июль', 'Август', 'Сентябрь', 'Октябрь', 'Ноябрь', 'Декабрь'
];

// ===========================================
// GLOBAL STATE
// ===========================================
let portfolioChart = null;
let currentMonthId = null;
let currentPortfolioData = null;
let currentTransfersData = [];
let currentSnapshot = null;
let previousSnapshot = null;
let currentComparison = null;
let yieldHistory = []; // Array of monthly yield percentages for forecasting
let globalCategories = {}; // Unified Category Definitions

// ===========================================
// UTILITY: Format currency
// ===========================================
function formatMoney(value) {
    const sign = value >= 0 ? '' : '-';
    return sign + Math.abs(value).toLocaleString('ru-RU', { minimumFractionDigits: 2 }) + ' $';
}

// ===========================================
// UTILITY: Get Badge Class
// ===========================================
function getBadgeClass(source) {
    if (!source) return 'b-default';
    // Clean string: "Google Token" -> "b-google-token"
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

    if (newMode === 'money') {
        el.innerText = el.getAttribute('data-money');
    } else {
        el.innerText = el.getAttribute('data-pct');
    }
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
        return `${sign}${formatMoney(val)}`;
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
}


// 1. Fetch data for all months from Jan to current
async function fetchYearSequence(currentMonthId) {
    const year = currentMonthId.split('-')[0];
    const monthIndex = parseInt(currentMonthId.split('-')[1]);
    const sequence = [];

    // Fetch in parallel
    const promises = [];
    for (let m = 1; m <= monthIndex; m++) {
        const id = `${year}-${String(m).padStart(2, '0')}`;
        promises.push(Promise.all([
            fetchPortfolioData(id),
            fetchTransfersForMonth(id)
        ]).then(([data, transfers]) => ({
            id,
            data,
            transfers: transfers.flatMap(t => t.transfers) // Flatten immediately
        })));
    }

    const results = await Promise.all(promises);

    // Sort by id ensures Jan -> Feb -> ...
    return results.sort((a, b) => a.id.localeCompare(b.id));
}

// 2. Modified Dietz Yield (Isolated Month)
// Yield = (End - Start - NetFlow) / (Start + NetFlow/2)
// 2. Simple Yield (Profit / Invested Capital)
// User Formula: Yield = (End - (Start + Deposits - Withdraws)) / (Start + Deposits)
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

// 5. Orchestrator
// 5. Orchestrator
async function calculateYearStats(currentMonthId) {
    // 1. Fetch sequence of months from Jan up to currentMonthId
    const sequence = await fetchYearSequence(currentMonthId);

    const yields = [];
    let currentMonthYield = 0;
    let currentMonthProfit = 0;

    // Initialize Start Balance for Jan 1st as 0 (Assumption: Portfolio starts fresh or rollover not tracked yet)
    // In a real system, we would need the Dec 31st snapshot of previous year.
    let prevBalance = 0;
    let accumulatedNetFlow = 0; // To track invested capital for YTD profit

    // Iterate sequentially through months to build the chain
    for (const item of sequence) {
        if (!item.data) {
            // If data is missing for a month, break chain? or assume flat?
            // We assume 0 yield for missing month to keep index sync
            yields.push(0);
            continue;
        }

        // 1. Calculate End Balance for this month
        // We take the "base" portfolio from JSON and merge transfers to get the actual final state
        const monthData = JSON.parse(JSON.stringify(item.data)); // Deep copy to not mutate cache if any
        mergeTransfers(monthData, item.transfers);
        const endBalance = calculateTotalBalance(monthData);

        // 2. Start Balance
        // For the very first month (Jan), prevBalance is 0. 
        // For subsequent months, it's the endBalance of the previous iteration.
        const startBalance = prevBalance;

        // Calc Net Flow for this month
        let mDeposits = 0;
        let mWithdraws = 0;
        if (item.transfers) {
            item.transfers.forEach(t => {
                if (t.type === 'deposit') mDeposits += t.amount;
                if (t.type === 'withdraw') mWithdraws += t.amount;
            });
        }
        const mNetFlow = mDeposits - mWithdraws;

        // 3. Calculate Yield for this specific month
        let yieldVal = 0;
        let profitVal = 0;

        // ZERO KILOMETER LOGIC FOR FORECAST
        if (sequence.indexOf(item) === 0) {
            yieldVal = 0;
            profitVal = 0;
            // For first month, we treat EndBalance as the initial capital injection (if start is 0)
            // So accumulated Net Flow becomes the EndBalance 
            accumulatedNetFlow += endBalance;
        } else {
            yieldVal = calculateSimpleYield(startBalance, endBalance, item.transfers);
            profitVal = endBalance - (startBalance + mNetFlow);
            accumulatedNetFlow += mNetFlow;
        }

        yields.push(yieldVal);

        // If this is the selected month, save its specific yield for display
        if (item.id === currentMonthId) {
            currentMonthYield = yieldVal;
            currentMonthProfit = profitVal;
        }

        // 4. Prepare for next month
        prevBalance = endBalance;
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

    return {
        monthYield: currentMonthYield,
        monthProfit: currentMonthProfit,
        ytd: ytd,
        ytdProfit: ytdProfit,
        projected: projected,
        projectedProfit: projectedProfit
    };
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

// checkFileExists removed (logic moved to DataService)

// ===========================================
// GET AVAILABLE MONTHS (VIA DATA SERVICE)
// ===========================================
async function getAvailableMonths() {
    try {
        return await dataService.getAvailableMonths(generateCandidateMonths);
    } catch (e) {
        console.error('Error getting available months:', e);
        showError('Не удалось получить список месяцев. Проверьте настройки источника данных.');
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

    if (currentMode === 'percent') {
        btn.innerText = btn.getAttribute('data-money');
        btn.setAttribute('data-mode', 'money');
        btn.style.backgroundColor = '#e2e8f0';
    } else {
        btn.innerText = btn.getAttribute('data-pct');
        btn.setAttribute('data-mode', 'percent');
        btn.style.backgroundColor = '#edf2f7';
    }
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
window.showCalcTooltip = function (e, el) {
    e.preventDefault();
    e.stopPropagation();

    // Remove any existing tooltip
    const existing = document.getElementById('calc-tooltip');
    if (existing) existing.remove();

    const calcText = decodeURIComponent(el.getAttribute('data-calc'));

    // Create tooltip element
    const tooltip = document.createElement('div');
    tooltip.id = 'calc-tooltip';
    tooltip.textContent = calcText;

    document.body.appendChild(tooltip);

    // Position tooltip
    const rect = el.getBoundingClientRect();
    tooltip.style.left = rect.left + 'px';
    tooltip.style.top = (rect.bottom + 8) + 'px';

    // Close on click outside
    const closeHandler = (event) => {
        if (!tooltip.contains(event.target) && event.target !== el) {
            tooltip.remove();
            document.removeEventListener('click', closeHandler);
        }
    };
    setTimeout(() => document.addEventListener('click', closeHandler), 10);
};

// ===========================================
// POPULATE MONTH SELECTOR
// ===========================================
// ===========================================
// POPULATE MONTH SELECTOR
// ===========================================
async function populateMonthSelector() {
    const selector = document.getElementById('monthSelector');
    selector.innerHTML = '<option>Загрузка...</option>'; // Temporary loading state

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
        selector.innerHTML = '<option>Нет доступных данных</option>';
    }

    // Add change event listener
    selector.addEventListener('change', (e) => {
        loadMonth(e.target.value);
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
function calculatePerformance(startBalance, endBalance, transfers, isFirstMonth = false) {
    // ZERO KILOMETER LOGIC (First Month)
    if (isFirstMonth) {
        return {
            startBalance: 0,
            endBalance,
            totalDeposits: endBalance, // Treat everything as a deposit
            totalWithdraws: 0,
            netFlow: endBalance,
            profit: 0, // Forced 0
            yieldPercent: 0 // Forced 0
        };
    }

    // NORMAL LOGIC
    // Calculate deposits and withdrawals separately
    let totalDeposits = 0;
    let totalWithdraws = 0;

    if (transfers && Array.isArray(transfers)) {
        transfers.forEach(t => {
            if (t.type === 'deposit') {
                totalDeposits += t.amount;
            } else if (t.type === 'withdraw') {
                totalWithdraws += t.amount;
            }
        });
    }

    // Net flow = Deposits - Withdrawals
    const netFlow = totalDeposits - totalWithdraws;

    // Profit = EndBalance - (StartBalance + NetFlow)
    const profit = endBalance - (startBalance + netFlow);

    // Yield % = Profit / (StartBalance + Deposits) * 100
    // Simple Return on Invested Capital
    const investedCapital = startBalance + totalDeposits;

    let yieldPercent = 0;
    if (investedCapital > 0.01) {
        yieldPercent = (profit / investedCapital) * 100;
    } else if (profit > 0) {
        // Edge case: Profit with 0 capital (Airdrops etc with no cost basis)
        yieldPercent = 100; // Cap at 100% or consider Infinite? User prefers simple.
    }

    return {
        startBalance,
        endBalance,
        totalDeposits,
        totalWithdraws,
        netFlow,
        profit,
        yieldPercent
    };
}

// ===========================================
// UPDATE FORECAST UI
// ===========================================
// (Old forecast logic removed)

// ===========================================
// MERGE TRANSFERS INTO PORTFOLIO (VIRTUAL BALANCE)
// ===========================================
function mergeTransfers(portfolioData, transfers) {
    if (!transfers || transfers.length === 0) return portfolioData;
    if (!portfolioData || !portfolioData.portfolio) return portfolioData;

    transfers.forEach(t => {
        if (t.type === 'deposit') {
            applyTransferOperation(portfolioData, t.category, t.source, t.name, t.amount);
        } else if (t.type === 'withdraw') {
            applyTransferOperation(portfolioData, t.category, t.source, t.name, -t.amount);
        } else if (t.type === 'move') {
            applyTransferOperation(portfolioData, t.from_category, t.from_source, t.from_name, -t.amount);
            applyTransferOperation(portfolioData, t.to_category, t.to_source, t.to_name, t.amount);
        }
    });

    return portfolioData;
}

function applyTransferOperation(portfolioData, categoryId, source, assetName, amount) {
    // Find category by id (strict match)
    let category = portfolioData.portfolio.find(c => c.id === categoryId);

    if (!category) {
        // Create new category if not found
        category = {
            id: categoryId,
            title: categoryId, // Will show as-is if new
            color: '#cbd5e0', // Default GRAY
            items: []
        };
        portfolioData.portfolio.push(category);
    }

    // Find asset by name + source (strict match)
    let asset = category.items.find(i => i.name === assetName && i.source === source);

    if (!asset) {
        asset = {
            name: assetName,
            source: source,
            val: 0
        };
        category.items.push(asset);
    }

    // Store original value before first adjustment
    if (asset.originalVal === undefined) {
        asset.originalVal = asset.val;
    }

    // Track adjustment history for tooltip
    if (!asset.adjustmentHistory) {
        asset.adjustmentHistory = [];
    }
    asset.adjustmentHistory.push(amount);

    // Apply adjustment
    asset.val += amount;

    // Mark as virtual
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
    document.getElementById('perf-deposits').textContent =
        '+' + formatMoney(performance.totalDeposits);

    // Withdraws (always show as positive number with minus context)
    document.getElementById('perf-withdraws').textContent =
        '-' + formatMoney(performance.totalWithdraws);

    // Net Flow (with sign and color)
    const netFlowEl = document.getElementById('perf-netflow');
    const netFlowSign = performance.netFlow >= 0 ? '+' : '';
    netFlowEl.textContent = netFlowSign + formatMoney(performance.netFlow);
    netFlowEl.classList.remove('perf-positive', 'perf-negative');
    if (performance.netFlow > 0) {
        netFlowEl.classList.add('perf-positive');
    } else if (performance.netFlow < 0) {
        netFlowEl.classList.add('perf-negative');
    }

    if (isFirstMonth) {
        // Zero Kilometer: Show 0 PnL, don't hide
        // Ensure standard display logic runs below
    }

    // Show PnL tab for all months (including first)
    if (pnlTab) pnlTab.style.display = '';

    // Show PnL tab for non-first months
    if (pnlTab) pnlTab.style.display = '';

    // PnL with tooltip
    const pnlEl = document.getElementById('perf-pnl');
    const pnlTabEl = document.getElementById('tab-pnl');
    const pnlSign = performance.profit >= 0 ? '+' : '';
    const yieldStr = performance.yieldPercent.toFixed(2);
    pnlEl.textContent = `${pnlSign}${formatMoney(performance.profit)} (${pnlSign}${yieldStr}%)`;

    // Set tooltip with start balance
    pnlTabEl.title = `Старт месяца: ${formatMoney(performance.startBalance)}`;

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

// Fetch ALL transfer files for a specific month
// Tries: transfers-YYYY-MM.json and transfers-YYYY-MM-DD.json for days 01-31
async function fetchTransfersForMonth(monthId) {
    const allTransfers = [];

    // Generate all possible filenames for this month
    const filenames = [`transfers-${monthId}.json`];

    // Add day-specific files (01-31)
    for (let day = 1; day <= 31; day++) {
        const dayStr = String(day).padStart(2, '0');
        filenames.push(`transfers-${monthId}-${dayStr}.json`);
    }

    // Fetch all in parallel
    const promises = filenames.map(async (filename) => {
        const data = await fetchTransfersData(filename);
        if (data && data.transfers && data.transfers.length > 0) {
            return {
                date: data.meta?.date || monthId,
                transfers: data.transfers
            };
        }
        return null;
    });

    const results = await Promise.all(promises);

    // Combine and sort by date (oldest first)
    results.forEach(r => {
        if (r) allTransfers.push(r);
    });

    allTransfers.sort((a, b) => a.date.localeCompare(b.date));

    return allTransfers;
}

// ===========================================
// LOAD MONTH DATA
// ===========================================
async function loadMonth(monthId) {
    const listContainer = document.getElementById('portfolio-list');

    // Show loading state
    listContainer.innerHTML = '<div class="loading">Загрузка данных...</div>';
    currentMonthId = monthId;

    // Determine previous month
    const prevMonthId = getPreviousMonthId(monthId);
    const isFirstMonth = prevMonthId === null;

    try {
        // Load all data in parallel: Current Portfolio, Previous Portfolio, Current Transfers, Previous Transfers
        const [currentData, prevData, monthTransfers, prevMonthTransfers] = await Promise.all([
            fetchPortfolioData(monthId),
            prevMonthId ? fetchPortfolioData(prevMonthId) : Promise.resolve(null),
            fetchTransfersForMonth(monthId),
            prevMonthId ? fetchTransfersForMonth(prevMonthId) : Promise.resolve([])
        ]);

        // Flatten all transfers
        const flatTransfers = monthTransfers.flatMap(g => g.transfers);
        const prevFlatTransfers = prevMonthTransfers.flatMap(g => g.transfers);

        // Check if current month data exists
        if (!currentData) {
            const monthInfo = availableMonths.find(m => m.id === monthId);
            const label = monthInfo ? monthInfo.label : monthId;
            showError(`Данные за ${label} не найдены.<br>Создайте файл <code>data/${monthId}.json</code>`);
            return;
        }

        // Validate data format
        if (!currentData.portfolio || !Array.isArray(currentData.portfolio)) {
            throw new Error('Invalid data format: missing portfolio array');
        }

        // Store current data globally
        currentPortfolioData = currentData;
        currentTransfersData = flatTransfers;

        // --- VIRTUAL BALANCE MAGIC ---
        // Apply transfers to BOTH current and previous data to get "Virtual Balances"
        mergeTransfers(currentPortfolioData, currentTransfersData);
        if (prevData) {
            mergeTransfers(prevData, prevFlatTransfers);
        }
        // -----------------------------

        // --- MOM COMPARISON ---
        // Build snapshots AFTER merge (so we compare virtual balances)
        currentSnapshot = buildSnapshot(currentPortfolioData);
        previousSnapshot = prevData ? buildSnapshot(prevData) : null;

        // Get adjustments per asset for adjusted start calculation
        const adjustments = getAdjustmentsPerAsset(flatTransfers, currentPortfolioData);

        // Compare snapshots
        currentComparison = previousSnapshot
            ? compareSnapshots(currentSnapshot, previousSnapshot, adjustments)
            : null;
        // ---------------------

        // Calculate balances (AFTER merge)
        const endBalance = calculateTotalBalance(currentPortfolioData);
        const startBalance = prevData ? calculateTotalBalance(prevData) : 0;

        // Calculate performance
        const performance = calculatePerformance(startBalance, endBalance, currentTransfersData, isFirstMonth);

        // --- FORECASTING 2.0 ---
        try {
            const forecastStats = await calculateYearStats(monthId);
            updateForecastUI(forecastStats);
        } catch (err) {
            console.error('Forecasting error:', err);
            updateForecastUI(null);
        }
        // -----------------------

        // Update UI
        updatePerformanceUI(performance, isFirstMonth, flatTransfers.length > 0);
        renderPortfolio(currentData, currentComparison);
        renderTransfers(monthTransfers, currentData);

        // Reset to portfolio view
        switchTab('portfolio');

    } catch (error) {
        console.error('Failed to load portfolio data:', error);
        showError(`Ошибка загрузки данных: ${error.message}`);
    }
}

// ===========================================
// RENDER PORTFOLIO
// ===========================================
function renderPortfolio(data, comparison = null) {
    const listContainer = document.getElementById('portfolio-list');
    const chartCanvas = document.getElementById('portfolioChart');

    // Clear containers
    listContainer.innerHTML = '';

    // Destroy previous chart if exists
    if (portfolioChart) {
        portfolioChart.destroy();
        portfolioChart = null;
    }

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

    // Prepare chart data
    const chartLabels = [];
    const chartValues = [];
    const chartColors = [];

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
        const money = cat.total.toLocaleString('ru-RU', { minimumFractionDigits: 2 }) + ' $';

        // Category delta badge
        const catComparison = comparison?.categories?.[cat.id];
        const catDeltaBadge = formatDeltaBadge(catComparison);

        chartLabels.push(cat.title);
        chartValues.push(cat.total);
        chartColors.push(cat.color);

        const section = document.createElement('div');
        section.className = 'category-block';

        const rows = cat.items.map(item => {
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

            let displayVal = item.val.toLocaleString('ru-RU', { minimumFractionDigits: 2 }) + ' $';

            if (item.isVirtual) {
                // Build calculation breakdown for tooltip
                const originalVal = item.originalVal || 0;
                let calcParts = [formatMoney(originalVal)];
                if (item.adjustmentHistory) {
                    item.adjustmentHistory.forEach(adj => {
                        const sign = adj >= 0 ? '+' : '';
                        calcParts.push(`${sign}${formatMoney(adj)}`);
                    });
                }
                calcParts.push(`= ${formatMoney(item.val)}`);
                const tooltipText = calcParts.join('\n');

                // Add red asterisk with click handler to show tooltip
                const itemId = `virtual-${item.name}-${item.source}`.replace(/[^a-z0-9]/gi, '-');
                displayVal = `<span class="virtual-marker" id="${itemId}" data-calc="${encodeURIComponent(tooltipText)}" onclick="showCalcTooltip(event, this)"><span style="color: #e53e3e; font-weight: bold; cursor: pointer; margin-right: 4px;">*</span>${displayVal}</span>`;
            }

            return `
            <tr class="${rowClass}">
                <td>
                    <div class="asset-name">
                        ${item.name}
                        <span class="badge ${getBadgeClass(item.source)}">${item.source}</span>${newBadge}
                    </div>
                </td>
                <td class="amount">${deltaBadge}${displayVal}</td>
            </tr>
        `;
        }).join('');

        section.innerHTML = `
            <details>
                <summary>
                    <div class="header-title">
                        <span class="color-dot" style="background-color: ${cat.color};"></span>
                        <span>${cat.title}</span>
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

    // Format grand total string
    const grandTotalStr = grandTotal.toLocaleString('ru-RU', { minimumFractionDigits: 2 }) + ' $';

    // Plugin to draw text in center of the doughnut (chartArea) with specified font size
    const centerTextPlugin = {
        id: 'centerText',
        beforeDraw: function (chart) {
            const ctx = chart.ctx;
            const { top, bottom, left, right } = chart.chartArea;
            const centerX = (left + right) / 2;
            const centerY = (top + bottom) / 2;

            ctx.save();
            const fontSize = 16; // px
            ctx.font = `800 ${fontSize}px monospace`;
            ctx.fillStyle = '#2d3748';
            ctx.textAlign = 'center';
            ctx.textBaseline = 'middle';

            ctx.fillText(grandTotalStr, centerX, centerY);
            ctx.restore();
        }
    };

    // Create Chart.js doughnut chart
    const ctx = chartCanvas.getContext('2d');
    portfolioChart = new Chart(ctx, {
        type: 'doughnut',
        data: {
            labels: chartLabels,
            datasets: [{
                data: chartValues,
                backgroundColor: chartColors,
                borderWidth: 0,
                hoverOffset: 6
            }]
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
                        font: { size: 11 }
                    }
                },
                tooltip: {
                    callbacks: {
                        label: function (context) {
                            const val = context.raw;
                            const pct = ((val / grandTotal) * 100).toFixed(2) + '%';
                            return ` ${pct} (${val.toLocaleString('ru-RU')} $)`;
                        }
                    }
                }
            }
        }
    });
}

// ===========================================
// SHOW ERROR
// ===========================================
function showError(message) {
    const listContainer = document.getElementById('portfolio-list');
    listContainer.innerHTML = `<div class="error">${message}</div>`;
}

// (Modal functionality removed - using static files only)

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
        container.innerHTML = '<div class="transfers-empty">Транзакций не найдено</div>';
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
                            <div class="transfer-category-dot" style="background-color: ${fromColor};"></div>
                            <div class="transfer-path">${fromStr}</div>
                        </div>
                        <div class="transfer-row">
                            <div class="transfer-category-dot" style="background-color: ${toColor};"></div>
                            <div class="transfer-path">${toStr}</div>
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
                        <div class="transfer-category-dot" style="background-color: ${categoryColor};"></div>
                        <div class="transfer-path">${pathDisplay}</div>
                    </div>
                </div>
                <div class="transfer-amount ${amountClass}">${amountPrefix}${formatMoney(t.amount)}</div>
            </div>
        `;
    }

    // Render each date group
    const html = allTransfersData.map(group => {
        const dateHeader = `<div class="transfers-date-header">${group.date}</div>`;
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
            if (t.dataset.source === source) t.classList.add('active');
            else t.classList.remove('active');
        });

        if (source === 'remote') {
            remoteFields.classList.add('visible');
        } else {
            remoteFields.classList.remove('visible');
        }
    };

    // Open Modal
    btnOpen.addEventListener('click', () => {
        // Load current config into fields
        inputToken.value = dataService.config.githubToken || '';
        inputOwner.value = dataService.config.owner || '';
        inputRepo.value = dataService.config.repo || '';
        inputBranch.value = dataService.config.branch || 'main';
        inputPath.value = dataService.config.path || 'data';
        updateUIState(dataService.config.sourceType);

        authStatus.style.display = 'none';
        modal.style.display = 'flex';
    });

    // Close Modal
    const closeModal = () => {
        modal.style.display = 'none';
    };
    btnCancel.addEventListener('click', closeModal);

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
            authStatus.textContent = 'Проверка соединения...';

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
                authStatus.textContent = 'Ошибка: ' + result.message;
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

async function init() {
    // Init Settings UI
    initSettingsUI();

    // 0. Load shared categories
    await fetchCategories();

    // 1. Populate month selector
    // Catch initial error if remote is configured but invalid
    try {
        await populateMonthSelector();
    } catch (e) {
        showError('Ошибка инициализации. Проверьте настройки.');
    }

    // 2. Setup tab handlers
    setupTabHandlers();
}

// Start the app
init();
