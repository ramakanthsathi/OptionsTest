#!/usr/bin/env python
"""
aziz_options_scanner.py
=======================

Options signal scanner built from the rules in Andrew Aziz's
"How to Day Trade for a Living" (Bear Bull Traders edition).

HONEST FRAMING -- READ THIS FIRST
---------------------------------
1. The book is about day-trading *stocks*. The author states plainly:
   "I don't trade Options or Futures". There is no options strategy in it.
   Everything in the STOCK layer below (Stocks in Play filters, the nine
   setups, 2% risk rule, 2:1 reward:risk, time-of-day rules) comes from the
   book. Everything in the OPTIONS layer (expiry/strike selection, delta
   targeting, liquidity filters, option-level R:R, contract sizing) is my
   translation and is NOT from the book. Each such place is tagged
   `# [NOT FROM BOOK]`.

2. The book's Rule 10: "Indicators only indicate; they should not be allowed
   to dictate." The author is explicit that his trading is discretionary and
   that purely mechanical systems lose to institutional algos. This script is
   therefore a *scanner + trade-plan generator*, exactly like the Trade Ideas
   scanners the author runs. It does not place orders and it does not claim a
   win rate. Nothing here has been backtested; there is no historical options
   data in the free feed to do that honestly.

3. Data is Yahoo Finance via `yfinance`: quotes and option chains are delayed
   (typically 15 min), intraday bars can lag, and float / short-interest
   fields are sometimes missing. The book's Bull Flag momentum strategy needs
   real-time 1-minute data and a hotkey platform; with delayed data it is
   flagged LOW confidence here. The author also says explicitly that the
   catalyst check ("why did it gap?") is done by reading the news yourself;
   this script surfaces the earnings date and recent headlines but cannot
   judge a catalyst for you.

4. Nothing here is financial advice. Options can expire worthless; the max
   loss on a long option is the entire premium if you cannot exit at your
   planned stop (halts, gaps, illiquid quotes).

Usage
-----
    python aziz_options_scanner.py                       # screener universe
    python aziz_options_scanner.py -t AAPL NVDA TSLA     # your own list
    python aziz_options_scanner.py --account 25000 --risk-pct 1
    python aziz_options_scanner.py --replay 2026-09-09 -t AAPL   # stock setups on a past session
    python aziz_options_scanner.py --show-rejected       # see why setups were thrown out
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
import warnings
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore", category=FutureWarning)
warnings.filterwarnings("ignore", category=RuntimeWarning)

# ---------------------------------------------------------------------------
# TLS bootstrap: this machine runs AVG's HTTPS scanning, whose root CA is not
# in certifi. ca_bundle.pem = certifi + AVG root. Never fall back to verify=False.
# ---------------------------------------------------------------------------
_HERE = os.path.dirname(os.path.abspath(__file__))
_BUNDLE = os.path.join(_HERE, "ca_bundle.pem")
if os.path.exists(_BUNDLE):
    for _k in ("SSL_CERT_FILE", "CURL_CA_BUNDLE", "REQUESTS_CA_BUNDLE"):
        os.environ.setdefault(_k, _BUNDLE)

from datasources import make_source, DataError, NY as _NY  # noqa: E402
import catalyst as catmod  # noqa: E402

NY = ZoneInfo("America/New_York")


# ===========================================================================
# CONFIG -- numbers cited from the book are annotated with the chapter
# ===========================================================================
@dataclass
class Config:
    # --- account / risk (Ch. 3) ---
    account_size: float = 25_000.0     # author says he keeps ~$25k in his account
    risk_pct: float = 1.0              # "absolute maximum ... 2%"; he often uses 1%. Default 1%.
    min_reward_risk: float = 2.0       # Rule 5: minimum 2:1

    # --- Stocks in Play, pre-market Gappers scanner (Ch. 4) ---
    min_gap_pct: float = 2.0           # gapped up/down at least 2%
    min_premarket_vol: int = 50_000    # traded at least 50,000 shares pre-market
    min_avg_daily_vol: int = 500_000   # average daily volume > 500,000
    min_atr: float = 0.50              # ATR of at least 50 cents
    max_short_pct_float: float = 30.0  # avoid short interest > 30%
    # --- Stocks in Play, intraday Volume Radar (Ch. 4) ---
    min_rel_vol: float = 1.5           # trading at >= 1.5x normal volume
    min_gap_dollars: float = 1.0       # gapped at least $1 (radar variant)

    # --- price band ---
    # Book: <$10 = momentum only; $10-$100 = his main range; he avoids >$100 because
    # he can't buy enough *shares*. That reason doesn't apply to options, so the
    # ceiling is off by default; >$100 names are just flagged.  [NOT FROM BOOK]
    min_price: float = 5.0
    max_price: float = 0.0             # 0 = no ceiling

    # --- chart / indicator settings (Ch. 5) ---
    bar_interval: str = "5m"           # author's primary chart is 5-minute
    rsi_period: int = 14
    atr_period: int = 14
    rsi_reversal_scan: float = 20.0    # his scanner highlights RSI < 20 / > 80
    rsi_reversal_extreme: float = 10.0 # "RSI must be lower than 10" (strategy summary)
    min_consecutive_candles: int = 4   # "four or more consecutive candlesticks"
    level_tolerance_pct: float = 0.25  # S/R is an *area*: ~5-10c on a $20 stock => ~0.25-0.5%
    level_tolerance_min: float = 0.05  # ... but never tighter than 5 cents
    orb_minutes: int = 5               # 5-min ORB (author now also likes 15/30)
    orb_max_range_atr: float = 0.75    # opening range must be "significantly smaller" than ATR
    daily_lookback: int = 60           # days of daily bars used to draw S/R levels
    sr_min_touches: int = 2            # "connecting two or more bottoms/tops"

    # --- universe ---
    universe_size: int = 40            # cap on screener names (each costs ~4 API calls)
    peer_cluster_min: int = 3          # Ch. 4: "a few stocks in one sector" on the scanner = sector move, not in play
    include_etfs: bool = False
    include_mid_day: bool = True       # author: Mid-day is "the most dangerous time"

    # --- OPTIONS layer  [NOT FROM BOOK] ---
    target_delta: float = 0.60         # slightly ITM: tracks the stock, less IV/theta drag
    min_dte: int = 1                   # avoid 0DTE by default (gamma/theta risk)
    max_dte: int = 21
    min_open_interest: int = 100
    min_option_volume: int = 10
    max_spread_pct: float = 10.0       # (ask-bid)/mid; wider than this = not day-tradeable
    risk_free_rate: float = 0.04
    max_premium_pct_account: float = 5.0  # cap total premium at 5% of account regardless of sizing


# ===========================================================================
# DATA HELPERS
# ===========================================================================
def _now_et() -> datetime:
    return datetime.now(NY)


def _to_float(x, default=np.nan) -> float:
    try:
        if x is None:
            return default
        v = float(x)
        return v if np.isfinite(v) else default
    except (TypeError, ValueError):
        return default


# Data access lives in datasources.py (UnusualWhalesSource / YahooSource).


# ===========================================================================
# INDICATORS (Ch. 5: 9 EMA, 20 EMA, 50 SMA, 200 SMA, VWAP, prev close, RSI for scans)
# ===========================================================================
def atr(daily: pd.DataFrame, n: int) -> float:
    h, l, c = daily["High"], daily["Low"], daily["Close"]
    pc = c.shift(1)
    tr = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    v = tr.rolling(n).mean().iloc[-1]
    return float(v) if np.isfinite(v) else float("nan")


def rsi(close: pd.Series, n: int) -> pd.Series:
    d = close.diff()
    up = d.clip(lower=0.0)
    dn = (-d).clip(lower=0.0)
    # Wilder smoothing
    au = up.ewm(alpha=1.0 / n, adjust=False).mean()
    ad = dn.ewm(alpha=1.0 / n, adjust=False).mean()
    rs = au / ad.replace(0.0, np.nan)
    out = 100.0 - 100.0 / (1.0 + rs)
    return out.fillna(100.0).where(ad > 0, 100.0).where(au > 0, 0.0)


def add_session_indicators(bars: pd.DataFrame, cfg: Config) -> pd.DataFrame:
    """bars: intraday 5m incl. pre/post. Adds EMA/SMA/RSI (computed on regular-session
    bars only, continuous across days like a trading platform does) and per-session VWAP."""
    df = bars.copy()
    tod = df.index.time
    df["session"] = [(t >= pd.Timestamp("09:30").time()) and (t < pd.Timestamp("16:00").time()) for t in tod]
    df["premarket"] = [t < pd.Timestamp("09:30").time() for t in tod]
    df["date"] = df.index.date
    reg = df[df["session"]].copy()
    reg["ema9"] = reg["Close"].ewm(span=9, adjust=False).mean()
    reg["ema20"] = reg["Close"].ewm(span=20, adjust=False).mean()
    reg["sma50"] = reg["Close"].rolling(50, min_periods=10).mean()
    reg["sma200"] = reg["Close"].rolling(200, min_periods=20).mean()
    reg["rsi"] = rsi(reg["Close"], cfg.rsi_period)
    tp = (reg["High"] + reg["Low"] + reg["Close"]) / 3.0
    pv = (tp * reg["Volume"]).groupby(reg["date"]).cumsum()
    vv = reg["Volume"].groupby(reg["date"]).cumsum().replace(0, np.nan)
    reg["vwap"] = pv / vv
    reg["cum_vol"] = reg["Volume"].groupby(reg["date"]).cumsum()
    return reg


# ===========================================================================
# LEVELS (Ch. 7, Support or Resistance: horizontal lines through daily wicks,
# >=2 touches, only near the current price; half/whole dollars on cheap stocks)
# ===========================================================================
@dataclass
class Level:
    price: float
    kind: str          # "daily", "prev_close", "premarket_high", "premarket_low", "round"
    touches: int = 1

    def label(self) -> str:
        return f"{self.price:.2f}({self.kind}{'x' + str(self.touches) if self.touches > 1 else ''})"


def tolerance(price: float, cfg: Config) -> float:
    return max(cfg.level_tolerance_min, price * cfg.level_tolerance_pct / 100.0)


def daily_levels(daily: pd.DataFrame, price: float, atr_val: float, cfg: Config) -> list[Level]:
    d = daily.tail(cfg.daily_lookback)
    pts = list(d["High"].values) + list(d["Low"].values)
    pts = [float(p) for p in pts if np.isfinite(p)]
    tol = tolerance(price, cfg) * 2  # clustering window = 2x the trade tolerance
    pts.sort()
    clusters: list[list[float]] = []
    for p in pts:
        if clusters and p - clusters[-1][0] <= tol:
            clusters[-1].append(p)
        else:
            clusters.append([p])
    levels = []
    window = max(3.0 * atr_val, price * 0.05) if np.isfinite(atr_val) else price * 0.05
    for c in clusters:
        if len(c) >= cfg.sr_min_touches:
            lv = float(np.mean(c))
            if abs(lv - price) <= window:                   # only levels near the current range
                levels.append(Level(lv, "daily", len(c)))
    return levels


def all_levels(daily: pd.DataFrame, reg_today: pd.DataFrame, pre_today: pd.DataFrame,
               prev_close: float, price: float, atr_val: float, cfg: Config) -> list[Level]:
    lv = daily_levels(daily, price, atr_val, cfg)
    if np.isfinite(prev_close):
        lv.append(Level(prev_close, "prev_close"))
    if not pre_today.empty:
        lv.append(Level(float(pre_today["High"].max()), "premarket_high"))
        lv.append(Level(float(pre_today["Low"].min()), "premarket_low"))
    if price < 10:  # book: half/whole dollars matter most on cheap stocks
        base = math.floor(price)
        for x in (base - 0.5, base, base + 0.5, base + 1.0):
            if x > 0:
                lv.append(Level(float(x), "round"))
    # merge near-duplicates, keep the more "important" kind
    lv.sort(key=lambda L: L.price)
    merged: list[Level] = []
    for L in lv:
        if merged and abs(L.price - merged[-1].price) <= tolerance(price, cfg):
            m = merged[-1]
            m.touches = m.touches + L.touches          # a daily level confirmed by prev close / premarket = stronger
            if L.kind == "prev_close" or (L.kind == "daily" and m.kind != "prev_close"):
                m.kind = L.kind
                m.price = L.price if L.kind == "daily" else m.price
        else:
            merged.append(L)
    return merged


def next_level(levels: list[Level], from_price: float, direction: int, min_dist: float) -> Optional[Level]:
    """Nearest level at least min_dist away in `direction` (+1 up / -1 down)."""
    cands = [L for L in levels if (L.price - from_price) * direction >= min_dist]
    if not cands:
        return None
    return min(cands, key=lambda L: abs(L.price - from_price))


def near_level(levels: list[Level], price: float, tol: float, kinds=None) -> Optional[Level]:
    best = None
    for L in levels:
        if kinds and L.kind not in kinds:
            continue
        if abs(L.price - price) <= tol and (best is None or abs(L.price - price) < abs(best.price - price)):
            best = L
    return best


# ===========================================================================
# CANDLE CLASSIFICATION (Ch. 6)
# ===========================================================================
def candle_type(o: float, h: float, l: float, c: float) -> str:
    rng = h - l
    if rng <= 0:
        return "flat"
    body = abs(c - o)
    upper = h - max(o, c)
    lower = min(o, c) - l
    if body <= 0.30 * rng:
        if lower >= 2 * body and lower > upper * 1.5:
            return "hammer"           # bullish doji
        if upper >= 2 * body and upper > lower * 1.5:
            return "shooting_star"    # bearish doji
        return "doji"                 # spinning top / simple doji
    if body >= 0.60 * rng:
        return "bull_strong" if c > o else "bear_strong"
    return "bull" if c > o else "bear"


INDECISION = {"doji", "hammer", "shooting_star"}


# ===========================================================================
# SIGNALS
# ===========================================================================
@dataclass
class StockSignal:
    ticker: str
    strategy: str
    direction: str            # "LONG" / "SHORT"
    entry: float
    stop: float
    target: float
    confidence: str           # "high" / "medium" / "low"
    rationale: list[str]
    time_bucket: str
    bar_time: str
    extra: dict = field(default_factory=dict)

    @property
    def risk(self) -> float:
        return abs(self.entry - self.stop)

    @property
    def reward(self) -> float:
        return abs(self.target - self.entry)

    @property
    def rr(self) -> float:
        return self.reward / self.risk if self.risk > 0 else 0.0


def time_bucket(ts: pd.Timestamp) -> str:
    t = ts.time()
    if t < pd.Timestamp("09:30").time():
        return "premarket"
    if t < pd.Timestamp("11:00").time():
        return "open"        # 9:30-11:00 "the Open"
    if t < pd.Timestamp("15:00").time():
        return "midday"      # 11:00-15:00 "Mid-day ... the most dangerous time"
    if t < pd.Timestamp("16:00").time():
        return "close"       # 15:00-16:00 "the Close"
    return "afterhours"


# Ch. 7 "Trading Based on the Time of Day"
STRATEGY_TIMES = {
    "BullFlag":      {"open"},
    "ABCD":          {"open", "midday"},
    "ORB":           {"open"},
    "VWAP":          {"open", "midday", "close"},
    "BottomReversal": {"open", "midday", "close"},
    "TopReversal":   {"open", "midday", "close"},
    "MATrend":       {"midday", "close"},
    "SupportResistance": {"open", "midday", "close"},
    "RedToGreen":    {"open", "midday", "close"},
}


class SetupDetector:
    def __init__(self, ticker: str, reg: pd.DataFrame, levels: list[Level], atr_val: float,
                 prev_close: float, cfg: Config, rejected: list):
        self.t = ticker
        self.cfg = cfg
        self.levels = levels
        self.atr = atr_val
        self.prev_close = prev_close
        self.rejected = rejected
        self.today = reg[reg["date"] == reg["date"].iloc[-1]].copy()
        self.day = self.today  # alias
        self.last = self.today.iloc[-1]
        self.bar_ts = self.today.index[-1]
        self.bucket = time_bucket(self.bar_ts)
        self.price = float(self.last["Close"])
        self.tol = tolerance(self.price, cfg)
        self.buffer = max(0.05, 0.10 * atr_val) if np.isfinite(atr_val) else 0.05  # stop buffer beyond a level
        self.vwap = float(self.last["vwap"])
        self.ema9 = float(self.last["ema9"])
        self.ema20 = float(self.last["ema20"])
        self.day_high = float(self.today["High"].max())
        self.day_low = float(self.today["Low"].min())
        self.med_range = float((self.today["High"] - self.today["Low"]).median())
        self.med_vol = float(self.today["Volume"].median()) if len(self.today) else 0.0

    # ---- helpers ------------------------------------------------------
    def _reject(self, strategy: str, why: str):
        self.rejected.append({"ticker": self.t, "strategy": strategy, "why": why})

    def _finish(self, sig: StockSignal) -> Optional[StockSignal]:
        """Common gates: time-of-day, positive risk, 2:1 reward:risk (Rule 5)."""
        if sig.risk <= 0:
            self._reject(sig.strategy, "non-positive risk")
            return None
        # A stop a few cents from the entry is not "a reasonable technical level" (Ch. 3) -- it is
        # noise, and it inflates R:R into fantasy numbers. Floor: 10c or 10% of ATR, whichever is larger.
        floor = max(0.10, 0.10 * self.atr) if np.isfinite(self.atr) else 0.10
        if sig.risk < floor:
            self._reject(sig.strategy, f"stop only {sig.risk:.2f} from entry (< floor {floor:.2f}); price is sitting on the level, "
                                       f"wait for it to move away or for a real confirmation")
            return None
        if self.bucket not in STRATEGY_TIMES[sig.strategy]:
            self._reject(sig.strategy, f"wrong time of day ({self.bucket}) for this strategy per book")
            return None
        if not self.cfg.include_mid_day and self.bucket == "midday":
            self._reject(sig.strategy, "mid-day trading disabled")
            return None
        if sig.rr < self.cfg.min_reward_risk:
            self._reject(sig.strategy, f"reward:risk {sig.rr:.2f} < {self.cfg.min_reward_risk} "
                                       f"(entry {sig.entry:.2f} stop {sig.stop:.2f} target {sig.target:.2f})")
            return None
        return sig

    def _target(self, direction: int, from_price: float, min_dist: float) -> Optional[Level]:
        return next_level(self.levels, from_price, direction, min_dist)

    def _mk(self, strategy, direction, entry, stop, target, conf, why, **extra) -> Optional[StockSignal]:
        sig = StockSignal(self.t, strategy, "LONG" if direction > 0 else "SHORT",
                          round(entry, 2), round(stop, 2), round(target, 2), conf, why,
                          self.bucket, self.bar_ts.strftime("%Y-%m-%d %H:%M"), extra)
        return self._finish(sig)

    def _closest_ma_target(self, direction: int) -> Optional[tuple[float, str]]:
        """Reversal exit: VWAP / 9 EMA / 20 EMA 'whichever is closer' (book)."""
        cands = [(self.vwap, "VWAP"), (self.ema9, "9EMA"), (self.ema20, "20EMA")]
        cands = [(p, n) for p, n in cands if np.isfinite(p) and (p - self.price) * direction > 0]
        if not cands:
            return None
        return min(cands, key=lambda x: abs(x[0] - self.price))

    # ---- Strategy 6: VWAP ---------------------------------------------
    def vwap_setup(self) -> Optional[StockSignal]:
        d = self.today
        if len(d) < 4:                       # book waits 10-15 min after the open
            return None
        last3 = d.tail(3)
        # "no interest ... sideways near VWAP -> stay away"
        if len(d) >= 6 and (abs(d.tail(6)["Close"] - d.tail(6)["vwap"]) <= self.tol).all():
            self._reject("VWAP", "price chopping sideways on VWAP (book: stay away)")
            return None
        held_above = (last3["Close"] > last3["vwap"]).all()
        touched_from_above = (last3["Low"] <= last3["vwap"] + self.tol).any()
        held_below = (last3["Close"] < last3["vwap"]).all()
        touched_from_below = (last3["High"] >= last3["vwap"] - self.tol).any()
        dist = abs(self.price - self.vwap)
        if dist > 0.5 * self.atr:            # "buy as close as possible to VWAP"
            return None
        if held_above and touched_from_above:
            stop = self.vwap - self.buffer
            tgt = self._target(+1, self.price, self.tol)
            if not tgt:
                self._reject("VWAP", "no resistance level above for a target"); return None
            return self._mk("VWAP", +1, self.price, stop, tgt.price, "medium",
                            [f"last 3 bars closed above VWAP {self.vwap:.2f} after testing it",
                             f"stop = 5-min close below VWAP (modelled as VWAP-{self.buffer:.2f})",
                             f"target = next level {tgt.label()}"])
        if held_below and touched_from_below:
            stop = self.vwap + self.buffer
            tgt = self._target(-1, self.price, self.tol)
            if not tgt:
                self._reject("VWAP", "no support level below for a target"); return None
            return self._mk("VWAP", -1, self.price, stop, tgt.price, "medium",
                            [f"last 3 bars closed below VWAP {self.vwap:.2f} after failing to break it",
                             f"stop = 5-min close above VWAP (modelled as VWAP+{self.buffer:.2f})",
                             f"target = next level {tgt.label()}"])
        return None

    # ---- Strategy 9: Opening Range Breakout -----------------------------
    def orb_setup(self) -> Optional[StockSignal]:
        n = max(1, self.cfg.orb_minutes // 5)
        d = self.today
        if len(d) <= n:
            return None
        orange = d.iloc[:n]
        or_hi, or_lo = float(orange["High"].max()), float(orange["Low"].min())
        if (or_hi - or_lo) > self.cfg.orb_max_range_atr * self.atr:
            self._reject("ORB", f"opening range {or_hi - or_lo:.2f} not significantly smaller than ATR {self.atr:.2f}")
            return None
        after = d.iloc[n:]
        # find the breakout bar; only act if it is within the last 2 bars (don't chase)
        up = after.index[after["Close"] > or_hi]
        dn = after.index[after["Close"] < or_lo]
        for direction, idx, lvl in ((+1, up, or_hi), (-1, dn, or_lo)):
            if len(idx) == 0:
                continue
            first = idx[0]
            bars_since = len(after.loc[first:]) - 1
            if bars_since > 2:
                if bars_since <= 6:   # only worth mentioning while it is still "recent"
                    self._reject("ORB", f"breakout {'up' if direction>0 else 'down'} happened {bars_since} bars ago (chasing)")
                continue
            if abs(self.price - lvl) > 0.5 * self.atr:
                self._reject("ORB", "price already extended > 0.5 ATR from the range"); continue
            if direction > 0 and self.price <= self.vwap:
                self._reject("ORB", "upside break but price below VWAP"); continue
            if direction < 0 and self.price >= self.vwap:
                self._reject("ORB", "downside break but price above VWAP"); continue
            stop = self.vwap - direction * self.buffer            # book: stop = close through VWAP
            tgt = self._target(direction, self.price, self.tol)
            if not tgt:
                self._reject("ORB", "no technical level for a target"); continue
            return self._mk("ORB", direction, self.price, stop, tgt.price, "medium",
                            [f"{self.cfg.orb_minutes}-min opening range {or_lo:.2f}-{or_hi:.2f} "
                             f"({or_hi-or_lo:.2f} < {self.cfg.orb_max_range_atr}xATR {self.atr:.2f})",
                             f"5-min close {'above' if direction>0 else 'below'} the range, price on the right side of VWAP {self.vwap:.2f}",
                             f"stop = VWAP {'-' if direction>0 else '+'} {self.buffer:.2f}; target = next level {tgt.label()}"])
        return None

    # ---- Strategies 3/4: Reversals -------------------------------------
    def _consecutive_run(self, bearish: bool) -> int:
        """Count consecutive same-colour candles ending at the last *completed* run
        (allow the most recent 1-2 bars to be the turn)."""
        d = self.today
        best = 0
        for end in (len(d) - 1, len(d) - 2, len(d) - 3):
            if end < 0:
                continue
            run = 0
            for i in range(end, -1, -1):
                o, c = float(d["Open"].iloc[i]), float(d["Close"].iloc[i])
                if (c < o) if bearish else (c > o):
                    run += 1
                else:
                    break
            best = max(best, run)
        return best

    def reversal_setup(self, bottom: bool) -> Optional[StockSignal]:
        name = "BottomReversal" if bottom else "TopReversal"
        d = self.today
        if len(d) < self.cfg.min_consecutive_candles + 2:
            return None
        run = self._consecutive_run(bearish=bottom)
        if run < self.cfg.min_consecutive_candles:
            return None
        # RSI at the extreme of the run (min/max of the last run+2 bars)
        window = d.tail(run + 2)
        rsi_ext = float(window["rsi"].min() if bottom else window["rsi"].max())
        if bottom and rsi_ext > self.cfg.rsi_reversal_scan:
            self._reject(name, f"{run} red candles but RSI low {rsi_ext:.0f} > {self.cfg.rsi_reversal_scan}"); return None
        if not bottom and rsi_ext < 100 - self.cfg.rsi_reversal_scan:
            self._reject(name, f"{run} green candles but RSI high {rsi_ext:.0f} < {100-self.cfg.rsi_reversal_scan}"); return None
        # "extreme": the run must be a real stretch, not a slow drift
        run_move = abs(float(window["Close"].iloc[-1]) - float(window["Open"].iloc[0]))
        ext = float(window["Low"].min()) if bottom else float(window["High"].max())
        if run_move < 0.5 * self.atr:
            self._reject(name, f"run only moved {run_move:.2f} (< 0.5 ATR); book wants extremes, not slow drifts"); return None
        # near a significant level ("area", not an exact number): allow a wick overshoot of ~0.3 ATR
        lvl = near_level(self.levels, ext, max(2 * self.tol, 0.3 * self.atr))
        if not lvl:
            self._reject(name, f"run extreme {ext:.2f} not near a daily support/resistance level"); return None
        # confirmation candle: indecision, or a strong candle in the reversal direction
        last2 = d.tail(2)
        ctypes = [candle_type(*[float(x) for x in r[["Open", "High", "Low", "Close"]]]) for _, r in last2.iterrows()]
        want_strong = "bull_strong" if bottom else "bear_strong"
        confirmed = any(ct in INDECISION or ct == want_strong for ct in ctypes)
        if not confirmed:
            self._reject(name, f"no indecision/strong reversal candle yet (last two: {ctypes})"); return None
        # entry trigger: first new 5-min high (bottom) / low (top)
        prev, cur = d.iloc[-2], d.iloc[-1]
        trig = (cur["High"] > prev["High"]) if bottom else (cur["Low"] < prev["Low"])
        if not trig:
            self._reject(name, "waiting for the first new 5-min high/low (entry trigger)"); return None
        # volume at the reversal point should be elevated
        vol_ok = float(window["Volume"].max()) >= 1.5 * self.med_vol if self.med_vol else True
        direction = +1 if bottom else -1
        stop = (self.day_low - self.buffer) if bottom else (self.day_high + self.buffer)  # book: low/high of day
        risk = abs(self.price - stop)
        # Book's exits: "(1) the next level, or (2) VWAP or 9 EMA or 20 EMA (whichever is closer)".
        # Walk them nearest-first and take the first that still gives 2:1; say what was skipped.
        cands = [(p, n) for p, n in ((self.vwap, "VWAP"), (self.ema9, "9EMA"), (self.ema20, "20EMA"))
                 if np.isfinite(p) and (p - self.price) * direction > self.tol]
        nxt = self._target(direction, self.price, self.tol)
        if nxt:
            cands.append((nxt.price, nxt.label()))
        cands.sort(key=lambda x: abs(x[0] - self.price))
        if not cands:
            self._reject(name, "no MA or level to target"); return None
        skipped, chosen = [], None
        for p, n in cands:
            if abs(p - self.price) / risk >= self.cfg.min_reward_risk:
                chosen = (p, n); break
            skipped.append(f"{n} {p:.2f}")
        if not chosen:
            self._reject(name, f"no book target gives {self.cfg.min_reward_risk}:1 vs stop {stop:.2f} (targets: {', '.join(skipped)})")
            return None
        tgt_price, tgt_name = chosen
        conf = "high" if (bottom and rsi_ext <= self.cfg.rsi_reversal_extreme) or \
                         (not bottom and rsi_ext >= 100 - self.cfg.rsi_reversal_extreme) else "medium"
        if not vol_ok or skipped:
            conf = "low" if not vol_ok else "medium"
        why = [f"{run} consecutive {'red' if bottom else 'green'} 5-min candles, RSI {'low' if bottom else 'high'} {rsi_ext:.0f}",
               f"run extreme {ext:.2f} at level {lvl.label()}; confirmation candles {ctypes}",
               f"entry = first new 5-min {'high' if bottom else 'low'}; stop = {'low' if bottom else 'high'} of day {'-' if bottom else '+'} buffer",
               f"target = {tgt_name}" + (f" (nearer targets {', '.join(skipped)} skipped: < 2:1)" if skipped else " (closest book target)"),
               "volume at reversal " + ("elevated" if vol_ok else "NOT elevated (book wants high volume)")]
        return self._mk(name, direction, self.price, stop, tgt_price, conf, why, rsi=round(rsi_ext, 1), run=run)

    # ---- Strategy 5: Moving Average trend ---------------------------------
    def ma_trend_setup(self) -> Optional[StockSignal]:
        d = self.today
        if len(d) < 8:
            return None
        last6 = d.tail(6)
        ema = last6["ema9"]
        rising = float(ema.iloc[-1]) > float(ema.iloc[0])
        above = (last6["Close"] > ema).all()
        below = (last6["Close"] < ema).all()
        cur = d.iloc[-1]
        near = 0.35 * self.atr   # "buy as close as possible to the moving average line (to have a small stop)"
        if above and rising and not (float(cur["Low"]) <= self.ema9 + self.tol and self.price - self.ema9 <= near):
            self._reject("MATrend", f"uptrend above 9 EMA {self.ema9:.2f} but no pullback to it yet (price {self.price:.2f})")
            return None
        if below and (not rising) and not (float(cur["High"]) >= self.ema9 - self.tol and self.ema9 - self.price <= near):
            self._reject("MATrend", f"downtrend below 9 EMA {self.ema9:.2f} but no rally into it yet (price {self.price:.2f})")
            return None
        if above and rising and float(cur["Low"]) <= self.ema9 + self.tol and self.price - self.ema9 <= near:
            stop = self.ema9 - self.buffer                      # "5 to 10 cents below the moving average"
            tgt = self._target(+1, self.price, self.tol)
            if not tgt:
                self._reject("MATrend", "no level above to target"); return None
            return self._mk("MATrend", +1, self.price, stop, tgt.price, "medium",
                            [f"6 bars closed above a rising 9 EMA ({self.ema9:.2f}); this bar pulled back to it and held",
                             f"stop = 9 EMA - {self.buffer:.2f}; target = next level {tgt.label()} (book rides until MA breaks)"])
        if below and (not rising) and float(cur["High"]) >= self.ema9 - self.tol and self.ema9 - self.price <= near:
            stop = self.ema9 + self.buffer
            tgt = self._target(-1, self.price, self.tol)
            if not tgt:
                self._reject("MATrend", "no level below to target"); return None
            return self._mk("MATrend", -1, self.price, stop, tgt.price, "medium",
                            [f"6 bars closed below a falling 9 EMA ({self.ema9:.2f}); this bar rallied into it and was rejected",
                             f"stop = 9 EMA + {self.buffer:.2f}; target = next level {tgt.label()}"])
        return None

    # ---- Strategy 7: Support / Resistance --------------------------------
    def sr_setup(self) -> Optional[StockSignal]:
        d = self.today
        cur = d.iloc[-1]
        ct = candle_type(*[float(cur[k]) for k in ("Open", "High", "Low", "Close")])
        if ct not in INDECISION:
            return None
        sup = near_level(self.levels, float(cur["Low"]), self.tol)
        res = near_level(self.levels, float(cur["High"]), self.tol)
        vol_ok = float(cur["Volume"]) >= 1.2 * self.med_vol if self.med_vol else True
        if sup and self.price > sup.price:
            stop = sup.price - self.buffer
            tgt = self._target(+1, self.price, self.tol)
            if not tgt:
                self._reject("SupportResistance", "no next level above"); return None
            return self._mk("SupportResistance", +1, self.price, stop, tgt.price, "medium" if vol_ok else "low",
                            [f"indecision candle ({ct}) at support {sup.label()}, closed above it",
                             f"stop = 5-min close below the level (modelled {stop:.2f}); target = next level {tgt.label()}",
                             "volume " + ("confirms" if vol_ok else "does not confirm")])
        if res and self.price < res.price:
            stop = res.price + self.buffer
            tgt = self._target(-1, self.price, self.tol)
            if not tgt:
                self._reject("SupportResistance", "no next level below"); return None
            return self._mk("SupportResistance", -1, self.price, stop, tgt.price, "medium" if vol_ok else "low",
                            [f"indecision candle ({ct}) at resistance {res.label()}, closed below it",
                             f"stop = 5-min close above the level (modelled {stop:.2f}); target = next level {tgt.label()}",
                             "volume " + ("confirms" if vol_ok else "does not confirm")])
        return None

    # ---- Strategy 8: Red-to-Green / Green-to-Red --------------------------
    def red_to_green_setup(self) -> Optional[StockSignal]:
        d = self.today
        if len(d) < 4 or not np.isfinite(self.prev_close):
            return None
        last3 = d.tail(3)
        rising_vol = float(last3["Volume"].iloc[-1]) > float(last3["Volume"].iloc[0])
        gap_down = float(d["Open"].iloc[0]) < self.prev_close
        gap_up = float(d["Open"].iloc[0]) > self.prev_close
        dist = self.prev_close - self.price
        if gap_down and dist > 0 and self.price > self.vwap and (last3["Close"].diff().dropna() > 0).all() and rising_vol:
            stop = self.vwap - self.buffer
            return self._mk("RedToGreen", +1, self.price, stop, self.prev_close, "medium",
                            [f"gapped down, now above VWAP {self.vwap:.2f} and rising on rising volume toward prev close {self.prev_close:.2f}",
                             "stop = break of VWAP; target = previous day close (book: 'should work immediately')"])
        if gap_up and dist < 0 and self.price < self.vwap and (last3["Close"].diff().dropna() < 0).all() and rising_vol:
            stop = self.vwap + self.buffer
            return self._mk("RedToGreen", -1, self.price, stop, self.prev_close, "medium",
                            [f"gapped up, now below VWAP {self.vwap:.2f} and falling on rising volume toward prev close {self.prev_close:.2f}",
                             "stop = break of VWAP; target = previous day close (Green-to-Red)"])
        return None

    # ---- Strategy 1: ABCD ------------------------------------------------
    def abcd_setup(self) -> Optional[StockSignal]:
        d = self.today
        if len(d) < 6:
            return None
        highs, lows, closes = d["High"].values, d["Low"].values, d["Close"].values
        b_i = int(np.argmax(highs))
        if b_i < 2 or b_i >= len(d) - 2:
            return None
        a_i = int(np.argmin(lows[:b_i]))
        A, B = float(lows[a_i]), float(highs[b_i])
        if (B - A) < max(0.75 * self.atr, 0.02 * A):
            return None
        after = d.iloc[b_i + 1:]
        c_i_rel = int(np.argmin(after["Low"].values))
        C = float(after["Low"].values[c_i_rel])
        retrace = (B - C) / (B - A)
        if not (0.2 <= retrace <= 0.7) or C <= A:
            self._reject("ABCD", f"pullback retrace {retrace:.0%} outside 20-70% or C below A"); return None
        held = after.iloc[c_i_rel:]
        if len(held) < 2 or (held["Low"] < C - self.tol).any():
            return None
        if not (self.price > float(d["Close"].iloc[-2]) and self.price < B and self.price - C <= 0.5 * (B - C)):
            self._reject("ABCD", "not close enough to C / already back near B"); return None
        stop = C - self.buffer
        return self._mk("ABCD", +1, self.price, stop, B, "medium",
                        [f"A {A:.2f} -> B {B:.2f} (+{B-A:.2f}), pullback held at C {C:.2f} ({retrace:.0%} retrace) for {len(held)} bars",
                         f"entry near C; stop = loss of C; first target = B (book sells half at D, trails the rest)",
                         f"measured-move D = {C + (B-A):.2f}  [NOT FROM BOOK: projection only]"],
                        A=A, B=B, C=C)

    # ---- Strategy 2: Bull Flag momentum -----------------------------------
    def bull_flag_setup(self) -> Optional[StockSignal]:
        d = self.today
        if len(d) < 6:
            return None
        cur = d.iloc[-1]
        highs = d["High"].values
        # pole: >= 3 bars ending within the last 9 bars, moving >= max(2%, 0.5 ATR); the pole's last
        # bar must be its high point and the flag must stay under it. Collect all valid (pole, flag)
        # pairs and keep the biggest pole so a flag bar is never mistaken for the pole end.
        cands = []
        for end in range(len(d) - 3, max(len(d) - 10, 1), -1):
            for start in range(end - 2, max(end - 6, -1), -1):
                pole = d.iloc[start:end + 1]
                move = float(pole["Close"].iloc[-1]) - float(pole["Open"].iloc[0])
                if move < max(0.5 * self.atr, 0.02 * float(pole["Open"].iloc[0])):
                    continue
                if (pole["Close"] > pole["Open"]).sum() < 0.6 * len(pole):
                    continue
                if highs[end] < highs[start:end + 1].max():
                    continue
                flag = d.iloc[end + 1:-1]
                if not (2 <= len(flag) <= 6):
                    continue
                f_hi, f_lo = float(flag["High"].max()), float(flag["Low"].min())
                if f_hi > highs[end] or (f_hi - f_lo) > 0.5 * move or f_lo < float(pole["Open"].iloc[0]) + 0.5 * move:
                    continue
                cands.append((move, f_hi, f_lo, len(flag), float(flag["Volume"].mean())))
        if not cands:
            return None
        move, f_hi, f_lo, n_flag, flag_vol = max(cands)
        if float(cur["Close"]) > f_hi and float(cur["Volume"]) > flag_vol:
            stop = f_lo - self.buffer
            tgt = self.price + move                              # [NOT FROM BOOK] flag-pole projection
            return self._mk("BullFlag", +1, self.price, stop, tgt, "low",
                            [f"pole +{move:.2f} then {n_flag}-bar consolidation {f_lo:.2f}-{f_hi:.2f}; breakout bar on volume",
                             "stop = break below consolidation; book sells half on the way up, trails the rest",
                             "LOW confidence: book says this needs real-time 1-min data + hotkeys; Yahoo data is delayed"])
        self._reject("BullFlag", "flag formed but no breakout above consolidation high yet")
        return None

    def run_all(self) -> list[StockSignal]:
        out = []
        for fn in (self.vwap_setup, self.orb_setup, lambda: self.reversal_setup(True),
                   lambda: self.reversal_setup(False), self.ma_trend_setup, self.sr_setup,
                   self.red_to_green_setup, self.abcd_setup, self.bull_flag_setup):
            try:
                s = fn()
            except Exception as e:  # keep scanning other setups
                self._reject("internal", f"{getattr(fn, '__name__', 'setup')} error: {e}")
                s = None
            if s:
                out.append(s)
        return out


def merge_confluent(sigs: list[StockSignal]) -> list[StockSignal]:
    """Two strategies that produce the *same* plan (direction, stop, target within a few cents)
    are one trade with two reasons, not two trades. Merge them and keep the higher confidence."""
    rank = {"high": 2, "medium": 1, "low": 0}
    out: list[StockSignal] = []
    for s in sigs:
        for m in out:
            if m.direction == s.direction and abs(m.stop - s.stop) <= 0.03 * max(1.0, m.entry) / 10 + 0.02 \
                    and abs(m.target - s.target) <= 0.02 + 0.001 * m.entry:
                m.strategy = f"{m.strategy}+{s.strategy}"
                m.rationale = m.rationale + [f"-- confluence with {s.strategy}:"] + s.rationale
                if rank[s.confidence] > rank[m.confidence]:
                    m.confidence = s.confidence
                break
        else:
            out.append(s)
    return out


# ===========================================================================
# STOCK-IN-PLAY ASSESSMENT (Ch. 4)
# ===========================================================================
@dataclass
class StockContext:
    ticker: str
    price: float
    prev_close: float
    gap_pct: float
    premarket_vol: float
    avg_daily_vol: float
    rel_vol: float
    atr: float
    float_shares: float
    short_pct_float: float
    float_category: str
    catalyst: str
    catalyst_grade: str
    catalyst_kind: str
    sector: str
    excess_vs_sector: float
    excess_vs_spy: float
    headlines: list[str]
    in_play: bool
    in_play_reasons: list[str]
    warnings: list[str]
    levels: list[str]
    bar_time: str
    time_bucket: str


def relative_volume(reg: pd.DataFrame, today: pd.DataFrame, avg_daily_vol: float) -> float:
    """Today's cumulative volume vs the average cumulative volume at the same
    time-of-day over the prior sessions in the feed (better than a linear pro-rata)."""
    t_last = today.index[-1].time()
    cum_today = float(today["Volume"].sum())
    prior = reg[reg["date"] != today["date"].iloc[-1]]
    if prior["date"].nunique() >= 3:
        same = prior[[ts.time() <= t_last for ts in prior.index]]
        per_day = same.groupby("date")["Volume"].sum()
        base = float(per_day.mean()) if len(per_day) else np.nan
        if np.isfinite(base) and base > 0:
            return cum_today / base
    # fallback: linear pro-rata of average daily volume
    mins = (today.index[-1] - today.index[0]).total_seconds() / 60 + 5
    frac = min(1.0, max(mins, 5) / 390)
    return cum_today / (avg_daily_vol * frac) if avg_daily_vol else np.nan


def float_category(float_shares: float, price: float) -> str:
    if not np.isfinite(float_shares):
        return "unknown"
    if float_shares < 5e6:
        return "low (<5M): book = Bull Flag momentum only, long only"
    if float_shares <= 500e6:
        return "medium (5-500M): book = all strategies, mostly VWAP & S/R"
    return "large (>500M): book = mostly Moving Average & Reversal, needs a catalyst"


def assess_stock(t: str, daily: pd.DataFrame, intra: pd.DataFrame, ref: dict, source, cfg: Config,
                 now: datetime, replay_date: Optional[str], replay_time: Optional[str] = None
                 ) -> Optional[tuple[StockContext, pd.DataFrame, list[Level], float]]:
    """ref: dict from DataSource.reference(); catalyst grading uses source.earnings_catalyst / news_items /
    sector / market_context (see catalyst.py)."""
    reg = add_session_indicators(intra, cfg)
    if reg.empty:
        return None
    if replay_date:
        rd = pd.Timestamp(replay_date).date()
        cutoff = pd.Timestamp(replay_time or "16:00").time()
        keep = [(ts.date() < rd) or (ts.date() == rd and ts.time() <= cutoff) for ts in reg.index]
        reg = reg[keep]
        intra = intra[[(ts.date() < rd) or (ts.date() == rd and ts.time() <= cutoff) for ts in intra.index]]
        daily = daily[[ts.date() < rd for ts in daily.index]]
        if reg.empty or reg["date"].iloc[-1] != rd:
            return None
    sess_date = reg["date"].iloc[-1]
    today = reg[reg["date"] == sess_date]
    pre_today = intra[(intra["premarket"] if "premarket" in intra else
                       [(ts.date() == sess_date and ts.time() < pd.Timestamp("09:30").time()) for ts in intra.index])]
    if daily.empty or len(daily) < cfg.atr_period + 2:
        return None
    # previous close = last daily close strictly before the session date
    prior_daily = daily[[ts.date() < sess_date for ts in daily.index]]
    if prior_daily.empty:
        return None
    prev_close = float(prior_daily["Close"].iloc[-1])
    atr_val = atr(prior_daily, cfg.atr_period)
    price = float(today["Close"].iloc[-1])
    open_px = float(today["Open"].iloc[0])
    gap_pct = (open_px - prev_close) / prev_close * 100.0
    # Unusual Whales pre-market bars carry real consolidated volume. Yahoo's carry 0 (verified on
    # both the 1m and 5m endpoints), so with Yahoo the book's "50,000 shares pre-market" criterion
    # cannot be evaluated: report NaN, never a fake 0.
    pm_vol = float(pre_today["Volume"].sum()) if len(pre_today) and (pre_today["Volume"] > 0).any() else float("nan")
    # average daily volume from the daily bars themselves (consolidated on both sources)
    avg_vol = _to_float(prior_daily["Volume"].tail(30).mean(), _to_float(ref.get("avg_daily_vol")))
    rel_vol = relative_volume(reg, today, avg_vol)
    flt = _to_float(ref.get("float_shares"))
    short_pct = _to_float(ref.get("short_pct_float"))
    levels = all_levels(prior_daily, today, pre_today, prev_close, price, atr_val, cfg)

    reasons, warns = [], []
    gap_ok = abs(gap_pct) >= cfg.min_gap_pct
    radar_ok = (np.isfinite(rel_vol) and rel_vol >= cfg.min_rel_vol) and abs(open_px - prev_close) >= cfg.min_gap_dollars
    if gap_ok:
        reasons.append(f"gap {gap_pct:+.1f}% (>= {cfg.min_gap_pct}%)")
    if radar_ok:
        reasons.append(f"relative volume {rel_vol:.1f}x (>= {cfg.min_rel_vol}x) and gap >= ${cfg.min_gap_dollars}")
    if not (gap_ok or radar_ok):
        warns.append(f"NOT in play: gap {gap_pct:+.1f}%, rel vol {rel_vol:.1f}x")
    if not np.isfinite(pm_vol):
        warns.append(f"pre-market volume UNAVAILABLE from Yahoo (book wants >= {cfg.min_premarket_vol:,}; check your broker/scanner)")
    elif pm_vol < cfg.min_premarket_vol:
        warns.append(f"pre-market volume {pm_vol:,.0f} < {cfg.min_premarket_vol:,} (Gappers criterion)")
    if avg_vol < cfg.min_avg_daily_vol:
        warns.append(f"avg daily volume {avg_vol:,.0f} < {cfg.min_avg_daily_vol:,}")
    if not np.isfinite(atr_val) or atr_val < cfg.min_atr:
        warns.append(f"ATR {atr_val:.2f} < ${cfg.min_atr:.2f}")
    if np.isfinite(short_pct) and short_pct > cfg.max_short_pct_float:
        warns.append(f"short interest {short_pct:.0f}% of float > {cfg.max_short_pct_float:.0f}% (squeeze risk)")
    if price < 10:
        warns.append("price < $10: book allows only Bull Flag momentum here; options are usually illiquid")
    if price > 100:
        warns.append("price > $100: outside the book's preferred range (share-count reason; less relevant for options)")
    # ---- catalyst grade (book Ch. 4 list + Rule 4 independence test) ----
    cat_now = now if not replay_date else datetime.combine(sess_date, datetime.min.time(), NY) + timedelta(hours=16)
    stock_pct = (price / prev_close - 1.0) * 100.0
    mkt = source.market_context()
    sector = source.sector(t) or ""
    etf = catmod.sector_etf(sector)
    spy_pct = mkt.get("SPY", 0.0)
    sector_pct = mkt.get(etf) if etf else None
    cat = catmod.grade(t, source.earnings_catalyst(t, cat_now), source.news_items(t), stock_pct, spy_pct, sector_pct,
                       sector, rel_vol if np.isfinite(rel_vol) else 0.0, cat_now)
    nxt = source.next_earnings(t, cat_now)
    cat_text = cat.label() + (f" | next earnings {nxt}" if nxt and cat.kind != "earnings" else "")
    if cat.grade == "F":
        warns.append("Rule 4: moving with its sector/market, no catalyst of its own -> not a Stock in Play")
    elif cat.grade == "D":
        warns.append("no catalyst identified (book: check the news yourself)")
    hard_fail = (not (gap_ok or radar_ok)) or avg_vol < cfg.min_avg_daily_vol or \
                (np.isfinite(atr_val) and atr_val < cfg.min_atr) or \
                (np.isfinite(short_pct) and short_pct > cfg.max_short_pct_float) or cat.grade == "F"
    ctx = StockContext(
        ticker=t, price=round(price, 2), prev_close=round(prev_close, 2), gap_pct=round(gap_pct, 2),
        premarket_vol=pm_vol, avg_daily_vol=avg_vol, rel_vol=round(rel_vol, 2) if np.isfinite(rel_vol) else float("nan"),
        atr=round(atr_val, 2) if np.isfinite(atr_val) else float("nan"), float_shares=flt,
        short_pct_float=round(short_pct, 1) if np.isfinite(short_pct) else float("nan"),
        float_category=float_category(flt, price), catalyst=cat_text, catalyst_grade=cat.grade, catalyst_kind=cat.kind,
        sector=sector, excess_vs_sector=round(cat.excess_vs_sector, 2), excess_vs_spy=round(cat.excess_vs_spy, 2), headlines=[],
        in_play=not hard_fail, in_play_reasons=reasons, warnings=warns,
        levels=[L.label() for L in levels], bar_time=today.index[-1].strftime("%Y-%m-%d %H:%M"),
        time_bucket=time_bucket(today.index[-1]),
    )
    return ctx, reg, levels, atr_val


# ===========================================================================
# OPTIONS LAYER  [NOT FROM BOOK]
# ===========================================================================
def _ncdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs_price_delta(S: float, K: float, T: float, r: float, sigma: float, call: bool) -> tuple[float, float]:
    if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        intrinsic = max(0.0, (S - K) if call else (K - S))
        return intrinsic, (1.0 if (call and S > K) else (-1.0 if (not call and S < K) else 0.0))
    d1 = (math.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    if call:
        return S * _ncdf(d1) - K * math.exp(-r * T) * _ncdf(d2), _ncdf(d1)
    return K * math.exp(-r * T) * _ncdf(-d2) - S * _ncdf(-d1), _ncdf(d1) - 1.0


@dataclass
class OptionPlan:
    contract: str
    expiry: str
    dte: int
    kind: str                 # CALL / PUT
    strike: float
    bid: float
    ask: float
    mid: float
    spread_pct: float
    iv: float
    delta: float
    volume: int
    open_interest: int
    est_value_at_target: float
    est_value_at_stop: float
    risk_per_contract: float  # planned loss if the stock stop is honoured
    reward_per_contract: float
    option_rr: float
    contracts: int
    total_planned_risk: float
    total_premium: float      # max loss if you cannot exit at the stop
    notes: list[str]
    actionable: bool = False  # liquid + >=1 contract + option-level R:R passes the book's 2:1


def pick_option(sig: StockSignal, S: float, cfg: Config, now: datetime, log, source) -> Optional[OptionPlan]:
    call = sig.direction == "LONG"
    try:
        chain = source.option_chain(sig.ticker, call, cfg.min_dte, cfg.max_dte)
    except DataError as e:
        log(f"    {sig.ticker}: option chain unavailable ({e})")
        return None
    if not chain:
        log(f"    {sig.ticker}: no {'calls' if call else 'puts'} with {cfg.min_dte}-{cfg.max_dte} DTE")
        return None
    today = now.date()
    # nearest expiry inside the DTE window (the whole chain is already limited to that window)
    dte = min(c["dte"] for c in chain)
    expiry = next(c["expiry"] for c in chain if c["dte"] == dte)
    exp_dt = datetime.combine(datetime.strptime(expiry, "%Y-%m-%d").date(), datetime.min.time(), NY) + timedelta(hours=16)
    T = max((exp_dt - now).total_seconds() / (365.0 * 24 * 3600), 1e-4)
    # same-day exit assumption: value at target/stop with today's remaining session decayed
    close_today = datetime.combine(today, datetime.min.time(), NY) + timedelta(hours=16)
    T_exit = max((exp_dt - max(close_today, now)).total_seconds() / (365.0 * 24 * 3600), 1e-4)

    rows = []
    for c in chain:
        if c["expiry"] != expiry:
            continue
        K, bid, ask, iv = c["strike"], c["bid"], c["ask"], c["iv"]
        # sanity: two-sided quote, plausible IV, strike within 25% of spot (chains carry stale
        # deep-ITM rows with absurd IVs that would otherwise win the delta search)
        if not np.isfinite(K) or ask <= 0 or bid <= 0 or not np.isfinite(iv) or not (0.01 < iv < 3.0):
            continue
        if abs(K - S) / S > 0.25:
            continue
        mid = (bid + ask) / 2
        spread_pct = (ask - bid) / mid * 100
        delta = c.get("delta")
        if delta is None or not np.isfinite(delta):          # Yahoo has no greeks -> Black-Scholes
            _, delta = bs_price_delta(S, K, T, cfg.risk_free_rate, iv, call)
        rows.append(dict(K=K, bid=bid, ask=ask, mid=mid, spread_pct=spread_pct, iv=iv,
                         delta=float(delta), oi=c["oi"], vol=c["vol"], sym=c["sym"]))
    if not rows:
        return None
    liquid = [x for x in rows if x["oi"] >= cfg.min_open_interest and x["vol"] >= cfg.min_option_volume
              and x["spread_pct"] <= cfg.max_spread_pct]
    notes = []
    pool = liquid
    if not pool:
        # show the best *quoted* contract so the user can see why it is unusable, but only from
        # strikes with a real two-sided market and a spread that is not absurd
        pool = [x for x in rows if x["spread_pct"] <= 3 * cfg.max_spread_pct]
        if not pool:
            log(f"    {sig.ticker}: no contract with a sane two-sided quote near the money")
            return None
        notes.append(f"NO contract met liquidity filters (OI>={cfg.min_open_interest}, vol>={cfg.min_option_volume}, "
                     f"spread<={cfg.max_spread_pct}%); showing best available -- probably not tradeable intraday")
    best = min(pool, key=lambda x: abs(abs(x["delta"]) - cfg.target_delta))
    K, iv = best["K"], best["iv"]
    if abs(abs(best["delta"]) - cfg.target_delta) > 0.15:
        notes.append(f"closest *liquid* strike has delta {best['delta']:.2f}, far from the {cfg.target_delta} target; "
                     f"strikes nearer the money failed the OI/volume/spread filters")
    # Re-price at the stock target and stop, same IV, same-day exit. Sell at (mid - half spread) = bid-equivalent.
    half = (best["ask"] - best["bid"]) / 2
    v_t, _ = bs_price_delta(sig.target, K, T_exit, cfg.risk_free_rate, iv, call)
    v_s, _ = bs_price_delta(sig.stop, K, T_exit, cfg.risk_free_rate, iv, call)
    exit_t = max(v_t - half, 0.0); exit_s = max(v_s - half, 0.0)
    entry = best["ask"]
    risk_pc = max(entry - exit_s, 0.01) * 100
    reward_pc = max(exit_t - entry, 0.0) * 100
    orr = reward_pc / risk_pc if risk_pc > 0 else 0.0
    max_risk = cfg.account_size * cfg.risk_pct / 100.0
    contracts = int(max_risk // risk_pc) if risk_pc > 0 else 0
    prem_cap = cfg.account_size * cfg.max_premium_pct_account / 100.0
    if contracts * entry * 100 > prem_cap:
        contracts = int(prem_cap // (entry * 100))
        notes.append(f"contracts capped so total premium <= {cfg.max_premium_pct_account}% of account")
    if contracts == 0:
        notes.append("0 contracts: one contract's planned risk already exceeds your per-trade risk budget")
    if dte <= 1:
        notes.append("<=1 DTE: gamma/theta are extreme; the delta-based estimates below are rough")
    # How much of the planned stock-equivalent risk is eaten by the bid/ask spread alone?
    stock_equiv_risk = abs(best["delta"]) * sig.risk * 100
    spread_cost = (best["ask"] - best["bid"]) * 100
    if stock_equiv_risk > 0:
        pct = spread_cost / stock_equiv_risk * 100
        notes.append(f"bid/ask spread costs ${spread_cost:.0f}/contract = {pct:.0f}% of the delta-equivalent stock risk "
                     f"(${stock_equiv_risk:.0f}). Tight book stops + wide option spreads is the core problem.")
    if orr < cfg.min_reward_risk:
        notes.append(f"option-level reward:risk {orr:.2f} < {cfg.min_reward_risk} after spread/theta -- the stock setup "
                     f"passed 2:1 but the option does not")
    is_liquid = not any(n.startswith("NO contract") for n in notes)
    return OptionPlan(
        actionable=bool(is_liquid and contracts >= 1 and orr >= cfg.min_reward_risk),
        contract=best["sym"], expiry=expiry, dte=dte, kind="CALL" if call else "PUT", strike=K,
        bid=round(best["bid"], 2), ask=round(best["ask"], 2), mid=round(best["mid"], 2),
        spread_pct=round(best["spread_pct"], 1), iv=round(iv * 100, 1), delta=round(best["delta"], 2),
        volume=best["vol"], open_interest=best["oi"],
        est_value_at_target=round(exit_t, 2), est_value_at_stop=round(exit_s, 2),
        risk_per_contract=round(risk_pc, 2), reward_per_contract=round(reward_pc, 2), option_rr=round(orr, 2),
        contracts=contracts, total_planned_risk=round(contracts * risk_pc, 2),
        total_premium=round(contracts * entry * 100, 2), notes=notes,
    )


# ===========================================================================
# SCORING / OUTPUT
# ===========================================================================
CONF_W = {"high": 1.0, "medium": 0.7, "low": 0.4}


def score(ctx: StockContext, sig: StockSignal, opt: Optional[OptionPlan]) -> float:
    """Heuristic ranking only. Not a probability. Not backtested."""
    s = 0.0
    s += min(sig.rr, 5.0) * 10                       # stock R:R (book's #1 criterion)
    s += CONF_W[sig.confidence] * 20
    s += 10 if ctx.in_play else -30
    s += {"A": 15, "B": 8, "C": 0, "D": -5, "F": -30}.get(ctx.catalyst_grade, 0)
    s += min(ctx.rel_vol, 5.0) * 3 if np.isfinite(ctx.rel_vol) else 0
    s -= 5 * len(ctx.warnings)
    if sig.time_bucket == "midday":
        s -= 8                                       # book: worst time of day
    if opt:
        s += min(opt.option_rr, 5.0) * 6
        if not opt.actionable:
            s -= 60          # a plan you cannot actually execute must never outrank one you can
    return round(s, 1)


def fmt_money(x: float) -> str:
    return f"${x:,.2f}"


def alert_line(sig: StockSignal, opt: OptionPlan) -> str:
    """Compact one-liner, e.g. 'GME 9/18 $22 Puts at 1.16 x10 | stock 21.07 -> 20.39, stop 21.13 | Green-to-Red'"""
    m, d = int(opt.expiry[5:7]), int(opt.expiry[8:10])
    kind = "Calls" if opt.kind == "CALL" else "Puts"
    size = f" x{opt.contracts}" if opt.contracts else " (0 contracts fit the risk budget)"
    return (f"{sig.ticker} {m}/{d} ${opt.strike:g} {kind} at {opt.ask:.2f}{size} | "
            f"stock {sig.entry:.2f} -> {sig.target:.2f}, stop {sig.stop:.2f} | {sig.strategy} {sig.direction}")


def print_report(results: list[dict], rejected: list[dict], cfg: Config, now: datetime, show_rejected: bool,
                 source_name: str = "yahoo"):
    line = "=" * 100
    print(line)
    print(f"AZIZ-RULES OPTIONS SCANNER   run {now:%Y-%m-%d %H:%M %Z}   account {fmt_money(cfg.account_size)}  "
          f"risk/trade {cfg.risk_pct}% = {fmt_money(cfg.account_size*cfg.risk_pct/100)}   min R:R {cfg.min_reward_risk}:1   "
          f"data: {source_name}")
    print(line)
    if not results:
        print("No setups passed the book's gates on this run. That is the normal outcome most of the day --")
        print("the author takes 2-3 trades a day and says 'if day trading is not boring, you are overtrading'.")
    else:
        n_act = sum(1 for r in results if r["option"] and r["option"].actionable)
        print(f"{len(results)} stock setup(s) passed the book's gates; {n_act} translate into an option plan that also "
              f"passes 2:1 and fits the risk budget.")
    dirs: dict[str, set] = {}
    for r in results:
        dirs.setdefault(r["signal"].ticker, set()).add(r["signal"].direction)
    for i, r in enumerate(results, 1):
        ctx, sig, opt = r["ctx"], r["signal"], r["option"]
        tag = "ACTIONABLE" if (opt and opt.actionable) else "not actionable as an option"
        print(f"\n#{i}  {sig.ticker}  {sig.direction}  {sig.strategy}   score {r['score']}   "
              f"confidence {sig.confidence}   bar {sig.bar_time} ({sig.time_bucket})   [{tag}]")
        if len(dirs.get(sig.ticker, ())) > 1:
            print("    CONFLICT: an opposite-direction setup also fired on this ticker. Book (Ch. 6): when bulls and "
                  "bears are equal, 'wise traders stand aside'.")
        print(f"    STOCK PLAN  entry {sig.entry:.2f}  stop {sig.stop:.2f}  target {sig.target:.2f}  "
              f"risk/sh {sig.risk:.2f}  reward/sh {sig.reward:.2f}  R:R {sig.rr:.2f}:1")
        for why in sig.rationale:
            print(f"      - {why}")
        pmv = f"{ctx.premarket_vol:,.0f}" if np.isfinite(ctx.premarket_vol) else "n/a"
        print(f"    STOCK IN PLAY: {'YES' if ctx.in_play else 'NO'}  gap {ctx.gap_pct:+.1f}%  relvol {ctx.rel_vol:.1f}x  "
              f"ATR {ctx.atr:.2f}  premkt vol {pmv}  avg vol {ctx.avg_daily_vol:,.0f}  "
              f"short% {ctx.short_pct_float}  float {ctx.float_category}")
        print(f"    CATALYST grade {ctx.catalyst_grade}: {ctx.catalyst}")
        print(f"      sector {ctx.sector or 'n/a'}: excess move vs sector {ctx.excess_vs_sector:+.1f} pts, vs SPY {ctx.excess_vs_spy:+.1f} pts")
        for h in ctx.headlines:
            print(f"      news: {h}")
        for w in ctx.warnings:
            print(f"      warn: {w}")
        if opt:
            print(f"    >>> {alert_line(sig, opt)}")
            print(f"    OPTION [not from book]  {opt.contract}  {opt.kind} {opt.strike:g} exp {opt.expiry} ({opt.dte} DTE)  "
                  f"bid/ask {opt.bid}/{opt.ask} (spread {opt.spread_pct}%)  IV {opt.iv}%  delta {opt.delta}  "
                  f"vol {opt.volume}  OI {opt.open_interest}")
            print(f"      est. value if stock hits target: {opt.est_value_at_target}   if stock hits stop: {opt.est_value_at_stop}   "
                  f"option R:R {opt.option_rr}:1")
            print(f"      size: {opt.contracts} contract(s)  planned risk {fmt_money(opt.total_planned_risk)}  "
                  f"MAX LOSS (premium) {fmt_money(opt.total_premium)}")
            for n in opt.notes:
                print(f"      note: {n}")
        else:
            print("    OPTION: no usable chain")
    if show_rejected and rejected:
        print("\n" + "-" * 100)
        print("REJECTED SETUPS (why the rules said no):")
        for rj in rejected:
            print(f"  {rj['ticker']:6s} {rj['strategy']:18s} {rj['why']}")
    print("\n" + line)
    latency = "real-time consolidated (Unusual Whales)" if source_name == "unusualwhales" else "DELAYED ~15 min (Yahoo)"
    print(f"This is a scanner, not advice. Rule 10: indicators indicate, they do not dictate. Data: {latency}.")
    print(line)


def to_jsonable(o):
    """Recursively convert dataclasses / numpy / NaN into strict-JSON-safe values."""
    if isinstance(o, (StockContext, StockSignal, OptionPlan)):
        d = asdict(o)
        if isinstance(o, StockSignal):
            d.update(risk=round(o.risk, 2), reward=round(o.reward, 2), rr=round(o.rr, 2))
        return to_jsonable(d)
    if isinstance(o, dict):
        return {k: to_jsonable(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [to_jsonable(v) for v in o]
    if isinstance(o, (np.floating, np.integer)):
        o = o.item()
    if isinstance(o, float) and not np.isfinite(o):
        return None
    if isinstance(o, (str, int, float, bool)) or o is None:
        return o
    return str(o)


# ===========================================================================
# MAIN
# ===========================================================================
def apply_peer_cluster_rule(contexts: list[StockContext], cfg: Config) -> None:
    """Book, Ch. 4: "If I have a few stocks in one sector [on the scanner], there is a good chance
    that these stocks are not in play. They have high relative volume because their sector is under
    heavy trading by institutional traders."  When >= peer_cluster_min names in the same sector are
    moving >= min_gap_pct the same way today, each of them is a sector move (grade F, not in play)
    unless it has an A-grade catalyst of its own (earnings / guidance / FDA / M&A)."""
    by_sector: dict[tuple[str, int], list[StockContext]] = {}
    for c in contexts:
        if not c.sector or not np.isfinite(c.prev_close) or c.prev_close <= 0:
            continue
        chg = (c.price / c.prev_close - 1.0) * 100.0
        if abs(chg) >= cfg.min_gap_pct:
            by_sector.setdefault((c.sector, 1 if chg > 0 else -1), []).append(c)
    for (sector, sign), members in by_sector.items():
        if len(members) < cfg.peer_cluster_min:
            continue
        names = " ".join(m.ticker for m in members)
        for c in members:
            if c.catalyst_grade == "A":
                c.warnings.append(f"sector cluster ({len(members)} {sector} names {'up' if sign > 0 else 'down'} together: {names}) "
                                  f"but A-grade catalyst of its own -> kept")
                continue
            c.catalyst_grade = "F"
            c.catalyst_kind = "sector-cluster"
            c.catalyst = f"F [sector-cluster] {len(members)} {sector} names {'up' if sign > 0 else 'down'} together today ({names}); " + c.catalyst
            c.in_play = False
            c.warnings.append(f"Rule 4 / Ch. 4: {len(members)} {sector} stocks gapping the same way -> sector move, not a Stock in Play")


def scan_once(cfg: Config, source, tickers: list[str], now: datetime, log, replay: Optional[str] = None,
              replay_time: Optional[str] = None, include_not_in_play: bool = False, with_options: bool = True):
    """Evaluate every ticker on its latest bar. Returns (results, rejected, contexts); results are
    unsorted dicts {ctx, signal, option, score}. Used by main() and by paper_trader.py.
    Two passes: assess every stock first (so the sector-cluster rule can see the whole list), then
    run the setup detectors and the options layer."""
    a = argparse.Namespace(replay=replay, replay_time=replay_time, include_not_in_play=include_not_in_play,
                           no_options=not with_options)
    results, rejected, contexts, assessed_all = [], [], [], []
    for i, t in enumerate(tickers, 1):
        try:
            daily = source.daily_bars(t)
            intra = source.intraday_bars(t, cfg.bar_interval)
        except DataError as e:
            log(f"  [{i}/{len(tickers)}] {t}: no bars ({e})"); continue
        ref = source.reference(t)
        try:
            assessed = assess_stock(t, daily, intra, ref, source, cfg, now, a.replay, a.replay_time)
        except Exception as e:
            log(f"  [{i}/{len(tickers)}] {t}: assessment error: {e}"); continue
        if not assessed:
            log(f"  [{i}/{len(tickers)}] {t}: no session data"); continue
        assessed_all.append((i, t, assessed))
        contexts.append(assessed[0])
        if source.name == "yahoo":
            time.sleep(0.2)  # be polite to Yahoo

    apply_peer_cluster_rule(contexts, cfg)

    for i, t, (ctx, reg, levels, atr_val) in assessed_all:
        det = SetupDetector(t, reg, levels, atr_val, ctx.prev_close, cfg, rejected)
        sigs = merge_confluent(det.run_all())
        tag = "IN PLAY" if ctx.in_play else ("sector move" if ctx.catalyst_kind == "sector-cluster" else "not in play")
        log(f"  [{i}/{len(tickers)}] {t:6s} {ctx.price:8.2f}  gap {ctx.gap_pct:+6.1f}%  relvol {ctx.rel_vol:5.1f}x  "
            f"ATR {ctx.atr:5.2f}  {tag:12s} setups: {', '.join(s.strategy for s in sigs) or '-'}")
        if not sigs:
            continue
        if not ctx.in_play and not a.include_not_in_play:
            for s in sigs:
                rejected.append({"ticker": t, "strategy": s.strategy, "why": "stock fails Stock-in-Play gate: " + "; ".join(ctx.warnings)})
            continue
        ctx.headlines = source.news(t)
        for s in sigs:
            opt = None
            if not a.no_options and not a.replay:
                opt = pick_option(s, ctx.price, cfg, now, log, source)
            results.append({"ctx": ctx, "signal": s, "option": opt, "score": score(ctx, s, opt)})

    return results, rejected, contexts


def main(argv=None):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")   # curly quotes in headlines on Windows consoles
    except Exception:
        pass
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("-t", "--tickers", nargs="*", help="tickers to scan (default: Yahoo screener gappers)")
    p.add_argument("--watchlist", help="text file with one ticker per line")
    p.add_argument("--account", type=float, default=Config.account_size)
    p.add_argument("--risk-pct", type=float, default=Config.risk_pct, help="max %% of account risked per trade (book max 2)")
    p.add_argument("--min-rr", type=float, default=Config.min_reward_risk)
    p.add_argument("--universe-size", type=int, default=Config.universe_size)
    p.add_argument("--max-price", type=float, default=Config.max_price, help="0 = no ceiling")
    p.add_argument("--orb-minutes", type=int, default=Config.orb_minutes, choices=[5, 15, 30, 60])
    p.add_argument("--min-dte", type=int, default=Config.min_dte)
    p.add_argument("--max-dte", type=int, default=Config.max_dte)
    p.add_argument("--target-delta", type=float, default=Config.target_delta)
    p.add_argument("--max-spread", type=float, default=Config.max_spread_pct, help="max option bid/ask spread as %% of mid")
    p.add_argument("--no-midday", action="store_true", help="skip 11:00-15:00 setups (book: most dangerous time)")
    p.add_argument("--include-not-in-play", action="store_true", help="also report setups on stocks that fail the Stock-in-Play gate")
    p.add_argument("--no-options", action="store_true", help="stock-level signals only")
    p.add_argument("--replay", help="YYYY-MM-DD: evaluate setups on a past session (stock only; no historical options)")
    p.add_argument("--replay-time", help="HH:MM ET: evaluate as of this bar on the replay date (default: session close)")
    p.add_argument("--source", choices=["auto", "uw", "yahoo"], default="auto",
                   help="data backend: uw = Unusual Whales (needs UW_API_KEY), yahoo = free delayed fallback, auto = uw if key present")
    p.add_argument("--include-etfs", action="store_true", help="keep ETFs in the screener universe (book: Stocks in Play need a company catalyst)")
    p.add_argument("--show-rejected", action="store_true")
    p.add_argument("--out", default="signals", help="output folder for JSON")
    p.add_argument("-q", "--quiet", action="store_true")
    a = p.parse_args(argv)

    cfg = Config(account_size=a.account, risk_pct=min(a.risk_pct, 2.0), min_reward_risk=a.min_rr,
                 universe_size=a.universe_size, max_price=a.max_price, orb_minutes=a.orb_minutes,
                 min_dte=a.min_dte, max_dte=a.max_dte, target_delta=a.target_delta, max_spread_pct=a.max_spread,
                 include_mid_day=not a.no_midday)
    if a.risk_pct > 2.0:
        print("risk-pct capped at 2% -- the book's absolute maximum.")
    log = (lambda *x: None) if a.quiet else (lambda *x: print(*x, flush=True))
    now = _now_et()
    cfg.include_etfs = a.include_etfs
    try:
        source = make_source(a.source, log)
    except DataError as e:
        print(e); return 1
    log(f"Data source: {source.name}" + ("" if source.name != "yahoo" else "  (delayed ~15 min, no pre-market volume)"))

    # ---- universe ----
    tickers: list[str] = []
    if a.tickers:
        tickers = [t.upper() for t in a.tickers]
    if a.watchlist:
        with open(a.watchlist) as f:
            tickers += [ln.strip().upper() for ln in f if ln.strip() and not ln.startswith("#")]
    if not tickers:
        log(f"Building universe from the {source.name} screener (the pre-market Gappers scan)...")
        tickers = source.universe(cfg)
        log(f"  {len(tickers)} candidates: {' '.join(tickers)}")
    tickers = list(dict.fromkeys(tickers))
    if not tickers:
        print("No tickers to scan."); return 1

    results, rejected, contexts = scan_once(cfg, source, tickers, now, log, replay=a.replay,
                                            replay_time=a.replay_time, include_not_in_play=a.include_not_in_play,
                                            with_options=not a.no_options and not a.replay)
    results.sort(key=lambda r: -r["score"])
    print_report(results, rejected, cfg, now, a.show_rejected, source.name)

    os.makedirs(a.out, exist_ok=True)
    stamp = (f"replay_{a.replay}_{(a.replay_time or '16:00').replace(':', '')}" if a.replay
             else now.strftime("%Y-%m-%d_%H%M"))
    path = os.path.join(a.out, f"signals_{stamp}.json")
    payload = to_jsonable({"run_at": now.isoformat(), "source": source.name, "config": asdict(cfg), "replay": a.replay,
                           "results": [{"score": r["score"], "stock": r["ctx"], "signal": r["signal"], "option": r["option"]} for r in results],
                           "scanned": contexts, "rejected": rejected})
    with open(path, "w") as f:
        json.dump(payload, f, indent=2, allow_nan=False)
    log(f"\nSaved {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
