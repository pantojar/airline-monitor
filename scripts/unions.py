"""Read union websites for bargaining milestones, group by group.

For each site in config/labor_sources.json the job tries, in order:
  1. WordPress REST API: recent posts (/wp-json/wp/v2/posts) and uploaded PDFs (/wp-json/wp/v2/media)
  2. An RSS feed (/feed/, /rss, /rss.xml)
PDFs whose titles look like announcements (update, ratification, results, vote, tentative agreement,
message, bulletin) are downloaded and their text extracted. Contract documents are skipped unless the
title itself states a status ("..._Ratified").

Each item is split into clauses; a clause that names a tracked group (for a carrier the site covers,
and a union the site belongs to) and a bargaining milestone becomes a dated event for that group:
  "Fleet Service, Passenger Service, and Central Load Planners did not ratify their tentative agreements"
    -> Fleet Service: Contract rejected; Passenger Service: Contract rejected; Load Planners: Contract rejected
Processed URLs are cached in data/union_cache.json, so each item is read once.
"""
import datetime as dt
import email.utils
import html
import io
import json
import os
import re
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

STOP = {"and", "related", "employees", "employee", "of", "the", "&", "other", "classifications", "inc", "airlines",
        "southwest", "delta", "endeavor", "united", "representatives", "agreement", "group"}
SYNONYMS = {"inflight": "flight attendant", "crewmember": "", "technicians": "technician", "stores": "storekeeper"}
ANNOUNCE_RE = re.compile(r"update|ratif|result|vote|tentative|\bTA\b|announce|message|bulletin|news|negotiat|mediat|strike|picket|status|reject", re.I)
CONTRACT_DOC_RE = re.compile(r"agreement|contract|\bCBA\b|\bLOA\b|letter of agreement", re.I)

STATUS_RULES = [
    ("Contract rejected", r"\breject|did not ratify|not ratified|failed to ratify|voted down|vote(d)? no\b|not been ratified"),
    ("Ratified", r"ratif(y|ied|ies|ication)|approved? (the|their|a) (new )?(contract|agreement|TA)"),
    ("Tentative agreement", r"tentative (agreement|deal)|agreement in principle|reached (a|an) (deal|agreement)"),
    ("Strike vote", r"strike (vote|authori[sz]ation)|vote[sd]? to (authori[sz]e|strike)"),
    ("Seeking release", r"request(s|ed|ing)? (a )?release|release from (the )?(NMB|mediation)|cooling[- ]off|proffer"),
    ("In mediation", r"\bmediat|\bNMB\b|national mediation board"),
    ("Picketing", r"\bpicket|\brall(y|ies)\b"),
    ("In negotiations", r"\bnegotiat|\bbargaining session|\bmet with the company|\bmeet with the company|\btalks\b"),
]
CLAUSE_SPLIT = re.compile(r"(?<=[.!?;])\s+|\s+(?:while|whereas|but|however)\s+", re.I)


def clean(s):
    s = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", s or "")
    s = re.sub(r"<[^>]+>", " ", s)
    return re.sub(r"\s+", " ", html.unescape(s).replace(" ", " ")).strip()


def group_tokens(group):
    words = [w for w in re.findall(r"[a-z]+", group.lower()) if w not in STOP]
    words = [SYNONYMS.get(w, w) for w in words]
    toks = []
    for w in " ".join(words).split():
        w = w[:-1] if w.endswith("s") and len(w) > 4 else w
        if len(w) >= 3 and w not in toks:
            toks.append(w)
    return toks[:2]


# other names unions use for the same craft as the 10-K
ALT_NAMES = [
    (r"stock clerk|material specialist|storekeeper", ["store"]),
    (r"mechanic|technician", ["mechanic"]),
]


def group_alternates(group):
    out = [group_tokens(group)]
    for rx, toks in ALT_NAMES:
        if re.search(rx, group, re.I):
            out.append(toks)
    return out


