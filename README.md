# Airline Financial Monitor

A peer-comparison dashboard for US-listed passenger airlines: revenue, operating margin, labor cost, labor and fuel shares of operating expense, cash returned to shareholders, liquidity and debt, plus an industry and labor news feed.

The page is `index.html`. It loads the files in `data/` each time it opens.

## How it stays current

A GitHub Actions job (`.github/workflows/update.yml`) runs every morning:

1. `scripts/update.py` pulls each carrier's structured financial data from SEC EDGAR (XBRL company facts) and rebuilds `data/financials.json`.
2. It collects recent headlines from news feeds into `data/news_feed.json`.
3. It commits the changed files, and GitHub Pages republishes the site.

If a carrier or feed fails to download, the previous data is kept and the failure is recorded in `data/meta.json`.

To run it on demand, open the **Actions** tab, choose **Refresh data**, then **Run workflow**.

## Files you might edit

| File | What it controls |
|---|---|
| `config/carriers.json` | Carriers covered, peer groups, status, notes, news search terms, and per-carrier series rules (e.g. Republic's merger break) |
| `config/overrides.json` | Hand-entered figures used only where SEC's structured data has a gap |
| `config/news_queries.json` | Industry-wide news searches (labor, fuel, earnings) |
| `data/news_curated.json` | Hand-written news items with summaries; shown alongside the automatic feed |

## Settings

- **Repository variable `SEC_CONTACT`** (Settings → Secrets and variables → Actions → Variables): an email address. The SEC asks automated requests to identify a contact.
- **GitHub Pages** (Settings → Pages): deploy from branch `main`, folder `/ (root)`.

## Method notes

Figures are GAAP as filed, in $ millions. Quarters reported only year-to-date (every Q4, and all cash-flow items) are derived as year-to-date minus the prior year-to-date. Labor is `LaborAndRelatedExpense`; fuel is `FuelCosts`; shareholder returns are `PaymentsForRepurchaseOfCommonStock` plus dividends paid. Debt definitions vary by carrier (some include finance leases). See the **Sources & notes** tab on the page for carrier-specific gaps.
