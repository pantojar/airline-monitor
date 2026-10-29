"""Monthly operational reliability from DOT's on-time flight records (BTS TranStats).

Source: "Marketing Carrier On-Time Performance" (table FGK in TranStats' Airline On-Time Performance
database), one row per scheduled domestic flight by every carrier that reports on-time data. Each row
carries both the operating carrier (the airline that flew it, such as SkyWest) and the marketing
carrier (the brand it was sold under, such as Delta Connection), so one download gives both views:

  by operating carrier       on-time, cancellations, diversions and delay causes for each airline
  mainline + regional        the same, rolled up to the brand the passenger booked

A month is downloaded once, summed by (marketing carrier, operating carrier), and kept in
data/reliability_cache.json. The two most recent months are re-read each time in case BTS revises them.

Definitions (DOT): a flight is on time if it arrives less than 15 minutes after schedule. Cancelled
and diverted flights count as not on time. Delay minutes by cause come from the carriers' own
attribution of delays of 15 minutes or more.
"""
import csv
import datetime as dt
import io
import json
import os
import re
import time
import urllib.parse
import urllib.request
import zipfile

import dot

BASE = "https://www.transtats.bts.gov/DL_SelectFields.aspx"
TABLE, DB = "FGK", "b0-gvzr"
FIELDS = ["YEAR", "MONTH", "MKT_UNIQUE_CARRIER", "OP_UNIQUE_CARRIER", "DUP", "FLIGHTS", "ARR_DEL15", "ARR_DELAY_NEW",
          "CANCELLED", "CANCELLATION_CODE", "DIVERTED", "CARRIER_DELAY", "WEATHER_DELAY", "NAS_DELAY", "SECURITY_DELAY",
          "LATE_AIRCRAFT_DELAY"]
# summed per (marketing, operating) pair
SUMS = ["flights", "arr", "ontime", "del15", "delay_min", "cancelled", "diverted", "canc_carrier", "canc_weather", "canc_nas",
        "canc_security", "carrier_min", "weather_min", "nas_min", "security_min", "late_aircraft_min"]
FIRST_MONTH = "2023-01"


def download_month(ts, year, month, tries=3):
    url = BASE + "?" + urllib.parse.urlencode({"gnoyr_VQ": TABLE, "QO_fu146_anzr": DB})
    last = None
    for i in range(tries):
        try:
            page = ts._open(urllib.request.Request(url)).read().decode("utf-8", "replace")
            form = {}
            for m in re.finditer(r'<input[^>]*type="hidden"[^>]*>', page, re.I):
                tag = m.group(0)
                name = re.search(r'name="([^"]*)"', tag)
                val = re.search(r'value="([^"]*)"', tag)
                if name:
                    form[name.group(1)] = (val.group(1) if val else "").replace("&amp;", "&")
            form.update({"cboGeography": "All", "cboYear": str(year), "cboPeriod": str(month), "btnDownload": "Download"})
            body = urllib.parse.urlencode(list(form.items()) + [(f, "on") for f in FIELDS]).encode()
            req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/x-www-form-urlencoded", "Referer": url})
            with ts._open(req, timeout=600) as r:
                data = r.read()
            if data[:2] != b"PK":
                raise ValueError("TranStats did not return a zip file")
            z = zipfile.ZipFile(io.BytesIO(data))
            return z.read(z.namelist()[0]).decode("utf-8-sig", "replace")
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(10 * (i + 1))
    raise last