def clause_names_group(clause, toks):
    if not toks:
        return False
    if isinstance(toks[0], list):
        return any(clause_names_group(clause, t) for t in toks)
    words = re.findall(r"[a-z]+", clause.lower())
    return all(any(w.startswith(t) for w in words) for t in toks)


def status_of(clause):
    for st, rx in STATUS_RULES:
        if re.search(rx, clause, re.I):
            return st
    return None


# ---------------------------------------------------------------- fetching
class Fetch:
    def __init__(self, ua):
        # the conventional form for automated readers, which many site firewalls let through
        self.ua = "Mozilla/5.0 (compatible; " + ua.replace(" ", "/", 1) + ")"

    def get(self, url, accept="*/*", timeout=25, limit=8_000_000):
        req = urllib.request.Request(url, headers={"User-Agent": self.ua, "Accept": accept, "Accept-Encoding": "identity"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = r.read(limit + 1)
            if len(data) > limit:
                raise ValueError("too large")
            return data, r.headers.get("Content-Type", "")


def pdf_text(data, max_pages=12):
    from pypdf import PdfReader  # installed in the workflow
    rd = PdfReader(io.BytesIO(data))
    return clean(" ".join((p.extract_text() or "") for p in rd.pages[:max_pages]))


def _err(e):
    code = getattr(e, "code", None)
    return f"HTTP {code}" if code else type(e).__name__


def list_items(f, site, since):
    """Return (method, [{date, title, url, kind, text?}])"""
    base = site["url"].rstrip("/")
    items, methods, tried = [], [], []
    after = since + "T00:00:00"
    for page in range(1, 5):
        try:
            data, _ = f.get(f"{base}/wp-json/wp/v2/posts?per_page=50&page={page}&after={after}&_fields=date,link,title,content", "application/json")
            got = json.loads(data)
        except Exception as e:  # noqa: BLE001
            if page == 1:
                tried.append("posts: " + _err(e))
            break
        if not isinstance(got, list) or not got:
            break
        for p in got:
            items.append({"date": p["date"][:10], "title": clean(p["title"]["rendered"]), "url": p["link"], "kind": "post",
                          "text": clean(p.get("content", {}).get("rendered", ""))[:30000]})
        if "wp-posts" not in methods:
            methods.append("wp-posts")
        if len(got) < 50:
            break
    for page in range(1, 5):
        try:
            data, _ = f.get(f"{base}/wp-json/wp/v2/media?mime_type=application/pdf&per_page=100&page={page}&after={after}&_fields=date,source_url,title", "application/json")
            got = json.loads(data)
        except Exception as e:  # noqa: BLE001
            if page == 1:
                tried.append("media: " + _err(e))
            break
        if not isinstance(got, list) or not got:
            break
        for m in got:
            items.append({"date": m["date"][:10], "title": clean(m["title"]["rendered"]).replace("_", " "), "url": m["source_url"], "kind": "pdf"})
        if "wp-media" not in methods:
            methods.append("wp-media")
        if len(got) < 100:
            break
    if not methods:
        for path in site.get("feeds") or ["/feed/", "/rss", "/rss.xml", "/feed"]:
            try:
                data, _ = f.get(base + path if path.startswith("/") else path, "application/rss+xml, application/xml")
                root = ET.fromstring(data)
                for it in root.iter("item"):
                    try:
                        d = email.utils.parsedate_to_datetime(it.findtext("pubDate")).date().isoformat()
                    except Exception:  # noqa: BLE001
                        continue
                    if d < since:
                        continue
                    items.append({"date": d, "title": clean(it.findtext("title")), "url": (it.findtext("link") or "").strip(), "kind": "post",
                                  "text": clean(it.findtext("{http://purl.org/rss/1.0/modules/content/}encoded") or it.findtext("description") or "")[:30000]})
                methods.append("rss " + path)
                break
            except Exception as e:  # noqa: BLE001
                tried.append(path + ": " + _err(e))
                continue
    return ",".join(methods) or None, items, tried


# ---------------------------------------------------------------- events
OTHER_CARRIERS = r"Air Canada|WestJet|Air Wisconsin|Spirit|Breeze|Avelo|Sun Country|GoJet|CommuteAir|Atlas Air|FedEx|\bUPS\b|Kalitta|Omni Air|Amerijet|Silver Airways|Cape Air|Hawaiian|Horizon|Envoy|Piedmont|\bPSA\b|Endeavor|Mesa|SkyWest|Republic|Frontier|JetBlue|Allegiant|Alaska|Southwest|Delta|American|United"


def events_for(item, groups, site, aliases):
    """groups: tracked rows this site may speak for. Returns [(row_key, event)].
    A clause that names any airline only counts for the airlines it names; a clause that names none
    counts for the airline named in the item's title (or the site's only airline)."""
    text = item["title"] + ". " + (item.get("text") or "")
    if not ANNOUNCE_RE.search(text):
        return []
    title = item["title"]
    out = []
    for g in groups:
        unit = g.get("unit")
        unit_alias = aliases.get(unit) if unit not in (None, "Mainline") else None
        is_sub = bool(unit_alias and unit_alias.get("parent"))
        own_rx = unit_alias["match"] if is_sub else aliases.get(g["ticker"], {}).get("match", g["ticker"])
        if not is_sub and site.get("names", {}).get(g["ticker"]):
            own_rx += "|" + site["names"][g["ticker"]]  # names the site uses for that airline's groups, e.g. "TWU-IAM Association"
        subs_rx = "|".join(a["match"] for a in aliases.values() if a.get("parent") == g["ticker"] and a.get("match"))
        single = len(site.get("carriers", [])) == 1

        def names_own(t):
            if not re.search(own_rx, t, re.I):
                return False
            if not is_sub and subs_rx:
                # "United Ground Express" also contains "United": mainline only if the parent is named outside the subsidiary's name
                stripped = re.sub(subs_rx, " ", t, flags=re.I)
                return bool(re.search(own_rx, stripped, re.I))
            return True

        toks = group_alternates(g["group"])
        best = None
        for clause in CLAUSE_SPLIT.split(text):
            if len(clause) > 900 or not clause_names_group(clause, toks):
                continue
            st = status_of(clause)
            if not st:
                continue
            if re.search(OTHER_CARRIERS, clause, re.I):
                ok = names_own(clause)
            else:
                ok = names_own(title) or (single and not (subs_rx and not is_sub and re.search(subs_rx, title, re.I)) and (not is_sub or re.search(own_rx, text, re.I)))
            if ok:
                best = (st, clause.strip())
                break
        if best:
            out.append(((g["ticker"], g.get("unit"), g["group"]),
                        {"status": best[0], "date": item["date"], "source": site["name"], "kind": "union",
                         "title": item["title"], "url": item["url"], "quote": best[1][:400]}))
    return out


def collect(root, groups, ua, aliases, since_days=400, budget_s=600):
    """Return ({row_key: [events]}, site_report)."""
    cfg = json.load(open(os.path.join(root, "config", "labor_sources.json"), encoding="utf-8"))
    cache_path = os.path.join(root, "data", "union_cache.json")
    try:
        cache = json.load(open(cache_path, encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        cache = {}
    since = (dt.date.today() - dt.timedelta(days=since_days)).isoformat()
    f = Fetch(ua)
    found, report = {}, []
    t0 = time.time()
    def status_text(text):
        # keep only the clauses that carry a status: small enough to cache, and enough to re-match later
        return "; ".join(c.strip().rstrip(";") for c in CLAUSE_SPLIT.split(text or "") if len(c) <= 900 and status_of(c))[:6000]

    def match(it, rel, site):
        for k, e in events_for(it, rel, site, aliases):
            found.setdefault(k, []).append(e)

    for site in cfg:
        rel = [g for g in groups if g["ticker"] in site["carriers"]
               and any(re.search(u, g.get("union") or "", re.I) for u in site["unions"])]
        if time.time() - t0 > budget_s:
            # out of time this run: re-match what earlier runs read from this site
            for k, v in cache.items():
                if v.get("site") == site["name"] and "text" in v:
                    match({"url": k, "date": v["date"], "title": v.get("title", ""), "text": v["text"]}, rel, site)
            report.append({"name": site["name"], "url": site["url"], "method": "deferred", "items": 0, "groups": len(rel)})
            continue
        rep = {"name": site["name"], "url": site["url"], "groups": len(rel)}
        try:
            method, items, tried = list_items(f, site, since)
            if not method:
                rep["error"] = "; ".join(tried)[:200]
        except Exception as e:  # noqa: BLE001
            method, items = None, []
            rep["error"] = f"{type(e).__name__}"
        rep["method"], rep["items"] = method, len(items)
        nread = 0
        for it in sorted(items, key=lambda x: x["date"], reverse=True):
            key = it["url"]
            c = cache.get(key)
            if it["kind"] == "pdf":
                if c and "text" in c:
                    it["text"] = c["text"]
                elif c and (c.get("skipped") or c.get("error")):
                    continue
                else:
                    if not ANNOUNCE_RE.search(it["title"]) or (CONTRACT_DOC_RE.search(it["title"]) and not re.search(r"ratif|reject|tentative|\bTA\b", it["title"], re.I)):
                        cache[key] = {"date": it["date"], "skipped": True}
                        continue
                    if nread >= 40 or time.time() - t0 > budget_s:
                        continue  # read the rest next week
                    try:
                        data, _ = f.get(key, "application/pdf", limit=6_000_000)
                        it["text"] = pdf_text(data)
                        nread += 1
                        time.sleep(0.5)
                    except Exception as e:  # noqa: BLE001
                        cache[key] = {"date": it["date"], "error": type(e).__name__}
                        continue
            cache[key] = {"date": it["date"], "title": it["title"][:200], "site": site["name"], "text": status_text(it.get("text"))}
            match(it, rel, site)
        rep["pdfs_read"] = nread
        report.append(rep)
    # keep the cache to recent items
    cache = {k: v for k, v in cache.items() if v.get("date", "9999") >= since}
    with open(cache_path, "w", encoding="utf-8") as fh:
        json.dump(cache, fh, separators=(",", ":"))
    for k in found:
        found[k].sort(key=lambda e: e["date"], reverse=True)
    return found, report


if __name__ == "__main__":
    # offline check of clause matching
    groups = [{"ticker": "UAL", "unit": "United Airlines, Inc.", "group": g, "union": "IAM"} for g in
              ["Fleet Service", "Passenger Service", "Storekeepers", "Load Planners", "Security Officers", "Maintenance Instructors", "Fleet Tech Instructors", "Technicians"]] + \
             [{"ticker": "UAL", "unit": "United Ground Express, Inc.", "group": "Passenger Service", "union": "IAM"}]
    aliases = json.load(open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config", "labor_names.json")))
    site = {"name": "IAM District 141", "carriers": ["UAL"], "unions": ["IAM"]}
    tests = [
        {"date": "2026-06-26", "title": "Ratification.Results.JUN2026", "url": "u1",
         "text": "Voting has now concluded. Fleet Technical Instructors & Related, Maintenance Instructors, Security Officers, and Storekeepers ratified their tentative agreements. Fleet Service, Passenger Service, and Central Load Planners did not ratify their tentative agreements and remain under their current contracts while negotiations continue."},
        {"date": "2026-08-19", "title": "Storekeeper Employees Agreement 2026-2031 Ratified", "url": "u2", "text": ""},
        {"date": "2026-08-25", "title": "August 25 UGE Negotiations Update", "url": "u3", "text": "The UGE Passenger Service negotiating committee met with the Company this week."},
    ]
    for t in tests:
        for k, e in events_for(t, groups, site, aliases):
            print(t["title"][:30], "|", k[1], "|", k[2], "->", e["status"], "|", e["quote"][:80])
