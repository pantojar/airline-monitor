"""Share prices for the Equity Markets tab, from Tiingo (https://www.tiingo.com).

Needs the TIINGO_TOKEN repository secret. Runs on its own small workflow (.github/workflows/markets.yml)
near the market open, at noon and just after the close, Eastern time.

  daily history   /tiingo/daily/<ticker>/prices   close, dividend- and split-adjusted close, dividends
  live price      /iex/?tickers=...                latest IEX trade during market hours

data/markets.json keeps daily history from 2019 on. Only the last few weeks are re-read each run, so a
run costs one request per ticker plus one for all live prices (Tiingo's free tier allows 50 an hour).

Usage:
  python scripts/markets.py            # only acts near 9:45 a.m., 12:00 or 4:15 p.m. New York time
  python scripts/markets.py --now      # run regardless of the clock (manual runs)
"""
import datetime as dt
import json
import os
import sys
import time
import urllib.request
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
CONFIG = os.path.join(ROOT, "config")
API = "https://api.tiingo.com"
START = "2019-01-01"
BENCHMARKS = [{"ticker": "SPY", "name": "S&P 500 (SPY)"}, {"ticker": "JETS", "name": "U.S. airline ETF (JETS)"}]
TARGETS = [(9, 45), (12, 0), (16, 15)]  # New York time
NY = ZoneInfo("America/New_York")


def due(now=None):
    """GitHub schedules in UTC, so the workflow wakes for both the daylight-saving and standard-time slot;
    act only on the one that lands at (or up to 55 minutes after, GitHub runs late) a target time."""
    now = (now or dt.datetime.now(dt.timezone.utc)).astimezone(NY)
    mins = now.hour * 60 + now.minute
    return any(-5 <= mins - (h * 60 + m) < 55 for h, m in TARGETS) and now.weekday() < 5


def get(path, token):
    req = urllib.request.Request(API + path, headers={"Authorization": "Token " + token, "Content-Type": "application/json",
                                                      "User-Agent": "airline-monitor (github.com/pantojar/airline-monitor)"})
    last = None
    for i in range(3):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read())
        except Exception as e:  # noqa: BLE001
            last = e
            if getattr(e, "code", None) in (401, 403, 404):
                break
            time.sleep(5 * (i + 1))
    raise last


def load(path, default):
    try:
        return json.load(open(path, encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def main(argv):
    token = os.environ.get("TIINGO_TOKEN", "").strip()
    if not token:
        print("TIINGO_TOKEN is not set; skipping.")
        return
    if "--now" not in argv and not due():
        print("Not a scheduled market time in New York; skipping.")
        return
    carriers = [c for c in load(os.path.join(CONFIG, "carriers.json"), []) if c.get("status") == "Active"]
    names = [{"ticker": c["ticker"], "name": c["carrier"]} for c in carriers] + BENCHMARKS
    path = os.path.join(DATA, "markets.json")
    out = load(path, {})
    hist = out.get("tickers", {})
    errors = {}
    for n in names:
        t = n["ticker"]
        h = hist.get(t, {"name": n["name"], "eod": []})
        h["name"] = n["name"]
        start = START
        if h["eod"]:
            start = (dt.date.fromisoformat(h["eod"][-1][0]) - dt.timedelta(days=21)).isoformat()
        try:
            rows = get(f"/tiingo/daily/{t.lower()}/prices?startDate={start}&resampleFreq=daily", token)
            new = {r["date"][:10]: [r["date"][:10], round(r["close"], 4), round(r["adjClose"], 4), round(r.get("divCash") or 0, 4),
                                    round(r.get("splitFactor") or 1, 4)] for r in rows}
            if new:
                old = {r[0]: r for r in h["eod"]}
                # Tiingo re-bases the adjusted series after each dividend or split, so when one lands in the window
                # read the whole history again rather than mixing two bases
                if any(v[3] or v[4] != 1 for d, v in new.items() if d not in old) and start != START:
                    rows = get(f"/tiingo/daily/{t.lower()}/prices?startDate={START}&resampleFreq=daily", token)
                    old = {}
                    new = {r["date"][:10]: [r["date"][:10], round(r["close"], 4), round(r["adjClose"], 4), round(r.get("divCash") or 0, 4),
                                            round(r.get("splitFactor") or 1, 4)] for r in rows}
                old.update(new)
                h["eod"] = [old[d] for d in sorted(old)]
        except Exception as e:  # noqa: BLE001
            errors[t] = f"{type(e).__name__}: {e}"[:200]
        hist[t] = h
        time.sleep(0.5)
    try:
        live = get("/iex/?tickers=" + ",".join(n["ticker"].lower() for n in names), token)
        for r in live:
            t = (r.get("ticker") or "").upper()
            price = r.get("tngoLast") or r.get("last")
            if t in hist and price:
                hist[t]["live"] = {"price": round(price, 4), "prev_close": r.get("prevClose"), "time": r.get("timestamp")}
    except Exception as e:  # noqa: BLE001
        errors["live"] = f"{type(e).__name__}: {e}"[:200]
    out = {"at": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
           "source": "Tiingo", "tickers": hist, **({"errors": errors} if errors else {})}
    json.dump(out, open(path, "w", encoding="utf-8"), separators=(",", ":"))
    print(json.dumps({"at": out["at"], "tickers": {t: len(h["eod"]) for t, h in hist.items()}, "errors": errors}, indent=1))


if __name__ == "__main__":
    main(sys.argv[1:])
