"""Refresh the Airline Financial Monitor data files.

Runs on a schedule (see .github/workflows/update.yml). Standard library only.

  python scripts/update.py              # SEC financials + news
  python scripts/update.py --sec-only
  python scripts/update.py --news-only
  python scripts/update.py --facts-dir DIR   # read saved companyfacts JSON instead of fetching (testing)
  python scripts/update.py --force-dot       # refresh DOT Form 41 data now (otherwise weekly)

Outputs (all in data/):
  financials.json   one row per carrier-quarter, $ millions
  carriers.json     carrier list for the page (copied from config, plus last filing seen)
  news_feed.json    recent headlines from news feeds, tagged by carrier
  dot.json          DOT Form 41 figures by operating carrier and quarter (see scripts/dot.py)
  dot_annual.json   DOT headcount by employee group and pilot salaries, by carrier and year
  dot_fuel.json     DOT monthly fuel gallons and cost by carrier
  fuel_spot.json    daily Gulf Coast jet fuel spot price (FRED DJFUELUSGULF)
  cpi.json          monthly CPI-W and CPI-U, U.S. city average, not seasonally adjusted (BLS via FRED)
  exec_pay.json     CEO pay, median employee pay and pay ratio from proxy statements (see scripts/proxy.py)
  labor.json        union contracts from 10-Ks, labor sentences from 10-Qs, and news-based status (see scripts/labor.py)
  meta.json         when each part last refreshed and anything that failed

A failed fetch never wipes good data: the previous rows for that carrier are kept.
"""
import datetime as dt
import email.utils
import json
import math
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import dot  # noqa: E402
import proxy  # noqa: E402
import labor  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG = os.path.join(ROOT, "config")
DATA = os.path.join(ROOT, "data")

FIELDS = ["rev", "opex", "labor", "fuel", "opinc", "ni", "buyback", "div", "cash", "sti", "debt", "oplease", "da", "rent"]
FLOW_FIELDS = ["rev", "opex", "labor", "fuel", "opinc", "ni", "buyback", "div", "da", "rent"]
FIRST_QUARTER = "2023-03"


