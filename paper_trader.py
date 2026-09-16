#!/usr/bin/env python
"""
paper_trader.py -- forward-test the scanner's signals on an Alpaca PAPER account.

PAPER ONLY. The base URL is hard-coded to https://paper-api.alpaca.markets and there is no
flag to change it. This is a simulator, which is what the book prescribes before real money.

What it does, every cycle during the allowed windows:
  1. runs the scanner (Unusual Whales data) on the gapper universe
  2. keeps signals that are [ACTIONABLE], on an IN PLAY stock with a catalyst grade of at least
     --min-catalyst-grade (default B; see catalyst.py), in an allowed strategy, without a
     CONFLICT, and not already traded today
  3. if no position is open and the risk limits allow, buys the contract with a limit at the ask
  4. manages the open position off the UNDERLYING (book rules):
        stop    = 5-min close through the stock stop         -> sell all
        target  = stock touches the target                   -> sell half, stop -> break-even;
                  with 1 contract, sell all
        runner  = after the half, exit on the first 5-min bar that makes a new low (long) /
                  new high (short), or at break-even
        time    = 20 min after entry with the stock not past the entry in your favour -> sell
        breaker = option marked at <= 60% of entry premium   -> sell (mine, not the book)
        close   = 15:45 ET flat, no exceptions               (Rule 3)
  5. journals every entry/exit to journal.csv with stock-R and option-R

Risk state (daily loss, weekly loss, trade counts) is rebuilt from journal.csv at startup.
Sizing uses --account (default 10000 = your LIVE size), not the paper balance, so the journal
reflects trades you could actually take. The FINRA pattern-day-trader rule ($25k minimum,
3 day trades per 5 days) was eliminated effective 2026-06-04 (brokers have until 2027-10-20 to
implement), so no day-trade cap is applied by default; --max-trades-per-day is a discipline
setting (book: 2-3 trades a day), not a regulatory one.

Keys: APCA_API_KEY_ID and APCA_API_SECRET_KEY (process env or Windows User env). Never printed.
UW_API_KEY as for the scanner.

    python paper_trader.py                       # run until 15:45
    python paper_trader.py --once                # one scan cycle, then exit (dry look)
    python paper_trader.py --dry-run             # everything except sending orders
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, asdict, field
from datetime import datetime, timedelta, date
from typing import Optional
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

_HERE = os.path.dirname(os.path.abspath(__file__))
_BUNDLE = os.path.join(_HERE, "ca_bundle.pem")
if os.path.exists(_BUNDLE):
    for _k in ("SSL_CERT_FILE", "CURL_CA_BUNDLE", "REQUESTS_CA_BUNDLE"):
        os.environ.setdefault(_k, _BUNDLE)

from aziz_options_scanner import Config, scan_once, alert_line, StockSignal, OptionPlan  # noqa: E402
from datasources import make_source, DataError, NY  # noqa: E402
from publish import BlobSync, StatusPublisher  # noqa: E402
import journal_report  # noqa: E402

JOURNAL = os.path.join(_HERE, "journal.csv")
STATE = os.path.join(_HERE, "paper_state.json")
JOURNAL_FIELDS = ["date", "ticker", "strategy", "direction", "contract", "contracts", "entry_time", "exit_time",
                  "stock_entry", "stock_stop", "stock_target", "stock_exit", "option_entry", "option_exit",
                  "exit_reason", "stock_r", "option_r", "pnl", "in_play", "catalyst", "time_bucket", "score"]


# ===========================================================================
# Alpaca paper client (REST, no SDK dependency)
# ===========================================================================
def _user_env(name: str) -> Optional[str]:
    v = os.environ.get(name)
    if not v and sys.platform == "win32":
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as h:
                v, _ = winreg.QueryValueEx(h, name)
        except OSError:
            v = None
    return v.strip() if v else None


class AlpacaPaper:
    BASE = "https://paper-api.alpaca.markets/v2"     # PAPER ONLY -- deliberately not configurable

    def __init__(self, log):
        self.key = _user_env("APCA_API_KEY_ID")
        self.secret = _user_env("APCA_API_SECRET_KEY")
        if not self.key or not self.secret:
            raise RuntimeError("APCA_API_KEY_ID / APCA_API_SECRET_KEY not set (process env or Windows User env)")
        self.log = log
        cafile = os.environ.get("SSL_CERT_FILE")
        self._ctx = ssl.create_default_context(cafile=cafile if cafile and os.path.exists(cafile) else None)

    def _req(self, method: str, path: str, body: Optional[dict] = None, params: Optional[dict] = None):
        url = self.BASE + path + ("?" + urllib.parse.urlencode(params) if params else "")
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers={
            "APCA-API-KEY-ID": self.key, "APCA-API-SECRET-KEY": self.secret,
            "Content-Type": "application/json", "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, context=self._ctx, timeout=30) as r:
                raw = r.read().decode("utf-8")
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"Alpaca {e.code} {method} {path}: {e.read().decode('utf-8', 'replace')[:300]}") from None

    def account(self) -> dict:
        return self._req("GET", "/account")

    def clock(self) -> dict:
        return self._req("GET", "/clock")

    def positions(self) -> list[dict]:
        return self._req("GET", "/positions") or []

    def position(self, symbol: str) -> Optional[dict]:
        try:
            return self._req("GET", f"/positions/{symbol}")
        except RuntimeError as e:
            if " 404 " in str(e):
                return None
            raise

    def submit_limit(self, symbol: str, qty: int, side: str, limit_price: float) -> dict:
        return self._req("POST", "/orders", {"symbol": symbol, "qty": str(int(qty)), "side": side, "type": "limit",
                                             "time_in_force": "day", "limit_price": f"{limit_price:.2f}"})

    def submit_market(self, symbol: str, qty: int, side: str) -> dict:
        return self._req("POST", "/orders", {"symbol": symbol, "qty": str(int(qty)), "side": side, "type": "market",
                                             "time_in_force": "day"})

    def order(self, order_id: str) -> dict:
        return self._req("GET", f"/orders/{order_id}")

    def last_fill(self, symbol: str, side: str) -> Optional[float]:
        """Most recent filled order price for symbol/side (used when a position vanished while we were down)."""
        try:
            orders = self._req("GET", "/orders", params={"status": "closed", "symbols": symbol, "limit": 20, "direction": "desc"}) or []
        except RuntimeError:
            return None
        for o in orders:
            if o.get("side") == side and o.get("status") == "filled" and o.get("filled_avg_price"):
                return float(o["filled_avg_price"])
        return None

    def cancel(self, order_id: str):
        try:
            self._req("DELETE", f"/orders/{order_id}")
        except RuntimeError:
            pass

    def wait_fill(self, order_id: str, seconds: int) -> tuple[int, float]:
        """Returns (filled_qty, avg_price). Cancels the remainder after `seconds`."""
        deadline = time.time() + seconds
        while time.time() < deadline:
            o = self.order(order_id)
            if o.get("status") in ("filled",):
                return int(float(o.get("filled_qty") or 0)), float(o.get("filled_avg_price") or 0)
            if o.get("status") in ("canceled", "expired", "rejected"):
                return int(float(o.get("filled_qty") or 0)), float(o.get("filled_avg_price") or 0)
            time.sleep(2)
        self.cancel(order_id)
        time.sleep(1)
        o = self.order(order_id)
        return int(float(o.get("filled_qty") or 0)), float(o.get("filled_avg_price") or 0)


# ===========================================================================
# Trade state
# ===========================================================================
@dataclass
class OpenTrade:
    ticker: str
    strategy: str
    direction: str            # LONG / SHORT (the stock view; the option is always bought)
    contract: str
    contracts: int            # currently held
    contracts_initial: int
    entry_time: str
    stock_entry: float
    stock_stop: float
    stock_target: float
    option_entry: float
    in_play: bool
    catalyst: str
    time_bucket: str
    score: float
    option_planned_risk: float = 0.0   # per-contract planned loss (premium points) if the stock stop is honoured
    half_taken: bool = False
    half_exit_price: float = 0.0
    breakeven: bool = False
    best_stock: float = 0.0   # best price seen in our favour


class RiskBook:
    """Rebuilds daily/weekly P&L and the 5-day day-trade count from journal.csv."""

    def __init__(self, cfg, args):
        self.cfg, self.args = cfg, args
        self.rows = []
        if os.path.exists(JOURNAL):
            with open(JOURNAL, newline="", encoding="utf-8") as f:
                self.rows = list(csv.DictReader(f))

    def _pnl(self, since: date) -> float:
        return sum(float(r["pnl"] or 0) for r in self.rows if r["date"] >= since.isoformat())

    def today_pnl(self, today: date) -> float:
        return self._pnl(today)

    def week_pnl(self, today: date) -> float:
        return self._pnl(today - timedelta(days=today.weekday()))

    def day_trades_5d(self, today: date) -> int:
        cutoff = (pd.Timestamp(today) - pd.tseries.offsets.BDay(5)).date().isoformat()
        return sum(1 for r in self.rows if r["date"] > cutoff)

    def traded_today(self, today: date) -> set[str]:
        return {r["ticker"] for r in self.rows if r["date"] == today.isoformat()}

    def can_open(self, today: date) -> tuple[bool, str]:
        acct = self.cfg.account_size
        if self.today_pnl(today) <= -self.args.daily_loss_pct / 100 * acct:
            return False, f"daily loss limit hit ({self.today_pnl(today):+.0f})"
        if self.week_pnl(today) <= -self.args.weekly_loss_pct / 100 * acct:
            return False, f"weekly loss limit hit ({self.week_pnl(today):+.0f})"
        n_today = sum(1 for r in self.rows if r["date"] == today.isoformat())
        if self.args.max_trades_per_day and n_today >= self.args.max_trades_per_day:
            return False, f"{n_today} trades already today (--max-trades-per-day {self.args.max_trades_per_day}; book: 2-3 a day)"
        if self.args.max_day_trades_5d and self.day_trades_5d(today) >= self.args.max_day_trades_5d:
            return False, f"{self.day_trades_5d(today)} day trades in the last 5 business days (--max-day-trades-5d)"
        return True, ""

    def journal(self, row: dict):
        new = not os.path.exists(JOURNAL)
        with open(JOURNAL, "a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=JOURNAL_FIELDS)
            if new:
                w.writeheader()
            w.writerow({k: row.get(k, "") for k in JOURNAL_FIELDS})
        self.rows.append({k: str(row.get(k, "")) for k in JOURNAL_FIELDS})


def load_state() -> Optional[OpenTrade]:
    if os.path.exists(STATE):
        with open(STATE, encoding="utf-8") as f:
            d = json.load(f)
        return OpenTrade(**d) if d else None
    return None


def save_state(t: Optional[OpenTrade]):
    with open(STATE, "w", encoding="utf-8") as f:
        json.dump(asdict(t) if t else None, f, indent=1)


# ===========================================================================
# Trader
# ===========================================================================
class PaperTrader:
    def __init__(self, cfg: Config, args, log):
        self.cfg, self.a, self.log = cfg, args, log
        self.source = make_source("uw", log)
        self.broker = None if args.dry_run else AlpacaPaper(log)
        self.blob = BlobSync(log)
        if self.blob.enabled:
            self.blob.sync_down(["journal.csv", "paper_state.json"])
        self.status = StatusPublisher(self.blob, log)
        self.risk = RiskBook(cfg, args)
        self.open: Optional[OpenTrade] = load_state()
        self.allowed = set(s.strip() for s in args.strategies.split(","))
        self.last_scan_info: dict = {"time": None, "candidates": [], "note": "not started"}
        self.funnel: dict = {}
        self.events: list[str] = []

    # ---- persistence / status ----------------------------------------------------
    def persist(self):
        if self.blob.enabled:
            self.blob.sync_up(["journal.csv", "paper_state.json"])

    def note(self, msg: str):
        self.log(msg)
        self.events.append(f"{self.now():%H:%M:%S} {msg}")
        self.events = self.events[-60:]

    def publish_status(self):
        today = self.now().date().isoformat()
        rows = journal_report.load(JOURNAL)
        stats = journal_report.summarize(rows) if rows else None
        doc = dict(
            date=today, mode="dry-run" if self.a.dry_run else "paper",
            account_for_sizing=self.cfg.account_size, risk_pct=self.cfg.risk_pct,
            open_position=asdict(self.open) if self.open else None,
            today_trades=[r for r in rows if r.get("date") == today],
            today_pnl=self.risk.today_pnl(self.now().date()), week_pnl=self.risk.week_pnl(self.now().date()),
            can_open=self.risk.can_open(self.now().date()),
            last_scan=self.last_scan_info, events=self.events[-30:], stats=stats,
            windows=self.a.windows, strategies=sorted(self.allowed), min_catalyst_grade=self.a.min_catalyst_grade,
        )
        try:
            self.status.publish(doc)
        except Exception as e:   # never let the page break the trader
            self.log(f"   status publish failed: {e}")

    # ---- reconciliation ----------------------------------------------------------------
    def reconcile(self):
        """Make the state file agree with what Alpaca actually holds before doing anything else."""
        if not self.broker:
            return
        held = {p["symbol"]: p for p in self.broker.positions()
                if p.get("asset_class") == "us_option" or len(p.get("symbol", "")) > 12}
        if self.open and self.open.contract not in held:
            px = self.broker.last_fill(self.open.contract, "sell") or self.open.option_entry
            self.note(f"reconcile: state had {self.open.contract} but Alpaca holds none -> journaling as closed externally @ {px:.2f}")
            t = self.open
            n = t.contracts_initial or 1
            pnl = (px - t.option_entry) * 100 * n
            self.risk.journal(dict(date=self.now().date().isoformat(), ticker=t.ticker, strategy=t.strategy, direction=t.direction,
                                   contract=t.contract, contracts=n, entry_time=t.entry_time[11:16],
                                   exit_time=self.now().strftime("%H:%M"), stock_entry=t.stock_entry, stock_stop=t.stock_stop,
                                   stock_target=t.stock_target, stock_exit="", option_entry=t.option_entry, option_exit=round(px, 2),
                                   exit_reason="closed externally / while trader was down", stock_r="", option_r="",
                                   pnl=round(pnl, 2), in_play=t.in_play, catalyst=t.catalyst, time_bucket=t.time_bucket, score=t.score))
            self.open = None
            save_state(None)
        for sym, p in held.items():
            if self.open and self.open.contract == sym:
                qty = int(float(p.get("qty") or 0))
                if qty != self.open.contracts:
                    self.note(f"reconcile: Alpaca holds {qty} x {sym}, state said {self.open.contracts} -> adopting {qty}")
                    self.open.contracts = qty
                    save_state(self.open)
                continue
            # an option position we have no plan for: the bot cannot manage it -> close it
            qty = int(float(p.get("qty") or 0))
            self.note(f"reconcile: orphan position {qty} x {sym} with no plan -> closing at market")
            try:
                o = self.broker.submit_market(sym, qty, "sell")
                self.broker.wait_fill(o["id"], 60)
            except RuntimeError as e:
                self.note(f"   could not close orphan: {e}")
        self.persist()

    # ---- helpers ------------------------------------------------------------
    def now(self) -> datetime:
        return datetime.now(NY)

    def in_entry_window(self, now: datetime) -> bool:
        t = now.strftime("%H:%M")
        return any(lo <= t < hi for lo, hi in self.a.windows)

    def stock_bars(self, ticker: str, interval: str, n: int) -> pd.DataFrame:
        df = self.source.intraday_bars(ticker, interval)
        df = df[df["market_time"].isin(["r", None]) | (df["market_time"].isna())] if "market_time" in df else df
        return df.tail(n)

    def option_mark(self, contract: str, fallback: float) -> float:
        if self.broker:
            p = self.broker.position(contract)
            if p and p.get("current_price"):
                return float(p["current_price"])
        return fallback

    # ---- entry ----------------------------------------------------------------
    def find_candidates(self, now: datetime) -> list[dict]:
        tickers = [t.upper() for t in self.a.tickers] if self.a.tickers else self.source.universe(self.cfg)
        results, rejected, ctxs = scan_once(self.cfg, self.source, tickers, now, lambda *x: None, with_options=True,
                                            include_not_in_play=True)
        dirs: dict[str, set] = {}
        for r in results:
            dirs.setdefault(r["signal"].ticker, set()).add(r["signal"].direction)
        traded = self.risk.traded_today(now.date())
        out = []
        for r in results:
            sig, ctx, opt = r["signal"], r["ctx"], r["option"]
            base = sig.strategy.split("+")
            if not opt or not opt.actionable:
                continue
            if not ctx.in_play:
                continue
            if not any(b in self.allowed for b in base):
                continue
            if len(dirs[sig.ticker]) > 1:
                continue
            if sig.ticker in traded:
                continue
            if "ABCDF".index(ctx.catalyst_grade) > "ABCDF".index(self.a.min_catalyst_grade):
                continue
            if opt.spread_pct > self.cfg.max_spread_pct:
                continue
            out.append(r)
        out.sort(key=lambda r: -r["score"])
        # funnel: where did signals die this scan? (shown on the status page)
        n_inplay = sum(1 for r in results if r["ctx"].in_play)
        n_grade = sum(1 for r in results if r["ctx"].in_play and "ABCDF".index(r["ctx"].catalyst_grade) <= "ABCDF".index(self.a.min_catalyst_grade))
        n_strat = sum(1 for r in results if r["ctx"].in_play and any(b in self.allowed for b in r["signal"].strategy.split("+")))
        n_liquid = sum(1 for r in results if r["ctx"].in_play and r["option"] and not any(x.startswith("NO contract") for x in r["option"].notes))
        n_act = sum(1 for r in results if r["ctx"].in_play and r["option"] and r["option"].actionable)
        self.funnel = {"scanned": len(ctxs), "in_play_stocks": sum(1 for c in ctxs if c.in_play),
                       "stock_setups_2to1": len(results), "on_in_play_stock": n_inplay, "grade_ok": n_grade,
                       "strategy_ok": n_strat, "option_liquid": n_liquid, "option_actionable": n_act, "candidates": len(out),
                       "grades": dict(sorted(__import__("collections").Counter(c.catalyst_grade for c in ctxs).items())),
                       "stock_setups": [f"{r['signal'].ticker} {r['signal'].strategy} {r['signal'].direction} rr {r['signal'].rr:.1f} "
                                        f"grade {r['ctx'].catalyst_grade}{'' if r['ctx'].in_play else ' (not in play)'}"
                                        + (f" | opt: {r['option'].contract} spread {r['option'].spread_pct}% R:R {r['option'].option_rr}" if r['option'] else " | opt: none")
                                        for r in results[:12]]}
        return out

    def enter(self, r: dict, now: datetime):
        sig: StockSignal = r["signal"]; opt: OptionPlan = r["option"]; ctx = r["ctx"]
        qty = max(1, min(opt.contracts, self.a.max_contracts))
        self.log(f"ENTRY {alert_line(sig, opt)}")
        if self.a.dry_run:
            fill_qty, fill_px = qty, opt.ask
            self.log(f"   dry-run: would buy {qty} x {opt.contract} @ {opt.ask:.2f}")
        else:
            o = self.broker.submit_limit(opt.contract, qty, "buy", opt.ask)
            fill_qty, fill_px = self.broker.wait_fill(o["id"], self.a.fill_wait)
            if fill_qty == 0:
                self.log("   not filled within the wait -> skipped (book: do not chase)")
                return
            self.log(f"   filled {fill_qty} @ {fill_px:.2f}")
        self.open = OpenTrade(
            ticker=sig.ticker, strategy=sig.strategy, direction=sig.direction, contract=opt.contract,
            contracts=fill_qty, contracts_initial=fill_qty, entry_time=now.isoformat(),
            stock_entry=sig.entry, stock_stop=sig.stop, stock_target=sig.target, option_entry=fill_px,
            in_play=ctx.in_play, catalyst=ctx.catalyst, time_bucket=sig.time_bucket, score=r["score"],
            option_planned_risk=opt.risk_per_contract / 100.0, best_stock=sig.entry)
        save_state(self.open)
        self.events.append(f"{now:%H:%M:%S} ENTRY {alert_line(sig, opt)} filled @ {fill_px:.2f}")
        self.persist(); self.publish_status()

    # ---- exit -------------------------------------------------------------------
    def sell(self, qty: int, reason: str, stock_px: float, now: datetime, mark_hint: float) -> float:
        t = self.open
        if self.a.dry_run:
            px = mark_hint
            self.log(f"   dry-run: would sell {qty} x {t.contract} ~{px:.2f} ({reason})")
        else:
            p = self.broker.position(t.contract)
            bid_guess = float(p["current_price"]) if p and p.get("current_price") else mark_hint
            o = self.broker.submit_limit(t.contract, qty, "sell", max(0.01, round(bid_guess * 0.98, 2)))
            fq, px = self.broker.wait_fill(o["id"], 45)
            if fq < qty:                                  # must get out: market for the remainder
                o2 = self.broker.submit_market(t.contract, qty - fq, "sell")
                fq2, px2 = self.broker.wait_fill(o2["id"], 30)
                px = (px * fq + px2 * fq2) / max(fq + fq2, 1)
            self.log(f"   sold {qty} @ {px:.2f} ({reason})")
        return px

    def close_and_journal(self, qty: int, reason: str, stock_px: float, now: datetime, mark_hint: float):
        t = self.open
        px = self.sell(qty, reason, stock_px, now, mark_hint)
        t.contracts -= qty
        if t.contracts > 0:
            t.half_taken, t.half_exit_price, t.breakeven = True, px, True
            save_state(t)
            self.log(f"   half off, stop moved to break-even {t.stock_entry:.2f}")
            self.events.append(f"{now:%H:%M:%S} {t.ticker} half off @ {px:.2f}, stop -> break-even")
            self.persist(); self.publish_status()
            return
        # blended option exit across the half and the runner
        n_half = t.contracts_initial - qty
        opt_exit = (t.half_exit_price * n_half + px * qty) / t.contracts_initial if n_half else px
        stock_risk = abs(t.stock_entry - t.stock_stop)
        stock_move = (stock_px - t.stock_entry) * (1 if t.direction == "LONG" else -1)
        pnl = (opt_exit - t.option_entry) * 100 * t.contracts_initial
        opt_risk = t.option_planned_risk if t.option_planned_risk > 0 else max(t.option_entry * 0.4, 0.01)
        row = dict(date=now.date().isoformat(), ticker=t.ticker, strategy=t.strategy, direction=t.direction,
                   contract=t.contract, contracts=t.contracts_initial, entry_time=t.entry_time[11:16], exit_time=now.strftime("%H:%M"),
                   stock_entry=t.stock_entry, stock_stop=t.stock_stop, stock_target=t.stock_target, stock_exit=round(stock_px, 2),
                   option_entry=t.option_entry, option_exit=round(opt_exit, 2), exit_reason=reason,
                   stock_r=round(stock_move / stock_risk, 2) if stock_risk else "",
                   option_r=round((opt_exit - t.option_entry) / opt_risk, 2),   # option P&L in units of the planned per-contract risk
                   pnl=round(pnl, 2), in_play=t.in_play, catalyst=t.catalyst, time_bucket=t.time_bucket, score=t.score)
        self.risk.journal(row)
        self.log(f"CLOSED {t.ticker} {reason}: stock R {row['stock_r']}  option P&L {pnl:+.0f}  "
                 f"(day {self.risk.today_pnl(now.date()):+.0f}, week {self.risk.week_pnl(now.date()):+.0f})")
        self.open = None
        save_state(None)
        self.events.append(f"{now:%H:%M:%S} CLOSED {t.ticker} {reason} pnl {pnl:+.0f}")
        self.persist(); self.publish_status()

    def manage(self, now: datetime):
        t = self.open
        if not t:
            return
        try:
            m1 = self.stock_bars(t.ticker, "1m", 3)
            m5 = self.stock_bars(t.ticker, "5m", 4)
        except DataError as e:
            self.log(f"   manage: no bars ({e})"); return
        if m1.empty or m5.empty:
            return
        px = float(m1["Close"].iloc[-1])
        long = t.direction == "LONG"
        sign = 1 if long else -1
        mark = self.option_mark(t.contract, t.option_entry)
        # completed 5-min bars only (the last row may be in progress)
        done5 = m5.iloc[:-1] if (now - m5.index[-1]).total_seconds() < 300 else m5
        last5 = done5.iloc[-1] if len(done5) else None
        prev5 = done5.iloc[-2] if len(done5) > 1 else None
        t.best_stock = max(t.best_stock, px) if long else min(t.best_stock, px)
        entered = datetime.fromisoformat(t.entry_time)
        mins = (now - entered).total_seconds() / 60

        # 1. hard close
        if now.strftime("%H:%M") >= self.a.flat_by:
            self.close_and_journal(t.contracts, "hard close 15:45 (Rule 3)", px, now, mark); return
        # 2. option circuit breaker
        if mark <= t.option_entry * (1 - self.a.breaker_pct / 100):
            self.close_and_journal(t.contracts, f"option breaker: mark {mark:.2f} <= {100-self.a.breaker_pct:.0f}% of entry", px, now, mark); return
        # 3. stop: 5-min close through the level (or break-even after the half)
        stop = t.stock_entry if t.breakeven else t.stock_stop
        if last5 is not None and (float(last5["Close"]) - stop) * sign < 0:
            self.close_and_journal(t.contracts, f"stop: 5-min close {float(last5['Close']):.2f} through {stop:.2f}", px, now, mark); return
        # 4. target touched
        if (px - t.stock_target) * sign >= 0 and not t.half_taken:
            if t.contracts >= 2:
                self.close_and_journal(t.contracts // 2, f"target {t.stock_target:.2f} touched: half off", px, now, mark); return
            self.close_and_journal(t.contracts, f"target {t.stock_target:.2f} touched", px, now, mark); return
        # 5. runner exit: new 5-min low (long) / high (short) after the half
        if t.half_taken and last5 is not None and prev5 is not None:
            weak = (float(last5["Low"]) < float(prev5["Low"])) if long else (float(last5["High"]) > float(prev5["High"]))
            if weak:
                self.close_and_journal(t.contracts, "runner: new 5-min low/high (book: buyers exhausted)", px, now, mark); return
        # 6. time stop
        if mins >= self.a.time_stop_min and (px - t.stock_entry) * sign <= 0 and not t.half_taken:
            self.close_and_journal(t.contracts, f"time stop: {mins:.0f} min, stock not in our favour", px, now, mark); return

    # ---- main loop ------------------------------------------------------------------
    def run(self):
        cfg = self.cfg
        self.log(f"paper trader | account for sizing {cfg.account_size:,.0f} | risk/trade {cfg.risk_pct}% | "
                 f"windows {self.a.windows} | strategies {sorted(self.allowed)} | dry_run={self.a.dry_run}")
        if self.broker:
            acct = self.broker.account()
            self.log(f"Alpaca PAPER account: equity {float(acct.get('equity', 0)):,.0f}  options level {acct.get('options_trading_level')}  "
                     f"options BP {acct.get('options_buying_power')}")
        if self.a.wait_until:
            while self.now().strftime("%H:%M") < self.a.wait_until and not self.a.ignore_clock:
                if self.now().weekday() >= 5:
                    self.log("weekend - exiting"); return
                time.sleep(30)
            self.log(f"start gate {self.a.wait_until} ET passed")
        self.reconcile()
        if self.open:
            self.log(f"resuming open trade: {self.open.ticker} {self.open.contract} x{self.open.contracts}")
        self.last_scan_info["note"] = "running"
        self.publish_status()
        last_scan = last_pub = 0.0
        while True:
            now = self.now()
            if time.time() - last_pub >= self.a.publish_every:
                last_pub = time.time(); self.publish_status()
            if not self.a.ignore_clock and (now.strftime("%H:%M") >= "16:00" or now.weekday() >= 5):
                self.log("market closed for today - exiting")
                self.last_scan_info["note"] = "finished for the day"
                self.publish_status(); break
            try:
                if self.open:
                    self.manage(now)
                elif self.in_entry_window(now) and time.time() - last_scan >= self.a.scan_every:
                    last_scan = time.time()
                    ok, why = self.risk.can_open(now.date())
                    if not ok:
                        self.log(f"{now:%H:%M} no new trades: {why}")
                        self.last_scan_info = {"time": now.strftime("%H:%M:%S"), "note": f"no new trades: {why}", "candidates": []}
                    else:
                        cands = self.find_candidates(now)
                        self.last_scan_info = {"time": now.strftime("%H:%M:%S"), "note": f"{len(cands)} candidate(s)",
                                               "candidates": [alert_line(c["signal"], c["option"]) for c in cands[:5]],
                                               "funnel": self.funnel}
                        self.log(f"{now:%H:%M} scan: {len(cands)} candidate(s)" +
                                 (" -> " + "; ".join(alert_line(c['signal'], c['option']) for c in cands[:3]) if cands else ""))
                        if cands:
                            self.enter(cands[0], now)
                elif time.time() - last_scan >= self.a.scan_every:
                    last_scan = time.time()
                    self.log(f"{now:%H:%M} outside entry window; waiting")
            except (DataError, RuntimeError) as e:
                self.log(f"   error: {e}")
            if self.a.once and not self.open:
                self.publish_status(); break
            time.sleep(self.a.poll_every if self.open else 5)


def main(argv=None):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--account", type=float, default=10_000, help="account size used for SIZING (your live size), not the paper balance")
    p.add_argument("--risk-pct", type=float, default=1.0)
    p.add_argument("--max-spread", type=float, default=10.0, help="max option spread %% of mid (10 for the paper test so the journal can measure the cost; use 5 live)")
    p.add_argument("--min-price", type=float, default=10.0, help="universe floor (book: $10-$100 is the range for all strategies; sub-$10 options are illiquid)")
    p.add_argument("--max-contracts", type=int, default=2)
    p.add_argument("--strategies", default="VWAP,ORB,SupportResistance,RedToGreen",
                   help="comma list of allowed strategies (book's core ones by default; add MATrend,BottomReversal,TopReversal,ABCD,BullFlag later)")
    p.add_argument("--windows", default="09:45-11:00", help="entry windows ET, comma-separated, e.g. 09:45-11:00,15:00-15:30")
    p.add_argument("--flat-by", default="15:45")
    p.add_argument("--time-stop-min", type=int, default=20)
    p.add_argument("--breaker-pct", type=float, default=40.0, help="sell if the option marks this %% below entry")
    p.add_argument("--daily-loss-pct", type=float, default=2.0)
    p.add_argument("--weekly-loss-pct", type=float, default=5.0)
    p.add_argument("--max-trades-per-day", type=int, default=3, help="discipline cap (book: 2-3 trades a day); 0 = off")
    p.add_argument("--max-day-trades-5d", type=int, default=0,
                   help="legacy PDT-style cap (3 per 5 business days); 0 = off. PDT was eliminated 2026-06-04 but some brokers still enforce it until 2027-10")
    p.add_argument("--ignore-clock", action="store_true", help="testing only: run the loop outside market hours")
    p.add_argument("--wait-until", default=None, help="HH:MM ET: sleep until this time before starting (for a UTC cron that fires early)")
    p.add_argument("--publish-every", type=int, default=120, help="seconds between status-page publishes while idle")
    p.add_argument("--min-catalyst-grade", default="B", choices=list("ABCD"),
                   help="worst catalyst grade to auto-trade: A=earnings/guidance/FDA/M&A, B=+contract/product/offering/mgmt/analyst/legal, "
                        "C=+no headline but independent of sector on high rel vol, D=+unknown. F (sector move) is never traded")
    p.add_argument("--universe-size", type=int, default=30)
    p.add_argument("-t", "--tickers", nargs="*", help="scan only these tickers instead of the screener universe")
    p.add_argument("--scan-every", type=int, default=60, help="seconds between scans while flat")
    p.add_argument("--poll-every", type=int, default=20, help="seconds between checks while in a trade")
    p.add_argument("--fill-wait", type=int, default=60, help="seconds to wait for the entry limit to fill before giving up")
    p.add_argument("--dry-run", action="store_true", help="scan and log decisions but send no orders")
    p.add_argument("--once", action="store_true", help="one cycle then exit")
    a = p.parse_args(argv)
    a.windows = [tuple(w.split("-")) for w in a.windows.split(",")]
    a.risk_pct = min(a.risk_pct, 2.0)

    cfg = Config(account_size=a.account, risk_pct=a.risk_pct, max_spread_pct=a.max_spread, min_price=a.min_price,
                 universe_size=a.universe_size, include_mid_day=False)
    log = lambda *x: print(f"[{datetime.now(NY):%H:%M:%S}]", *x, flush=True)
    try:
        PaperTrader(cfg, a, log).run()
    except (RuntimeError, DataError) as e:
        print(e); return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
