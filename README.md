# Aziz-Rules Options Scanner

A scanner that encodes the rules from Andrew Aziz's *How to Day Trade for a Living* and
translates the resulting **stock** setups into an **options** trade plan.

## Read this before trusting anything it prints

**1. The book is not about options.** It is a stock day-trading book. The author writes:
*"I don't trade Options or Futures either."* The only place options appear is a one-line
glossary entry. So "options signals based on this book" necessarily means:

| Layer | Source |
|---|---|
| Stocks-in-Play filters (gap ≥ 2 %, avg vol ≥ 500 k, ATR ≥ $0.50, short interest < 30 %, relative volume ≥ 1.5×, catalyst) | Book, Ch. 4 |
| The nine setups (ABCD, Bull Flag, Bottom/Top Reversal, MA Trend, VWAP, Support/Resistance, Red-to-Green, ORB) with their entries, stops and targets | Book, Ch. 7 |
| Risk rules: never risk > 2 % of the account, minimum 2:1 reward:risk, stop at a *technical* level, not a convenient one | Book, Ch. 3 |
| Time-of-day rules (Open / Mid-day / Close, which strategies belong where) | Book, Ch. 7 |
| Candle classification (bullish, bearish, doji, hammer, shooting star) | Book, Ch. 6 |
| Expiry choice, strike choice (delta ≈ 0.60), liquidity filters, option-level R:R, contract sizing | **Mine – not in the book.** Every such spot in the code is tagged `[NOT FROM BOOK]`. |

**2. It is a scanner, not a trading system.** The author's Rule 10 is *"Indicators only
indicate; they should not be allowed to dictate,"* and he is blunt that a purely mechanical
system loses to institutional algorithms. He uses scanners (Trade Ideas) to find candidates and
then decides by hand. This script is that scanner. It prints a plan and the reasoning; you decide.

**3. It has not been backtested and makes no performance claim.** Unusual Whales does carry
historical per-contract option data (2-year lookback), so a backtest is now *possible* — it just
has not been built, and until it is, nothing in the output is a probability or an expected return. The `score` is a ranking heuristic so you look at the best
candidate first, nothing more.

**4. Data sources.** Two backends behind one interface (`datasources.py`); `--source auto`
picks Unusual Whales when `UW_API_KEY` is set, else Yahoo.

| | Unusual Whales (API Basic) | Yahoo (free fallback) |
|---|---|---|
| Latency | **real-time** — measured tape lag 0.0 min on 2026-09-11 | ~15 min delayed |
| Volume | **consolidated** — 5-min bars matched Yahoo's SIP tape at ratio 1.00 | consolidated |
| Pre-market volume | **yes** (bars tagged `pr`) → the 50k-share Gappers rule is evaluated | **no** — every pre-market bar has volume 0; reported as *unavailable*, never faked |
| Universe | screener: \|change\| ≥ 2 %, price band, ETFs excluded (`--include-etfs`), ranked by relative volume | screener, ranked by \|gap\| |
| Float / short interest | `shorts/{t}/interest-float/v2` (biweekly) | sometimes present |
| Catalyst | per-ticker earnings calendar: report within the last 3 days (or yesterday's post-market) → `EARNINGS (...)` | Yahoo earnings timestamp |
| Option chain | NBBO bid/ask, OI, volume, IV **and the vendor's delta** | delayed chain; delta estimated with Black-Scholes |
| Requests | ~4 per ticker per scan (+2 per signal); 40k/day plan limit | rate-limited, be gentle |

The Bull Flag momentum strategy is still reported as *low* confidence even on real-time data:
the book says it needs a hotkey execution platform and 1-minute reads, which a 5-minute scanner
cannot replace. The catalyst check ("why did it gap?") is manual in the book too — the script
flags earnings and prints headlines; it cannot judge news.

**5. What the live runs showed (11 Sep 2026, 15:14–15:19 ET, 25–30 gappers):** the stock
setups pass the book's 2:1 test, but **almost none survive translation to options**, because
the book's stops are tight (typically 0.1–0.5 × ATR) and the option bid/ask spread alone was
20 %–1100 % of the delta-equivalent stock risk. The script prints this number on every plan.
The names where the translation *does* work are mega-liquid tickers with penny-wide spreads
(SPY, QQQ, AAPL, NVDA …) — and those are exactly the names the book says **not** to trade
unless they have unusual relative volume and a catalyst that day. That tension is real and the
script does not hide it.

## Install

```bash
pip install yfinance pandas numpy
```

Set the Unusual Whales key as an environment variable (User scope on Windows). The scanner reads
it from the process environment or the Windows User environment; it never prints it:

```powershell
[Environment]::SetEnvironmentVariable("UW_API_KEY", "<your key>", "User")
```

`python uw_diagnostic.py` runs ~8 read-only requests and reports latency, pre-market volume,
volume coverage vs Yahoo, and option-chain fields — run it once to confirm what your plan delivers.

`ca_bundle.pem` in this folder is the certifi CA bundle plus the AVG Antivirus root that
intercepts HTTPS on this machine. The scripts pick it up automatically. If you run this somewhere
without AVG, delete the file.

## Run

```bash
python aziz_options_scanner.py
```

Default mode runs the book's pre-market Gappers scan on the Unusual Whales screener (|change|
≥ 2 %, price ≥ $5, ETFs excluded), keeps the top 40 by relative volume, and evaluates every
setup on the latest 5-minute bar. With `--source yahoo` the universe comes from Yahoo's screener
ranked by |gap| instead.

