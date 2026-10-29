"""Revenue by type (passenger, loyalty, cargo, refinery) from each airline's 10-Q and 10-K.

The SEC's company-facts API only carries undimensioned totals, so revenue by product line is read from each
filing's XBRL instance document (<doc>_htm.xml). Only the revenue facts are kept, cached per filing in
data/revsplit_cache.json, so each filing is downloaded once. config/revenue_map.json says which lines make up
each category for each airline. Quarters come from three-month figures, or from year-to-date differences
(Q4 = full year minus nine months).
"""
import datetime as dt
import json
import os
import re
import time
import urllib.request
import xml.etree.ElementTree as ET

XBRLI = "{http://www.xbrl.org/2003/instance}"
DIM = "{http://xbrl.org/2006/xbrldi}explicitMember"
CATS = ["passenger", "loyalty", "cargo", "refinery"]


def _get(url, ua):
    req = urllib.request.Request(url, headers={"User-Agent": ua, "Accept-Encoding": "identity"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return r.read()


def revenue_facts(xml_bytes):
    """[[concept, start, end, [members], value]] for revenue facts that carry at least one dimension."""
    root = ET.fromstring(xml_bytes)
    ctx = {}
    for c in root.iter(XBRLI + "context"):
        p = c.find(XBRLI + "period")
        s, e = p.find(XBRLI + "startDate"), p.find(XBRLI + "endDate")
        if s is None or e is None:
            continue
        mems = [m.text.split(":")[-1] for m in c.iter(DIM) if m.text]
        if mems:
            ctx[c.get("id")] = (s.text, e.text, mems)
    out = []
    for el in root:
        tag = el.tag.split("}")[-1]
        if not re.match(r"Revenue|TravelMiles|NonTravelMiles", tag) or el.get("contextRef") not in ctx or not (el.text or "").strip():
            continue
        try:
            v = float(el.text)
        except ValueError:
            continue
        s, e, mems = ctx[el.get("contextRef")]
        out.append([tag, s, e, mems, v])
    return out


def _days(a, b):
    return (dt.date.fromisoformat(b) - dt.date.fromisoformat(a)).days


def series(facts, spec):
    """{(start, end): value} for one member spec, preferring the fact with the fewest dimensions."""
    member, _, concept = spec.partition("@")
    best = {}
    for tag, s, e, mems, v in facts:
        if member not in mems:
            continue
        if concept:
            if not tag.startswith(concept):
                continue
        elif not tag.startswith("RevenueFromContractWithCustomer"):
            continue
        k = (s, e)
        if k not in best or len(mems) < best[k][0]:
            best[k] = (len(mems), v)
    return {k: v for k, (n, v) in best.items()}


def quarters(sr):
    """{quarter_end: value} from three-month figures, or year-to-date differences."""
    q = {}
    for (s, e), v in sr.items():
        if 80 < _days(s, e) < 100:
            q[e] = v
    ytd = {(s, e): v for (s, e), v in sr.items() if s[5:] == "01-01" and s[:4] == e[:4]}
    for (s, e), v in ytd.items():
        if e in q:
            continue
        prev_end = {"06-30": "03-31", "09-30": "06-30", "12-31": "09-30"}.get(e[5:])
        if prev_end:
            pv = ytd.get((s, e[:4] + "-" + prev_end))
            if pv is not None:
                q[e] = v - pv
    return q


def update_revsplit(root, meta, ua, carriers):
    data = os.path.join(root, "data")
    cfg = json.load(open(os.path.join(root, "config", "revenue_map.json"), encoding="utf-8"))
    cache_path = os.path.join(data, "revsplit_cache.json")
    try:
        cache = json.load(open(cache_path, encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        cache = {}
    rows, report = [], {}
    since = str(dt.date.today().year - 3) + "-01-01"
    for c in carriers:
        t = c["ticker"]
        if t not in cfg or not c.get("cik"):
            continue
        cik = int(c["cik"])
        facts, got, err = [], 0, None
        try:
            sub = json.loads(_get(f"https://data.sec.gov/submissions/CIK{cik:010d}.json", ua))
            r = sub["filings"]["recent"]
            # oldest filing first: when a later filing restates a prior period, keep the figure as first reported,
            # so each year's quarters and full-year total come from the same basis
            for i in sorted(range(len(r["form"])), key=lambda i: r["filingDate"][i]):
                form = r["form"][i]
                if form not in ("10-Q", "10-K") or r["reportDate"][i] < since:
                    continue
                acc = r["accessionNumber"][i]
                if acc not in cache:
                    url = f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc.replace('-', '')}/" + re.sub(r"\.htm$", "_htm.xml", r["primaryDocument"][i])
                    try:
                        cache[acc] = revenue_facts(_get(url, ua))
                        got += 1
                        time.sleep(0.3)
                    except Exception as e:  # noqa: BLE001
                        cache[acc] = []
                        err = f"{acc}: {type(e).__name__}"
                facts += cache[acc]
        except Exception as e:  # noqa: BLE001
            report[t] = f"failed: {type(e).__name__}"
            continue
        m = cfg[t]
        per = {}
        for cat in CATS + ["passenger_minus"]:
            specs = m.get(cat)
            if not specs:
                continue
            tot = {}
            for spec in specs:
                # "A|B": use line A, or line B for periods where A isn't reported (Southwest's 10-K shows only total other revenue)
                qs = {}
                for alt in spec.split("|"):
                    for e, v in quarters(series(facts, alt)).items():
                        qs.setdefault(e, v)
                for e, v in qs.items():
                    tot.setdefault(e, []).append(v)
            # a quarter counts only when every line in the category is present
            per[cat] = {e: sum(vs) for e, vs in tot.items() if len(vs) == len(specs)}
        if "passenger_minus" in per and "passenger" in per:
            per["passenger"] = {e: v - per["passenger_minus"][e] for e, v in per["passenger"].items() if e in per["passenger_minus"]}
        ends = sorted(per.get("passenger", {}))
        for e in ends:
            row = {"ticker": t, "quarter": e[:7]}
            for cat in CATS:
                if cat in per and e in per[cat]:
                    row[cat] = round(per[cat][e] / 1e6, 1)
            rows.append(row)
        report[t] = {"quarters": len(ends), "new_filings": got, **({"error": err} if err else {})}
    if rows:
        json.dump(cache, open(cache_path, "w", encoding="utf-8"), separators=(",", ":"))
        json.dump(sorted(rows, key=lambda r: (r["ticker"], r["quarter"])), open(os.path.join(data, "revenue_split.json"), "w", encoding="utf-8"), indent=0, separators=(",", ":"))
    meta["revsplit"] = {"at": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"), "carriers": report}
