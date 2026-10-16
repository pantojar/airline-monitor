"""Refresh the Airline Financial Monitor data files.

Runs on a schedule (see .github/workflows/update.yml). Standard library only.

  python scripts/update.py              # SEC financials + news
  python scripts/update.py --sec-only
  python scripts/update.py --news-only
  python scripts/update.py --facts-dir DIR   # read saved companyfacts JSON instead of fetching (testing)

Outputs (all in data/):
  financials.json   one row per carrier-quarter, $ millions
  carriers.json     carrier list for the page (copied from config, plus last filing seen)
  news_feed.json    recent headlines from news feeds, tagged by carrier
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

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG = os.path.join(ROOT, "config")
DATA = os.path.join(ROOT, "data")

FIELDS = ["rev", "opex", "labor", "fuel", "opinc", "ni", "buyback", "div", "cash", "sti", "debt"]
FLOW_FIELDS = ["rev", "opex", "labor", "fuel", "opinc", "ni", "buyback", "div"]
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
    return f"AirlineFinancialMonitor/1.0 ({contact or 'github.com/' + repo})"


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


def extract(cf, QE):
    debt = pick(cf, ["LongTermDebt"], inst_q, QE)
    for alt in (add(inst_q(cf, "LongTermDebtCurrent", QE), inst_q(cf, "LongTermDebtNoncurrent", QE)),
                add(inst_q(cf, "LongTermDebtAndCapitalLeaseObligationsCurrent", QE), inst_q(cf, "LongTermDebtAndCapitalLeaseObligations", QE))):
        for k, v in alt.items():
            debt.setdefault(k, v)
    return {
        "rev": pick(cf, ["Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax", "RevenueFromContractWithCustomerIncludingAssessedTax"], flow_q, QE),
        "opex": pick(cf, ["OperatingExpenses", "CostsAndExpenses", "OperatingCostsAndExpenses"], flow_q, QE),
        "labor": pick(cf, ["LaborAndRelatedExpense"], flow_q, QE),
        "fuel": pick(cf, ["FuelCosts"], flow_q, QE),
        "opinc": pick(cf, ["OperatingIncomeLoss"], flow_q, QE),
        "ni": pick(cf, ["NetIncomeLoss"], flow_q, QE),
        "buyback": pick(cf, ["PaymentsForRepurchaseOfCommonStock"], flow_q, QE),
        "div": pick(cf, ["PaymentsOfDividendsCommonStock", "PaymentsOfDividends"], flow_q, QE),
        "cash": pick(cf, ["CashAndCashEquivalentsAtCarryingValue", "Cash"], inst_q, QE),
        "sti": pick(cf, ["ShortTermInvestments", "AvailableForSaleSecuritiesDebtSecuritiesCurrent"], inst_q, QE),
        "debt": debt,
    }


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
    meta["sec"] = {"at": now_iso(), "carriers": status}


# ---------------------------------------------------------------- news
LABOR_RE = re.compile(r"union|contract|pilot|flight attendant|mechanic|strike|mediat|labor|negotiat|ratif|picket|teamsters|\bAFA\b|\bALPA\b|\bTWU\b|\bIAM\b", re.I)
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
        "DAL": r"\bDelta\b", "UAL": r"\bUnited Airlines\b", "AAL": r"American Airlines|\bEnvoy\b|\bPSA\b|Piedmont",
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
            key = re.sub(r"\W+", " ", g["title"].lower()).strip()
            if key in seen:
                continue
            seen.add(key)
            tags = set(q["tags"].split())
            for t, rx in name_tags.items():
                if re.search(rx, g["title"]):
                    tags.add(t)
            if len(tags) > 1:
                tags.discard("Industry")
            kind = q["kind"] or ("Labor" if LABOR_RE.search(g["title"]) else "Earnings" if EARN_RE.search(g["title"]) else "Headline")
            if q["kind"] is None and LABOR_RE.search(g["title"]):
                kind = "Labor"
            items.append({"date": g["date"], "tags": " ".join(sorted(tags)), "kind": kind, "headline": g["title"], "summary": "", "source": g["source"], "url": g["url"]})
    if not items and old:
        items = old  # feed outage: keep what we had
    cutoff = (dt.date.today() - dt.timedelta(days=keep_days)).isoformat()
    items = sorted([i for i in items if i["date"] >= cutoff], key=lambda i: i["date"], reverse=True)[:cap]
    for n, i in enumerate(items):
        i["id"] = f"f{n + 1}"
    save(os.path.join(DATA, "news_feed.json"), items)
    meta["news"] = {"at": now_iso(), "items": len(items), "errors": errors}


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
    meta["generated"] = now_iso()
    save(os.path.join(DATA, "meta.json"), meta)
    bad = [t for t, s in meta.get("sec", {}).get("carriers", {}).items() if not s.get("ok")]
    print(json.dumps({"generated": meta["generated"], "sec_failures": bad, "news_items": meta.get("news", {}).get("items")}, indent=1))


if __name__ == "__main__":
    main(sys.argv[1:])