def load(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def user_agent():
    contact = os.environ.get("SEC_CONTACT", "").strip()
    repo = os.environ.get("GITHUB_REPOSITORY", "airline-monitor")
    # SEC asks automated clients to identify themselves with a contact.
    return f"AirlineFinancialMonitor {contact or 'github.com/' + repo}"


def http_get(url, accept="application/json", tries=3):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": user_agent(), "Accept": accept, "Accept-Encoding": "identity"})
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.read()
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(2 * (i + 1))
    raise last


# ---------------------------------------------------------------- quarters
def quarter_ends(today):
    out = []
    y = int(FIRST_QUARTER[:4])
    while True:
        for md in ("03-31", "06-30", "09-30", "12-31"):
            d = f"{y}-{md}"
            if d >= f"{FIRST_QUARTER}-31"[:10] and dt.date.fromisoformat(d) <= today:
                out.append(d)
            if dt.date.fromisoformat(d) > today:
                return out
        y += 1


def days(a, b):
    return (dt.date.fromisoformat(b) - dt.date.fromisoformat(a)).days


# ---------------------------------------------------------------- XBRL extraction
def facts(cf, concept):
    v = cf.get("facts", {}).get("us-gaap", {}).get(concept)
    if not v:
        return []
    best = {}
    for f in v.get("units", {}).get("USD", []):
        if not re.match(r"^10-[QK]", f.get("form", "")):
            continue
        k = (f.get("start") or "") + "|" + f["end"]
        if k not in best or f.get("filed", "") > best[k].get("filed", ""):
            best[k] = f
    return list(best.values())


def flow_q(cf, concept, QE):
    fs = [f for f in facts(cf, concept) if f.get("start")]
    out = {}
    for i, E in enumerate(QE):
        at_e = [f for f in fs if f["end"] == E]
        q = next((f for f in at_e if 80 < days(f["start"], f["end"]) < 100), None)
        if q is not None:
            out[E] = q["val"]
            continue
        if i == 0:
            continue
        prev = QE[i - 1]
        for f in at_e:
            p = next((g for g in fs if g["end"] == prev and g["start"] == f["start"]), None)
            if p is not None:
                out[E] = f["val"] - p["val"]
                break
    return out


def inst_q(cf, concept, QE):
    fs = [f for f in facts(cf, concept) if not f.get("start")]
    out = {}
    for E in QE:
        f = next((g for g in fs if g["end"] == E), None)
        if f is not None:
            out[E] = f["val"]
    return out


def pick(cf, concepts, fn, QE):
    res = {}
    for c in concepts:
        for k, v in fn(cf, c, QE).items():
            res.setdefault(k, v)
    return res


def add(a, b):
    return {k: a[k] + b[k] for k in a if k in b}


RENT_CONCEPTS = ["AircraftRental", "OperatingLeaseCost", "OperatingLeasesRentExpenseNet", "LeaseAndRentalExpense"]


def rent_q(cf, QE):
    """Rent for EBITDAR, per quarter. Aircraft rent where the income statement shows it; otherwise operating lease
    cost. Some carriers report lease cost only in the annual 10-K: those quarters get a quarter of the latest
    full-year figure."""
    out = pick(cf, RENT_CONCEPTS, flow_q, QE)
    annual = []
    for c in RENT_CONCEPTS + ["OperatingLeasePayments"]:  # last resort: cash paid on operating leases (lease note)
        annual = [f for f in facts(cf, c) if f.get("start") and 350 < days(f["start"], f["end"]) < 380]
        if annual:
            break
    for E in QE:
        if E in out or not annual:
            continue
        prior = [f for f in annual if f["end"] <= E or f["start"] <= E]
        if prior:
            f = max(prior, key=lambda g: g["end"])
            if days(f["end"], E) < 550:
                out[E] = f["val"] / 4
    return out


def extract(cf, QE):
    debt = pick(cf, ["LongTermDebt"], inst_q, QE)
    for alt in (add(inst_q(cf, "LongTermDebtCurrent", QE), inst_q(cf, "LongTermDebtNoncurrent", QE)),
                add(inst_q(cf, "LongTermDebtAndCapitalLeaseObligationsCurrent", QE), inst_q(cf, "LongTermDebtAndCapitalLeaseObligations", QE))):
        for k, v in alt.items():
            debt.setdefault(k, v)
    oplease = pick(cf, ["OperatingLeaseLiability"], inst_q, QE)
    for k, v in add(inst_q(cf, "OperatingLeaseLiabilityCurrent", QE), inst_q(cf, "OperatingLeaseLiabilityNoncurrent", QE)).items():
        oplease.setdefault(k, v)
    out = {
        "rev": pick(cf, ["Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax", "RevenueFromContractWithCustomerIncludingAssessedTax"], flow_q, QE),
        "opex": pick(cf, ["OperatingExpenses", "CostsAndExpenses", "OperatingCostsAndExpenses"], flow_q, QE),
        "labor": pick(cf, ["LaborAndRelatedExpense"], flow_q, QE),
        "fuel": pick(cf, ["FuelCosts"], flow_q, QE),
        "opinc": pick(cf, ["OperatingIncomeLoss"], flow_q, QE),
        "ni": pick(cf, ["NetIncomeLoss"], flow_q, QE),
        "buyback": pick(cf, ["PaymentsForRepurchaseOfCommonStock"], flow_q, QE),
        "div": pick(cf, ["PaymentsOfDividendsCommonStock", "PaymentsOfDividends"], flow_q, QE),
        "cash": pick(cf, ["CashAndCashEquivalentsAtCarryingValue", "Cash"], inst_q, QE),
        "sti": pick(cf, ["ShortTermInvestments", "AvailableForSaleSecuritiesDebtSecuritiesCurrent", "MarketableSecuritiesCurrent"], inst_q, QE),  # JetBlue and Republic use the last
        "debt": debt,
        # operating lease liabilities (current + noncurrent): mostly leased aircraft, which reported debt leaves out
        "oplease": oplease,
        # for EBITDAR: depreciation and amortization, and operating lease (rent) cost
        "da": pick(cf, ["DepreciationDepletionAndAmortization", "DepreciationAndAmortization", "DepreciationAmortizationAndAccretionNet", "Depreciation"], flow_q, QE),
        "rent": rent_q(cf, QE),
    }
    # an estimated rent figure alone doesn't make a quarter: keep it only where the quarter was reported
    out["rent"] = {k: v for k, v in out["rent"].items() if k in out["opinc"]}
    return out


def round1(v):
    return math.floor(v / 1e5 + 0.5) / 10  # $ millions, one decimal


def latest_filing(cf):
    best = None
    for g in cf.get("facts", {}).values():
        for v in g.values():
            for u in v.get("units", {}).values():
                for f in u:
                    if re.match(r"^10-[QK]", f.get("form", "")) and (best is None or f.get("filed", "") > best[1]):
                        best = (f.get("form"), f.get("filed", ""))
    return {"form": best[0], "filed": best[1]} if best else None


def carrier_rows(c, cf, QE, overrides):
    d = extract(cf, QE)
    rows = []
    for E in QE:
        q = E[:7]
        if c.get("series_start") and q < c["series_start"]:
            continue
        if q in c.get("drop_quarters", []):
            continue
        r = {"ticker": c["ticker"], "carrier": c["carrier"], "quarter": q}
        for f in FIELDS:
            v = d[f].get(E)
            r[f] = None if v is None else round1(v)
        if r["div"] is not None and r["div"] < 0:
            r["div"] = 0
        for f in c.get("null_fields", {}).get(q, []):
            r[f] = None
        r["basis"] = "SEC XBRL"
        for o in overrides:
            if o["ticker"] == c["ticker"] and o["quarter"] == q:
                filled = False
                for f, v in o["values"].items():
                    if r.get(f) is None:
                        r[f] = v
                        filled = True
                if filled:
                    r["basis"] = o.get("source", "manual")
        if all(r[f] is None for f in FIELDS):
            continue
        rows.append(r)
    return rows


def update_sec(meta, facts_dir=None):
    carriers = load(os.path.join(CONFIG, "carriers.json"), [])
    overrides = load(os.path.join(CONFIG, "overrides.json"), [])
    old = load(os.path.join(DATA, "financials.json"), [])
    QE = quarter_ends(dt.date.today())
    rows, status = [], {}
    for c in carriers:
        t = c["ticker"]
        try:
            if facts_dir:
                cf = load(os.path.join(facts_dir, f"{t}.json"))
                if cf is None:
                    raise FileNotFoundError(f"{t}.json")
            else:
                url = f"https://data.sec.gov/api/xbrl/companyfacts/CIK{int(c['cik']):010d}.json"
                cf = json.loads(http_get(url))
                time.sleep(0.3)  # stay well under SEC's 10 requests/second
            new = carrier_rows(c, cf, QE, overrides)
            if not new:
                raise ValueError("no usable quarters found")
            rows.extend(new)
            status[t] = {"ok": True, "latest_filing": latest_filing(cf), "quarters": len(new), "through": new[-1]["quarter"]}
        except Exception as e:  # noqa: BLE001
            kept = [r for r in old if r.get("ticker") == t]
            rows.extend(kept)
            status[t] = {"ok": False, "error": f"{type(e).__name__}: {e}"[:300], "kept_previous_rows": len(kept)}
            print(f"[warn] {t}: {e}", file=sys.stderr)
    save(os.path.join(DATA, "financials.json"), rows)
    page_carriers = []
    for c in carriers:
        pc = {k: c[k] for k in ("ticker", "carrier", "group", "status", "returns_complete", "note")}
        pc["cik"] = str(c["cik"])
        lf = status.get(c["ticker"], {}).get("latest_filing")
        if lf:
            pc["latest_filing"] = f"{lf['form']} filed {lf['filed']}"
        page_carriers.append(pc)
    save(os.path.join(DATA, "carriers.json"), page_carriers)
    meta["sec"] = {"at": now_iso(), "contact_set": bool(os.environ.get("SEC_CONTACT", "").strip()), "carriers": status}


# ---------------------------------------------------------------- news
LABOR_RE = re.compile(r"union|contract|pilot|flight attendant|mechanic|strike|mediat|labor|negotiat|ratif|picket|teamsters|\bAFA\b|\bALPA\b|\bTWU\b|\bIAM\b", re.I)
# Investor-chatter and filing-bot headlines that add noise rather than news
SKIP_RE = re.compile(r"Form 4|Form 144|13F|price target|\bupgrade|\bdowngrade|shares (sold|bought|acquired)|stake in|position in|holdings in|stock (rises|falls|jumps|drops|slides|gains)|short interest|options? (trading|activity)", re.I)
EARN_RE = re.compile(r"earnings|quarter|profit|loss|revenue|guidance|outlook|forecast", re.I)


def gnews_url(q):
    return "https://news.google.com/rss/search?" + urllib.parse.urlencode(
        {"q": f"{q} when:14d", "hl": "en-US", "gl": "US", "ceid": "US:en"})


def parse_feed(xml_bytes):
    out = []
    root = ET.fromstring(xml_bytes)
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        link = (it.findtext("link") or "").strip()
        src_el = it.find("source")
        src = (src_el.text or "").strip() if src_el is not None else ""
        if src and title.endswith(" - " + src):
            title = title[: -len(" - " + src)].strip()
        pub = it.findtext("pubDate")
        try:
            date = email.utils.parsedate_to_datetime(pub).date().isoformat()
        except Exception:  # noqa: BLE001
            date = None
        if title and link.startswith("https://") and date:
            out.append({"title": title, "url": link, "source": src, "date": date})
    return out


def update_news(meta, per_query=6, keep_days=30, cap=80):
    carriers = load(os.path.join(CONFIG, "carriers.json"), [])
    queries = [{"query": c["news_query"], "tags": c["ticker"], "kind": None} for c in carriers if c.get("news_query") and c.get("status") == "Active"]
    queries += load(os.path.join(CONFIG, "news_queries.json"), [])
    name_tags = {
        "DAL": r"\bDelta\b", "UAL": r"\bUnited Airlines\b", "AAL": r"American Airlines|Envoy Air|PSA Airlines|Piedmont Airlines",
        "LUV": r"Southwest", "ALK": r"Alaska Air|Hawaiian|Horizon", "JBLU": r"JetBlue", "ULCC": r"Frontier",
        "ALGT": r"Allegiant|Sun Country", "SKYW": r"SkyWest", "RJET": r"Republic Airways|\bMesa\b",
    }
    old = load(os.path.join(DATA, "news_feed.json"), [])
    items, seen, errors = [], set(), []
    for q in queries:
        try:
            got = parse_feed(http_get(gnews_url(q["query"]), accept="application/rss+xml"))[:per_query]
            time.sleep(1)
        except Exception as e:  # noqa: BLE001
            errors.append(f"{q['query']}: {type(e).__name__}")
            continue
        for g in got:
            if SKIP_RE.search(g["title"]):
                continue
            key = re.sub(r"\W+", " ", g["title"].lower()).strip()
            if key in seen:
                continue
            seen.add(key)
            tags = set(q["tags"].split())
            # a carrier search sometimes returns unrelated stories (e.g. another company's earnings):
            # keep it only if the headline names an airline
            if "Industry" not in tags and not any(re.search(name_tags[t], g["title"]) for t in tags if t in name_tags):
                continue
            for t, rx in name_tags.items():
                if re.search(rx, g["title"]):
                    tags.add(t)
            if len(tags) > 1:
                tags.discard("Industry")
            kind = q["kind"] or ("Labor" if LABOR_RE.search(g["title"]) else "Earnings" if EARN_RE.search(g["title"]) else "Headline")
            if q["kind"] is None and LABOR_RE.search(g["title"]):
                kind = "Labor"
            items.append({"date": g["date"], "tags": " ".join(sorted(tags)), "kind": kind, "headline": g["title"], "summary": "", "source": g["source"], "url": g["url"]})
    # the airlines' own newsrooms (same sites the bargaining tracker reads): bargaining-relevant releases only
    company, cerr = company_releases(name_tags, keep_days)
    errors += cerr
    if company:
        def toks(t):
            return {w for w in re.findall(r"[a-z0-9]+", t.lower()) if len(w) > 3}
        ct = [(toks(c["headline"]), c["date"]) for c in company]
        def dup(i):  # press coverage of something the company itself announced
            a = toks(i["headline"])
            return any(a and len(a & b) / len(a | b) >= 0.45 and abs((dt.date.fromisoformat(i["date"]) - dt.date.fromisoformat(d)).days) <= 3 for b, d in ct)
        items = company + [i for i in items if not dup(i)]
    if not items and old:
        items = old  # feed outage: keep what we had
    cutoff = (dt.date.today() - dt.timedelta(days=keep_days)).isoformat()
    items = sorted([i for i in items if i["date"] >= cutoff], key=lambda i: (i["date"], bool(i.get("company"))), reverse=True)[:cap + 40]
    for n, i in enumerate(items):
        i["id"] = f"f{n + 1}"
    save(os.path.join(DATA, "news_feed.json"), items)
    meta["news"] = {"at": now_iso(), "items": len(items), "errors": errors}


RELEASE_RE = re.compile(
    r"union|contract|agreement|ratif|tentative|negotiat|bargain|labor|strike|pilots|flight attendants|mechanics|technicians|dispatchers|"
    r"quarter|results|earnings|guidance|outlook|financial|revenue|profit|dividend|repurchase|notes|offering|debt|liquidity|investor|"
    r"fleet|aircraft order|orders? \d|deliver|capacity|"
    r"chief|\bceo\b|president|appoint|names? .* (to|as)|\bboard\b|resign|retire|executive|leadership|"
    r"merger|acqui|combin|transaction|antitrust|hart-scott|department of transportation|\bDOT\b|furlough|layoff|job cuts|restructur|bankrupt",
    re.I)


def company_releases(name_tags, keep_days):
    """Recent releases from each airline's newsroom, filtered to what matters at the bargaining table."""
    import unions
    try:
        sites = [x for x in load(os.path.join(CONFIG, "labor_sources.json"), []) if x.get("company")]
    except Exception:  # noqa: BLE001
        return [], []
    since = (dt.date.today() - dt.timedelta(days=keep_days)).isoformat()
    f = unions.Fetch(user_agent())
    out, errors, seen = [], [], set()
    for site in sites:
        try:
            method, got, tried = unions.list_items(f, site, since)
        except Exception as e:  # noqa: BLE001
            errors.append(f"{site['name']}: {type(e).__name__}")
            continue
        if not method:
            errors.append(f"{site['name']}: unavailable")
        for it in got:
            title = it.get("title") or ""
            if not it.get("date") or it["date"] < since or not RELEASE_RE.search(title) or SKIP_RE.search(title):
                continue
            key = re.sub(r"\W+", " ", title.lower()).strip()
            if key in seen:
                continue
            seen.add(key)
            tags = set(site.get("carriers", []))
            for t, rx in name_tags.items():
                if re.search(rx, title):
                    tags.add(t)
            kind = "Labor" if LABOR_RE.search(title) else "Earnings" if EARN_RE.search(title) else "Corporate"
            out.append({"date": it["date"], "tags": " ".join(sorted(tags)), "kind": kind, "headline": title, "summary": "",
                        "source": site["name"], "url": it["url"], "company": True})
    return out, errors


def fred_series(sid, since="2022-01-01"):
    text = http_get(f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={sid}&cosd={since}", accept="text/csv").decode("utf-8", "replace")
    rows = []
    for line in text.strip().splitlines()[1:]:
        d, _, v = line.partition(",")
        try:
            rows.append([d, round(float(v), 3)])
        except ValueError:
            continue  # holidays are blank
    return rows


def eia_daily_diesel(html):
    """EIA's daily prices page (eia.gov/todayinenergy/prices.php): Gulf Coast low-sulfur diesel close and its date."""
    m = re.search(r"Wholesale Spot Petroleum Prices,\s*(\d{1,2})/(\d{1,2})/(\d{2})\s*Close", html)
    i = html.find("Low-Sulfur Diesel")
    if not m or i < 0:
        return None
    block = html[i:i + 1500]
    g = re.search(r"Gulf Coast\s*</td>\s*<td[^>]*>\s*([\d.]+)", block)
    if not g:
        return None
    d = dt.date(2000 + int(m.group(3)), int(m.group(1)), int(m.group(2))).isoformat()
    return d, float(g.group(1))


def update_fuel_spot(meta):
    """Daily U.S. Gulf Coast kerosene-type jet fuel spot price ($/gal).

    The official series is EIA's, via FRED (DJFUELUSGULF). EIA publishes it about once a week, so it runs several
    days behind. To keep the chart current, days after the last official price are estimated from EIA's daily
    prices page: that day's Gulf Coast ultra-low-sulfur diesel close plus jet fuel's recent spread to diesel
    (median of the last 20 days both FRED series cover). Estimates are flagged and replaced once EIA posts the
    official figure."""
    info = {}
    try:
        rows = fred_series("DJFUELUSGULF")
        if len(rows) < 100:
            raise ValueError("too few observations")
        info = {"at": now_iso(), "latest": rows[-1][0], "rows": len(rows)}
    except Exception as e:  # noqa: BLE001
        meta.setdefault("fuel_spot", {})["error"] = f"{type(e).__name__}: {e}"[:200]
        print(f"[warn] fuel spot: {e}", file=sys.stderr)
        return
    # daily diesel closes, kept so a missed morning doesn't lose a day
    hist_path = os.path.join(DATA, "eia_daily_diesel.json")
    hist = load(hist_path, {})
    try:
        got = eia_daily_diesel(http_get("https://www.eia.gov/todayinenergy/prices.php", accept="text/html").decode("utf-8", "replace"))
        if got:
            hist[got[0]] = got[1]
            info["diesel_close"] = got[0]
        else:
            info["estimate_error"] = "EIA daily prices page: diesel price not found"
    except Exception as e:  # noqa: BLE001
        info["estimate_error"] = f"EIA daily prices page: {type(e).__name__}"
    hist = dict(sorted(hist.items())[-90:])
    save(hist_path, hist)
    last = rows[-1][0]
    later = [(d, v) for d, v in hist.items() if d > last]
    if later:
        try:
            ulsd = dict(fred_series("DDFUELUSGULF", (dt.date.fromisoformat(last) - dt.timedelta(days=60)).isoformat()))
            jet = dict(rows)
            both = sorted(d for d in jet if d in ulsd)[-20:]
            if len(both) < 5:
                raise ValueError("too little overlap")
            diffs = sorted(jet[d] - ulsd[d] for d in both)
            spread = diffs[len(diffs) // 2]
            for d, v in later:
                rows.append([d, round(v + spread, 3), 1])
            info["estimated_through"] = later[-1][0]
            info["spread"] = round(spread, 3)
        except Exception as e:  # noqa: BLE001
            info["estimate_error"] = f"jet-diesel spread: {type(e).__name__}: {e}"[:200]
    save(os.path.join(DATA, "fuel_spot.json"), rows)
    meta["fuel_spot"] = info


def update_cpi(meta):
    """Consumer Price Index, U.S. city average, all items, not seasonally adjusted (1982-84=100), from BLS via FRED:
    CPI-W (CWUR0000SA0, urban wage earners and clerical workers) and CPI-U (CPIAUCNS, all urban consumers)."""
    out, info = {}, {"at": now_iso()}
    for key, sid in (("cpiw", "CWUR0000SA0"), ("cpiu", "CPIAUCNS")):
        try:
            rows = fred_series(sid, "1990-01-01")
            if len(rows) < 100:
                raise ValueError("too few observations")
            out[key] = [[d[:7], v] for d, v in rows]
            info[key] = out[key][-1][0]
        except Exception as e:  # noqa: BLE001
            info[key + "_error"] = f"{type(e).__name__}: {e}"[:200]
            prev = load(os.path.join(DATA, "cpi.json"), {}) or {}
            if prev.get(key):
                out[key] = prev[key]
    if out:
        save(os.path.join(DATA, "cpi.json"), out)
    meta["cpi"] = info


def now_iso():
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def main(argv):
    meta = load(os.path.join(DATA, "meta.json"), {}) or {}
    facts_dir = None
    if "--facts-dir" in argv:
        facts_dir = argv[argv.index("--facts-dir") + 1]
    if "--news-only" not in argv:
        update_sec(meta, facts_dir)
    if "--sec-only" not in argv:
        update_news(meta)
    if "--sec-only" not in argv and "--news-only" not in argv:
        update_fuel_spot(meta)
        update_cpi(meta)
        try:
            listed = [c for c in load(os.path.join(CONFIG, "carriers.json"), []) if c.get("status") == "Active"]
            proxy.update_exec_pay(ROOT, meta, user_agent(), listed, force="--force-proxy" in argv)
        except Exception as e:  # noqa: BLE001
            meta.setdefault("exec_pay", {})["error"] = f"{type(e).__name__}: {e}"[:300]
            print(f"[warn] exec pay: {e}", file=sys.stderr)
        try:
            labor.update_labor(ROOT, meta, user_agent(), listed, force="--force-labor" in argv or os.environ.get("FORCE_DOT") == "true")
        except Exception as e:  # noqa: BLE001
            meta.setdefault("labor", {})["error"] = f"{type(e).__name__}: {e}"[:300]
            print(f"[warn] labor: {e}", file=sys.stderr)
    if "--sec-only" not in argv and "--news-only" not in argv and "--no-dot" not in argv:
        try:
            dot.update_dot(ROOT, meta, user_agent(), force="--force-dot" in argv or os.environ.get("FORCE_DOT") == "true")
        except Exception as e:  # noqa: BLE001
            meta.setdefault("dot", {})["error"] = f"{type(e).__name__}: {e}"[:300]
            print(f"[warn] DOT: {e}", file=sys.stderr)
    meta["generated"] = now_iso()
    save(os.path.join(DATA, "meta.json"), meta)
    bad = [t for t, s in meta.get("sec", {}).get("carriers", {}).items() if not s.get("ok")]
    print(json.dumps({"generated": meta["generated"], "sec_failures": bad, "news_items": meta.get("news", {}).get("items"),
                      "dot": meta.get("dot")}, indent=1))


if __name__ == "__main__":
    main(sys.argv[1:])
