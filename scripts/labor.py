"""Union contracts and bargaining status, built only from automatic sources.

1. 10-K union tables. Each carrier's annual report lists its represented groups: union, headcount
   and the date the agreement becomes amendable under the Railway Labor Act. The job reads those
   tables from the latest 10-K (re-read only when a new 10-K is filed). Where a company describes
   its contracts in prose instead of a table, the relevant sentences are quoted as-is.
2. Company disclosures. Sentences in the latest 10-Q that mention ratification, tentative
   agreements, mediation or negotiations are quoted with a link.
3. News. For each group that is past, or within 18 months of, its amendable date, a news search
   runs weekly. The most recent headline that names the carrier, the craft and a bargaining
   milestone sets the group's status, and the headline is shown with its link.
"""
import datetime as dt
import email.utils
import html
import json
import os
import re
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

UNION_RE = re.compile(r"Association|Brotherhood|Teamsters|Union|Fraternal|\bALPA\b|\bAFA\b|\bTWU\b|\bIAM\b|\bIBT\b|\bAMFA\b|\bCWA\b|\bAPFA\b|\bSWAPA\b|\bPAFCA\b")
AMEND_HDR_RE = re.compile(r"amendable|open for amendment|contract status|status of agreement", re.I)
MONTHS = {m: i for i, m in enumerate(["january", "february", "march", "april", "may", "june", "july", "august",
                                      "september", "october", "november", "december"], 1)}


def clean(s):
    s = re.sub(r"<[^>]+>", " ", s)
    s = html.unescape(s).replace(" ", " ").replace("​", " ")
    return re.sub(r"\s+", " ", s).strip()


def strip_marks(s):
    """Remove footnote markers like (1), (a), (b) and return (text, markers)."""
    marks = re.findall(r"\((\d{1,2}|[a-z])\)", s)
    return re.sub(r"\s*\((?:\d{1,2}|[a-z])\)", "", s).strip(), marks


def parse_amendable(s):
    """'Amendable 3/2/2027' -> ('2027-03-02','day'); 'August 2021' -> ('2021-08','month'); '2027' -> ('2027','year')."""
    t = s.lower()
    m = re.search(r"(\d{1,2})/(\d{1,2})/(\d{4})", t)
    if m:
        return f"{m.group(3)}-{int(m.group(1)):02d}-{int(m.group(2)):02d}", "day"
    m = re.search(r"(" + "|".join(MONTHS) + r")\s+(\d{1,2}),\s*(\d{4})", t)
    if m:
        return f"{m.group(3)}-{MONTHS[m.group(1)]:02d}-{int(m.group(2)):02d}", "day"
    m = re.search(r"(" + "|".join(MONTHS) + r")\s+(\d{4})", t)
    if m:
        return f"{m.group(2)}-{MONTHS[m.group(1)]:02d}", "month"
    m = re.search(r"\b(19|20)\d{2}\b", t)
    if m:
        return m.group(0), "year"
    return None, None


def tables(doc):
    for m in re.finditer(r"(?is)<table.*?</table>", doc):
        rows = []
        for tr in re.findall(r"(?is)<tr.*?</tr>", m.group(0)):
            cells = [clean(c) for c in re.findall(r"(?is)<t[dh][^>]*>(.*?)</t[dh]>", tr)]
            cells = [c for c in cells if c and c not in ("$", "%")]
            if cells:
                rows.append(cells)
        yield m.start(), m.end(), rows


def _col(header, *pats):
    for i, h in enumerate(header):
        if any(re.search(p, h, re.I) for p in pats):
            return i
    return None


