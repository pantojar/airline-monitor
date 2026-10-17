"""DOT Form 41 data for individual operating carriers (BTS TranStats).

Tables used (all public, from https://www.transtats.bts.gov):
  P-1.2   quarterly profit and loss                     -> revenue, opex, operating profit, net income
  P-6     quarterly operating expense by object         -> salaries by employee group, benefits, fuel
  P-1(a)  monthly employees                             -> full-time equivalents (FTE)
  T-100 T1 monthly traffic and capacity, all service    -> available seat miles, RPMs, passengers, departures

Each table is downloaded through TranStats' "Download" form (an ASP.NET page) for one year at a
time, then summed across reporting regions (domestic, Atlantic, Latin, Pacific, international,
system; DOT carriers report each region once, so the sum is the system total) and rolled up to
calendar quarters.

Dollar figures in data/dot.json are $ millions; traffic is in millions (ASMs, RPMs) or thousands
(passengers); FTE is the average of the quarter's three monthly counts.
"""
import csv
import datetime as dt
import http.cookiejar
import io
import json
import os
import re
import time
import urllib.parse
import urllib.request
import zipfile

BASE = "https://www.transtats.bts.gov/DL_SelectFields.aspx"
FIN_DB = "Nv4 Pn44vr4 Sv0n0pvny"      # Air Carrier Financial Reports (Form 41)
SUM_DB = "Nv4 Pn44vr4 f7zzn4B"        # Air Carrier Summary Data (T-100 summary)

TABLES = {
    "p12": {"code": "FMI", "db": FIN_DB, "period": "quarter",
            "fields": ["YEAR", "QUARTER", "UNIQUE_CARRIER", "REGION",
                       "OP_REVENUES", "TRANS_REV_PAX", "OP_EXPENSES", "OP_PROFIT_LOSS", "NET_INCOME"]},
    "p6": {"code": "FME", "db": FIN_DB, "period": "quarter",
           "fields": ["YEAR", "QUARTER", "UNIQUE_CARRIER", "REGION",
                      "SALARIES_FLIGHT", "SALARIES_MAINT", "SALARIES_TRAFFIC", "SALARIES_MGT", "SALARIES_OTHER",
                      "SALARIES", "BENEFITS_PENSIONS", "BENEFITS_PAYROLL", "BENEFITS_PERSONNEL", "BENEFITS",
                      "SALARIES_BENEFITS", "AIRCRAFT_FUEL", "OP_EXPENSE"]},
    "p1a": {"code": "GEF", "db": FIN_DB, "period": "month",
            "fields": ["YEAR", "MONTH", "UNIQUE_CARRIER", "EMPFULL", "EMPPART", "EMPTOTAL", "EMPFTE"]},
    "t1": {"code": "FJH", "db": SUM_DB, "period": "month",
           "fields": ["YEAR", "MONTH", "UNIQUE_CARRIER", "REGION", "SERVICE_CLASS",
                      "REV_PAX_ENP_110", "REV_PAX_MILES_140", "AVL_SEAT_MILES_320", "REV_ACRFT_DEP_PERF_510"]},
    # Annual employment by labor category
    "p10": {"code": "GDF", "db": FIN_DB, "period": "year",
            "fields": ["YEAR", "UNIQUE_CARRIER", "ENTITY", "GENERAL_MANAGE", "PILOTS_COPILOTS", "OTHER_FLT_PERS",
                       "PASS_GEN_SVC_ADMIN", "MAINTENANCE", "ARCFT_TRAF_HANDLING_GRP1", "GEN_ARCFT_TRAF_HANDLING",
                       "AIRCRAFT_CONTROL", "PASSENGER_HANDLING", "CARGO_HANDLING", "TRAINEES_INTRUCTOR", "STATISTICAL",
                       "TRAFFIC_SOLICITERS", "OTHER", "TRANSPORT_RELATED", "TOTAL"]},
    # Aircraft operating expenses: pilot and copilot salaries (summed across aircraft types)
    "p52": {"code": "FMK", "db": FIN_DB, "period": "quarter",
            "fields": ["YEAR", "QUARTER", "UNIQUE_CARRIER", "REGION", "AIRCRAFT_TYPE", "PILOT_FLY_OPS"]},
    # Monthly fuel consumption and cost
    "p12a": {"code": "FMH", "db": FIN_DB, "period": "month",
             "fields": ["YEAR", "MONTH", "UNIQUE_CARRIER", "TOTAL_GALLONS", "TOTAL_COST"]},
}

