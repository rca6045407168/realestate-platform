# reip — Real Estate Investment Platform

End-to-end implementation of *Allocating Capital Across U.S. Real Estate: A
Framework for Appreciation, Cashflow, and Total Return* (working paper, May
2026). Pulls free public real estate datasets into a single DuckDB store,
ranks MSAs on the framework's two-axis Appreciation × Cashflow scoring
model, classifies each MSA into one of five archetypes, overlays the eight
sources of property-level alpha, and underwrites individual properties
with pro forma + DSCR + IRR + sensitivity.

Spiritual successor to [`project_pikachu`](https://github.com/rca241231/project_pikachu)
(2019, Redfin + AirDNA + BLS) and [`project_raichu`](https://github.com/rca241231/project_raichu).

What's new vs. pikachu:
- 14+ datasets instead of 3 — Zillow, Redfin, FHFA, ACS, IRS, BPS, BLS, FEMA, FRED, Saiz, Wharton
- MSA-level scoring (not zip), per the framework's Table 5 weights
- Two-axis output: Appreciation Score + Cashflow Score + blended Total Return
- Five archetype classifier: Coastal Gateway / Sun Belt Growth / Cashflow Heartland / Boom-Bust Beta / Resource & Niche
- Property-level alpha overlay: 8 flags + ARV / 70%-rule / BRRRR refinance
- Underwriting workspace: pro forma, DSCR, 5-yr IRR, equity multiple, sensitivity table
- DuckDB store (one file, fast analytics) instead of CSV append
- Resilient ingest: per-source caching, partial failure tolerated

## What's new (May 2026)

The platform got the operator layer it needed. Each surface is data-grounded:

| Surface | What it does | Data |
|---|---|---|
| **Stress test** | Multi-scenario underwriter — base/stress/worst with state-aware overlays (FL hurricane, TX tax, CA rent cap, rust-belt rehab) + GREEN/YELLOW/RED gate + walk-away price | Per-state effective tax rates, default opex/vacancy from ACS |
| **Buy box per zip** | Translates a zip's macro data into target price/rent/rehab bands + trend ARV + sales-based ARV + climate exposure + "typical deal" you can stress-test in one click | ZHVI / ZORI / state tax / Redfin Data Center sales |
| **Climate risk overlay** | Per-zip 0-100 score from real FEMA NFIP damage history. Fort Myers FL (Lee County, Hurricane Ian) lands at 100/100 — $1.2B in 5y payouts. Amplifies worst-case stress scenarios | FEMA NFIP claims by county-year |
| **Deal pipeline** | localStorage pipeline with status pills (researching → underwritten → offer → closed/passed), notes, climate column, side-by-side compare modal | Client state |
| **Portfolio view** | Roll-up across saved deals: equity, monthly cash flow (pre + post-tax), weighted IRR, depreciation tax savings, concentration warnings (single-state / climate-correlated / RED-equity / thin DSCR), per-deal pre-vs-post-tax table | tax.py models active vs passive deduction |
| **50-year strategy tab** | Reproducible empirical analysis: 8-regime decomposition, drawdown panel, momentum transition matrix, 34-year strategy backtest, rent yield panel — all against current FHFA HPI + ZORI | 410 metros 1975-2025, 264 ZORI metros 2015-2024, 15K momentum transitions |
| **Ask reip chat** | Pipeline-aware — chat sees your saved deals + notes + verdicts and references them in answers. Can call `buy_box`, `stress_test`, `top_zips`, `top_msas`, `parse_remarks` as tools | Claude Sonnet, OAuth fallback to Claude Code |
| **Top Zips diversify mode** | "Diversify from my pipeline" checkbox — computes state-concentration from saved equity and demotes already-concentrated states in the ranking | Pipeline localStorage + zip_returns rank |
| **Data freshness badge** | Publication-lag-aware: only flags "stale" when the publisher has released a newer file we haven't pulled. Click for per-source breakdown | source-by-source cadence rules |

### Strategy summary (from `docs/STRATEGY.md`)

50-year backtest findings:

- **Geography matters more than timing.** Median 30y CAGR has 1.8pp spread; within-regime spreads are 8-12pp/yr.
- **Median worst drawdown: -16%, median time-to-recover: 9.8 years.** Worst-decile metros (Merced -65%, Vegas -61%, Modesto -60%) took 14-15 years.
- **3-year momentum is real.** Top-Q-past stays top-half 64% of the time. Forward 3y return is +18% (Q1) vs +10.6% (Q4) — 7.4pp/yr edge.
- **Sun Belt Growth beat CA Coastal** over 34 years (+5.09% / -26% DD vs +4.66% / -36% DD).
- **Yield ≠ no-growth.** Correlation only -0.26. Rockford IL: 8.77% yield AND +183% appreciation 2015-2024.

Recommended allocation: 40% Sun Belt Growth (momentum-screened) / 30% All-Weather lifestyle / 20% Cashflow Heartland / 10% speculative. Climate-severe capped at 10%. Hold ≥10 years.

Run it: `reip strategy backtest` or browse the **Strategy** tab in the SPA.

## Quick start

```bash
uv venv --python 3.11 --managed-python
source .venv/bin/activate
uv pip install -e .

reip init                                  # create schema
reip ingest                                # pull all default sources

reip serve                                 # API + web UI on http://localhost:8787
reip msa-rank --top 25                     # CLI alternative
```

Total cold ingest is ~10–15 minutes (Redfin's 1GB TSV dominates). Subsequent
runs are cache-hit.

## Web UI (`reip serve`)

FastAPI backend + single-file SPA. Four screens:

- **MSA dashboard** — sortable / filterable rankings with archetype color
  coding and completeness bars; click any row for the full breakdown.
- **Underwriting workspace** — paste financing assumptions, get a single
  GREEN / YELLOW / RED verdict, plain-English reasons, BRRRR refi,
  5-yr IRR, and a sensitivity grid behind a 'show me the math' toggle.
  Mitigation checkboxes live-update the verdict (no re-submit needed).
- **AVM signal** — zip-level ZHVI vs Redfin sale divergence, hot/cold/aligned.
- **Remarks parser** — paste any MLS public-remarks blob, see the 7-flag
  alpha stack and matched terms.

API surface (auto-documented at `/docs`):

```
GET  /api/msas                        ranked MSAs (sort_by, archetype, min_pop, limit)
GET  /api/msas/{cbsa_code}            full breakdown + percentile ranks
GET  /api/avm                         zip mispricing (direction=cold|hot|all)
POST /api/remarks                     parse free-text MLS remarks
POST /api/underwritings               pro forma + sensitivity + recommendation
POST /api/underwritings/mitigations   re-run gate with verified mitigations
GET  /api/coverage-map                county-level coverage (Phase-2 stub)
```

## Recommendation gate (Framework §9.4)

Three verdicts. **The thresholds are the platform's moral center — not
softened when a deal sits just below.**

```
GREEN_THRESHOLDS = {
    "stabilized_dscr_min":               1.30,
    "refi_appraisal_stress_min_ltv":     0.70,
    "insurance_trend_max_pct":           0.20,
    "climate_pct_max":                   0.75,
    "alpha_stack_min_flags":              2,
    "stress_min_coc_on_residual":        0.08,
}
YELLOW_THRESHOLDS = { "stabilized_dscr_min": 1.10, "stabilized_dscr_max": 1.30 }
# RED: any YELLOW threshold fails without verified mitigations,
#      OR DSCR < 1.10, OR climate top decile, OR sensitivity → negative CF.
```

**Memphis fixture acceptance** (long paper Table 9): 1.07× stabilized DSCR
screens RED on default rules, upgrades to YELLOW only with all 5 verified
mitigations (term sheet + reserves + contractor bid + hard-money primary &
backup + LTR fallback PM). The tests assert exactly that.

Mitigation rules:

```python
REQUIRED_MITIGATIONS_BY_FAILURE = {
    "thin_dscr":           ["verified_70pct_ltv_term_sheet",
                            "documented_capital_reserve_min_25k"],
    "rehab_overrun_risk":  ["signed_contractor_bid"],
    "financing_risk":      ["committed_hard_money_primary",
                            "committed_hard_money_backup"],
    "exit_risk":           ["ltr_fallback_pm_identified"],
}
```

## Data sources

Every signal answers one of three investment questions: **yield**, **growth**,
or **left-tail risk**.

| Source | Geography | Signal type | Auth |
|---|---|---|---|
| **Zillow ZHVI** | zip, monthly | growth (price level + appreciation) | none |
| **Zillow ZORI** | zip, monthly | yield (observed rent index) | none |
| **Redfin Data Center** | zip + county, weekly | growth + risk (DOM, sale-to-list, inventory) | none |
| **IRS SOI migration** | county-pair, annual | growth (net AGI inflow) | none |
| **Census Building Permits Survey** | county, monthly | growth (supply pipeline) | none |
| **FEMA NFIP claims** | county, annual | risk (climate/flood loss) | none |
| **HUD Fair Market Rents** | zip, annual | yield fallback when ZORI sparse | free token |
| **BLS QCEW** | county, quarterly | growth (employment + wages) | none |
| **FRED** | national, monthly | macro (mortgage rate, CPI shelter, HPI) | free key |
| **Census ZCTA → County** | static | crosswalk for joining zip ↔ county data | none |
| **Redfin live listings** | per-property | property-level cash-flow modelling (ported from pikachu) | scraped |

Optional `.env`:

```
HUD_API_TOKEN=        # https://www.huduser.gov/hudapi/public/register
FRED_API_KEY=         # https://fred.stlouisfed.org/docs/api/api_key.html
CENSUS_API_KEY=       # https://api.census.gov/data/key_signup.html
```

## MSA scoring model (Framework Table 5)

Two scores per CBSA. Per the framework: NO trailing price appreciation
appears as a feature — only leading indicators. Weights:

| Group | Component | Weight | Source |
|---|---|---|---|
| Demand 40% | 5-yr population CAGR | 10% | Census ACS |
|  | 5-yr employment CAGR | 10% | BLS QCEW |
|  | 5-yr median household income CAGR | 10% | Census ACS |
|  | Net domestic migration % of pop | 10% | IRS migration |
| Supply 20% | Permits per 1,000 households (3y) | 10% | Census BPS |
|  | Months of inventory | 5% | Redfin |
|  | Saiz supply elasticity | 5% | Saiz (2010) / Wharton |
| Pricing 20% | Gross rent yield | 10% | ZORI / ZHVI |
|  | Price-to-income (inverted) | 5% | ZHVI / ACS income |
|  | 12-mo DOM trend (inverted) | 5% | Redfin |
| Risk 20% | Climate / flood claims | 5% | FEMA NFIP |
|  | Insurance trend proxy | 5% | FEMA paid trend |
|  | Regulatory friction (WRLURI) | 5% | Wharton 2018 |
|  | Effective property tax rate | 5% | Tax Foundation |

```
appreciation_score = Σ(demand+supply weights × z(factor)) − 0.5 × risk
cashflow_score     = Σ(yield weights × z(factor)) − 0.5 × risk
total_return       = blend × appreciation_score + (1−blend) × cashflow_score
```

Robust z = (x − median) / IQR so a few mega-MSAs don't dominate.

## Archetype classifier (Framework §4)

Each MSA classified by yield, growth, and named-market heuristics:

- **Coastal Gateway** — yield ≤ 4%, low growth, supply-inelastic
- **Sun Belt Growth** — yield 5–7%, pop CAGR ≥ 1.5%
- **Cashflow Heartland** — yield ≥ 7%, pop CAGR ≤ 0.5%
- **Boom-Bust Beta** — Las Vegas, Phoenix, Riverside, Cape Coral, Reno
- **Resource & Niche** — small / lifestyle / energy / college / STR-heavy

## Property-level alpha overlay (Framework §5)

The eight durable sources of alpha. Computed per listing in `redfin_listings`:

| Flag | Source |
|---|---|
| `flag_fixer_upper` | listed price < (ARV − rehab) × 0.80 |
| `flag_distressed`  | long DOM + below comp psf |
| `flag_long_dom`    | DOM > 60d |
| `flag_motivated_language` | regex parser on `public_remarks` (`reip remarks`) |
| `flag_assumable`   | regex parser on `public_remarks` (FHA/VA/USDA assumable) |
| `flag_information_avm` | zip in 'cold' AVM band (Redfin sales ≤ −1σ vs ZHVI) |
| `flag_adu_eligible` | state-level proxy (CA/OR/WA/MN/CO) + `public_remarks` use-change parse |
| `flag_price_cuts`  | requires RESO `price_change_timestamp` (TODO) |
| `flag_oz`          | Opportunity Zone tract overlay (TODO) |

Plus `arv_estimate`, `rehab_estimate`, `max_70_rule_bid`, and an
`alpha_stack` count (deals stacking 2–3+ sources are the framework's prized
"4-stack" — distressed seller × value-add × use-change × operational).

## Underwriting workspace (Framework §8)

```
reip underwrite --price 70000 --rehab 25000 --arv 130000 --rent 1200 --rate 0.075
```

Returns: pro forma year 1 (NOI, DSCR, cap rate, cash-on-cash), BRRRR
refinance module (cash-out, equity-left-in, post-refi cash flow,
infinite-return flag), 5-year IRR + equity multiple at terminal cap,
optional sensitivity table over rent ±10%, vacancy ±200bp, exit cap ±100bp.

## Output

`reip top` prints to stdout and optionally writes `data/ranked.csv` with all
features. Sample top-rated zips on a 2026-05 ingest:

| zip | locale | ZHVI | YoY | yield | DOM | net AGI inflow | score |
|---|---|---|---|---|---|---|---|
| 14441 | Hilton, NY (Rochester) | $245k | 3.4% | n/a | 6 | $848k | 5.75 |
| 14608 | downtown Rochester, NY | $121k | 8.9% | 13.7% | 20 | $30M | 2.74 |
| 43609 | Toledo, OH | $70k | 7.8% | 17.6% | 46 | $14M | 2.49 |
| 63133 | St. Louis, MO | $79k | 6.0% | 19.2% | 40 | $9.6M | 2.45 |
| 14611 | Rochester, NY | $116k | 6.2% | 14.9% | 13 | $30M | 2.32 |

## Operator-quality features

| Feature | What it gives you | Inspired by |
|---|---|---|
| Rich CLI tables | Color-coded archetype, +/- score tinting, completeness bars | Shiki |
| `reip diff` | Largest movers vs. previous snapshot — alpha lives in regime changes | Diff viewer |
| `reip freshness` | Per-source data age + cadence-aware stale flag | Token counter |
| `reip refresh` | Only re-pulls sources past their cadence | LiteLLM router |
| `reip backtest` | Golden-ranking regression test (Memphis is always Cashflow Heartland, Austin is always appreciation > cashflow) | Promptfoo |
| `reip report` | Self-contained HTML w/ sortable MSA table, JS underwriting calculator, and per-MSA ZHVI sparklines (no server) | Pyodide |
| `reip avm` | Zip-level AVM mispricing signal — hot/cold zips where Redfin sales diverge from ZHVI smoothed index | Information alpha (§5.7) |
| `reip remarks` | Free-text MLS-remarks parser — motivated, distressed, use-change, assumable, price-cut, short-sale, probate flags | Behavioral alpha (§5.8) |

## CLI

```bash
reip init                                            # create schema
reip ingest                                          # all sources, unconditional
reip ingest --only zillow --only redfin              # subset
reip refresh                                         # smart: only stale sources
reip refresh --dry-run                               # show what's stale, don't pull
reip freshness                                       # per-source data-age table
reip status                                          # row counts

# MSA-level (framework Table 5)
reip msa-rank --top 25                               # by total return
reip msa-rank --by appreciation --top 15
reip msa-rank --by cashflow     --top 15
reip msa-rank --archetype "Sun Belt Growth"          # filter by archetype
reip msa-rank --blend 0.7 --top 10                   # 70/30 weighting toward appreciation
reip archetype Memphis                               # one-MSA factor breakdown

# Diff vs. previous snapshot (auto-written by every msa-rank run)
reip diff --by total --top 20
reip diff --by appreciation
reip diff --by cashflow

# Property-level
reip alpha                                           # 8-flag overlay on listings
reip underwrite --price 70000 --rehab 25000 --arv 130000 --rent 1200 --sensitivity

# Information / Behavioral alpha
reip avm --direction cold --top 20                   # zips selling below ZHVI = buy candidates
reip avm --direction hot  --top 20                   # zips selling above ZHVI = momentum
reip remarks "motivated seller, sold as-is, 3.25% assumable VA"

# Quality gate + interactive report
reip backtest                                        # 8-MSA golden ranking test
reip report --out data/reip-report.html              # self-contained HTML, opens from disk

# Legacy zip-level (pikachu compatibility)
reip top --top 25 --min-completeness 0.85
```

## Sample MSA outputs (2026-05-08 ingest)

**Top by Cashflow Score** — matches the Cashflow Heartland thesis:

| CBSA | MSA | Yield | Pop CAGR | Cashflow Score |
|---|---|---|---|---|
| 32820 | Memphis, TN-MS-AR | 7.78% | -0.07% | +0.106 |
| 38300 | Pittsburgh, PA | 8.87% | +0.14% | +0.114 |
| 40380 | Rochester, NY | 7.99% | +0.18% | +0.083 |
| 28700 | Kingsport-Bristol, TN-VA | 7.80% | +0.21% | +0.087 |

**Top by Appreciation Score** — matches the migration-driven Sun Belt / Mountain West thesis:

| CBSA | MSA | Pop CAGR | Net Migration | Appr Score |
|---|---|---|---|---|
| 17660 | Coeur d'Alene, ID | +2.96% | +4.43% | +0.381 |
| 24540 | Greeley, CO | +2.91% | +5.96% | +0.377 |
| 22660 | Fort Collins-Loveland, CO | +1.46% | +4.59% | +0.332 |

**`reip archetype Austin`**:
```
Austin-Round Rock-San Marcos, TX  (12420)
  archetype:           Mixed
  population:          2,357,497
  5y pop CAGR:         +2.75%
  net migration % pop: +3.89%
  permits/1000 HH:     7.23           ← oversupply signal
  gross yield:         4.77%          ← below Sun Belt floor
  Saiz elasticity:     3.0
  Wharton WRLURI:      -0.2
  Appreciation Score:  +0.148
  Cashflow Score:      -0.101
  Total Return Score:  +0.024
```

## Schema

`store.py` defines one raw table per source plus a `zip_county_xwalk`. Joins
happen in `score.py` via a single SQL view. Add a loader:

1. Drop a module under `src/reip/loaders/` exposing `load(con, **kwargs) -> int`.
2. Register it in `cli.py` `SOURCES`.
3. Define its raw table in `store.py` `SCHEMA`.

## What's deliberately not built (yet)

- **First Street Foundation** climate scores — needs registration; left as a
  free-tier integration point.
- **ATTOM / county assessor sold comps** — paid, but the highest-leverage upgrade.
- **MLS via IDX/RETS** — gated, broker-licensed.
- **CoreLogic insurance loss costs** — paid.
- **AirDNA STR** — pikachu's tokens are stale; re-add once Richard refreshes them.

These are the "moat" datasets — buy them after a thesis is validated.