def parse_union_tables(doc):
    out, seen = [], set()
    for start, end, rows in tables(doc):
        flat = " ".join(" ".join(r) for r in rows)
        if not (AMEND_HDR_RE.search(flat) and UNION_RE.search(flat)):
            continue
        hi = next((i for i, r in enumerate(rows) if any(AMEND_HDR_RE.search(c) for c in r)), None)
        if hi is None:
            continue
        header = rows[hi]
        c_amend = _col(header, r"amendable|amendment|status")
        c_union = _col(header, r"^union$|union\b|representative")
        c_group = _col(header, r"group|class|craft")
        c_emp = _col(header, r"employees|number|crewmembers")
        if c_amend is None or c_group is None:
            continue
        before = clean(doc[max(0, start - 2500):start])
        units = re.findall(r"([A-Z][A-Za-z.,&\- ]{1,40}?)(?:['’]s|(?<=s)['’])\s+union contract", before)
        unit = re.split(r"\.\s", units[-1])[-1].strip() if units else None
        after = clean(re.split(r"(?i)<table", doc[end:end + 6000])[0])[:1500]
        after = re.split(r"\s(?:The RLA governs|Under the RLA|The Railway Labor Act)", after)[0]
        notes = {k: v.strip() for k, v in re.findall(r"\((\d{1,2}|[a-z])\)\s+(.+?)(?=\s\((?:\d{1,2}|[a-z])\)\s|$)", after)}
        for r in rows[hi + 1:]:
            if len(r) == 1 and r[0].endswith(":"):
                unit = r[0].rstrip(":").strip()
                continue
            if len(r) < len(header) - 1:
                continue
            # rows can be shorter than the header when a cell is blank; align from the left
            def cell(i):
                return r[i] if i is not None and i < len(r) else ""
            group, gm = strip_marks(cell(c_group))
            union, um = strip_marks(cell(c_union))
            amend_raw, am = strip_marks(cell(c_amend))
            emp = re.sub(r"[^\d]", "", strip_marks(cell(c_emp))[0]) if c_emp is not None else ""
            if not group or not amend_raw or group.lower().startswith(("total", "percentage")):
                continue
            date, prec = parse_amendable(amend_raw)
            key = (unit, group, union, amend_raw)
            if key in seen:
                continue
            seen.add(key)
            marks = gm + um + am
            out.append({"unit": unit, "group": group, "union": union, "employees": int(emp) if emp else None,
                        "amendable_text": amend_raw, "amendable": date, "precision": prec,
                        "footnote": " ".join(notes[k] for k in marks if k in notes)[:600] or None})
    return out


SENT_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z(])")


BOILER_RE = re.compile(r"under the RLA|must notify|self-help|if either party|after receipt of such notice|cooling[- ]off period that must|could (adversely|materially)|may be unable|there can be no assurance|risk|forward-looking|prospects of|vested|401\(k\)|non-elective", re.I)


def tidy(sentences):
    """Drop table fragments and duplicates from quoted filing text."""
    out = []
    for s in sentences or []:
        if len(re.findall(r"\b\d[\d,.]*\b", s)) > 8 or BOILER_RE.search(s):
            continue
        if not any(s in o or o in s for o in out):
            out.append(s)
    return out


SUBSIDIARY_WORDS = r"Envoy|PSA|Piedmont|Endeavor|Horizon|Hawaiian|McGee|United Ground Express|Mesa"
MONTH_DATE_RE = re.compile(r"\b(?:in|on)\s+(January|February|March|April|May|June|July|August|September|October|November|December)(?:\s+(\d{1,2}),)?\s+(\d{4})", re.I)


def ratifications(sentences):
    """[(craft, unit_word or None, 'YYYY-MM', sentence)] for 10-Q sentences reporting a ratified agreement."""
    out = []
    for s in sentences or []:
        if not re.search(r"ratif", s, re.I) or re.search(r"tentative agreement (?:was|were) rejected|failed to ratify|did not ratify", s, re.I):
            continue
        m = MONTH_DATE_RE.search(s)
        if not m:
            continue
        ym = f"{m.group(3)}-{MONTHS[m.group(1).lower()]:02d}"
        unit = re.search(SUBSIDIARY_WORDS, s)
        for craft, rx, _ in CRAFTS:
            if re.search(rx, s, re.I):
                out.append((craft, unit.group(0) if unit else None, ym, s))
    return out
CRAFT_WORD_RE = re.compile(r"pilot|flight attendant|mechanic|technician|union|collective bargaining|CBA|employee group|dispatcher|fleet service|agents|crew|labor association", re.I)


