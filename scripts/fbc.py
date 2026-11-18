"""EPI Family Budget Calculator: rebuild data/fbc.json from EPI's published workbook.

EPI publishes the full dataset once a year as one Excel file (fbc_data_YYYY.xlsx, linked from
https://www.epi.org/resources/budget/). The job looks for the newest file once a week and rebuilds
data/fbc.json only when the file changed. Standard library only (the workbook is read as zipped XML).

  python scripts/fbc.py                 # check EPI for a new edition (weekly unless --force)
  python scripts/fbc.py --file PATH     # build from a workbook already on disk

data/fbc.json:
  edition, dollars        e.g. 2026 edition, figures in 2025 dollars
  families                the 10 family types, in the order of each area's values
  parts                   budget parts, in the order of each family's values (annual dollars; last is the total)
  metro                   [id, state, name, top100 (0/1), median family income, values...]
  bases                   places listed first on the page (config/fbc_bases.json)
data/fbc_county.json:
  county                  [id, state, fips, name, median family income, values...]
values: 10 families x 8 parts = 80 annual figures, family by family.
"""
import datetime as dt
import hashlib
import io
import json
import os
import re
import sys
import urllib.request
import xml.etree.ElementTree as ET
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
CONFIG = os.path.join(ROOT, "config")
PAGE = "https://www.epi.org/resources/budget/"
FILE_RE = re.compile(r"https?://files\.epi\.org/[^\"'\s>]*fbc_data_(\d{4})\.xlsx", re.I)
FAMILIES = ["1p0c", "1p1c", "1p2c", "1p3c", "1p4c", "2p0c", "2p1c", "2p2c", "2p3c", "2p4c"]
PARTS = ["housing", "food", "transportation", "healthcare", "other", "childcare", "taxes", "total"]
HEAD = {"housing": "housing", "food": "food", "transportation": "transportation", "healthcare": "healthcare",
        "other necessities": "other", "childcare": "childcare", "taxes": "taxes", "total": "total"}
NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
RNS = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"


def load(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, path)


def fetch(url, ua, timeout=120):
    req = urllib.request.Request(url, headers={"User-Agent": ua, "Accept": "*/*", "Accept-Encoding": "identity"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


# ------------------------------------------------------------------ workbook reader
def col_index(ref):
    n = 0
    for ch in re.match(r"[A-Z]+", ref).group(0):
        n = n * 26 + ord(ch) - 64
    return n - 1


def sheets(data):
    """{sheet name: list of rows (list of values)}. Formula cells give their cached value, or None."""
    z = zipfile.ZipFile(io.BytesIO(data))
    shared = []
    if "xl/sharedStrings.xml" in z.namelist():
        for si in ET.fromstring(z.read("xl/sharedStrings.xml")).findall("m:si", NS):
            shared.append("".join(t.text or "" for t in si.iter("{%s}t" % NS["m"])))
    rels = {r.get("Id"): r.get("Target") for r in ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))}
    out = {}
    for s in ET.fromstring(z.read("xl/workbook.xml")).find("m:sheets", NS):
        target = rels[s.get(RNS)].lstrip("/")
        path = target if target.startswith("xl/") else "xl/" + target
        rows = []
        for row in ET.fromstring(z.read(path)).find("m:sheetData", NS):
            vals = []
            for c in row.findall("m:c", NS):
                i = col_index(c.get("r"))
                v = c.find("m:v", NS)
                t = c.get("t")
                if t == "inlineStr":
                    val = "".join(x.text or "" for x in c.iter("{%s}t" % NS["m"]))
                elif v is None or v.text is None:
                    val = None
                elif t == "s":
                    val = shared[int(v.text)]
                elif t in ("str", "b", "e"):
                    val = v.text
                else:
                    f = float(v.text)
                    val = int(f) if f.is_integer() else f
                while len(vals) < i:
                    vals.append(None)
                vals.append(val)
            rows.append(vals)
        out[s.get("name")] = rows
    return out


