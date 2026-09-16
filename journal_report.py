"""
journal_report.py -- what the paper test is actually for.

Reads journal.csv and answers, per strategy / catalyst grade / time bucket:
  trades, win rate, average win, average loss, expectancy (avg R per trade), profit factor,
  and -- the key number -- stock-R vs option-R (how much the options translation costs).
Also equity curve, max drawdown, and current streak.

    python journal_report.py             # console
    python journal_report.py --json      # machine-readable (used for the status page)
"""
from __future__ import annotations

import argparse
import csv
import json
import os
from collections import defaultdict

_HERE = os.path.dirname(os.path.abspath(__file__))
JOURNAL = os.path.join(_HERE, "journal.csv")


def load(path: str = JOURNAL) -> list[dict]:
    if not os.path.exists(path):
        return []
    with open(path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    for r in rows:
        for k in ("stock_r", "option_r", "pnl", "contracts", "score"):
            try:
                r[k] = float(r.get(k) or 0)
            except ValueError:
                r[k] = 0.0
        r["catalyst_grade"] = (r.get("catalyst") or "?")[:1]
    return rows


def _bucket_stats(rows: list[dict]) -> dict:
    n = len(rows)
    if n == 0:
        return dict(trades=0)
    wins = [r for r in rows if r["pnl"] > 0]
    losses = [r for r in rows if r["pnl"] <= 0]
    gross_w = sum(r["pnl"] for r in wins)
    gross_l = -sum(r["pnl"] for r in losses)
    return dict(
        trades=n, wins=len(wins), win_rate=round(len(wins) / n, 3),
        avg_win=round(gross_w / len(wins), 2) if wins else 0.0,
        avg_loss=round(-gross_l / len(losses), 2) if losses else 0.0,
        pnl=round(sum(r["pnl"] for r in rows), 2),
        profit_factor=round(gross_w / gross_l, 2) if gross_l > 0 else (float("inf") if gross_w > 0 else 0.0),
        avg_stock_r=round(sum(r["stock_r"] for r in rows) / n, 2),
        avg_option_r=round(sum(r["option_r"] for r in rows) / n, 2),
        translation_cost_r=round(sum(r["stock_r"] - r["option_r"] for r in rows) / n, 2),
    )


def summarize(rows: list[dict]) -> dict:
    by = lambda key: {k: _bucket_stats(v) for k, v in sorted(_group(rows, key).items())}
    equity, peak, dd, cur = 0.0, 0.0, 0.0, []
    for r in rows:
        equity += r["pnl"]; peak = max(peak, equity); dd = min(dd, equity - peak)
        cur.append(round(equity, 2))
    streak = 0
    for r in reversed(rows):
        s = 1 if r["pnl"] > 0 else -1
        if streak == 0 or (streak > 0) == (s > 0):
            streak += s
        else:
            break
    # translation cost the honest way: realised R of the option legs vs realised R of the stock legs
    st = [r["option_r"] for r in rows if r.get("instrument") == "stock"]
    op = [r["option_r"] for r in rows if r.get("instrument") == "option"]
    return dict(
        overall=_bucket_stats(rows), by_strategy=by("strategy"), by_grade=by("catalyst_grade"),
        by_time=by("time_bucket"), by_exit=by("exit_reason"), by_direction=by("direction"),
        by_instrument=by("instrument"),
        stock_leg_avg_r=round(sum(st) / len(st), 2) if st else None,
        option_leg_avg_r=round(sum(op) / len(op), 2) if op else None,
        equity_curve=cur, max_drawdown=round(dd, 2), streak=streak,
        exit_reasons={k: len(v) for k, v in _group(rows, "exit_reason").items()},
    )


def _group(rows, key):
    g = defaultdict(list)
    for r in rows:
        k = r.get(key) or "?"
        if key == "exit_reason":
            k = k.split(":")[0].split("(")[0].strip()
        g[k].append(r)
    return g


def print_report(s: dict, n_rows: int):
    o = s["overall"]
    print("=" * 90)
    print(f"JOURNAL REPORT  {n_rows} closed trades")
    print("=" * 90)
    if not n_rows:
        print("no trades yet"); return
    print(f"P&L {o['pnl']:+,.2f}   win rate {o['win_rate']:.0%}   avg win {o['avg_win']:+.2f}   avg loss {o['avg_loss']:+.2f}   "
          f"profit factor {o['profit_factor']}   max DD {s['max_drawdown']:+,.2f}   streak {s['streak']:+d}")
    print(f"avg stock-R {o['avg_stock_r']:+.2f}   avg option-R {o['avg_option_r']:+.2f}   "
          f"translation cost {o['translation_cost_r']:+.2f} R/trade  <-- the number that decides if options make sense")
    if s.get("stock_leg_avg_r") is not None or s.get("option_leg_avg_r") is not None:
        print(f"stock legs avg realised R {s.get('stock_leg_avg_r')}   option legs avg realised R {s.get('option_leg_avg_r')}")
    for title, key in (("BY INSTRUMENT", "by_instrument"), ("BY STRATEGY", "by_strategy"), ("BY CATALYST GRADE", "by_grade"),
                       ("BY TIME OF DAY", "by_time"), ("BY EXIT REASON", "by_exit")):
        print(f"\n{title}")
        print(f"  {'bucket':32s} {'n':>3s} {'win%':>5s} {'pnl':>9s} {'PF':>5s} {'stkR':>6s} {'optR':>6s}")
        for k, v in s[key].items():
            if v.get("trades"):
                print(f"  {k[:32]:32s} {v['trades']:3d} {v['win_rate']:5.0%} {v['pnl']:9.2f} {v['profit_factor']!s:>5s} "
                      f"{v['avg_stock_r']:6.2f} {v['avg_option_r']:6.2f}")
    print("\nVerdict guide: expectancy > 0 over >= 40 trades AND translation cost small relative to avg stock-R -> the")
    print("options translation is viable; if stock-R is positive but option-R is not, the setups work and the options don't.")


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("--json", action="store_true")
    p.add_argument("--file", default=JOURNAL)
    a = p.parse_args(argv)
    rows = load(a.file)
    s = summarize(rows)
    if a.json:
        print(json.dumps(s, indent=1, default=str))
    else:
        print_report(s, len(rows))


if __name__ == "__main__":
    main()