def labor_sentences(doc, pattern, limit=6):
    """Sentences around each match of `pattern` that talk about represented employees, minus boilerplate."""
    text = clean(re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", doc))
    hits, spans = [], []
    for m in re.finditer(pattern, text, re.I):
        a = max(text.rfind(". ", 0, m.start()) + 2, m.start() - 350, 0)
        b = text.find(". ", m.end())
        b = len(text) if b < 0 else min(b + 1, m.end() + 400)
        snip = text[a:b].strip()
        spans.append((a, b))
        if len(snip) < 40 or BOILER_RE.search(snip) or not CRAFT_WORD_RE.search(snip):
            continue
        if snip[0].islower():
            snip = "…" + snip
        if not any(snip in h or h in snip for h in hits):
            hits.append(snip)
        if len(hits) >= limit:
            break
    return hits


# ---------------------------------------------------------------- news
CRAFTS = [
    ("pilots", r"pilot", '(pilots)'),
    ("flight attendants", r"flight attendant|inflight|cabin crew", '("flight attendants")'),
    ("mechanics", r"mechanic|technician|maintenance(?! (control|instructor|planner))", '(mechanics OR technicians)'),
    ("fleet service", r"fleet|ramp|provisioning|freight", '("fleet service" OR ramp)'),
    ("passenger service", r"passenger service|customer service|agents|clerical", '("customer service" OR "passenger service" OR agents)'),
    ("dispatchers", r"dispatch|flight superintendent", '(dispatchers)'),
]
STATUS_RULES = [  # (status, pattern) checked in order on a headline
    ("Contract rejected", r"\breject(?:s|ed|ing)?\s+(?:(?:the|a|their|its|new|proposed|latest|second|final|\S+-backed)\s+)*(?:tentative|TA\b|T/A\b|contract|agreement|deal|CBA)|\b(?:TA|T/A|tentative agreement|contract|agreement|deal)s?\s+(?:is\s+|was\s+|were\s+|has been\s+)?(?:overwhelmingly\s+)?rejected|\brejection of (?:the |a )?(?:tentative|TA\b|contract|agreement)"),
    ("Ratified", r"ratif(y|ied|ies|ication)|approve[sd]? (new )?(contract|deal|agreement)"),
    ("Tentative agreement", r"tentative (agreement|deal)|agreement in principle|reach(es|ed)? (a )?(deal|agreement)|\bTA\b"),
    ("Strike vote", r"strike (vote|authori[sz]ation)|vote[sd]? to (authori[sz]e|strike)"),
    ("Seeking release", r"request(s|ed|ing)? (a )?release|release from (the )?(NMB|mediation)|cooling[- ]off|proffer"),
    ("In mediation", r"mediat|\bNMB\b|national mediation board"),
    ("Picketing", r"\bpicket|\brall(y|ies)\b|\bprotest"),
    ("In negotiations", r"negotiat|\btalks\b|bargain"),
]
UNIT_NAMES = {"Mainline": None}


def status_from_footnote(footnote, union):
    """Apply the status rules to a 10-K footnote, sentence by sentence. A sentence that names unions
    must name this row's union for its status to apply."""
    if not footnote:
        return None
    acr = re.findall(r"\(\s*[“\"]?([A-Z][A-Z\-]{1,9})[”\"]?\s*\)", union or "") or ([union] if union and len(union) <= 8 else [])
    for status, rx in STATUS_RULES:
        for sent in SENT_RE.split(footnote):
            if BOILER_RE.search(sent) or not re.search(rx, sent, re.I):
                continue
            named = re.findall(r"\b(ALPA|AFA|TWU|IBT|IAM|AMFA|CWA|APFA|PAFCA|SWAPA|APA)\b", sent)
            if named and not any(a.split("-")[0] in named for a in acr):
                continue
            return status
    return None


def craft_of(group):
    for name, rx, q in CRAFTS:
        if re.search(rx, group, re.I):
            return name, rx, q
    return None, None, None


def news_search(query, ua, days=365):
    url = "https://news.google.com/rss/search?" + urllib.parse.urlencode(
        {"q": f"{query} when:{days}d", "hl": "en-US", "gl": "US", "ceid": "US:en"})
    req = urllib.request.Request(url, headers={"User-Agent": ua})
    with urllib.request.urlopen(req, timeout=60) as r:
        root = ET.fromstring(r.read())
    out = []
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        src_el = it.find("source")
        src = (src_el.text or "").strip() if src_el is not None else ""
        if src and title.endswith(" - " + src):
            title = title[: -len(" - " + src)].strip()
        try:
            date = email.utils.parsedate_to_datetime(it.findtext("pubDate")).date().isoformat()
        except Exception:  # noqa: BLE001
            continue
        link = (it.findtext("link") or "").strip()
        if title and link.startswith("https://"):
            out.append({"title": title, "date": date, "source": src, "url": link})
    return out


def status_from_news(items, carrier_rx, craft_rx):
    """Most recent headline naming the carrier, the craft and a milestone -> (status, item)."""
    for it in sorted(items, key=lambda x: x["date"], reverse=True):
        t = it["title"]
        if not re.search(carrier_rx, t, re.I) or not re.search(craft_rx, t, re.I):
            continue
        for status, rx in STATUS_RULES:
            if re.search(rx, t, re.I):
                return status, it
    return None, None


# ---------------------------------------------------------------- job
def _get(url, ua):
    req = urllib.request.Request(url, headers={"User-Agent": ua, "Accept-Encoding": "identity"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return r.read().decode("utf-8", "replace")


def latest_filing(subs, form):
    r = subs["filings"]["recent"]
    for i, f in enumerate(r["form"]):
        if f == form:
            return {"accession": r["accessionNumber"][i], "filed": r["filingDate"][i], "doc": r["primaryDocument"][i]}
    return None


def update_labor(root, meta, ua, carriers, force=False):
    data_dir = os.path.join(root, "data")
    path = os.path.join(data_dir, "labor.json")
    try:
        prev = json.load(open(path, encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        prev = {"companies": {}, "groups": [], "news": {}}
    pm = meta.get("labor", {})
    if not force and pm.get("at"):
        try:
            age = dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(pm["at"].replace("Z", "+00:00"))
            if age < dt.timedelta(days=6) and not pm.get("errors"):
                return False
        except ValueError:
            pass
    aliases = json.load(open(os.path.join(root, "config", "labor_names.json"), encoding="utf-8"))
    companies, errors = {}, []
    for c in carriers:
        t, cik = c["ticker"], int(c["cik"])
        old = prev["companies"].get(t, {})
        try:
            subs = json.loads(_get(f"https://data.sec.gov/submissions/CIK{cik:010d}.json", ua))
            time.sleep(0.3)
        except Exception as e:  # noqa: BLE001
            errors.append(f"{t}: submissions {type(e).__name__}")
            if old:
                companies[t] = old
            continue
        rec = {"ticker": t}
        k = latest_filing(subs, "10-K")
        if k:
            url = f"https://www.sec.gov/Archives/edgar/data/{cik}/{k['accession'].replace('-', '')}/{k['doc']}"
            if old.get("tenk", {}).get("accession") == k["accession"]:
                rec["tenk"] = old["tenk"]
            else:
                try:
                    doc = _get(url, ua)
                    time.sleep(0.5)
                    groups = parse_union_tables(doc)
                    prose = [] if groups else labor_sentences(doc, r"amendable|collective bargaining agreement", 8)
                    rec["tenk"] = {"accession": k["accession"], "filed": k["filed"], "url": url, "groups": groups, "prose": prose}
                except Exception as e:  # noqa: BLE001
                    errors.append(f"{t}: 10-K {type(e).__name__}")
                    if old.get("tenk"):
                        rec["tenk"] = old["tenk"]
        q = latest_filing(subs, "10-Q")
        if q:
            url = f"https://www.sec.gov/Archives/edgar/data/{cik}/{q['accession'].replace('-', '')}/{q['doc']}"
            if old.get("tenq", {}).get("accession") == q["accession"]:
                rec["tenq"] = old["tenq"]
            else:
                try:
                    doc = _get(url, ua)
                    time.sleep(0.5)
                    rec["tenq"] = {"accession": q["accession"], "filed": q["filed"], "url": url,
                                   "sentences": tidy(labor_sentences(doc, r"ratif|tentative agreement|mediat|national mediation board|negotiat|amendable|represented by|voted", 10))[:6]}
                except Exception as e:  # noqa: BLE001
                    errors.append(f"{t}: 10-Q {type(e).__name__}")
                    if old.get("tenq"):
                        rec["tenq"] = old["tenq"]
        companies[t] = rec

    for rec in companies.values():
        if rec.get("tenq"):
            rec["tenq"]["sentences"] = tidy(rec["tenq"].get("sentences"))

    # one row per group, with news-based status
    today = dt.date.today()
    horizon = (today + dt.timedelta(days=548)).isoformat()
    groups, news_cache = [], {}
    try:
        manual = json.load(open(os.path.join(root, "config", "labor_groups_manual.json"), encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        manual = []
    for t, rec in companies.items():
        for g in (rec.get("tenk") or {}).get("groups", []):
            unit = g.get("unit")
            group = g["group"]
            if unit in (None, "Mainline"):
                # "Endeavor Pilots", "Southwest Flight Attendants": the airline is in the group name
                for k, a in aliases.items():
                    if a.get("prefix") and group.startswith(a["prefix"] + " "):
                        unit, group = (k if k not in (t,) else unit), group[len(a["prefix"]) + 1:]
                        break
            carrier = aliases[unit] if unit not in (None, "Mainline") and unit in aliases else aliases.get(t, {"name": unit or t})
            row = dict(g, ticker=t, unit=unit, group=group[:1].upper() + group[1:], group_raw=g["group"], carrier=carrier["name"])
            amend = g.get("amendable") or ""
            cut = today.isoformat()[:len(amend)] if amend else ""
            row["open"] = bool(amend) and (amend < cut if g.get("precision") != "year" else amend < str(today.year))
            soon = bool(amend) and not row["open"] and amend[:7] <= (today + dt.timedelta(days=365)).isoformat()[:len(amend[:7])]
            craft, craft_rx, craft_q = craft_of(group)
            row["craft"] = craft
            row["status"] = "Past amendable date" if row["open"] else ("Amendable within a year" if soon else ("Contract in effect" if amend else "Not stated"))
            row["status_source"] = "10-K"
            fs = status_from_footnote(g.get("footnote"), g.get("union"))
            if fs and fs != "Ratified":
                row["status"], row["status_date"] = fs, (rec.get("tenk") or {}).get("filed")
            if craft and carrier.get("query") and (row["open"] or soon or (amend and amend <= horizon)):
                key = (carrier["query"], craft)
                if key not in news_cache:
                    try:
                        news_cache[key] = news_search(f'{carrier["query"]} {craft_q} (union OR contract OR negotiations OR mediation OR ratify OR "tentative agreement" OR strike)', ua)
                        time.sleep(1.2)
                    except Exception as e:  # noqa: BLE001
                        errors.append(f"news {carrier['name']} {craft}: {type(e).__name__}")
                        news_cache[key] = None
                items = news_cache[key]
                if items is None:  # keep last week's result on a failed search
                    pr = next((p for p in prev.get("groups", []) if p.get("ticker") == t and p.get("group") == g["group"] and p.get("unit") == g.get("unit")), None)
                    if pr and pr.get("status_source") == "news":
                        for f in ("status", "status_source", "news"):
                            row[f] = pr.get(f)
                else:
                    st, it = status_from_news(items, carrier["match"], craft_rx)
                    if st and it["date"] >= (row.get("status_date") or ""):
                        row["status"], row["status_source"], row["news"], row["status_date"] = st, "news", it, it["date"]
            # the company's own 10-Q report of a ratification wins unless a later headline says otherwise
            tq = rec.get("tenq") or {}
            unit_word = None if unit in (None, "Mainline", "Alaska", "United Airlines, Inc.") else unit.split()[0]
            for craft_r, uw, ym, sent in ratifications(tq.get("sentences")):
                if craft_r != craft or (uw or None) != (unit_word if unit_word in re.findall(SUBSIDIARY_WORDS, sent) else None):
                    continue
                news_date = (row.get("news") or {}).get("date", "")
                if not news_date or ym >= news_date[:7] or row["status"] in ("In negotiations", "Picketing"):
                    row["status"], row["status_source"], row["status_date"], row["filing_quote"] = "Ratified", "10-Q", ym, sent
                    row["filing_url"] = tq.get("url")
                break
            if row.get("news"):
                row["status_date"] = row.get("status_date") or row["news"]["date"]
            groups.append(row)
        # companies that describe contracts in prose: one row per craft the prose mentions, status from news only
        prose = (rec.get("tenk") or {}).get("prose") or []
        if prose and not (rec.get("tenk") or {}).get("groups"):
            manual_rows = {m["craft"]: m for m in manual if m["ticker"] == t and not m.get("add") and not m.get("override")}
            carrier = aliases.get(t, {"name": t})
            text = " ".join(prose)
            for craft, craft_rx, craft_q in CRAFTS:
                if not re.search(craft_rx, text, re.I):
                    continue
                row = {"ticker": t, "unit": None, "group": craft[:1].upper() + craft[1:], "union": "", "employees": None,
                       "amendable_text": "See 10-K text", "amendable": None, "precision": None, "footnote": None,
                       "carrier": carrier["name"], "craft": craft, "open": None, "status": "See 10-K text", "status_source": "10-K", "prose": True}
                m = manual_rows.get(craft)
                if m:
                    # hand-entered from the contracts themselves (config/labor_groups_manual.json)
                    amend = m.get("amendable") or ""
                    row.update(union=m.get("union", ""), amendable=amend or None, precision={4: "year", 7: "month"}.get(len(amend), "day") if amend else None,
                               amendable_text=None if amend else "Not stated", employees=m.get("employees"), prose=False, manual=True)
                    if amend:
                        row["open"] = amend < today.isoformat()[:len(amend)]
                        soon = not row["open"] and amend[:7] <= (today + dt.timedelta(days=365)).isoformat()[:7]
                        row["status"] = "Past amendable date" if row["open"] else ("Amendable within a year" if soon else "Contract in effect")
                    else:
                        row["status"] = "Not stated"
                if m and m.get("status"):
                    row["status"], row["status_date"], row["status_source"] = m["status"], m.get("status_date"), "manual"
                    row["filing_quote"] = m.get("status_note")
                far = bool(m and m.get("amendable") and m["amendable"] > horizon)
                if carrier.get("query") and not far:  # a contract years from opening: old headlines don't set its status
                    key = (carrier["query"], craft)
                    if key not in news_cache:
                        try:
                            news_cache[key] = news_search(f'{carrier["query"]} {craft_q} (union OR contract OR negotiations OR mediation OR ratify OR "tentative agreement" OR strike)', ua)
                            time.sleep(1.2)
                        except Exception as e:  # noqa: BLE001
                            errors.append(f"news {carrier['name']} {craft}: {type(e).__name__}")
                            news_cache[key] = []
                    st, it = status_from_news(news_cache[key] or [], carrier["match"], craft_rx)
                    if st:
                        row["status"], row["status_source"], row["news"], row["status_date"] = st, "news", it, it["date"]
                tq = rec.get("tenq") or {}
                for craft_r, uw, ym, sent in ratifications(tq.get("sentences")):
                    if craft_r == craft and ym >= (row.get("status_date") or "")[:7]:
                        row["status"], row["status_source"], row["status_date"], row["filing_quote"], row["filing_url"] = "Ratified", "10-Q", ym, sent, tq.get("url")
                        break
                groups.append(row)
    # hand corrections to a 10-K group (e.g. a new contract's amendable date before the next 10-K): "override": true
    for m in manual:
        if not m.get("override"):
            continue
        for row in groups:
            if row["ticker"] != m["ticker"] or (row.get("group") or "").lower() != (m.get("group") or "").lower() or (m.get("unit") and row.get("unit") != m["unit"]):
                continue
            amend = m.get("amendable")
            if amend:
                row["amendable"], row["amendable_text"] = amend, m.get("amendable_text") or row.get("amendable_text")
                row["precision"] = {4: "year", 7: "month"}.get(len(amend), "day")
                row["open"] = amend < today.isoformat()[:len(amend)]
                if row.get("status_source") == "10-K":
                    soon = not row["open"] and amend[:7] <= (today + dt.timedelta(days=365)).isoformat()[:7]
                    row["status"] = "Past amendable date" if row["open"] else ("Amendable within a year" if soon else "Contract in effect")
            row["manual_note"] = m.get("note")
    # groups the 10-K doesn't list yet (e.g. newly organized), entered by hand with "add": true
    names = {t: (rec.get("name") or next((g["carrier"] for g in groups if g["ticker"] == t), t)) for t, rec in companies.items()}
    for m in manual:
        if not m.get("add") or any(g["ticker"] == m["ticker"] and g.get("unit") == m.get("unit") and g.get("craft") == m["craft"] and g.get("group") == m.get("group") for g in groups):
            continue
        amend = m.get("amendable") or ""
        row = {"ticker": m["ticker"], "unit": m.get("unit"), "group": m.get("group") or m["craft"].capitalize(), "group_raw": m.get("group") or m["craft"],
               "union": m.get("union", ""), "employees": m.get("employees"), "amendable_text": m.get("amendable_text") or ("Not stated" if not amend else None),
               "amendable": amend or None, "precision": {4: "year", 7: "month"}.get(len(amend), "day") if amend else None, "footnote": None,
               "carrier": m.get("carrier") or names.get(m["ticker"], m["ticker"]), "craft": m["craft"], "open": m.get("open", (amend < today.isoformat()[:len(amend)]) if amend else True),
               "status": m.get("status") or "Not stated", "status_source": "manual", "status_date": m.get("status_date"),
               "filing_quote": m.get("status_note"), "manual": True}
        if amend and not m.get("status"):
            soon = not row["open"] and amend[:7] <= (today + dt.timedelta(days=365)).isoformat()[:7]
            row["status"], row["status_source"] = ("Past amendable date" if row["open"] else ("Amendable within a year" if soon else "Contract in effect")), "10-K"
        if m.get("source_url"):
            row["news"] = {"title": m.get("source_title") or m["source_url"], "url": m["source_url"], "source": m.get("source_name", ""), "date": m.get("status_date")}
        groups.append(row)
    # union websites: dated, group-level announcements; the newest event across all sources sets the status
    import unions
    try:
        uevents, ureport = unions.collect(root, groups, ua, aliases)
    except Exception as e:  # noqa: BLE001
        uevents, ureport = {}, [{"error": f"{type(e).__name__}: {e}"[:200]}]
        errors.append("union sites: " + type(e).__name__)
    rank = {"union": 3, "10-Q": 2, "news": 1, "10-K": 0}
    for row in groups:
        evs = uevents.get((row["ticker"], row.get("unit"), row["group"])) or uevents.get((row["ticker"], row.get("unit"), row.get("group_raw", ""))) or []
        if not evs:
            continue
        row["union_events"] = evs[:4]
        e = evs[0]
        cur = (row.get("status_date") or "")[:10]
        if e["date"] >= cur[:len(e["date"])] or rank["union"] > rank.get(row.get("status_source"), 0) and e["date"][:7] >= cur[:7]:
            row["status"], row["status_source"], row["status_date"] = e["status"], "union", e["date"]
            row["union_event"] = e
    stamp_status_changes(groups, prev.get("groups", []))
    out = {"generated": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
           "companies": companies, "groups": groups}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=1, ensure_ascii=False)
    meta["labor"] = {"at": out["generated"], "groups": len(groups), "errors": errors, "union_sites": ureport}
    return True


def _amend_dt(row):
    a = row.get("amendable") or ""
    try:
        return dt.date.fromisoformat(a if len(a) == 10 else (a + "-01" if len(a) == 7 else a + "-01-01"))
    except ValueError:
        return None


def stamp_status_changes(groups, prev_groups):
    """status_changed: the date each group's current status took effect, kept from run to run.

    A status carried over unchanged keeps its earlier date. A new status takes the date of the event that set it
    (a news story, union post or filing), or for statuses that follow from the contract dates, the date the threshold
    was crossed (amendable date, or a year before it). Otherwise it is the date this run first saw the change."""
    today = dt.date.today().isoformat()
    key = lambda r: (r.get("ticker"), r.get("unit"), r.get("group"))
    old = {key(r): r for r in prev_groups}
    for row in groups:
        p = old.get(key(row))
        if p and p.get("status") == row.get("status") and p.get("status_changed"):
            row["status_changed"] = p["status_changed"]
            continue
        st, d = row.get("status"), None
        ad = _amend_dt(row)
        if st == "Past amendable date" and ad:
            d = ad.isoformat()
        elif st == "Amendable within a year" and ad:
            d = (ad - dt.timedelta(days=365)).isoformat()
        elif row.get("status_source") in ("news", "union", "manual", "10-Q") and row.get("status_date"):
            d = row["status_date"][:10]
            # union posts newest first: walk back while they report the same status, to its first report
            for e in row.get("union_events") or []:
                if e.get("status") != st:
                    break
                d = min(d, e["date"][:10])
        changed = p is not None and p.get("status") != st
        if not d and changed:
            d = today  # the status changed since the last run, with no dated event behind it
        row["status_changed"] = d if d and d <= today else (today if changed else None)


if __name__ == "__main__":
    import sys
    for p in sys.argv[1:]:
        segs = json.load(open(p))
        doc = "\n".join(s for s in segs if not s.startswith("PROSE:"))
        rows = parse_union_tables(doc)
        print("==", os.path.basename(p), len(rows))
        for r in rows:
            print("  ", r["unit"], "|", r["group"], "|", r["union"][:40], "|", r["employees"], "|", r["amendable_text"], "->", r["amendable"], r["precision"], "|", (r["footnote"] or "")[:60])
