"""A filer's own (extension) XBRL elements, read from each 10-Q and 10-K.

SEC's company-facts API carries only standard-taxonomy elements (us-gaap, dei, ...), so a line a carrier tags with
its own element never shows up there. Allegiant reports fuel as algt:AircraftFuel, SkyWest as skyw:AircraftFuel and
later skyw:FuelCost. For carriers whose config entry lists "extension_elements", this reads those elements from each
filing's XBRL instance document (<doc>_htm.xml), keeping only totals (no dimensions), and adds them to the
company-facts data under the filer's own namespace, in the same shape, so the normal extraction picks them up.

Facts are cached per filing in data/extfacts_cache.json, so each filing is downloaded once. If SEC can't be reached,
the cached filings are still used.
"""
import json
import os
import re
import time
import urllib.request
import xml.etree.ElementTree as ET

XBRLI = "{http://www.xbrl.org/2003/instance}"


def _get(url, ua):
    req = urllib.request.Request(url, headers={"User-Agent": ua, "Accept-Encoding": "identity"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return r.read()


def instance_facts(xml_bytes, names):
    """{element: [[start or None, end, value], ...]} for undimensioned USD facts of the named (local) elements."""
    root = ET.fromstring(xml_bytes)
    ctx = {}
    for c in root.iter(XBRLI + "context"):
        ent = c.find(XBRLI + "entity")
        if (ent is not None and ent.find(XBRLI + "segment") is not None) or c.find(XBRLI + "scenario") is not None:
            continue  # dimensioned (segment, member) figure: not the total
        p = c.find(XBRLI + "period")
        s, e, i = p.find(XBRLI + "startDate"), p.find(XBRLI + "endDate"), p.find(XBRLI + "instant")
        if s is not None and e is not None:
            ctx[c.get("id")] = (s.text.strip(), e.text.strip())
        elif i is not None:
            ctx[c.get("id")] = (None, i.text.strip())
    usd = {u.get("id") for u in root.iter(XBRLI + "unit") if any((m.text or "").strip().endswith("USD") for m in u.iter(XBRLI + "measure"))}
    out = {}
    for el in root:
        tag = el.tag.split("}")[-1]
        if tag not in names or el.get("contextRef") not in ctx or el.get("unitRef") not in usd:
            continue
        try:
            v = float((el.text or "").strip())
        except ValueError:
            continue
        s, e = ctx[el.get("contextRef")]
        row = [s, e, v]
        if row not in out.setdefault(tag, []):
            out[tag].append(row)
    return out


def load_cache(root):
    try:
        return json.load(open(os.path.join(root, "data", "extfacts_cache.json"), encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_cache(root, cache):
    path = os.path.join(root, "data", "extfacts_cache.json")
    with open(path + ".tmp", "w", encoding="utf-8") as f:
        json.dump(cache, f, separators=(",", ":"), sort_keys=True)
    os.replace(path + ".tmp", path)


def add_extension_facts(cf, c, cache, ua, since):
    """Add the carrier's extension elements to its company-facts dict `cf`, in place. Returns a status dict."""
    names = c.get("extension_elements") or []
    if not names:
        return None
    cik = int(c["cik"])
    status = {"elements": names, "new_filings": 0}
    try:
        sub = json.loads(_get(f"https://data.sec.gov/submissions/CIK{cik:010d}.json", ua))
        r = sub["filings"]["recent"]
        for i in range(len(r["form"])):
            form = r["form"][i]
            if form not in ("10-Q", "10-K") or r["reportDate"][i] < since:
                continue
            acc = r["accessionNumber"][i]
            have = cache.get(acc)
            if have and set(names) <= set(have.get("names", [])):
                continue
            url = f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc.replace('-', '')}/" + re.sub(r"\.htm$", "_htm.xml", r["primaryDocument"][i])
            try:
                cache[acc] = {"cik": cik, "form": form, "filed": r["filingDate"][i], "names": names, "facts": instance_facts(_get(url, ua), set(names))}
                status["new_filings"] += 1
                time.sleep(0.3)  # stay well under SEC's 10 requests/second
            except Exception as e:  # noqa: BLE001
                status["error"] = f"{acc}: {type(e).__name__}"
    except Exception as e:  # noqa: BLE001  (fall back to whatever is cached)
        status["error"] = f"submissions: {type(e).__name__}: {e}"[:200]
    ns = cf.setdefault("facts", {}).setdefault(c["ticker"].lower() + "-ext", {})
    n = 0
    for acc, f in cache.items():
        if f.get("cik") != cik:
            continue
        for name, rows in f.get("facts", {}).items():
            if name not in names:
                continue
            usd = ns.setdefault(name, {"units": {"USD": []}})["units"]["USD"]
            for s, e, v in rows:
                fact = {"end": e, "val": v, "accn": acc, "form": f["form"], "filed": f["filed"]}
                if s:
                    fact["start"] = s
                usd.append(fact)
                n += 1
    status["facts"] = n
    return status