```bash
python aziz_options_scanner.py -t AAPL NVDA TSLA          # your own list
python aziz_options_scanner.py --watchlist mylist.txt     # one ticker per line
python aziz_options_scanner.py --account 50000 --risk-pct 1.5
python aziz_options_scanner.py --show-rejected            # print every setup and why it was rejected
python aziz_options_scanner.py --include-not-in-play      # also show setups on stocks that fail the Stocks-in-Play gate
python aziz_options_scanner.py --no-midday                # skip 11:00–15:00 (book: "the most dangerous time")
python aziz_options_scanner.py --orb-minutes 15           # 15-min opening range instead of 5
python aziz_options_scanner.py --min-dte 0                # allow 0DTE (off by default)
python aziz_options_scanner.py --no-options               # stock-level plans only
python aziz_options_scanner.py --source yahoo             # force the free delayed backend
python aziz_options_scanner.py --include-etfs             # keep ETFs in the screener universe
python aziz_options_scanner.py --replay 2026-09-10 --replay-time 09:50 -t HPQ MXL   # what the rules said at 09:50 that day (stock only)
```

`--risk-pct` is capped at 2 — the book's absolute maximum.

Every run writes `signals/signals_<date>_<time>.json` with the full plan, every scanned stock's
context, and every rejection.

## Reading the output

```
#1  MXL  LONG  MATrend   score 62.0   confidence medium   bar 2026-09-11 15:15 (close)   [not actionable as an option]
    STOCK PLAN  entry 76.02  stop 75.41  target 78.15  risk/sh 0.61  reward/sh 2.13  R:R 3.49:1
      - 6 bars closed above a rising 9 EMA (75.83); this bar pulled back to it and held
      - stop = 9 EMA - 0.42; target = next level 78.15(dailyx2)
    STOCK IN PLAY: YES  gap +2.1%  relvol 1.2x  ATR 4.18  premkt vol n/a ...
    CATALYST: unknown - check the news yourself (book: this step is manual)
    OPTION [not from book]  MXL260918C00075000  CALL 75 exp 2026-09-18 (7 DTE)  bid/ask 4.1/4.5 (spread 9.3%) ...
      est. value if stock hits target: 5.2   if stock hits stop: 3.55   option R:R 0.74:1
      size: 2 contract(s)  planned risk $187.21  MAX LOSS (premium) $900.00
      note: bid/ask spread costs $40/contract = 121% of the delta-equivalent stock risk ($33) ...
      note: option-level reward:risk 0.74 < 2.0 after spread/theta -- the stock setup passed 2:1 but the option does not
```

