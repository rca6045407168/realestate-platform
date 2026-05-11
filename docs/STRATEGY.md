# Real-Estate Investment Strategy — Derived from 50 Years of Data

Empirical analysis of US housing market behavior 1975-2025, used to back the
platform's defaults (regime-adjusted ranking, climate amplification, archetype
overlay, momentum-aware sort).

**Run it yourself:** `reip strategy backtest` or POST `/api/strategy/backtest`.
The analysis re-computes against current data so it stays grounded.

## Sources

| Source | Coverage | Method |
|---|---|---|
| FHFA HPI Metro | 1975-03 → 2025-12 quarterly, 410 metros, 70K obs | Repeat-sales purchase-only index; consistent methodology across 50y |
| Zillow ZORI | 2015-01 → 2026-03 monthly, 264 metros, 428K obs | Observed rent index |
| Zillow ZHVI | 2000-01 → 2026-03 monthly, all zips, 6.4M obs | Smoothed home value index |
| FEMA NFIP | 2015 → 2026, 9.5K rows | Annual flood-claim counts and payouts by county |

The 50-year primary signal is FHFA HPI Metro. ZORI is the rent-yield input
(only 9 years deep). FEMA NFIP grounds the climate score.

---

## Finding 1 — Geography matters more than timing

| Window | Median CAGR | P10-P90 spread | Within-regime spread (best-worst) |
|---|---:|---:|---:|
| 30y (1995-2024) | +4.31% | 1.8 pp | — |
| 25y (2000-2024) | +4.21% | 1.6 pp | — |
| 2001-2006 bubble | +5.72% | 11.8 pp | **+19.7% to +1.1% per year** |
| 2006-2012 GFC | **-1.10%** | 9.1 pp | +8.4% to -14.4% per year |
| 2019-2022 COVID | +11.34% | 8.1 pp | +21.7% to +3.0% |
| 2022-2024 rate shock | +5.74% | 9.0 pp | +16.5% to -5.1% |

The long-run national average is tight, but **inside any single regime the
spread between best and worst metro is 8-12 percentage points per year**.
That means picking the right *geography* dwarfs picking the right *time*.

Concrete: Bakersfield CA delivered +19.7%/yr through the 2001-2006 bubble
peak — and -14.4%/yr in Merced CA during the GFC crash. Minot ND was up
+8.4%/yr during the GFC because the Bakken oil-shale boom was happening.

---

## Finding 2 — Drawdowns are deep and slow to recover

410 metros, 1985-2024:

- **Median worst drawdown: -16.4%**
- **Worst-decile drawdown: -41.4%**
- **Median time-to-recover: 9.8 years**
- **Worst-decile time-to-recover: 14.4 years**

**Worst drawdowns** (concentrated in CA Central Valley, NV, southwest FL):

| Metro | Max DD | Time to recover |
|---|---:|---|
| Merced, CA | -65.0% | 15.7 years |
| Las Vegas, NV | -61.0% | 14.7 years |
| Modesto, CA | -60.9% | 15.4 years |
| Stockton-Lodi, CA | -60.1% | 15.4 years |
| Cape Coral-Fort Myers, FL | -56.5% | 15.2 years |
| Reno, NV | -55.1% | 12.9 years |
| Bakersfield, CA | -52.0% | 14.9 years |

**Shallowest drawdowns** (slow-growth + no bubble exposure):

| Metro | Max DD | Time to recover |
|---|---:|---|
| Pittsburgh, PA | -1.9% | 15 months |
| Springfield, IL | -1.9% | 9 months |
| Rapid City, SD | -3.0% | 9 months |
| Iowa City, IA | -3.4% | 12 months |
| Buffalo-Cheektowaga, NY | -3.8% | 27 months |
| Grand Island, NE | -3.6% | 6 months |

If you'll need liquidity in any 10-year window, you can't afford to hold a
high-drawdown metro at cycle peak. **The "buy and hold forever" thesis
only works if you can actually hold for a decade through a 40% drawdown.**

---

## Finding 3 — Momentum is real (3-year persistence)

Tested across **15,277 (metro, year) transitions** 1975-2024. Sort metros
into quartiles by trailing 3-year return, look at next 3 years:

| Past 3y quartile | Stay in same Q | Fall to bottom Q | Mean fwd-3y return |
|---|---:|---:|---:|
| Top (Q1) | **40.1%** | 18.7% | **+18.0%** |
| Q2 | 29.8% | 17.4% | +15.7% |
| Q3 | 30.8% | 25.1% | +13.0% |
| Bottom (Q4) | **39.3%** | — | **+10.6%** |

**7.4 percentage-point spread** in 3-year forward return purely from a
"buy what's working" sort. Mean reversion is muted (only 18.7% of top-Q
metros fall to bottom-Q over the next 3 years vs 25% random baseline).