def annual_cols(header):
    """Column of each budget part in the 'Annual' block: the second time each header appears."""
    seen, cols = {}, {}
    for i, h in enumerate(header):
        k = HEAD.get(str(h or "").strip().lower())
        if not k:
            continue
        seen[k] = seen.get(k, 0) + 1
        if seen[k] == 2:
            cols[k] = i
    if len(cols) != len(PARTS):
        raise ValueError("annual columns not found: " + ", ".join(p for p in PARTS if p not in cols))
    return [cols[p] for p in PARTS]


def header_row(rows, first):
    for i, r in enumerate(rows[:10]):
        if r and str(r[0] or "").strip() == "case_id":
            return i
    raise ValueError("no header row starting with case_id")


def build(data):
    S = sheets(data)
    summary = " ".join(str(v) for r in S.get("Summary", []) for v in r if v)
    m = re.search(r"(\d{4}) edition", summary)
    d = re.search(r"in (\d{4}) dollars", summary)
    out = {"edition": int(m.group(1)) if m else None, "dollars": int(d.group(1)) if d else None,
           "families": FAMILIES, "parts": PARTS, "metro": [], "county": []}

    def collect(rows, idf, fixed):
        h = header_row(rows, 0)
        head = [str(x or "").strip() for x in rows[h]]
        cols = annual_cols(head)
        pos = {k: head.index(k) for k in fixed}
        areas = {}
        for r in rows[h + 1:]:
            if not r or r[0] is None:
                continue
            r = r + [None] * (len(head) - len(r))
            fam = str(r[pos["Family"]]).strip()
            if fam not in FAMILIES:
                continue
            key = r[0]
            a = areas.setdefault(key, {"row": r, "vals": {}})
            a["vals"][fam] = [round(r[c]) if isinstance(r[c], (int, float)) else None for c in cols]
        res = []
        for key, a in areas.items():
            if len(a["vals"]) != len(FAMILIES):
                raise ValueError(f"area {key}: {len(a['vals'])} family types")
            flat = [v for f in FAMILIES for v in a["vals"][f]]
            res.append(idf(a["row"], pos) + flat)
        return res

    def mfi(v):
        return round(v) if isinstance(v, (int, float)) else None

    out["metro"] = collect(S["Metro"], lambda r, p: [r[0], r[p["State abv."]], str(r[p["Areaname"]]).strip(),
                                                      1 if r[p["top100"]] in (1, "1") else 0, mfi(r[p["median_family_income"]])],
                           ["Family", "State abv.", "Areaname", "top100", "median_family_income"])
    out["county"] = collect(S["County"], lambda r, p: [r[0], r[p["State abv."]], r[p["county_fips"]], str(r[p["County"]]).strip(),
                                                        mfi(r[p["median_family_income"]])],
                            ["Family", "State abv.", "county_fips", "County", "median_family_income"])
    # sanity: every family's parts add up to its total (EPI rounds each part, so allow a few dollars)
    for rec, k in ((out["metro"], 5), (out["county"], 5)):
        for a in rec:
            for i in range(len(FAMILIES)):
                v = a[k + i * 8:k + i * 8 + 8]
                if None in v or abs(sum(v[:7]) - v[7]) > 10:
                    raise ValueError(f"parts don't add to total: {a[:4]} {FAMILIES[i]}")
    if len(out["metro"]) < 400 or len(out["county"]) < 3000:
        raise ValueError(f"too few areas: {len(out['metro'])} metro, {len(out['county'])} county")
    return out


def attach_bases(out):
    cfg = load(os.path.join(CONFIG, "fbc_bases.json"), {}) or {}
    names = {(a[1], a[2]) for a in out["metro"]}
    county = out.get("county") or (load(os.path.join(DATA, "fbc_county.json"), {}) or {}).get("county", [])
    counties = {(a[1], a[3]) for a in county}
    bases, missing = [], []
    for b in cfg.get("bases", []):
        if b.get("area") and (b.get("st"), b["area"]) in names:
            bases.append(b)
        elif b.get("county") and (b.get("st"), b["county"]) in counties:
            bases.append(b)
        else:
            missing.append(b.get("label"))
    out["bases"] = bases
    return missing


