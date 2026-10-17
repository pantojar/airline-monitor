"""CEO pay ratio, median employee pay and CEO total compensation from proxy statements (DEF 14A).

Every SEC-listed company must disclose, in its annual proxy, the median employee's annual total
compensation, the CEO's annual total compensation, and the ratio between them (Item 402(u)).
The disclosure is prose, not tagged data, so this module finds the ratio sentence ("... 190 to 1",
"120:1") and reads the two dollar amounts around it, keeping only a CEO figure that reproduces the
stated ratio within 10%.

Compensation actually paid (Item 402(v)) is read from config/exec_pay.json, entered by hand, because
pay-versus-performance table layouts differ whenever a company had two CEOs in a year.
"""
import datetime as dt
import html
import json
import os
import re
import time
import urllib.request

RATIO_RE = re.compile(r"(\d[\d,]*(?:\.\d+)?)\s?(?:to|:|-to-)\s?1\b", re.I)
MONEY_RE = re.compile(r"\$\s?(\d{1,3}(?:,\d{3})+|\d{4,})(?:\.\d+)?(?!\s*(?:million|billion|thousand))", re.I)


def html_to_text(raw):
    raw = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", raw)
    raw = re.sub(r"(?i)<(br|/p|/div|/td|/th|/tr|/li)[^>]*>", " ", raw)
    raw = re.sub(r"<[^>]+>", "", raw)
    raw = html.unescape(raw).replace(" ", " ").replace("​", " ")
    return re.sub(r"\s+", " ", raw)


def parse_pay_ratio(text):
    """Return {ratio, median, ceo_total} or None."""
    for m in RATIO_RE.finditer(text):
        i = m.start()
        ctx_before = text[max(0, i - 1600):i]
        if not re.search(r"median", ctx_before, re.I) or not re.search(r"ratio|compensation", ctx_before, re.I):
            continue
        try:
            ratio = float(m.group(1).replace(",", ""))
        except ValueError:
            continue
        if not 2 <= ratio <= 3000:
            continue
        lo = max(0, i - 1600)
        ctx = text[lo:i + 1200]
        amounts = [(lo + a.start(), float(a.group(1).replace(",", ""))) for a in MONEY_RE.finditer(ctx)]
        meds = []
        for pos, v in amounts:
            if 15_000 <= v <= 400_000:
                before = text[max(0, pos - 320):pos]
                k = before.lower().rfind("median")
                if k >= 0:
                    meds.append((len(before) - k, v))
        if not meds:
            continue
        median = sorted(meds)[0][1]
        best = None
        for pos, v in amounts:
            if v < 300_000:
                continue
            err = abs(v / median - ratio) / ratio
            if err <= 0.10 and (best is None or err < best[0]):
                best = (err, v)
        if best is None:
            continue
        return {"ratio": ratio, "median": round(median), "ceo_total": round(best[1])}
    return None


def _get(url, ua):
    req = urllib.request.Request(url, headers={"User-Agent": ua, "Accept-Encoding": "identity"})
    with urllib.request.urlopen(req, timeout=90) as r:
        return r.read()


def update_exec_pay(root, meta, ua, carriers, force=False, per_company=2):
    """Parse each company's latest proxies (cached by accession) into data/exec_pay.json."""
    data_dir = os.path.join(root, "data")
    out_path = os.path.join(data_dir, "exec_pay.json")
    try:
        existing = json.load(open(out_path, encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        existing = []
    have = {r["accession"]: r for r in existing if r.get("accession")}
    manual = {(r["ticker"], r["fy"]): r for r in json.load(open(os.path.join(root, "config", "exec_pay.json"), encoding="utf-8"))}
    prev = meta.get("exec_pay", {})
    if not force and prev.get("at"):
        try:
            age = dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(prev["at"].replace("Z", "+00:00"))
            if age < dt.timedelta(days=6):
                return False
        except ValueError:
            pass
    rows, errors = [], []
    for c in carriers:
        t, cik = c["ticker"], int(c["cik"])
        try:
            subs = json.loads(_get(f"https://data.sec.gov/submissions/CIK{cik:010d}.json", ua))
            time.sleep(0.3)
            r = subs["filings"]["recent"]
            idx = [i for i, f in enumerate(r["form"]) if f == "DEF 14A"][:per_company]
        except Exception as e:  # noqa: BLE001
            errors.append(f"{t}: submissions {type(e).__name__}")
            rows += [x for x in existing if x.get("ticker") == t]
            continue
        for i in idx:
            acc = r["accessionNumber"][i]
            filed = r["filingDate"][i]
            url = f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc.replace('-', '')}/{r['primaryDocument'][i]}"
            if acc in have and have[acc].get("ratio") is not None:
                rows.append(have[acc])
                continue
            rec = {"ticker": t, "fy": int(filed[:4]) - 1, "filed": filed, "accession": acc, "url": url}
            try:
                parsed = parse_pay_ratio(html_to_text(_get(url, ua).decode("utf-8", "replace")))
                time.sleep(0.5)
                rec.update(parsed or {"ratio": None, "median": None, "ceo_total": None})
                rec["source"] = "auto" if parsed else "not found"
            except Exception as e:  # noqa: BLE001
                rec.update({"ratio": None, "median": None, "ceo_total": None, "source": f"error: {type(e).__name__}"})
                errors.append(f"{t} {filed}: {type(e).__name__}")
            rows.append(rec)
    # hand-verified values (names, compensation actually paid) take precedence
    by = {(r["ticker"], r["fy"]): r for r in rows}
    for k, m in manual.items():
        r = by.setdefault(k, {"ticker": k[0], "fy": k[1]})
        for f, v in m.items():
            if v is not None and (f not in ("ratio", "median", "ceo_total") or r.get(f) is None):
                r[f] = v
        if r.get("source") == "auto" and all(m.get(f) is None or abs(m[f] - r.get(f, m[f])) <= max(1, 0.01 * abs(m[f])) for f in ("ratio", "median", "ceo_total")):
            r["source"] = "auto, verified"
    out = sorted(by.values(), key=lambda r: (r["ticker"], -r["fy"]))
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=1, ensure_ascii=False)
    meta["exec_pay"] = {"at": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
                        "rows": len(out), "errors": errors}
    return True


if __name__ == "__main__":
    import sys
    for p in sys.argv[1:]:
        print(os.path.basename(p), parse_pay_ratio(open(p, encoding="utf-8").read()))