def f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def summarize(text):
    """{"MKT|OP": {sums}} for one month of flight records."""
    out = {}
    for r in csv.DictReader(io.StringIO(text)):
        if (r.get("DUP") or "").strip().upper() == "Y":
            continue  # second listing of a flight reported under two codes
        n = f(r.get("FLIGHTS")) or 1.0
        k = (r.get("MKT_UNIQUE_CARRIER") or "").strip() + "|" + (r.get("OP_UNIQUE_CARRIER") or "").strip()
        s = out.setdefault(k, dict.fromkeys(SUMS, 0.0))
        s["flights"] += n
        canc, div = f(r.get("CANCELLED")) > 0, f(r.get("DIVERTED")) > 0
        if canc:
            s["cancelled"] += n
            code = (r.get("CANCELLATION_CODE") or "").strip().upper()
            key = {"A": "canc_carrier", "B": "canc_weather", "C": "canc_nas", "D": "canc_security"}.get(code)
            if key:
                s[key] += n
        elif div:
            s["diverted"] += n
        else:
            s["arr"] += n
            late = f(r.get("ARR_DEL15")) > 0
            s["del15" if late else "ontime"] += n
            s["delay_min"] += f(r.get("ARR_DELAY_NEW"))
            for src, dst in (("CARRIER_DELAY", "carrier_min"), ("WEATHER_DELAY", "weather_min"), ("NAS_DELAY", "nas_min"),
                             ("SECURITY_DELAY", "security_min"), ("LATE_AIRCRAFT_DELAY", "late_aircraft_min")):
                s[dst] += f(r.get(src))
    return {k: {a: round(b) for a, b in v.items()} for k, v in out.items()}


def months_between(a, b):
    y, m = int(a[:4]), int(a[5:])
    out = []
    while f"{y}-{m:02d}" <= b:
        out.append(f"{y}-{m:02d}")
        m += 1
        if m > 12:
            y, m = y + 1, 1
    return out


def update_reliability(root, meta, user_agent, max_new=15, force=False):
    data = os.path.join(root, "data")
    cache_path = os.path.join(data, "reliability_cache.json")
    try:
        cache = json.load(open(cache_path, encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        cache = {}
    prev = meta.get("reliability", {})
    today = dt.date.today()
    # BTS posts a month roughly 6-8 weeks after it ends; look back 3 months for anything new
    last_possible = (today.replace(day=1) - dt.timedelta(days=1)).strftime("%Y-%m")
    have = sorted(k for k, v in cache.items() if v)
    latest = have[-1] if have else None
    wanted = [m for m in months_between(FIRST_MONTH, last_possible) if m not in cache or not cache[m]]
    # newly published month: refetch the two before it too (BTS sometimes revises)
    recheck = have[-2:] if force else []
    # don't re-ask BTS for an unpublished month more than once a day
    if not force and prev.get("checked") == today.isoformat():
        wanted = [m for m in wanted if latest is None or m <= latest]
    todo = (sorted(set(wanted + recheck)))[-max_new:]
    ts = dot.TranStats(user_agent)
    report, got = {}, 0
    for m in todo:
        try:
            text = download_month(ts, int(m[:4]), int(m[5:]))
            summ = summarize(text)
            if summ:
                cache[m] = summ
                got += 1
                report[m] = sum(v["flights"] for v in summ.values())
            else:
                report[m] = "empty (not yet published)"
                if m > (latest or ""):
                    break  # later months won't be there either
        except Exception as e:  # noqa: BLE001
            report[m] = f"failed: {type(e).__name__}: {e}"[:200]
            if m > (latest or ""):
                break
        time.sleep(2)
    if got:
        json.dump(cache, open(cache_path, "w", encoding="utf-8"), separators=(",", ":"))
    # page file: months x pairs, compact
    rows = []
    for m in sorted(cache):
        for k, v in cache[m].items():
            mk, op = k.split("|")
            rows.append([m, mk, op] + [v.get(s, 0) for s in SUMS])
    json.dump({"fields": ["month", "mkt", "op"] + SUMS, "rows": rows}, open(os.path.join(data, "reliability.json"), "w", encoding="utf-8"),
              separators=(",", ":"))
    have = sorted(k for k, v in cache.items() if v)
    meta["reliability"] = {"at": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
                           "checked": today.isoformat(), "first": have[0] if have else None, "latest": have[-1] if have else None,
                           "fetched": report}
