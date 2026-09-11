"""
catalyst.py -- mechanical version of the book's catalyst check.

Book, Ch. 4: Stocks in Play have a fundamental catalyst -- earnings, earnings warnings /
surprises, FDA decisions, M&A, partnerships / product releases, contract wins, restructurings /
management changes, splits / buybacks / offerings.
Book, Rule 4: "Is this stock moving because the overall market is moving, or because it has its
own catalyst?"  Stocks that move with their sector are "the herd", not in play.

grade(...) returns A / B / C / D / F -- see GRADES below. Everything here is deterministic keyword
and arithmetic logic; it will occasionally mislabel a headline. The evidence (headline, numbers)
is always returned so the report can show *why*.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

# ---- headline classification --------------------------------------------------------------
# type -> (grade, patterns). Patterns are matched case-insensitively against the headline.
CATALYST_TYPES: dict[str, tuple[str, list[str]]] = {
    "earnings":     ("A", [r"\bearnings\b", r"\bresults\b", r"\bq[1-4]\b", r"\beps\b", r"\brevenue\b", r"\bquarter(ly)?\b",
                           r"\bbeat(s)?\b", r"\bmiss(es|ed)?\b", r"\bprelim(inary)?\b"]),
    "guidance":     ("A", [r"\bguidance\b", r"\boutlook\b", r"\bforecast\b", r"\bsees\s+(fy|q[1-4]|20\d\d)", r"\braises?\b.*\b(view|target|forecast)\b",
                           r"\b(cuts?|lowers?)\b.*\b(view|forecast|guidance)\b", r"\bwarn(s|ing)?\b"]),
    "fda":          ("A", [r"\bfda\b", r"\bapprov(al|es|ed)\b", r"\bphase\s*[123]\b", r"\btrial\b", r"\bclinical\b", r"\bpdufa\b",
                           r"\bcomplete response letter\b", r"\bcrl\b"]),
    "m&a":          ("A", [r"\bmerger\b", r"\bacqui(re|res|red|sition)\b", r"\bbuyout\b", r"\btakeover\b", r"\bto be acquired\b",
                           r"\bdeal to buy\b", r"\bbid for\b", r"\bgo(es|ing)? private\b", r"\bstrategic alternatives\b"]),
    "contract":     ("B", [r"\bcontract\b", r"\baward(ed|s)?\b", r"\border(s)? (from|for|worth)\b", r"\bwins?\b.*\b(deal|order|contract)\b"]),
    "product":      ("B", [r"\blaunch(es|ed)?\b", r"\bpartnership\b", r"\bpartners? with\b", r"\bcollaborat", r"\bunveils?\b",
                           r"\bannounces? (new|the)\b"]),
    "offering":     ("B", [r"\boffering\b", r"\bprices? (public|secondary|registered)\b", r"\bconvertible\b", r"\bdilut", r"\bshelf\b",
                           r"\bat-the-market\b"]),
    "management":   ("B", [r"\bceo\b", r"\bcfo\b", r"\bchief executive\b", r"\bresign(s|ed|ation)?\b", r"\bsteps? down\b", r"\bappoints?\b",
                           r"\blayoffs?\b", r"\brestructur", r"\bjob cuts\b"]),
    "capital":      ("B", [r"\bbuyback\b", r"\brepurchase\b", r"\bstock split\b", r"\breverse split\b", r"\bdividend\b"]),
    "analyst":      ("B", [r"\bupgrade(s|d)?\b", r"\bdowngrade(s|d)?\b", r"\bprice (target|objective)\b", r"\binitiat(es|ed)\b.*\b(coverage|at)\b",
                           r"\boutperform\b", r"\bunderperform\b"]),
    "legal":        ("B", [r"\blawsuit\b", r"\bsettle(s|ment)\b", r"\bprobe\b", r"\binvestigation\b", r"\bsec\b.*\b(charges?|filing)\b",
                           r"\bdoj\b", r"\bftc\b", r"\brecall\b", r"\bshort report\b", r"\bhindenburg\b"]),
}
_COMPILED = {k: (g, [re.compile(p, re.I) for p in pats]) for k, (g, pats) in CATALYST_TYPES.items()}

# headlines that merely mention the ticker in a market wrap
_WRAP = re.compile(r"stocks? (to watch|moving|of the week)|market(s)? (today|wrap|update)|futures|premarket movers|"
                   r"top (gainers|losers)|what to watch|midday movers|after.?hours movers", re.I)

GRADES = {
    "A": "confirmed primary catalyst (earnings / guidance / FDA / M&A)",
    "B": "secondary catalyst (contract, product, offering, management, analyst, legal)",
    "C": "no headline found, but moving independently of sector & market (high rel volume or >= 5% move)",
    "D": "no headline found; move too small to judge independence",
    "F": "moving WITH its sector / the market -- Rule 4: not the stock's own move",
}


@dataclass
class CatalystCall:
    grade: str
    kind: str                 # e.g. "earnings", "contract", "none"
    evidence: str             # the headline / calendar entry / numbers
    excess_vs_sector: float   # stock % change minus sector ETF % change (pts)
    excess_vs_spy: float
    independent: Optional[bool]
    sector: str

    def label(self) -> str:
        ind = "" if self.independent is None else (" | independent of sector" if self.independent else " | moving with sector")
        return f"{self.grade} [{self.kind}] {self.evidence}{ind}"


def classify_headlines(items: list[dict], ticker: str, now: datetime, max_age_h: float = 36.0) -> Optional[tuple[str, str, str]]:
    """items: [{headline, created_at (ISO), is_major, tickers:[...]}]. Returns (kind, grade, headline)
    for the strongest classifiable headline *about this ticker* within max_age_h, else None."""
    best = None
    rank = {"A": 2, "B": 1}
    for it in items:
        h = it.get("headline") or ""
        ts = it.get("created_at")
        try:
            t = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
            if (now - t) > timedelta(hours=max_age_h):
                continue
        except (TypeError, ValueError):
            pass
        tks = it.get("tickers") or [ticker]
        if len(tks) > 3 and f"${ticker}" not in h.upper():   # co-mention in a list, not about this stock
            continue
        if _WRAP.search(h):
            continue
        for kind, (grade, pats) in _COMPILED.items():
            if any(p.search(h) for p in pats):
                if best is None or rank[grade] > rank[best[1]]:
                    best = (kind, grade, h.strip()[:140])
                break
    return best


def independence(stock_pct: float, spy_pct: float, sector_pct: Optional[float],
                 min_excess_pts: float = 1.5) -> tuple[Optional[bool], float, float]:
    """Rule 4 test. Returns (independent | None if inconclusive, excess_vs_sector, excess_vs_spy)."""
    ref = sector_pct if sector_pct is not None else spy_pct
    ex_sector = stock_pct - (sector_pct if sector_pct is not None else spy_pct)
    ex_spy = stock_pct - spy_pct
    if abs(stock_pct) < min_excess_pts:                       # not enough of a move to say anything
        return None, ex_sector, ex_spy
    same_dir_and_small_excess = (stock_pct * ref > 0) and abs(ex_sector) < max(min_excess_pts, 0.5 * abs(stock_pct))
    return (not same_dir_and_small_excess), ex_sector, ex_spy


def grade(ticker: str, earnings: Optional[str], headlines: list[dict], stock_pct: float, spy_pct: float,
          sector_pct: Optional[float], sector: str, rel_vol: float, now: datetime) -> CatalystCall:
    ind, ex_sector, ex_spy = independence(stock_pct, spy_pct, sector_pct)
    nums = f"stock {stock_pct:+.1f}% vs sector {sector_pct:+.1f}% / SPY {spy_pct:+.1f}%" if sector_pct is not None \
        else f"stock {stock_pct:+.1f}% vs SPY {spy_pct:+.1f}% (no sector ETF)"
    if earnings:
        return CatalystCall("A", "earnings", f"{earnings}; {nums}", ex_sector, ex_spy, ind, sector)
    hit = classify_headlines(headlines, ticker, now)
    if hit:
        kind, g, h = hit
        return CatalystCall(g, kind, f"\"{h}\"; {nums}", ex_sector, ex_spy, ind, sector)
    # independent move on high relative volume, or simply a very large independent move (>= 5%):
    # the catalyst exists even if no headline was found for it
    if ind is True and (rel_vol >= 1.5 or abs(stock_pct) >= 5.0):
        return CatalystCall("C", "unknown-independent", f"no headline; {nums}; rel vol {rel_vol:.1f}x", ex_sector, ex_spy, ind, sector)
    if ind is False:
        return CatalystCall("F", "sector-move", f"no headline; {nums}", ex_sector, ex_spy, ind, sector)
    return CatalystCall("D", "none", f"no headline; {nums}", ex_sector, ex_spy, ind, sector)


SECTOR_ETF = {
    "technology": "XLK", "healthcare": "XLV", "financial services": "XLF", "financial": "XLF", "energy": "XLE",
    "consumer cyclical": "XLY", "consumer defensive": "XLP", "industrials": "XLI", "utilities": "XLU",
    "real estate": "XLRE", "basic materials": "XLB", "communication services": "XLC",
}


def sector_etf(sector: Optional[str]) -> Optional[str]:
    return SECTOR_ETF.get((sector or "").strip().lower())
