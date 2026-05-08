# reip — Real Estate Investment Platform

Pulls free public real estate datasets into a single DuckDB store, then ranks
US zip codes on a yield × growth × risk composite. Spiritual successor to
[`project_pikachu`](https://github.com/rca241231/project_pikachu) (2019,
Redfin + AirDNA + BLS) and [`project_raichu`](https://github.com/rca241231/project_raichu).

What's new vs. pikachu:
- 7+ datasets instead of 3, with weekly/monthly history not just snapshots
- DuckDB store (one file, fast analytics) instead of CSV append
- Composite score with completeness audit per zip
- Resilient ingest: per-source caching, partial failure tolerated

## Quick start

```bash
uv venv --python 3.11 --managed-python
source .venv/bin/activate
uv pip install -e .

reip init                                  # create schema
reip ingest                                # pull all default sources
reip top --top 25 --min-completeness 0.85  # ranked zips
```

Total cold ingest is ~10–15 minutes (Redfin's 1GB TSV dominates). Subsequent
runs are cache-hit.

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

## Score

```
score = w_yield × z(yield) + w_growth × z(growth) − w_risk × z(risk)
```

- **yield** = `ZORI × 12 / ZHVI` (gross), capped at 20% to drop vacation-rental noise; HUD FMR 2BR fallback
- **growth** = `z(YoY ZHVI appreciation)` + `z(net IRS AGI inflow)` − `z(permits 12mo)` *(oversupply penalty)*
- **risk** = `z(FEMA NFIP claims)` + `z(median DOM)` − `z(sale-to-list)` *(seller power lowers risk)*

All sub-scores use **robust z** ((x − median) / IQR) so a few mega-counties
don't dominate. Dollar-denominated quantities are log-transformed first.

`completeness` (0–1) reports the fraction of the seven input signals present
per zip, so you can filter to high-confidence picks (`--min-completeness 0.85`).

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

## CLI

```bash
reip init                                       # create schema
reip ingest                                     # all sources
reip ingest --only zillow --only redfin         # subset
reip ingest --refresh                           # bypass cache
reip status                                     # row counts
reip top --top 25 --min-completeness 0.85       # ranking
reip top --w-yield 0.6 --w-growth 0.3 --w-risk 0.1 --out picks.csv
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
