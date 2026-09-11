"""Read-only diagnostic for the Unusual Whales API key.
Answers: does auth work; are candles real-time or delayed; is pre-market volume present;
is candle volume consolidated (SIP) or partial (Nasdaq-only) vs Yahoo; does the option chain
carry NBBO + delta; does the screener support the Gappers filters; float/short interest present.
Never prints the key. ~8 requests."""
import os, sys, json, time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

_HERE = os.path.dirname(os.path.abspath(__file__))
_BUNDLE = os.path.join(_HERE, "ca_bundle.pem")
if os.path.exists(_BUNDLE):
    for k in ("SSL_CERT_FILE", "CURL_CA_BUNDLE", "REQUESTS_CA_BUNDLE"):
        os.environ.setdefault(k, _BUNDLE)

import urllib.request, urllib.parse, ssl

NY = ZoneInfo("America/New_York")
BASE = "https://api.unusualwhales.com"


def get_key() -> str:
    k = os.environ.get("UW_API_KEY")
    if not k and sys.platform == "win32":
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as h:
                k, _ = winreg.QueryValueEx(h, "UW_API_KEY")
        except OSError:
            k = None
    if not k:
        sys.exit("UW_API_KEY not found in process env or User env. Set it and restart the terminal.")
    return k.strip()


KEY = get_key()
CTX = ssl.create_default_context(cafile=os.environ.get("SSL_CERT_FILE"))


def uw(path: str, **params):
    url = BASE + path + ("?" + urllib.parse.urlencode(params, doseq=True) if params else "")
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {KEY}", "Accept": "application/json"})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, context=CTX, timeout=30) as r:
            body = r.read().decode("utf-8")
            return r.status, json.loads(body), round(time.time() - t0, 2)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:300]
        return e.code, body, round(time.time() - t0, 2)


def hdr(t): print("\n" + "=" * 90 + "\n" + t + "\n" + "=" * 90)


now = datetime.now(NY)
print(f"now: {now:%Y-%m-%d %H:%M:%S %Z}")
T = "AAPL"

# 1. auth + stock-state -----------------------------------------------------------------
hdr("1. Auth + stock-state (prev_close, total_volume, tape_time)")
st, body, dt = uw(f"/api/stock/{T}/stock-state")
print("status", st, f"({dt}s)")
if st != 200:
    print(body); sys.exit("auth or endpoint failed - stopping")
state = body.get("data", body)
print(json.dumps(state, indent=1)[:800])
tape = state.get("tape_time")
if tape:
    tt = datetime.fromisoformat(tape.replace("Z", "+00:00")).astimezone(NY)
    print(f"tape_time {tt:%H:%M:%S} ET  -> lag vs now: {(now - tt).total_seconds()/60:.1f} min")
uw_total_vol = state.get("total_volume") or state.get("volume")

# 2. 5-min candles: freshness + premarket volume ------------------------------------------
hdr("2. 5-minute candles (freshness, market_time flags, pre-market volume)")
st, body, dt = uw(f"/api/stock/{T}/ohlc/5m", limit=300)
print("status", st, f"({dt}s)")
rows = body.get("data", []) if st == 200 else []
print("rows:", len(rows))
if rows:
    # figure out ordering
    def ts(r): return datetime.fromisoformat(r["start_time"].replace("Z", "+00:00")).astimezone(NY)
    rows.sort(key=ts)
    last = rows[-1]
    print("first:", ts(rows[0]).strftime("%Y-%m-%d %H:%M"), " last:", ts(last).strftime("%Y-%m-%d %H:%M"),
          "market_time", last.get("market_time"))
    print(f"latest bar start -> now: {(now - ts(last)).total_seconds()/60:.1f} min  (real-time = within ~5-6 min while market open)")
    today = [r for r in rows if ts(r).date() == ts(last).date()]
    pre = [r for r in today if r.get("market_time") == "pr"]
    reg = [r for r in today if r.get("market_time") == "r"]
    print(f"today: {len(today)} bars, premarket {len(pre)}, regular {len(reg)}")
    print(f"PRE-MARKET volume today: {sum(int(r.get('volume') or 0) for r in pre):,}  (Yahoo gives 0 here)")
    print(f"regular-session volume so far (sum of bars): {sum(int(r.get('volume') or 0) for r in reg):,}")
    print("sample bar:", {k: last.get(k) for k in ("start_time", "open", "high", "low", "close", "volume", "total_volume", "market_time")})

