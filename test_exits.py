"""Exit-logic tests for the paper trader: drives manage() with synthetic bars and a fake broker.
Run:  python test_exits.py
Covers the book's exits -- flat time stop, profit zone (break-even then trailing the last 5-min
candle), target half-off, and the stop -- without touching Alpaca or any network."""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd

import paper_trader as pt

NY = ZoneInfo("America/New_York")


def bars(closes, start="10:00", date="2026-09-22", spread=0.05):
    """closes -> 5-min OHLC frame (low/high padded by `spread`)."""
    idx = pd.date_range(f"{date} {start}", periods=len(closes), freq="5min", tz=NY)
    return pd.DataFrame({"Open": closes, "High": [c + spread for c in closes],
                         "Low": [c - spread for c in closes], "Close": closes,
                         "Volume": [1000] * len(closes)}, index=idx)


class FakeTrader(pt.PaperTrader):
    """PaperTrader with the network parts stubbed out."""

    def __init__(self, m5: pd.DataFrame, now: datetime, **argkw):
        a = argparse.Namespace(dry_run=True, flat_by="15:45", breaker_pct=40.0, time_stop_min=20,
                               time_stop_r=0.5, max_contracts=2, instrument="both", tickers=None,
                               fill_wait=5, max_notional_pct=100.0, strategies="VWAP", windows=[("09:45", "11:00")],
                               min_catalyst_grade="B", **argkw)
        self.a = a
        self.cfg = pt.Config(account_size=10000, risk_pct=1.0)
        self.log = lambda *x: None
        self.broker = None
        self.blob = type("B", (), {"enabled": False, "sync_up": lambda *a: None, "sync_down": lambda *a: None})()
        self.status = type("S", (), {"publish": lambda *a, **k: None})()
        self.risk = type("R", (), {"journal": lambda self_, row: self_.rows.append(row), "rows": [],
                                   "today_pnl": lambda *a: 0.0, "week_pnl": lambda *a: 0.0})()
        self.risk.rows = []
        self.events, self.funnel, self.last_scan_info = [], {}, {}
        self._m5, self._now = m5, now
        self.open = None

    # stubs
    def stock_bars(self, ticker, interval, n):
        return self._m5.tail(n) if interval == "5m" else self._m5.tail(1)

    def option_mark(self, contract, fallback):
        return fallback

    def persist(self): pass
    def publish_status(self): pass
    def note(self, msg): self.events.append(msg)
    def now(self): return self._now
    def _close_leg(self, symbol, qty, side, ref_px, reason, is_option): return ref_px


def make_trade(entry=100.0, stop=99.0, target=103.0, long=True, qty=100):
    return pt.OpenTrade(ticker="TEST", strategy="VWAP", direction="LONG" if long else "SHORT", contract="",
                        contracts=0, contracts_initial=0,
                        entry_time=(datetime(2026, 9, 22, 10, 0, tzinfo=NY)).isoformat(),
                        stock_entry=entry, stock_stop=stop, stock_target=target, option_entry=0.0,
                        in_play=True, catalyst="A [earnings]", time_bucket="open", score=50.0,
                        stock_qty=qty, stock_qty_initial=qty, stock_fill=entry, best_stock=entry)


results = []


def check(name, cond, detail=""):
    results.append(cond)
    print(f"{'PASS' if cond else 'FAIL'}  {name}{('  -- ' + detail) if detail and not cond else ''}")


# 1. flat after 20 min -> time stop
m5 = bars([100.0, 100.1, 100.05, 100.1, 100.05])
t = FakeTrader(m5, datetime(2026, 9, 22, 10, 25, tzinfo=NY))
t.open = make_trade()
t.manage(t.now())
row = t.risk.rows[-1] if t.risk.rows else {}
check("flat trade time-stops after 20 min", t.open is None and "time stop" in row.get("exit_reason", ""),
      f"open={t.open is not None} reason={row.get('exit_reason')}")

# 2. +0.6R after 20 min -> NO time stop, profit zone, stop at break-even or better
m5 = bars([100.0, 100.2, 100.4, 100.5, 100.6])
t = FakeTrader(m5, datetime(2026, 9, 22, 10, 25, tzinfo=NY))
t.open = make_trade()
t.manage(t.now())
check("+0.6R does not time-stop; enters profit zone", t.open is not None and t.open.profit_zone and t.open.breakeven,
      f"open={t.open is not None} zone={getattr(t.open, 'profit_zone', None)}")
check("trailing stop set at/above break-even", t.open is not None and t.open.trail_stop >= t.open.stock_entry,
      f"trail={getattr(t.open, 'trail_stop', None)} entry={100.0}")

# 3. in the profit zone, a 5-min close back through the trail -> exit (not a full-R loss)
prev_trail = t.open.trail_stop
m5b = bars([100.0, 100.2, 100.4, 100.5, 100.6, 99.9])
t._m5 = m5b
t._now = datetime(2026, 9, 22, 10, 30, tzinfo=NY)
t.manage(t.now())
row = t.risk.rows[-1] if t.risk.rows else {}
check("trailed stop closes the trade", t.open is None and "stop" in row.get("exit_reason", ""),
      f"open={t.open is not None} reason={row.get('exit_reason')} trail={prev_trail}")

# 4. target touched -> half off, stop to break-even (2 shares -> 1)
m5 = bars([100.0, 101.0, 102.0, 103.1])
t = FakeTrader(m5, datetime(2026, 9, 22, 10, 15, tzinfo=NY))
t.open = make_trade(qty=100)
t.manage(t.now())
check("target half-off leaves half on with break-even stop",
      t.open is not None and t.open.stock_qty == 50 and t.open.half_taken and t.open.breakeven,
      f"qty={getattr(t.open, 'stock_qty', None)} half={getattr(t.open, 'half_taken', None)}")

# 5. hard stop still fires before the profit zone
#    (now is 10:16 so the 10:10 candle is CLOSED -- the book's stop is a 5-min close through the level,
#     an in-progress bar below the stop is not yet a stop)
m5 = bars([100.0, 99.8, 98.9])
t = FakeTrader(m5, datetime(2026, 9, 22, 10, 16, tzinfo=NY))
t.open = make_trade()
t.manage(t.now())
row = t.risk.rows[-1] if t.risk.rows else {}
check("5-min close through the stop exits", t.open is None and "stop" in row.get("exit_reason", ""),
      f"reason={row.get('exit_reason')}")

# 6. short mirror: +0.6R short enters the profit zone
m5 = bars([100.0, 99.8, 99.6, 99.5, 99.4])
t = FakeTrader(m5, datetime(2026, 9, 22, 10, 25, tzinfo=NY))
t.open = make_trade(entry=100.0, stop=101.0, target=97.0, long=False)
t.manage(t.now())
check("short +0.6R enters profit zone and trails",
      t.open is not None and t.open.profit_zone and t.open.trail_stop <= t.open.stock_entry,
      f"zone={getattr(t.open, 'profit_zone', None)} trail={getattr(t.open, 'trail_stop', None)}")

print(f"\n{sum(results)}/{len(results)} exit tests passed")
raise SystemExit(0 if all(results) else 1)
