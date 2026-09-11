"""
datasources.py -- pluggable market-data backends for aziz_options_scanner.

Two implementations with the same small interface:

  UnusualWhalesSource  (default when UW_API_KEY is available)
      real-time consolidated candles incl. pre-market volume, screener universe,
      float + short interest, earnings calendar, news, option chains with NBBO and delta.
      Verified 2026-09-11: tape lag 0.0 min, 5-min bar volume == Yahoo consolidated (ratio 1.00).

  YahooSource          (free fallback, ~15-min delayed, no pre-market volume, no float on some names)

Interface (all methods may raise DataError; callers treat a ticker as "no data" on failure):
  universe(cfg)                    -> list[str]
  daily_bars(ticker)               -> DataFrame[Open High Low Close Volume], NY-tz daily index, regular session
  intraday_bars(ticker, interval)  -> DataFrame[Open High Low Close Volume], NY-tz index, includes pre/post market
  reference(ticker)                -> dict(float_shares, short_pct_float, short_asof, avg_daily_vol)
  catalyst(ticker, now)            -> str   ("EARNINGS (...)" or "unknown - ...")
  news(ticker, n)                  -> list[str]
  option_chain(ticker, call, min_dte, max_dte) -> list[dict(sym, strike, expiry, dte, bid, ask, iv, delta, oi, vol)]
      delta may be None (Yahoo); the scanner falls back to Black-Scholes then.

The API key is read from the UW_API_KEY environment variable (process env first, then the
Windows User environment so a freshly-set key works without restarting the terminal).
It is never logged or printed.
"""
from __future__ import annotations

import json
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

NY = ZoneInfo("America/New_York")
OCC_RE = re.compile(r"^(?P<root>[A-Z.]{1,6})(?P<ymd>\d{6})(?P<cp>[CP])(?P<strike>\d{8})$")


class DataError(RuntimeError):
    pass


def _f(x, default=np.nan) -> float:
    try:
        if x is None or x == "":
            return default
        v = float(x)
        return v if np.isfinite(v) else default
    except (TypeError, ValueError):
        return default


def parse_occ(sym: str) -> Optional[tuple[str, str, bool, float]]:
    """'AAPL260918C00330000' -> ('AAPL', '2026-09-18', True, 330.0)"""
    m = OCC_RE.match(sym or "")
    if not m:
        return None
    ymd = m["ymd"]
    return m["root"], f"20{ymd[:2]}-{ymd[2:4]}-{ymd[4:]}", m["cp"] == "C", int(m["strike"]) / 1000.0


# ===========================================================================
# Unusual Whales
# ===========================================================================
def load_uw_key() -> Optional[str]:
    k = os.environ.get("UW_API_KEY")
    if not k and sys.platform == "win32":
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as h:
                k, _ = winreg.QueryValueEx(h, "UW_API_KEY")
        except OSError:
            k = None
    return k.strip() if k else None


class UWClient:
    BASE = "https://api.unusualwhales.com"

    def __init__(self, key: str, log=None):
        self._key = key
        self.log = log or (lambda *a: None)
        self.requests = 0
        cafile = os.environ.get("SSL_CERT_FILE")
        self._ctx = ssl.create_default_context(cafile=cafile if cafile and os.path.exists(cafile) else None)

    def get(self, path: str, **params):
        clean = {k: v for k, v in params.items() if v is not None}
        url = self.BASE + path + ("?" + urllib.parse.urlencode(clean, doseq=True) if clean else "")
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {self._key}", "Accept": "application/json"})
        for attempt in range(4):
            self.requests += 1
            try:
                with urllib.request.urlopen(req, context=self._ctx, timeout=30) as r:
                    body = json.loads(r.read().decode("utf-8"))
                    return body.get("data", body) if isinstance(body, dict) else body
            except urllib.error.HTTPError as e:
                if e.code in (429, 500, 502, 503, 504) and attempt < 3:
                    wait = 1.5 * (attempt + 1)
                    self.log(f"    UW {e.code} on {path}; retrying in {wait:.0f}s")
                    time.sleep(wait)
                    continue
                detail = e.read().decode("utf-8", "replace")[:200]
                raise DataError(f"UW {e.code} {path}: {detail}") from None
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
                if attempt < 3:
                    time.sleep(1.0)
                    continue
                raise DataError(f"UW request failed {path}: {e}") from None
        raise DataError(f"UW request failed {path}")