- **STOCK PLAN** is the book's trade: entry, technical stop, next-level target, per-share R:R.
- **STOCK IN PLAY** is the Ch. 4 gate. `NO` means the book would not trade it at all.
- **OPTION** is the translation. `est. value if …` re-prices the contract with Black-Scholes at
  the stock target/stop, same IV, same-day exit, selling at the bid. Rough, especially ≤ 1 DTE.
- **planned risk** assumes you exit when the stock hits the stop. **MAX LOSS (premium)** is what
  you lose if you cannot (halt, gap, no bid). Both are always printed.
- **[ACTIONABLE]** means: a liquid contract exists, at least one contract fits your risk budget,
  and the *option-level* R:R still clears 2:1. Only these are worth acting on.
- **CONFLICT** means opposite-direction setups fired on the same ticker. Book: stand aside.
- A strategy name like `MATrend+SupportResistance` means two setups produced the *same* plan
  (same direction, stop and target) — one trade with two reasons, merged.
- A stop closer than max(10¢, 10 % of ATR) is rejected as noise ("price is sitting on the
  level"): the book's stop must be a technical level, and a 6-cent stop just inflates R:R.
- **REJECTED SETUPS** (with `--show-rejected`) is the most educational part: it shows how often
  the book's rules say *no* and why — wrong time of day, R:R under 2, no level to target,
  chasing an old breakout, price chopping on VWAP, etc.

## How each setup is detected (5-minute bars, regular session only)

| Strategy | Trigger | Stop | Target |
|---|---|---|---|
| VWAP | last 3 closes on one side of VWAP after testing it; price within 0.5 ATR of VWAP; rejects if chopping on VWAP for 6 bars | VWAP ± buffer (models "5-min close through VWAP") | next daily level |
| ORB | opening range (5/15/30/60 min) < 0.75 × ATR; 5-min close outside it within the last 2 bars; price on the right side of VWAP; not extended > 0.5 ATR | VWAP ± buffer | next daily level |
| Bottom / Top Reversal | ≥ 4 consecutive same-colour candles that moved ≥ 0.5 ATR; RSI ≤ 20 / ≥ 80 (high confidence at ≤ 10 / ≥ 90); extreme within ~0.3 ATR of a daily level; indecision or strong reversal candle; first new 5-min high/low | low/high of day ± buffer | nearest of VWAP / 9 EMA / 20 EMA / next level that still gives 2:1 (skipped ones are listed) |
| MA Trend | 6 closes above a rising (below a falling) 9 EMA; this bar pulled back into the EMA and held | 9 EMA ∓ buffer | next daily level (book rides until the MA breaks) |
| Support / Resistance | indecision candle whose wick touches a daily level and closes on the right side | level ∓ buffer | next daily level |
| Red-to-Green | gapped through prev close; now on the right side of VWAP; 3 rising (falling) closes on rising volume toward prev close | VWAP ∓ buffer | previous day close |
| ABCD | A→B ≥ max(0.75 ATR, 2 %); C retraces 20–70 % and holds ≥ 2 bars; price ticking up near C | below C | B (D = C + (B−A) printed as projection, not from book) |
| Bull Flag | pole ≥ max(0.5 ATR, 2 %) in ≤ 6 bars ending at its high; 2–6 bar flag under 50 % of the pole; breakout close on volume | below the flag | pole projection (not from book) |

Buffer = max($0.05, 0.1 × ATR), modelling the book's "break and close beyond the level".
Daily levels: highs/lows (wicks, per the book) of the last 60 daily bars, clustered, ≥ 2 touches,
within 3 ATR of price; plus previous close, pre-market high/low, and half/whole dollars for
stocks under $10.

`test_detectors.py` feeds each detector a synthetic session shaped like the book's diagrams and
checks it fires. It proves the code recognises the shapes; it says nothing about profitability.

## Options layer — the choices I made and why (none of this is in the book)

- **Expiry:** nearest with 1–21 DTE. 0DTE is off by default because gamma/theta make the
  delta-based estimates unreliable; `--min-dte 0` enables it.
- **Strike:** closest to |delta| 0.60 among *liquid* contracts (OI ≥ 100, volume ≥ 10, spread ≤
  10 % of mid). Slightly in-the-money tracks the stock move with less extrinsic value and IV
  sensitivity. If nothing liquid exists you are shown the best quoted contract and told it is
  probably untradeable.
- **Sizing:** contracts = floor(risk budget ÷ planned loss per contract), then capped so total
  premium ≤ 5 % of the account. This is the book's three-step share-size rule applied to
  contracts. The premium (max loss) is always shown separately.
- **Option R:R:** buy at the ask, sell at the bid, re-priced at the stock target and stop with
  the chain's IV. This is where most plans fail — see point 5 above.

## What would make this genuinely better

1. ~~A real-time data feed~~ — done (Unusual Whales).
2. ~~A pre-market volume source~~ — done.
3. Historical options data, so the translation could be backtested instead of reasoned about.
   UW's `option-contract/{id}/historic` and `/intraday` (2-year lookback) make this possible;
   not built yet.
4. Paper-trade logging (Alpaca) so plans are recorded against real fills — the book insists on
   months in a simulator before real money.
5. Your own catalyst judgement every morning. The author does this by hand for a reason.

## Files

- `aziz_options_scanner.py` — the scanner (setups, levels, risk rules, options layer, report)
- `datasources.py` — `UnusualWhalesSource` / `YahooSource` behind one interface
- `uw_diagnostic.py` — read-only check of what the UW key delivers
- `test_detectors.py` — synthetic-shape tests for the nine setups
- `ca_bundle.pem` — CA bundle for this machine (certifi + AVG root)
- `signals/` — JSON output per run

## Catalyst grading (`catalyst.py`)

The book's catalyst check is now mechanical, from two of its own rules:

- **Ch. 4 catalyst list** → headline classifier. UW headlines from the last 36 h *about this
  ticker* (≤ 3 tickers tagged, not a market wrap) are keyword-matched to: earnings, guidance,
  FDA, M&A (**grade A**); contract, product/partnership, offering, management/restructuring,
  buyback/split, analyst, legal (**grade B**). An earnings report in the last 3 days (calendar) is A.
- **Rule 4** ("is it the market moving, or the stock?") → the stock's % change vs SPY and vs its
  sector ETF (`market/sector-etfs`, one call). Independent = excess ≥ 1.5 pts and ≥ half the
  stock's own move.

| Grade | Meaning | Scanner | Paper trader (default `--min-catalyst-grade B`) |
|---|---|---|---|
| A | earnings / guidance / FDA / M&A | in play | trades |
| B | secondary catalyst headline | in play | trades |
| C | no headline, but independent of sector on ≥ 1.5× rel vol or a ≥ 5 % move | in play, flagged | skipped unless `--min-catalyst-grade C` |
| D | no headline, move too small to judge | in play, flagged | skipped |
| F | moving with its sector/SPY | **not in play** (Rule 4) | never |

Every grade prints its evidence (headline or the numbers), so a mislabel is visible. Keyword
classification will occasionally be wrong; a headline the feed didn't carry yields C/D, not F.
Verified on 2026-09-11: ACVA +44 % → A [m&a] "Copart to Acquire ACV"; KR, GME → A [earnings];
LEN +2.2 % with sector +0.9 % and no headline → F; MUFG (ADR) → D.

## Paper trading (Alpaca, paper endpoint only)

`paper_trader.py` forward-tests the signals on an Alpaca **paper** account, fully automatically
(entries, exits, journal) — the catalyst judgement is the grade above, not you. The base URL is
hard-coded to `paper-api.alpaca.markets`; there is no live switch. Keys go in `APCA_API_KEY_ID`
and `APCA_API_SECRET_KEY` (User-scope env vars; never printed).

```bash
python paper_trader.py --dry-run --once      # look, don't touch
python paper_trader.py                       # run from the entry window until 15:45
python paper_trader.py -t LEN UAL            # watchlist instead of the screener
```

Defaults encode the plan for a **$10k live account** (sizing uses `--account`, not the paper
balance): 1 % risk per trade, ≤ 2 contracts, spread ≤ 5 % of mid, entries only 09:45–11:00,
strategies VWAP / ORB / Support-Resistance / Red-to-Green, one position at a time, 3 trades a
day, daily loss limit 2 %, weekly 5 %, flat by 15:45. Exits are driven by the underlying
(5-min close through the stop; half off at target then break-even; runner out on a new 5-min
low/high; 20-min time stop) plus an option-level breaker at −40 % of premium.

Every closed trade is appended to `journal.csv` with stock-R, option-R and P&L. After ~40 trades
that file is the only honest answer to "does the translation work": expectancy per strategy, and
the gap between stock-R and option-R (the spread/theta cost).

The FINRA pattern-day-trader rule was eliminated effective 2026-06-04 (brokers have until
2027-10-20 to implement); `--max-day-trades-5d` exists only if your broker still enforces it.

## Running it unattended (Azure) and the private journal page

**Pending before you rely on it:** Alpaca paper keys set and `--dry-run --once` showing the account;
one real Open observed with a human watching; `az login` to your **personal** subscription (the CLI on
this machine is currently signed in to an employer tenant — the deploy script refuses those).

- `deploy/azure.sh "<subscription>"` — builds the image in the cloud (`az acr build`, no local Docker),
  creates a storage account (`journal` container, public-read blobs, CORS for svrtechservices.com),
  and a **Container Apps Job** on cron `25 13 * * 1-5` UTC. The container sleeps until 09:40 ET
  (`--wait-until`) so DST is handled in code, runs the day, exits after 15:45. Secrets are Container
  Apps secrets injected as env vars; nothing is baked into the image.
- The journal is durable in Blob (`journal.csv`, `paper_state.json`) — pulled at start, pushed after
  every trade — so the ephemeral container keeps history. On start the trader **reconciles** with
  Alpaca: a position it has no plan for is closed; a plan whose position vanished is journaled as
  closed externally.
- `journal_report.py` — expectancy, win rate, profit factor, drawdown, and stock-R vs option-R by
  strategy / catalyst grade / time of day / exit reason. Its output is embedded in the page.

**Private page.** svrtechservices.com turned out to be a Next.js static export on **Azure Static
Web Apps** (repo `ramakanthsathi/SVRSite`), not GitHub Pages — so the page is protected two ways:
1. `public/staticwebapp.config.json` routes `/journal/*` with `allowedRoles: ["journal"]`; anonymous
   visitors are 302-redirected to Microsoft login. Grant the role with
   `az staticwebapp users invite -n svr-website -g rg-svr-website --authentication-provider AAD
   --user-details <email> --role journal --domain www.svrtechservices.com` and accept the link.
2. The data itself (`today.json.enc`) is AES-256-GCM under a PBKDF2 key from `JOURNAL_PAGE_PASSPHRASE`,
   decrypted in the browser. The blob is public-read but unreadable without the passphrase.
Page: https://www.svrtechservices.com/journal/ (the registered custom domain is `www`).

**Deployed 2026-09-11** to the personal subscription: `rg-aziz-trader` — storage `stazizj68f9c45530`
(eastus), registry `acrazizt68f9c45530` (eastus), Container Apps environment `cae-aziz-trader` +
job `job-paper-trader` in **eastus2** (eastus had no Container Apps capacity that day). First manual
execution succeeded and published status from Azure. `deploy/job.sh start|list|stop` drives it via REST.