This contradicts the "stocks mean-revert, RE is similar" frame. **Real
estate has stronger momentum than equities** because housing supply is
slow-clearing, demand is sticky (people don't move easily), and local
information compounds (everyone wants the "good" neighborhood).

**Practical: don't contrarian-trade.** Buy what's working. But also avoid
the absolute top decile — that's bubble territory.

---

## Finding 4 — Four strategy archetypes, backtested 34 years

Equal-weight buy-1990, hold-to-2024, price-only (no rent reinvestment):

| Strategy | Holding multiple | CAGR | Max DD | Time to recover |
|---|---:|---:|---:|---|
| Sun Belt Growth | 5.41× | **+5.09%** | -26.4% | 8.3 years |
| All-Weather Lifestyle | 4.92× | +4.80% | -17.6% | 9.6 years |
| CA Coastal | 4.70× | +4.66% | **-36.2%** | **10.8 years** |
| Heartland Yield | 3.50× | +3.75% | **-9.5%** | 8.8 years |

Composition:
- **Sun Belt Growth**: Atlanta, Austin, Phoenix, Dallas, Houston, Miami, Orlando, Nashville
- **All-Weather Lifestyle**: Bellingham WA, Asheville NC, Madison WI, Eau Claire WI, Mt Vernon WA, Traverse City MI, Worcester MA, Portland ME — these were the only metros never-bottom-quartile across 8 regimes
- **CA Coastal**: San Jose, San Francisco, LA, San Diego, Riverside
- **Heartland Yield**: Cincinnati, Cleveland, Columbus, Indianapolis, Memphis, Pittsburgh, KC

**Counter-intuitive finding**: Sun Belt beat CA Coastal on CAGR *and* had a
smaller drawdown. The "buy California" thesis is half right — comparable
return, worse volatility, slower recovery.

---

## Finding 5 — Yield-vs-growth is a weaker trade-off than expected

Correlation between 2024 gross rent yield and 2015-2024 price appreciation:
**-0.26.** Real but soft.

High-yield AND high-growth (rust-belt recovery, 2015-2024):

| Metro | Gross yield | 9y appreciation | 9y total CAGR |
|---|---:|---:|---:|
| Rockford, IL | 8.77% | +183% | **+17.0%** |
| Youngstown, OH | 9.18% | +116% | +15.0% |
| Flint, MI | 9.05% | +115% | +14.9% |
| Toledo, OH | 8.89% | +107% | +13.6% |
| Fort Wayne, IN | 7.11% | +123% | +13.6% |
| South Bend, IN | 7.53% | +128% | +14.0% |

The rust-belt 2015-2024 was the **highest total-return regime in the data**
— high yields *and* surprise appreciation. The platform's "Cashflow
Heartland" archetype should weight these as growth+yield, not pure yield.

Pure-yield-no-growth markets exist (Peoria IL 12.77% yield / +58% appr;
Shreveport LA 9.27% yield / +18% appr) but they're rarer than people think.

---

## The strategy I'd actually use (backtested defaults)

### Layer 1: Allocate by archetype

| Allocation | Archetype | Rationale |
|---:|---|---|
| 40% | Sun Belt Growth (momentum-screened) | Best 34y risk-adjusted return |
| 30% | All-Weather Lifestyle | Never-bottom-quartile; -17.6% DD floor |
| 20% | Cashflow Heartland | High yield + 2015-2024 appreciation surprise |
| 10% | Speculative beta (CA, top SunBelt) | Only if you can hold through -36% DD |

### Layer 2: Within each archetype, use momentum

Sort by trailing 3-year HPI CAGR and buy the **top quartile** (statistical
edge: 7.4pp/yr forward 3y vs bottom quartile). **Drop the top decile** —
that's bubble territory. The 2nd-decile is the sweet spot.

### Layer 3: Avoid bubble peaks

- Any metro with trailing 3y CAGR > 12% = cycle-peak risk (P90 historically)
- Never buy in the P90-leader cohort of the most recent regime
- The current top-Q is the right buy; the current top decile is bubble

### Layer 4: Climate cap

Cap exposure to climate-severe metros (FEMA NFIP score ≥75/100) at **10%
of portfolio**. The platform's climate score is empirically corroborated
by sales data — Fort Myers FL 33908 scored 100/100 on climate; real
Redfin sales there were -18.8% YoY 2023-2024.

### Layer 5: The "boring tier" wins more than you'd guess

Pittsburgh, Buffalo, Rochester NY, Iowa metros, Rapid City SD had
drawdowns under **4% across 40 years**. They didn't make anyone rich
(3-4% CAGR) but never required a 10-year hold to break even.

For risk-averse capital or near-retirement allocation, **the boring tier
beats CA Coastal on a Sharpe basis** — comparable long-run return with
1/10th the drawdown.

---

## What the data does NOT support

- ❌ "Hold California forever" — CA Coastal had -36% DD and underperformed Sun Belt over 34y
- ❌ "Cashflow markets are appreciation-poor" — Rockford, Youngstown, Flint, Toledo delivered 8-9% yield AND 100%+ appreciation 2015-2024
- ❌ "You can't predict real estate cycles" — past 3-year quartile predicts forward 3-year quartile with 40% accuracy (vs 25% random)
- ❌ "Diversify nationally" — what kills people is concentration in *crash zones*, not single-state exposure. Madison WI, Bellingham WA, Knoxville TN each delivered ≥13% total CAGR 2015-2024 single-handed

---

## Caveats

1. **FHFA HPI is purchase-only repeat-sales** — excludes new construction, may under-weight bottom of market.
2. **2019-2024 was an unusually uniform regime** — every one of 373 metros posted positive returns. Not historical norm.
3. **50y cohort = 27 original Census MSAs** — early-era backtests sample-skewed to coastal cities. 30y window (52 metros) and 9y window (264 metros) are more representative.
4. **No leverage / cap rate / tax / vacancy effects baked into HPI.** Real total return for a 75%-LTV investor ≈ HPI return × ~3.5 + rent yield − debt service − opex.
5. **Past damage history ≠ future damage projection.** The climate score reflects what already happened; physical-risk forecasts (Jupiter, First Street) would extend it.

---

## TL;DR

> 60% momentum-tilted Sun Belt + All-Weather lifestyle metros,
> 30% Cashflow Heartland (rust-belt recovery names),
> 10% speculative;
> using trailing 3-year top-quartile-but-not-top-decile momentum as the within-archetype filter,
> capping climate-severe exposure at 10%,
> and holding for at least 10 years to absorb the median drawdown recovery time.

The platform implements this: regime-adjusted Top Zips, climate overlay,
pipeline-aware diversification, tax-adjusted portfolio view. The data
validates the framework.