# P-10 categories grouped for display
P10_GROUPS = {
    "pilots": ["PILOTS_COPILOTS"],
    "cabin": ["PASS_GEN_SVC_ADMIN", "OTHER_FLT_PERS"],
    "maint": ["MAINTENANCE"],
    "handling": ["ARCFT_TRAF_HANDLING_GRP1", "GEN_ARCFT_TRAF_HANDLING", "AIRCRAFT_CONTROL", "PASSENGER_HANDLING", "CARGO_HANDLING"],
    "mgmt": ["GENERAL_MANAGE"],
    "other": ["TRAINEES_INTRUCTOR", "STATISTICAL", "TRAFFIC_SOLICITERS", "OTHER", "TRANSPORT_RELATED"],
}


class TranStats:
    def __init__(self, user_agent):
        self.ua = user_agent
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def _open(self, req, timeout=180):
        req.add_header("User-Agent", self.ua)
        return self.opener.open(req, timeout=timeout)

    def download(self, table, year, tries=3):
        t = TABLES[table]
        url = BASE + "?" + urllib.parse.urlencode({"gnoyr_VQ": t["code"], "QO_fu146_anzr": t["db"]})
        last = None
        for i in range(tries):
            try:
                page = self._open(urllib.request.Request(url)).read().decode("utf-8", "replace")
                form = {}
                for m in re.finditer(r'<input[^>]*type="hidden"[^>]*>', page, re.I):
                    tag = m.group(0)
                    name = re.search(r'name="([^"]*)"', tag)
                    val = re.search(r'value="([^"]*)"', tag)
                    if name:
                        form[name.group(1)] = (val.group(1) if val else "").replace("&amp;", "&")
                form.update({"cboGeography": "All", "cboYear": str(year), "cboPeriod": "All", "btnDownload": "Download"})
                body = urllib.parse.urlencode(list(form.items()) + [(f, "on") for f in t["fields"]]).encode()
                req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/x-www-form-urlencoded", "Referer": url})
                with self._open(req) as r:
                    data = r.read()
                if data[:2] != b"PK":
                    raise ValueError("TranStats did not return a zip file")
                z = zipfile.ZipFile(io.BytesIO(data))
                text = z.read(z.namelist()[0]).decode("utf-8-sig", "replace")
                return list(csv.DictReader(io.StringIO(text)))
            except Exception as e:  # noqa: BLE001
                last = e
                time.sleep(5 * (i + 1))
        raise last


def num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def qend(year, q):
    return f"{int(year)}-{int(q) * 3:02d}"


