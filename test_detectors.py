"""Synthetic-session sanity tests for the setup detectors.
Run:  python test_detectors.py
These only prove the code recognises the shapes the book describes; they say nothing about profitability."""
import numpy as np
import pandas as pd
from zoneinfo import ZoneInfo

from aziz_options_scanner import Config, Level, SetupDetector, add_session_indicators

NY = ZoneInfo("America/New_York")
CFG = Config()


def session(bars: list[tuple[float, float, float, float, float]], start="09:30", date="2026-09-10") -> pd.DataFrame:
    """bars: list of (open, high, low, close, volume) 5-minute candles."""
    idx = pd.date_range(f"{date} {start}", periods=len(bars), freq="5min", tz=NY)
    df = pd.DataFrame(bars, columns=["Open", "High", "Low", "Close", "Volume"], index=idx)
    return add_session_indicators(df, CFG)


def run(name, reg, levels, atr_val, prev_close, expect):
    rejected = []
    det = SetupDetector("TEST", reg, levels, atr_val, prev_close, CFG, rejected)
    sigs = det.run_all()
    got = {s.strategy for s in sigs}
    ok = expect in got
    print(f"{'PASS' if ok else 'FAIL'}  {name}: expected {expect}, got {sorted(got) or '-'}")
    if not ok:
        for r in rejected:
            if r["strategy"] == expect:
                print("       reason:", r["why"])
    for s in sigs:
        if s.strategy == expect:
            print(f"       {s.direction} entry {s.entry} stop {s.stop} target {s.target} R:R {s.rr:.2f}")
    return ok


results = []

# --- Bottom reversal: 6 red candles into support 19.00, hammer, then new 5-min high, midday -----
bars = [(20.9, 21.0, 20.7, 20.75, 100)] * 3
p = 20.75
for _ in range(6):                                  # sell-off
    bars.append((p, p + 0.05, p - 0.32, p - 0.28, 300)); p -= 0.28
# hammer at the low (~19.07): small body, long lower wick, high volume
bars.append((p, p + 0.06, p - 0.30, p + 0.03, 900))
# first new 5-min high
bars.append((p + 0.03, p + 0.25, p, p + 0.20, 700))
reg = session(bars, start="11:00")
lv = [Level(19.05, "daily", 3), Level(21.0, "daily", 2)]
results.append(run("BottomReversal", reg, lv, atr_val=1.0, prev_close=21.2, expect="BottomReversal"))

# --- Top reversal mirror ------------------------------------------------------------------
bars = [(20.1, 20.2, 20.0, 20.05, 100)] * 3
p = 20.05
for _ in range(6):
    bars.append((p, p + 0.32, p - 0.05, p + 0.28, 300)); p += 0.28
bars.append((p, p + 0.30, p - 0.06, p - 0.03, 900))   # shooting star
bars.append((p - 0.03, p, p - 0.25, p - 0.20, 700))   # first new 5-min low
reg = session(bars, start="11:00")
lv = [Level(21.75, "daily", 3), Level(20.0, "daily", 2)]
results.append(run("TopReversal", reg, lv, atr_val=1.0, prev_close=19.8, expect="TopReversal"))

# --- ABCD: A=10.0 -> B=11.0, pullback to C=10.5 held 3 bars, now ticking up, open session ------
bars = [(10.0, 10.3, 9.98, 10.28, 500), (10.28, 10.6, 10.25, 10.58, 600), (10.58, 11.0, 10.55, 10.95, 900),
        (10.95, 10.97, 10.6, 10.62, 400), (10.62, 10.66, 10.5, 10.53, 300), (10.53, 10.58, 10.5, 10.55, 250),
        (10.55, 10.6, 10.52, 10.58, 350)]
reg = session(bars, start="09:40")
results.append(run("ABCD", reg, [Level(11.0, "daily", 2)], atr_val=0.6, prev_close=9.9, expect="ABCD"))

# --- Bull flag: pole 8.0 -> 8.6 in 3 bars, 3-bar flag 8.45-8.55, breakout bar on volume, open -----
bars = [(8.0, 8.2, 7.98, 8.18, 1000), (8.18, 8.42, 8.15, 8.40, 1500), (8.40, 8.62, 8.38, 8.60, 2000),
        (8.60, 8.58, 8.45, 8.50, 500), (8.50, 8.55, 8.46, 8.52, 400), (8.52, 8.55, 8.47, 8.53, 450),
        (8.53, 8.70, 8.52, 8.66, 1800)]
reg = session(bars, start="09:35")
results.append(run("BullFlag", reg, [], atr_val=0.5, prev_close=7.5, expect="BullFlag"))

# --- VWAP long: price tests VWAP from above and holds, target level 2x risk away --------------
bars = [(50.0, 50.4, 49.9, 50.3, 2000), (50.3, 50.5, 50.2, 50.45, 1500), (50.45, 50.5, 50.1, 50.4, 1200),
        (50.4, 50.45, 50.25, 50.42, 900), (50.42, 50.5, 50.28, 50.45, 800), (50.45, 50.5, 50.3, 50.44, 700)]
reg = session(bars, start="09:45")
results.append(run("VWAP", reg, [Level(51.4, "daily", 2)], atr_val=1.2, prev_close=49.0, expect="VWAP"))

# --- ORB long: tight 5-min range then close above, above VWAP, open -----------------------------
bars = [(30.0, 30.15, 29.95, 30.10, 5000), (30.10, 30.18, 30.05, 30.12, 3000), (30.12, 30.35, 30.10, 30.32, 4000)]
reg = session(bars, start="09:30")
results.append(run("ORB", reg, [Level(31.2, "daily", 2)], atr_val=0.9, prev_close=29.0, expect="ORB"))

# --- Red-to-Green: gapped down, above VWAP, rising 3 bars on rising volume toward prev close ----
bars = [(24.0, 24.1, 23.8, 23.9, 3000), (23.9, 24.0, 23.85, 23.95, 2000), (23.95, 24.1, 23.9, 24.05, 2500),
        (24.05, 24.2, 24.0, 24.15, 3000), (24.15, 24.3, 24.1, 24.25, 3600)]
reg = session(bars, start="10:00")
results.append(run("RedToGreen", reg, [], atr_val=0.8, prev_close=24.8, expect="RedToGreen"))

# --- MA trend long: 6 bars above a rising 9 EMA, pullback touches it and holds, midday ----------
p = 40.0; bars = []
for i in range(12):
    bars.append((p, p + 0.12, p - 0.04, p + 0.10, 800)); p += 0.10
last_o = p
# 9 EMA lags a straight-line trend by ~(span-1)/2 * slope = 0.40, so the dip must reach ~0.45 below
bars.append((last_o, last_o + 0.02, last_o - 0.48, last_o - 0.30, 700))   # dips into EMA, closes just above it
reg = session(bars, start="12:00")
results.append(run("MATrend", reg, [Level(p + 0.9, "daily", 2)], atr_val=1.0, prev_close=39.0, expect="MATrend"))

# --- Support/Resistance long: doji on support 15.00, closes above --------------------------------
bars = [(15.4, 15.45, 15.2, 15.25, 1000), (15.25, 15.3, 15.05, 15.1, 1500), (15.1, 15.18, 14.98, 15.11, 2500)]
reg = session(bars, start="13:00")
results.append(run("SupportResistance", reg, [Level(15.0, "daily", 3), Level(15.6, "daily", 2)], atr_val=0.7,
                   prev_close=15.9, expect="SupportResistance"))

print(f"\n{sum(results)}/{len(results)} detector shape tests passed")
