"""Read union websites for bargaining milestones, group by group.

For each site in config/labor_sources.json the job reads either the news listing page(s) named in "list"
(articles linked from it whose address matches "link"), or tries, in order:
  (a site with "craft" — and optionally "unit" — is a negotiations site for one group, e.g. a pilots'
   "Contract 2026" site: every milestone sentence on it counts for that group)
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
AIRLINE_WORDS = re.compile(r"airline|air\b|airways|aviation|flight|pilot|mechanic|technician|\bAMT|dispatch|ramp|fleet|passenger|agent|"
                           r"stores|cabin|crew|American|Envoy|Piedmont|PSA|United|Delta|Southwest|SWA|Alaska|Hawaiian|Horizon|Frontier|JetBlue", re.I)
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
    (r"^(mechanic|technician)", ["mechanic"]),
    (r"^(mechanic|technician)", ["amt"]),
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


MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
DATE_RE = re.compile(r"\b(?:(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.? (\d{1,2}),? (20\d\d)"
                     r"|(\d{1,2})/(\d{1,2})/(20\d\d)|(20\d\d)-(\d\d)-(\d\d))\b", re.I)


def find_date(s):
    m = DATE_RE.search(s or "")
    if not m:
        return None
    try:
        if m.group(1):
            return dt.date(int(m.group(3)), MONTHS[m.group(1)[:3].lower()], int(m.group(2))).isoformat()
        if m.group(4):
            return dt.date(int(m.group(6)), int(m.group(4)), int(m.group(5))).isoformat()
        return dt.date(int(m.group(7)), int(m.group(8)), int(m.group(9))).isoformat()
    except ValueError:
        return None


def page_date(raw, body=None):
    m = re.search(r'(?:article:published_time|datePublished|dc\.date|"date")["\']?\s*(?:content=|:)\s*["\'](20\d\d-\d\d-\d\d)', raw)
    return m.group(1) if m else find_date((body or clean(raw))[:3000])


def html_list(f, site, since):
    """News listing pages: links matching site["link"], dated from the text beside each link."""
    items, tried, seen = [], [], {}
    for url in site["list"]:
        try:
            data, _ = f.get(url, "text/html")
        except Exception as e:  # noqa: BLE001
            tried.append("list: " + _err(e))
            continue
        raw = data.decode("utf-8", "replace")
        for m in re.finditer(r'<a\b[^>]*?href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', raw, re.S | re.I):
            href = urllib.parse.urljoin(url, html.unescape(m.group(1)))
            if not re.search(site["link"], href) or href.rstrip("/") == url.rstrip("/"):
                continue
            title = clean(m.group(2))
            after = clean(raw[m.end():m.end() + 400])
            before = clean(raw[max(0, m.start() - 400):m.start()])
            nxt = re.search(r'<a\b[^>]*href=["\'](?!' + re.escape(m.group(1)) + ')', raw[m.end():m.end() + 400], re.I)
            if nxt:
                after = clean(raw[m.end():m.end() + nxt.start()])  # don't borrow the next item's date
            last_before = [x.group(0) for x in DATE_RE.finditer(before)][-1:]
            near = after if DATE_RE.search(after) else " ".join(last_before)
            cur = seen.get(href)
            if cur is None:
                cur = seen[href] = {"date": None, "title": "", "url": href, "kind": "page"}
                items.append(cur)
            if len(title) > len(cur["title"]) and not re.fullmatch(r"(?i)read more|more|continue reading|learn more", title):
                cur["title"] = title
            if not cur["date"]:
                cur["date"] = find_date(near)
    items = [i for i in items if len(i["title"]) >= 12 and (i["date"] is None or i["date"] >= since)]
    return ("html list" if items else None), items, tried or (["list: no news links found"] if not items else [])


def list_items(f, site, since):
    """Return (method, [{date, title, url, kind, text?}])"""
    if site.get("list"):
        return html_list(f, site, since)
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


def carrier_counts(text, site, aliases):
    """How often the document names each airline this site covers (case-sensitive, so "united front" is not United).
    Mainline counts exclude the subsidiary names that contain them ("United Ground Express")."""
    out = {}
    for tk in site.get("carriers", []):
        subs = [(k, a["match"]) for k, a in aliases.items() if a.get("parent") == tk and a.get("match")]
        stripped = text
        for k, m in subs:
            out[k] = len(re.findall(m, text))
            stripped = re.sub(m, " ", stripped)
        m = site.get("names", {}).get(tk)
        rx = aliases.get(tk, {}).get("match", tk) + ("|" + m if m else "")
        out[tk] = len(re.findall(rx, stripped))
    return out


def doc_owner(counts):
    best = sorted(counts.items(), key=lambda kv: -kv[1])
    if not best or best[0][1] == 0 or (len(best) > 1 and best[1][1] == best[0][1]):
        return None
    return best[0][0]


def events_for(item, groups, site, aliases):
    """groups: tracked rows this site may speak for. Returns [(row_key, event)].
    A clause that names any airline only counts for the airlines it names; a clause that names none
    counts for the airline named in the item's title (or the site's only airline)."""
    title = item["title"]
    body = item.get("text") or ""
    counts = item.get("counts") or carrier_counts(title + ". " + body, site, aliases)
    if CONTRACT_DOC_RE.search(title) and item.get("kind") == "pdf":
        body = ""  # a contract's own text is full of "mediation" and "Railway Labor Act"; only its title is news
    text = title + ". " + body
    if not ANNOUNCE_RE.search(text):
        return []
    owner = doc_owner(counts)
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
        own_key = unit if is_sub else g["ticker"]

        def names_own(t):
            if not re.search(own_rx, t, re.I):
                return False
            if not is_sub and subs_rx:
                # "United Ground Express" also contains "United": mainline only if the parent is named outside the subsidiary's name
                stripped = re.sub(subs_rx, " ", t, flags=re.I)
                return bool(re.search(own_rx, stripped, re.I))
            return True

        toks = group_alternates(g["group"])
        dedicated = bool(site.get("craft"))  # a negotiations site for one group: every milestone sentence is about it
        best = None
        for clause in CLAUSE_SPLIT.split(text):
            if len(clause) > 900 or not (dedicated or clause_names_group(clause, toks)):
                continue
            if re.match(r"\s*(In the news|In the media|Related|See also|Read more)\b", clause, re.I):
                continue  # a list of other outlets' headlines, not this union's own report
            st = status_of(clause)
            if not st:
                continue
            if dedicated:
                ok = not re.search(OTHER_CARRIERS, clause, re.I) or names_own(clause)
            elif re.search(OTHER_CARRIERS, title, re.I) and not names_own(title):
                # the item is about another airline (often a sister carrier: "Horizon AFA ..."), even where a
                # sentence mentions this one ("... as part of Alaska Air Group")
                ok = False
            elif re.search(OTHER_CARRIERS, clause, re.I):
                ok = names_own(clause)
            else:
                # no airline in the sentence: go by the title, else by which airline the document is about
                ok = names_own(title) or (not re.search(OTHER_CARRIERS, title, re.I) and owner == own_key) \
                    or (single and not (subs_rx and not is_sub and re.search(subs_rx, title, re.I)) and (not is_sub or re.search(own_rx, text, re.I)))
            if ok:
                # a routine "Negotiations Update" headline shouldn't hide the TA or vote result reported below it
                rank = [s for s, _ in STATUS_RULES].index(st)
                if best is None or rank < best[2]:
                    best = (st, clause.strip(), rank)
                if rank <= 2:
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
               and any(re.search(u, g.get("union") or "", re.I) for u in site.get("unions", [".*"]))
               and (not site.get("craft") or re.search(site["craft"], g["group"], re.I))
               and (not site.get("unit") or re.search(site["unit"], g.get("unit") or "Mainline", re.I))]
        if time.time() - t0 > budget_s:
            # out of time this run: re-match what earlier runs read from this site
            for k, v in cache.items():
                if v.get("site") == site["name"] and "text" in v:
                    match({"url": k, "date": v["date"], "title": v.get("title", ""), "text": v["text"],
                           "kind": v.get("kind"), "counts": v.get("counts")}, rel, site)
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
        for it in sorted(items, key=lambda x: x["date"] or "9999", reverse=True):
            key = it["url"]
            c = cache.get(key)
            if it["kind"] == "page":
                if c and "text" in c and "counts" in c:
                    it["text"], it["counts"], it["date"] = c["text"], c["counts"], c["date"]
                elif c and (c.get("skipped") or c.get("error")):
                    continue
                else:
                    if not (ANNOUNCE_RE.search(it["title"]) and (len(site["carriers"]) == 1 or AIRLINE_WORDS.search(it["title"]))):
                        cache[key] = {"date": it["date"] or dt.date.today().isoformat(), "skipped": True}
                        continue
                    if nread >= 25 or time.time() - t0 > budget_s:
                        continue
                    try:
                        data, _ = f.get(key, "text/html", limit=3_000_000)
                        raw = data.decode("utf-8", "replace")
                        body = re.search(r"(?is)<(article|main)\b.*?</\1>", raw)
                        it["text"] = clean(body.group(0) if body else raw)[:30000]
                        it["date"] = page_date(raw, it["text"] if body else None) or it["date"]  # the article's own date beats the listing's layout
                        nread += 1
                        time.sleep(0.5)
                    except Exception as e:  # noqa: BLE001
                        cache[key] = {"date": it["date"] or dt.date.today().isoformat(), "error": type(e).__name__}
                        continue
                    if not it["date"] or it["date"] < since:
                        cache[key] = {"date": it["date"] or dt.date.today().isoformat(), "skipped": True}
                        continue
            elif it["kind"] == "pdf":
                if c and "text" in c and "counts" in c:
                    it["text"], it["counts"] = c["text"], c["counts"]
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
            if "counts" not in it:
                it["counts"] = carrier_counts(it["title"] + ". " + (it.get("text") or ""), site, aliases)
            cache[key] = {"date": it["date"], "title": it["title"][:200], "site": site["name"], "kind": it["kind"],
                          "counts": it["counts"], "text": status_text(it.get("text"))}
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