# 3. consolidated vs partial volume: compare a completed bar + session total with Yahoo -----
hdr("3. Volume coverage vs Yahoo (consolidated SIP)")
try:
    import yfinance as yf
    y = yf.Ticker(T).history(period="2d", interval="5m", prepost=True)
    y = y[y.index.date == y.index[-1].date()]
    yreg = y[(y.index.time >= datetime.strptime("09:30", "%H:%M").time()) & (y.index.time < datetime.strptime("16:00", "%H:%M").time())]
    y_total = int(yreg["Volume"].sum())
    ysum_by_time = {t.strftime("%H:%M"): int(v) for t, v in yreg["Volume"].items()}
    print(f"Yahoo regular-session volume so far: {y_total:,}")
    if uw_total_vol:
        print(f"UW stock-state total_volume:          {int(uw_total_vol):,}   ratio UW/Yahoo = {int(uw_total_vol)/max(y_total,1):.2f}")
    if rows:
        uw_reg_sum = sum(int(r.get('volume') or 0) for r in reg)
        print(f"UW sum of regular 5m bars:            {uw_reg_sum:,}   ratio UW/Yahoo = {uw_reg_sum/max(y_total,1):.2f}")
        # compare 3 completed bars individually
        print("per-bar comparison (completed bars):")
        for r in reg[-5:-1]:
            k = ts(r).strftime("%H:%M")
            yv = ysum_by_time.get(k)
            uv = int(r.get("volume") or 0)
            print(f"   {k}  UW {uv:>10,}   Yahoo {yv if yv is None else f'{yv:,}':>10}   ratio {uv/yv if yv else float('nan'):.2f}")
    print("Interpretation: ratio ~1.0 => consolidated tape; ~0.2-0.5 => Nasdaq-only partial volume.")
except Exception as e:
    print("Yahoo comparison failed:", e)

# 4. option chain: NBBO + delta + OI ---------------------------------------------------------
hdr("4. Option contracts (NBBO, greeks, OI, volume)")
st, body, dt = uw(f"/api/stock/{T}/option-contracts", min_dte=1, max_dte=10, option_type="call", exclude_zero_oi_chains="true", limit=250)
print("status", st, f"({dt}s)")
if st == 200:
    cs = body.get("data", [])
    print("contracts returned:", len(cs))
    spot = float(state.get("close") or 0)
    near = sorted(cs, key=lambda c: abs(float(c.get("delta") or 0) - 0.6))[:3] if cs else []
    for c in near:
        print({k: c.get(k) for k in ("option_symbol", "nbbo_bid", "nbbo_ask", "delta", "implied_volatility", "open_interest", "volume", "last_tape_time")})
    if cs:
        n_nbbo = sum(1 for c in cs if c.get("nbbo_bid") not in (None, "0", 0) and c.get("nbbo_ask") not in (None, "0", 0))
        n_delta = sum(1 for c in cs if c.get("delta") not in (None, ""))
        print(f"with two-sided NBBO: {n_nbbo}/{len(cs)}   with delta: {n_delta}/{len(cs)}")
        lt = [c.get("last_tape_time") for c in cs if c.get("last_tape_time")]
        if lt:
            newest = max(datetime.fromisoformat(x.replace("Z", "+00:00")) for x in lt).astimezone(NY)
            print(f"newest contract tape time: {newest:%H:%M:%S} ET  (lag {(now-newest).total_seconds()/60:.1f} min)")
else:
    print(body)

# 5. screener with Gappers filters -----------------------------------------------------------
hdr("5. Stock screener: |change| >= 2%, price >= 5 (the Gappers scan)")
for label, kw in (("gappers up", dict(min_change=2)), ("gappers down", dict(max_change=-2))):
    st, body, dt = uw("/api/screener/stocks", min_underlying_price=5, order="relative_volume", order_direction="desc", limit=50, **kw)
    print(label, "status", st, f"({dt}s)")
    if st == 200:
        d = body.get("data", [])
        print("  rows:", len(d))
        for r in d[:4]:
            print("  ", {k: r.get(k) for k in ("ticker", "close", "prev_close", "relative_volume", "next_earnings_date", "er_time", "sector")})
    else:
        print(" ", body)

# 6. float / short interest ------------------------------------------------------------------
hdr("6. Float + short interest")
for p in (f"/api/shorts/{T}/interest-float/v2", f"/api/shorts/{T}/interest-float"):
    st, body, dt = uw(p)
    print(p, "status", st, f"({dt}s)")
    if st == 200:
        d = body.get("data", body)
        d = d[0] if isinstance(d, list) and d else d
        print("  ", json.dumps(d)[:400])
        break
    else:
        print("  ", str(body)[:200])

# 7. earnings premarket ----------------------------------------------------------------------
hdr("7. Earnings calendar (catalyst)")
st, body, dt = uw("/api/earnings/premarket", date=now.strftime("%Y-%m-%d"), limit=5)
print("status", st, f"({dt}s)")
if st == 200:
    for r in body.get("data", [])[:5]:
        print("  ", {k: r.get(k) for k in ("symbol", "report_date", "report_time", "expected_move_perc", "has_options")})
else:
    print(body)

print("\nDone. 8 requests used.")