class UnusualWhalesSource:
    name = "unusualwhales"
    intraday_sessions = 8      # how many prior sessions of 5m bars to pull (for the relative-volume profile)

    def __init__(self, key: str, log=None):
        self.c = UWClient(key, log)
        self.log = log or (lambda *a: None)
        self._earnings_cache: dict[str, list] = {}
        self._news_cache: dict[str, list] = {}
        self._info_cache: dict[str, dict] = {}
        self._mkt: Optional[dict[str, float]] = None

    # ---- universe -------------------------------------------------------
    def universe(self, cfg) -> list[str]:
        """Book's pre-market Gappers scan: |change| >= min_gap_pct, price band. UW's screener
        takes the change as a ratio (0.02 == 2%) and also returns consolidated relative volume."""
        gap = cfg.min_gap_pct / 100.0
        rows = []
        for kw in (dict(min_change=gap), dict(max_change=-gap)):
            q = dict(min_underlying_price=cfg.min_price, limit=200, **kw)
            if cfg.max_price > 0:
                q["max_underlying_price"] = cfg.max_price
            try:
                rows += self.c.get("/api/screener/stocks", **q) or []
            except DataError as e:
                self.log(f"  screener failed: {e}")
        out = {}
        for r in rows:
            t = r.get("ticker") or ""
            c, p = _f(r.get("close")), _f(r.get("prev_close"))
            if not t or not np.isfinite(c) or not np.isfinite(p) or p <= 0 or r.get("is_index"):
                continue
            it = (r.get("issue_type") or "").lower()
            if any(bad in it for bad in ("warrant", "unit", "right", "preferred")):
                continue
            # ETFs gap with their underlying, not on a company catalyst (book: Stocks in Play need one)
            if not getattr(cfg, "include_etfs", False) and ("etf" in it or "fund" in it or "trust" in it):
                continue
            if "." in t or "-" in t or "^" in t:
                continue
            chg = abs(c / p - 1.0) * 100.0
            rv = _f(r.get("relative_volume"), 0.0)
            if chg >= cfg.min_gap_pct and t not in out:
                out[t] = (rv, chg)
        # rank: relative volume first (book: "high relative volume" is what makes a Stock in Play)
        ranked = sorted(out.items(), key=lambda kv: (-kv[1][0], -kv[1][1]))
        return [t for t, _ in ranked[: cfg.universe_size]]

    # ---- bars -----------------------------------------------------------
    def daily_bars(self, t: str) -> pd.DataFrame:
        rows = self.c.get(f"/api/stock/{t}/ohlc/1d", limit=400) or []
        # UW returns three rows per day (pr / r / po); the book's daily chart is the regular session
        rows = [r for r in rows if r.get("market_time") in ("r", "regular", None)]
        if not rows:
            raise DataError(f"{t}: no daily bars")
        df = pd.DataFrame({
            "Open": [_f(r["open"]) for r in rows], "High": [_f(r["high"]) for r in rows],
            "Low": [_f(r["low"]) for r in rows], "Close": [_f(r["close"]) for r in rows],
            "Volume": [int(_f(r.get("volume"), 0)) for r in rows],
        }, index=pd.to_datetime([r["date"] for r in rows]))
        df.index = df.index.tz_localize(NY)
        return df.sort_index().dropna(subset=["Close"])

    def intraday_bars(self, t: str, interval: str = "5m") -> pd.DataFrame:
        per_day = {"1m": 960, "5m": 192, "10m": 96, "15m": 64, "30m": 32}.get(interval, 192)
        limit = min(2500, per_day * (self.intraday_sessions + 1))
        rows = self.c.get(f"/api/stock/{t}/ohlc/{interval}", limit=limit) or []
        if not rows:
            raise DataError(f"{t}: no intraday bars")
        idx = pd.to_datetime([r["start_time"] for r in rows], utc=True).tz_convert(NY)
        df = pd.DataFrame({
            "Open": [_f(r["open"]) for r in rows], "High": [_f(r["high"]) for r in rows],
            "Low": [_f(r["low"]) for r in rows], "Close": [_f(r["close"]) for r in rows],
            "Volume": [int(_f(r.get("volume"), 0)) for r in rows],
            "market_time": [r.get("market_time") for r in rows],
        }, index=idx)
        df = df[~df.index.duplicated(keep="last")].sort_index()
        return df.dropna(subset=["Close"])

    # ---- reference / catalyst / news -------------------------------------
    def reference(self, t: str) -> dict:
        out = {"float_shares": np.nan, "short_pct_float": np.nan, "short_asof": None, "avg_daily_vol": np.nan}
        try:
            d = self.c.get(f"/api/shorts/{t}/interest-float/v2")
            d = d[0] if isinstance(d, list) and d else d
            if isinstance(d, dict):
                out["float_shares"] = _f(d.get("total_float"))
                out["short_pct_float"] = _f(d.get("si_float")) * 100.0
                out["short_asof"] = d.get("market_date")
        except DataError as e:
            self.log(f"    {t}: short interest unavailable ({e})")
        return out

    def _earnings(self, t: str) -> list:
        if t not in self._earnings_cache:
            try:
                self._earnings_cache[t] = self.c.get(f"/api/stock/{t}/earnings") or []
            except DataError:
                self._earnings_cache[t] = []
        return self._earnings_cache[t]

    def earnings_catalyst(self, t: str, now: datetime) -> Optional[str]:
        """Earnings within the last 3 days (or today's pre/post-market) -> description, else None."""
        today = now.date()
        for r in self._earnings(t):
            rd = r.get("report_date")
            if not rd:
                continue
            try:
                d = datetime.strptime(rd, "%Y-%m-%d").date()
            except ValueError:
                continue
            if timedelta(days=-1) <= (today - d) <= timedelta(days=3):
                surprise = r.get("surprise_percentage")
                s = f", surprise {float(surprise):+.1f}%" if surprise not in (None, "") else ""
                return f"EARNINGS ({d} {r.get('report_time') or 'time n/a'}{s})"
        return None

    def next_earnings(self, t: str, now: datetime) -> Optional[str]:
        nxt = [r.get("report_date") for r in self._earnings(t) if r.get("report_date") and r.get("report_date") >= str(now.date())]
        return min(nxt) if nxt else None

    def news_items(self, t: str, n: int = 12) -> list[dict]:
        """Structured headlines: {headline, created_at, is_major, tickers}. Cached per ticker per run."""
        if t in self._news_cache:
            return self._news_cache[t]
        try:
            rows = self.c.get("/api/news/headlines", ticker=t, limit=n) or []
        except DataError:
            rows = []
        items = [dict(headline=r.get("headline", ""), created_at=r.get("created_at"), is_major=bool(r.get("is_major")),
                      tickers=r.get("tickers") or [t]) for r in rows]
        self._news_cache[t] = items
        return items

    def news(self, t: str, n: int = 4) -> list[str]:
        out = []
        for r in self.news_items(t)[:n]:
            ts = (r.get("created_at") or "")[:16].replace("T", " ")
            flag = " [major]" if r.get("is_major") else ""
            out.append(f"{ts}{flag} {r.get('headline', '')}")
        return out

    def sector(self, t: str) -> Optional[str]:
        if t not in self._info_cache:
            try:
                d = self.c.get(f"/api/stock/{t}/info") or {}
            except DataError:
                d = {}
            self._info_cache[t] = d
        return (self._info_cache[t] or {}).get("sector")

    def market_context(self) -> dict[str, float]:
        """% change today (last vs prev close) for SPY and every sector ETF, one call, cached."""
        if self._mkt is None:
            self._mkt = {}
            try:
                for r in self.c.get("/api/market/sector-etfs") or []:
                    last, prev = _f(r.get("last")), _f(r.get("prev_close"))
                    if np.isfinite(last) and np.isfinite(prev) and prev > 0 and r.get("ticker"):
                        self._mkt[r["ticker"]] = (last / prev - 1.0) * 100.0
            except DataError as e:
                self.log(f"  sector ETF context unavailable ({e})")
        return self._mkt

    # ---- options ----------------------------------------------------------
    def option_chain(self, t: str, call: bool, min_dte: int, max_dte: int) -> list[dict]:
        rows = self.c.get(f"/api/stock/{t}/option-contracts", option_type="call" if call else "put",
                          min_dte=max(0, min_dte), max_dte=max_dte, exclude_zero_oi_chains="true", limit=250) or []
        today = datetime.now(NY).date()
        out = []
        for r in rows:
            p = parse_occ(r.get("option_symbol", ""))
            if not p:
                continue
            _, expiry, is_call, K = p
            if is_call != call:
                continue
            dte = (datetime.strptime(expiry, "%Y-%m-%d").date() - today).days
            out.append(dict(
                sym=r["option_symbol"], strike=K, expiry=expiry, dte=dte,
                bid=_f(r.get("nbbo_bid"), 0.0), ask=_f(r.get("nbbo_ask"), 0.0),
                iv=_f(r.get("implied_volatility")), delta=_f(r.get("delta"), None),
                oi=int(_f(r.get("open_interest"), 0)), vol=int(_f(r.get("volume"), 0)),
                last_tape=r.get("last_tape_time"),
            ))
        return out


