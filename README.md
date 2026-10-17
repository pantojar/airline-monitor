# Airline Financial Monitor

A peer-comparison dashboard for US-listed passenger airlines: revenue, operating margin, labor cost, labor and fuel shares of operating expense, cash returned to shareholders, liquidity and debt, plus an industry and labor news feed.

The page is `index.html`. It loads the files in `data/` each time it opens.

## How it stays current

A GitHub Actions job (`.github/workflows/update.yml`) runs every morning:

1. `scripts/update.py` pulls each carrier's structured financial data from SEC EDGAR (XBRL company facts) and rebuilds `data/financials.json`.
2. It collects recent headlines from news feeds into `data/news_feed.json`.
3. Once a week (or when forced) `scripts/dot.py` downloads DOT Form 41 schedules from BTS TranStats — P-1.2 profit and loss, P-6 expenses by object (salaries by employee group, benefits, fuel), P-1(a) monthly employees, and T-100 traffic — and rebuilds `data/dot.json`, one row per operating carrier and quarter. Prior years are cached in `data/dot_raw_cache.json`; the current and previous year are re-downloaded to pick up revisions.
4. It commits the changed files, and GitHub Pages republishes the site.

If a carrier or feed fails to download, the previous data is kept and the failure is recorded in `data/meta.json`.

To run it on demand, open the **Actions** tab, choose **Refresh data**, then **Run workflow**. Tick *Also refresh DOT Form 41 data now* to pull DOT data outside the weekly cycle (BTS publishes quarterly financials about 75 days after quarter end; Q3 2026 is due December 17).

## Files you might edit

| File | What it controls |
|---|---|
| `config/carriers.json` | Carriers covered, peer groups, status, notes, news search terms, and per-carrier series rules (e.g. Republic's merger break) |
| `config/overrides.json` | Hand-entered figures used only where SEC's structured data has a gap |
| `config/news_queries.json` | Industry-wide news searches (labor, fuel, earnings) |
| `config/dot_carriers.json` | Operating carriers shown on the DOT tab: DOT code, owner, group, role, and footnotes |
| `data/news_curated.json` | Hand-written news items with summaries; shown alongside the automatic feed |

## Settings

- **Repository variable `SEC_CONTACT`** (Settings → Secrets and variables → Actions → Variables): an email address. The SEC asks automated requests to identify a contact.
- **GitHub Pages** (Settings → Pages): deploy from branch `main`, folder `/ (root)`.

## Method notes

Figures are GAAP as filed, in $ millions. Quarters reported only year-to-date (every Q4, and all cash-flow items) are derived as year-to-date minus the prior year-to-date. Labor is `LaborAndRelatedExpense`; fuel is `FuelCosts`; shareholder returns are `PaymentsForRepurchaseOfCommonStock` plus dividends paid. Debt definitions vary by carrier (some include finance leases). See the **Sources & notes** tab on the page for carrier-specific gaps.

DOT figures: salaries & benefits = P-6 total salaries and related fringe benefits less the "personnel expense" line, which reproduces the SEC labor line for single-certificate carriers (United and Southwest match to the dollar). Regions are summed to a system total; ASMs are T-100 all-service (class Z). FTEs are the quarter's average of monthly P-1(a) counts.
