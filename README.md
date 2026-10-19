# Airline Financial Monitor

A peer-comparison dashboard for US-listed passenger airlines: revenue, operating margin, labor cost, labor and fuel shares of operating expense, cash returned to shareholders, liquidity and debt, plus an industry and labor news feed.

The page is `index.html`. It loads the files in `data/` each time it opens.

## How it stays current

A GitHub Actions job (`.github/workflows/update.yml`) runs every morning:

1. `scripts/update.py` pulls each carrier's structured financial data from SEC EDGAR (XBRL company facts) and rebuilds `data/financials.json`.
2. It collects recent headlines from news feeds into `data/news_feed.json`.
3. Once a week (or when forced) `scripts/dot.py` downloads DOT Form 41 schedules from BTS TranStats — P-1.2 profit and loss, P-6 expenses by object (salaries by employee group, benefits, fuel), P-1(a) monthly employees, T-100 traffic, P-10 annual headcount by employee group, P-5.2 pilot salaries and P-12(a) monthly fuel cost — and rebuilds `data/dot.json` (carrier-quarters), `data/dot_annual.json` (headcount and average pilot salary by year) and `data/dot_fuel.json` (monthly fuel cost per gallon). Prior years are cached in `data/dot_raw_cache.json`; the current and previous year are re-downloaded to pick up revisions.
4. It downloads the daily U.S. Gulf Coast jet fuel spot price (EIA, via FRED series DJFUELUSGULF) into `data/fuel_spot.json`.
5. Once a week `scripts/proxy.py` checks each company's two most recent proxy statements (DEF 14A) and reads the CEO pay-ratio disclosure — median employee pay, CEO total and the ratio — into `data/exec_pay.json`. Compensation actually paid and CEO names come from `config/exec_pay.json`, entered by hand from each proxy.
6. It commits the changed files, and GitHub Pages republishes the site.

If a carrier or feed fails to download, the previous data is kept and the failure is recorded in `data/meta.json`.

To run it on demand, open the **Actions** tab, choose **Refresh data**, then **Run workflow**. Tick *Also refresh DOT Form 41 data now* to pull DOT data outside the weekly cycle (BTS publishes quarterly financials about 75 days after quarter end; Q3 2026 is due December 17).

## Files you might edit

| File | What it controls |
|---|---|
| `config/carriers.json` | Carriers covered, peer groups, status, notes, news search terms, and per-carrier series rules (e.g. Republic's merger break) |
| `config/overrides.json` | Hand-entered figures used only where SEC's structured data has a gap |
| `config/news_queries.json` | Industry-wide news searches (labor, fuel, earnings) |
| `config/exec_pay.json` | Hand-verified CEO pay figures: name, compensation actually paid, notes (one row per company and fiscal year) |
| `config/dot_carriers.json` | Operating carriers shown on the DOT tab: DOT code, owner, group, role, and footnotes |
| `data/news_curated.json` | Hand-written news items with summaries; shown alongside the automatic feed |
| `config/labor_sources.json` | Union websites the bargaining tracker reads (see below) |

### Adding a union or negotiations website

Add one line to `config/labor_sources.json`. WordPress sites and sites with an RSS feed need only a name, address, the union names it speaks for and the airlines it covers:

```json
{"name": "IAM District 141", "url": "https://iam141.org", "unions": ["\\bIAM\\b", "Machinists"], "carriers": ["UAL", "ALK"]}
```

A site that has neither, but has a news page listing articles, also needs `"list"` (the news page) and `"link"` (a pattern the article addresses match). A negotiations site for one group (a pilots' "Contract 2026" site, say) adds `"craft"` (and `"unit"` for a regional or subsidiary), so every milestone it posts counts for that group even when the sentence doesn't name the craft:

```json
{"name": "Frontier pilots negotiations", "url": "https://example.org", "carriers": ["ULCC"], "craft": "pilot"}
```

The **Union websites read** table on the Bargaining tab shows, after each weekly run, whether each site could be read and, if not, why.

## Settings

- **Repository variable `SEC_CONTACT`** (Settings → Secrets and variables → Actions → Variables): an email address. The SEC asks automated requests to identify a contact.
- **GitHub Pages** (Settings → Pages): deploy from branch `main`, folder `/ (root)`.

## Method notes

Figures are GAAP as filed, in $ millions. Quarters reported only year-to-date (every Q4, and all cash-flow items) are derived as year-to-date minus the prior year-to-date. Labor is `LaborAndRelatedExpense`; fuel is `FuelCosts`; shareholder returns are `PaymentsForRepurchaseOfCommonStock` plus dividends paid. Debt definitions vary by carrier (some include finance leases). See the **Sources & notes** tab on the page for carrier-specific gaps.

DOT figures: salaries & benefits = P-6 total salaries and related fringe benefits less the "personnel expense" line, which reproduces the SEC labor line for single-certificate carriers (United and Southwest match to the dollar). Regions are summed to a system total; ASMs are T-100 all-service (class Z). FTEs are the quarter's average of monthly P-1(a) counts.