# ===========================================================================
# Yahoo (fallback)
# ===========================================================================
class YahooSource:
    name = "yahoo"

    def __init__(self, log=None):
        import yfinance as yf
        self.yf = yf
        self.log = log or (lambda *a: None)
        self._info: dict[str, dict] = {}

    def _ticker_info(self, t: str) -> dict:
        if t not in self._info:
            try:
                self._info[t] = self.yf.Ticker(t).info or {}
            except Exception:
                self._info[t] = {}
        return self._info[t]

    def universe(self, cfg) -> list[str]:
        from yfinance import EquityQuery as EQ
        parts = [EQ("eq", ["region", "us"]), EQ("gt", ["avgdailyvol3m", cfg.min_avg_daily_vol]),
                 EQ("or", [EQ("gt", ["percentchange", cfg.min_gap_pct]), EQ("lt", ["percentchange", -cfg.min_gap_pct])]),
                 EQ("gt", ["intradayprice", cfg.min_price])]
        if cfg.max_price > 0:
            parts.append(EQ("lt", ["intradayprice", cfg.max_price]))
        q = EQ("and", parts)
        syms = []
        for asc in (False, True):
            try:
                r = self.yf.screen(q, size=min(250, cfg.universe_size * 3), sortField="percentchange", sortAsc=asc)
            except Exception as e:
                self.log(f"  screener call failed ({e})"); r = {}
            for row in r.get("quotes", []) or []:
                s = row.get("symbol", "")
                if s and not any(ch in s for ch in ".-^"):
                    syms.append((s, abs(_f(row.get("regularMarketChangePercent"), 0.0))))
        seen, out = set(), []
        for s, _ in sorted(syms, key=lambda x: -x[1]):
            if s not in seen:
                seen.add(s); out.append(s)
        return out[: cfg.universe_size]

    def daily_bars(self, t: str) -> pd.DataFrame:
        df = self.yf.Ticker(t).history(period="6mo", interval="1d", auto_adjust=False)
        if df is None or df.empty:
            raise DataError(f"{t}: no daily bars")
        df = df[["Open", "High", "Low", "Close", "Volume"]].dropna(subset=["Close"])
        df.index = df.index.tz_localize(NY) if df.index.tz is None else df.index.tz_convert(NY)
        return df

    def intraday_bars(self, t: str, interval: str = "5m") -> pd.DataFrame:
        df = self.yf.Ticker(t).history(period="7d", interval=interval, prepost=True, auto_adjust=False)
        if df is None or df.empty:
            raise DataError(f"{t}: no intraday bars")
        df = df[["Open", "High", "Low", "Close", "Volume"]].dropna(subset=["Close"])
        df.index = df.index.tz_localize("UTC").tz_convert(NY) if df.index.tz is None else df.index.tz_convert(NY)
        # Yahoo pre-market bars have volume 0; mark them so the scanner reports "unavailable"
        df["market_time"] = None
        return df

    def reference(self, t: str) -> dict:
        i = self._ticker_info(t)
        return {"float_shares": _f(i.get("floatShares")), "short_pct_float": _f(i.get("shortPercentOfFloat")) * 100.0,
                "short_asof": None, "avg_daily_vol": _f(i.get("averageVolume"))}

    def earnings_catalyst(self, t: str, now: datetime) -> Optional[str]:
        i = self._ticker_info(t)
        ts = i.get("earningsTimestamp") or i.get("earningsTimestampStart")
        if ts:
            try:
                et = datetime.fromtimestamp(int(ts), tz=NY)
                if timedelta(hours=-6) <= now - et <= timedelta(days=3):
                    return f"EARNINGS ({et:%Y-%m-%d %H:%M} ET)"
            except Exception:
                pass
        return None

    def next_earnings(self, t: str, now: datetime) -> Optional[str]:
        return None

    def news_items(self, t: str, n: int = 12) -> list[dict]:
        out = []
        try:
            for x in (self.yf.Ticker(t).news or [])[:n]:
                c = x.get("content", x)
                title = c.get("title") or x.get("title")
                pub = c.get("pubDate") or x.get("providerPublishTime")
                if isinstance(pub, (int, float)):
                    pub = datetime.fromtimestamp(pub, tz=NY).isoformat()
                if title:
                    out.append(dict(headline=title, created_at=pub, is_major=False, tickers=[t]))
        except Exception:
            pass
        return out

    def news(self, t: str, n: int = 4) -> list[str]:
        return [f"{str(x['created_at'])[:16]} {x['headline']}" for x in self.news_items(t)[:n]]

    def sector(self, t: str) -> Optional[str]:
        return self._ticker_info(t).get("sector")

    def market_context(self) -> dict[str, float]:
        if not hasattr(self, "_mkt"):
            self._mkt = {}
            for etf in ["SPY", "XLK", "XLV", "XLF", "XLE", "XLY", "XLP", "XLI", "XLU", "XLRE", "XLB", "XLC"]:
                try:
                    fi = self.yf.Ticker(etf).fast_info
                    last, prev = _f(fi.get("lastPrice")), _f(fi.get("previousClose"))
                    if np.isfinite(last) and np.isfinite(prev) and prev > 0:
                        self._mkt[etf] = (last / prev - 1.0) * 100.0
                except Exception:
                    pass
        return self._mkt

    def option_chain(self, t: str, call: bool, min_dte: int, max_dte: int) -> list[dict]:
        tk = self.yf.Ticker(t)
        try:
            expiries = list(tk.options)
        except Exception as e:
            raise DataError(f"{t}: no option chain ({e})")
        today = datetime.now(NY).date()
        out = []
        for e in expiries:
            dte = (datetime.strptime(e, "%Y-%m-%d").date() - today).days
            if not (min_dte <= dte <= max_dte):
                continue
            try:
                ch = tk.option_chain(e)
            except Exception:
                continue
            df = ch.calls if call else ch.puts
            for _, r in df.iterrows():
                out.append(dict(sym=str(r.get("contractSymbol")), strike=_f(r.get("strike")), expiry=e, dte=dte,
                                bid=_f(r.get("bid"), 0.0), ask=_f(r.get("ask"), 0.0), iv=_f(r.get("impliedVolatility")),
                                delta=None, oi=int(_f(r.get("openInterest"), 0)), vol=int(_f(r.get("volume"), 0)), last_tape=None))
        return out


def make_source(name: str, log=None):
    """name: 'auto' | 'uw' | 'yahoo'"""
    if name in ("auto", "uw"):
        key = load_uw_key()
        if key:
            return UnusualWhalesSource(key, log)
        if name == "uw":
            raise DataError("UW_API_KEY not set (process env or Windows User env)")
        if log:
            log("UW_API_KEY not found - falling back to Yahoo (delayed, no pre-market volume)")
    return YahooSource(log)