# ------------------------------------------------------------------ refresh
def find_url(ua):
    """Newest fbc_data_YYYY.xlsx linked from EPI's calculator page; if the page can't be read, try likely names."""
    try:
        html = fetch(PAGE, ua, timeout=60).decode("utf-8", "replace")
        found = sorted({(int(y), u) for u, y in ((m.group(0), m.group(1)) for m in FILE_RE.finditer(html))})
        if found:
            return found[-1][1], "page"
    except Exception:  # noqa: BLE001
        pass
    y = dt.date.today().year
    for yr in (y + 1, y, y - 1):
        u = f"https://files.epi.org/uploads/fbc_data_{yr}.xlsx"
        try:
            req = urllib.request.Request(u, method="HEAD", headers={"User-Agent": ua})
            with urllib.request.urlopen(req, timeout=30) as r:
                if r.status == 200:
                    return u, "guess"
        except Exception:  # noqa: BLE001
            continue
    return None, None


def update_fbc(root, meta, ua, force=False, path=None):
    global DATA, CONFIG
    DATA, CONFIG = os.path.join(root, "data"), os.path.join(root, "config")
    prev = meta.get("fbc", {}) or {}
    info = {"checked": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")}
    out_path = os.path.join(DATA, "fbc.json")
    have = os.path.exists(out_path)
    if path is None and have and not force and prev.get("checked"):
        last = dt.datetime.fromisoformat(prev["checked"].replace("Z", "+00:00"))
        if dt.datetime.now(dt.timezone.utc) - last < dt.timedelta(days=7):
            # nothing to fetch this run; still pick up edits to the base list
            cur = load(out_path)
            if cur:
                miss = attach_bases(cur)
                save(out_path, cur)
                prev["bases_missing"] = miss
            meta["fbc"] = prev
            return
    try:
        if path:
            data, url, how = open(path, "rb").read(), prev.get("url"), "file"
        else:
            url, how = find_url(ua)
            if not url:
                raise RuntimeError("no dataset link found on EPI's page")
            data = fetch(url, ua)
        sha = hashlib.sha256(data).hexdigest()[:16]
        info.update(url=url, found_by=how, bytes=len(data), sha=sha)
        if have and sha == prev.get("sha") and not path:
            cur = load(out_path)
            info.update(edition=prev.get("edition"), dollars=prev.get("dollars"), changed=prev.get("changed"))
            if cur:
                info["bases_missing"] = attach_bases(cur)
                save(out_path, cur)
        else:
            out = build(data)
            out["source"] = url or (f"https://files.epi.org/uploads/fbc_data_{out['edition']}.xlsx" if out["edition"] else PAGE)
            info["url"] = out["source"]
            info["bases_missing"] = attach_bases(out)
            # counties go in their own file, which the page loads only when someone searches
            save(os.path.join(DATA, "fbc_county.json"), {"edition": out["edition"], "county": out.pop("county")})
            save(out_path, out)
            info.update(edition=out["edition"], dollars=out["dollars"], metro=len(out["metro"]),
                        changed=info["checked"])
    except Exception as e:  # noqa: BLE001
        info["error"] = f"{type(e).__name__}: {e}"[:300]
        for k in ("url", "sha", "edition", "dollars", "changed"):
            if k in prev and k not in info:
                info[k] = prev[k]
        print(f"[warn] EPI family budgets: {e}", file=sys.stderr)
    meta["fbc"] = info


if __name__ == "__main__":
    argv = sys.argv[1:]
    meta_path = os.path.join(DATA, "meta.json")
    meta = load(meta_path, {}) or {}
    p = argv[argv.index("--file") + 1] if "--file" in argv else None
    update_fbc(ROOT, meta, "TWU-Airline-Industry-Monitor (github.com/pantojar/airline-monitor)", force="--force" in argv, path=p)
    tmp = meta_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=1)
    os.replace(tmp, meta_path)
    print(json.dumps(meta.get("fbc"), indent=1))