def aggregate(raw, codes):
    """raw: {table: [rows across years]} -> {(code, quarter): record}"""
    out = {}

    def rec(c, q):
        return out.setdefault((c, q), {"code": c, "quarter": q})

    # P-1.2 and P-6: quarterly, summed across regions; values reported in $000
    for table, fields in (("p12", {"OP_REVENUES": "rev", "TRANS_REV_PAX": "pax_rev", "OP_EXPENSES": "opex",
                                    "OP_PROFIT_LOSS": "opinc", "NET_INCOME": "ni"}),
                          ("p6", {"SALARIES_FLIGHT": "sal_flight", "SALARIES_MAINT": "sal_maint",
                                  "SALARIES_TRAFFIC": "sal_traffic", "SALARIES_MGT": "sal_mgt",
                                  "SALARIES_OTHER": "sal_other", "SALARIES": "sal",
                                  "BENEFITS_PENSIONS": "ben_pension", "BENEFITS_PAYROLL": "ben_payroll",
                                  "BENEFITS_PERSONNEL": "ben_personnel", "BENEFITS": "ben",
                                  "SALARIES_BENEFITS": "labor", "AIRCRAFT_FUEL": "fuel", "OP_EXPENSE": "p6_opex"})):
        acc = {}
        for r in raw.get(table, []):
            c = r.get("UNIQUE_CARRIER")
            if c not in codes:
                continue
            k = (c, qend(r["YEAR"], r["QUARTER"]))
            a = acc.setdefault(k, {})
            for src, dst in fields.items():
                a[dst] = a.get(dst, 0.0) + num(r.get(src))
        for (c, q), a in acc.items():
            r = rec(c, q)
            r.update({k: round(v / 1000, 1) for k, v in a.items()})
            if table == "p6":
                # Comparable to the SEC "salaries, wages and benefits" line: total salaries and fringe
                # benefits less DOT's "personnel expense" (training, uniforms, crew travel and the like).
                r["comp"] = round((a.get("labor", 0) - a.get("ben_personnel", 0)) / 1000, 1)

    # Monthly tables: keep complete quarters only
    def monthly(table, fn):
        by = {}
        for r in raw.get(table, []):
            c = r.get("UNIQUE_CARRIER")
            if c not in codes:
                continue
            y, m = int(r["YEAR"]), int(r["MONTH"])
            by.setdefault((c, y, m), []).append(r)
        quarters = {}
        for (c, y, m), rs in by.items():
            quarters.setdefault((c, qend(y, (m - 1) // 3 + 1)), {})[m] = rs
        for (c, q), months in quarters.items():
            if len(months) == 3:
                rec(c, q).update(fn(months))

    def emp(months):
        fte = [sum(num(r["EMPFTE"]) for r in rs) for rs in months.values()]
        tot = [sum(num(r["EMPTOTAL"]) for r in rs) for rs in months.values()]
        last = months[max(months)]
        return {"fte": round(sum(fte) / 3), "emp_total": round(sum(tot) / 3),
                "fte_end": round(sum(num(r["EMPFTE"]) for r in last))}

    def traffic(months):
        rows = [r for rs in months.values() for r in rs if r.get("SERVICE_CLASS") == "Z"]  # all service
        return {"asm": round(sum(num(r["AVL_SEAT_MILES_320"]) for r in rows) / 1e6, 1),
                "rpm": round(sum(num(r["REV_PAX_MILES_140"]) for r in rows) / 1e6, 1),
                "pax": round(sum(num(r["REV_PAX_ENP_110"]) for r in rows) / 1e3, 1),
                "deps": round(sum(num(r["REV_ACRFT_DEP_PERF_510"]) for r in rows))}

    monthly("p1a", emp)
    monthly("t1", traffic)
    return out


def aggregate_annual(raw, codes):
    """Headcount by labor category (P-10) and pilot salaries (P-5.2) by carrier and year."""
    out = {}
    for r in raw.get("p10", []):
        c = r.get("UNIQUE_CARRIER")
        if c not in codes:
            continue
        a = out.setdefault((c, int(r["YEAR"])), {"code": c, "year": int(r["YEAR"])})
        for g, cols in P10_GROUPS.items():
            a["h_" + g] = a.get("h_" + g, 0) + sum(num(r.get(k)) for k in cols)
        a["h_total"] = a.get("h_total", 0) + num(r.get("TOTAL"))
    pil = {}
    for r in raw.get("p52", []):
        c = r.get("UNIQUE_CARRIER")
        if c not in codes:
            continue
        k = (c, int(r["YEAR"]))
        pil.setdefault(k, {})
        q = int(r["QUARTER"])
        pil[k][q] = pil[k].get(q, 0.0) + num(r.get("PILOT_FLY_OPS"))
    for k, qs in pil.items():
        if len(qs) == 4:  # complete years only
            a = out.setdefault(k, {"code": k[0], "year": k[1]})
            a["pilot_sal"] = round(sum(qs.values()) / 1000, 1)  # $ millions
    rows = []
    for a in out.values():
        for kk in list(a):
            if kk.startswith("h_"):
                a[kk] = round(a[kk])
        if a.get("pilot_sal") and a.get("h_pilots"):
            a["pilot_avg"] = round(a["pilot_sal"] * 1e6 / a["h_pilots"])
        rows.append(a)
    return sorted(rows, key=lambda r: (r["code"], r["year"]))


def aggregate_fuel(raw, codes):
    """Monthly fuel gallons (millions) and cost ($ millions) by carrier, from P-12(a)."""
    acc = {}
    for r in raw.get("p12a", []):
        c = r.get("UNIQUE_CARRIER")
        if c not in codes:
            continue
        k = (c, f"{int(r['YEAR'])}-{int(r['MONTH']):02d}")
        a = acc.setdefault(k, [0.0, 0.0])
        a[0] += num(r.get("TOTAL_GALLONS"))
        a[1] += num(r.get("TOTAL_COST"))
    return [{"code": c, "month": m, "gal": round(g / 1e6, 3), "cost": round(cost / 1e6, 3)}
            for (c, m), (g, cost) in sorted(acc.items()) if g > 0]


def update_dot(root, meta, user_agent, first_year=2022, force=False, refetch_years=2):
    """Refresh data/dot.json. Weekly unless forced; on failure keep what was there."""
    data_dir = os.path.join(root, "data")
    cfg = json.load(open(os.path.join(root, "config", "dot_carriers.json"), encoding="utf-8"))
    codes = {c["code"] for c in cfg}
    cache_path = os.path.join(data_dir, "dot_raw_cache.json")
    prev = meta.get("dot", {})
    if not force and prev.get("at"):
        try:
            age = dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(prev["at"].replace("Z", "+00:00"))
            if age < dt.timedelta(days=6) and prev.get("ok"):
                return False
        except ValueError:
            pass
    try:
        cache = json.load(open(cache_path, encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        cache = {}
    this_year = dt.date.today().year
    years = list(range(first_year, this_year + 1))
    ts = TranStats(user_agent)
    status = {}
    for table in TABLES:
        # P-10 is filed once a year, after year end: the current year never exists yet
        tyears = [y for y in years if not (TABLES[table]["period"] == "year" and y >= this_year)]
        for y in tyears:
            key = f"{table}_{y}"
            if key in cache and y < this_year - (refetch_years - 1):
                continue  # settled history: reuse
            try:
                rows = ts.download(table, y)
                cache[key] = [{k: r.get(k, "") for k in TABLES[table]["fields"]} for r in rows
                              if r.get("UNIQUE_CARRIER") in codes and (table != "t1" or r.get("SERVICE_CLASS") == "Z")]
                status[key] = len(cache[key])
                time.sleep(2)
            except Exception as e:  # noqa: BLE001
                status[key] = f"failed: {type(e).__name__}: {e}"[:200]
    raw = {t: [r for y in years for r in cache.get(f"{t}_{y}", [])] for t in TABLES}
    recs = aggregate(raw, codes)
    annual = aggregate_annual(raw, codes)
    fuel = aggregate_fuel(raw, codes)
    rows = sorted(recs.values(), key=lambda r: (r["code"], r["quarter"]))
    failed = [k for k, v in status.items() if isinstance(v, str)]
    if rows:
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump(cache, f, separators=(",", ":"))
        with open(os.path.join(data_dir, "dot.json"), "w", encoding="utf-8") as f:
            json.dump(rows, f, indent=0, separators=(",", ":"))
        with open(os.path.join(data_dir, "dot_carriers.json"), "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=1, ensure_ascii=False)
        if annual:
            with open(os.path.join(data_dir, "dot_annual.json"), "w", encoding="utf-8") as f:
                json.dump(annual, f, indent=0, separators=(",", ":"))
        if fuel:
            with open(os.path.join(data_dir, "dot_fuel.json"), "w", encoding="utf-8") as f:
                json.dump(fuel, f, indent=0, separators=(",", ":"))
    latest = {}
    for r in rows:
        for k, f in (("financial", "labor"), ("traffic", "asm"), ("employment", "fte")):
            if r.get(f) is not None:
                latest[k] = max(latest.get(k, ""), r["quarter"])
    latest["headcount_year"] = max((a["year"] for a in annual if a.get("h_total")), default=None)
    latest["fuel_month"] = max((f["month"] for f in fuel), default=None)
    meta["dot"] = {"at": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
                   "ok": not failed, "failed": failed, "latest": latest, "rows": len(rows)}
    return True


if __name__ == "__main__":
    # Local test: python scripts/dot.py RAW_DIR  (RAW_DIR holds P12_YYYY.csv etc. from TranStats)
    import sys
    raw_dir = sys.argv[1]
    names = {"p12": "P12", "p6": "P6", "p1a": "P1a", "t1": "T1"}
    raw = {}
    for t, n in names.items():
        raw[t] = []
        for fn in sorted(os.listdir(raw_dir)):
            if fn.startswith(n + "_"):
                raw[t] += list(csv.DictReader(open(os.path.join(raw_dir, fn), encoding="utf-8-sig")))
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    cfg = json.load(open(os.path.join(root, "config", "dot_carriers.json"), encoding="utf-8"))
    recs = aggregate(raw, {c["code"] for c in cfg})
    print(json.dumps(sorted(recs.values(), key=lambda r: (r["code"], r["quarter"])), separators=(",", ":")))
