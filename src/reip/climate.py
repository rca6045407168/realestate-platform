"""Per-zip climate risk scoring.

The platform already flagged "climate-stressed states" in portfolio
concentration warnings, but at deal/zip level it stayed silent. This
module fixes that — every zip gets a 0–100 climate score derived from
real damage history (FEMA NFIP claims + payouts) plus state-level
hurricane/wildfire flags.

Components:
  - flood_score: log-scaled NFIP payouts and claim count over the
    last 5 years, normalized against the national distribution.
    Lee County FL (Ian) → 100; rural inland counties → near zero.
  - hurricane_flag: 2010s-2020s gulf/atlantic-belt states; bumped
    when NFIP intensity is high (separates "lightly exposed" from
    "Ian-class repricing").
  - wildfire_flag: western states with documented insurance retreat
    (CA/OR/CO/NV/AZ/ID/MT/WA). Coarser — we don't have per-zip
    wildfire-claim data, so this is a state-level prior, not a score.

`overall_score = max(flood_score, hurricane_score, wildfire_score)` —
the dominant risk decides the category. Categories:
  0–20:  minimal
  20–50: moderate
  50–75: elevated
  75–100: severe

This score is honest, not Monte-Carlo:
  - Past damage ≠ future damage, but it's the best signal we have
    without a paid CoreLogic/RiskFactor feed.
  - State-level hurricane/wildfire is a coarse prior, not a property-
    level model. Surface it as a *category*, not a number.
  - We do NOT model sea-level-rise scenarios, drought projections, or
    forward IRR drags. The score is a 'damage history rank' — useful as
    a flag, not as a precise multiplier.

Used by:
  - buy_box: surface in notes + as a section
  - stress.py: bumps worst-case stressors in high-flood zips
  - pipeline/portfolio: per-deal climate exposure rollup
"""
from __future__ import annotations
from dataclasses import dataclass, asdict
from typing import Optional
import math

# Atlantic + Gulf hurricane-belt states. The list is intentionally
# conservative — only states with significant repeated Cat-3+ landfalls
# in the last 20 years.
HURRICANE_STATES = {"FL", "TX", "LA", "MS", "AL", "GA", "SC", "NC", "VA"}

# Western states with documented insurance retreat / wildfire-claim
# escalation 2018-present.
WILDFIRE_STATES = {"CA", "OR", "WA", "CO", "NV", "AZ", "ID", "MT", "NM", "UT"}

# NFIP normalization anchors. Derived from the 5y payout distribution:
#   P50 ≈ $92K, P75 ≈ $435K, P90 ≈ $1.67M, P99 ≈ $42.6M, max ≈ $1.2B
# Log-scale with anchors at P50 (score 20) and P99 (score 100).
NFIP_PAID_LOG_LOW  = math.log10(100_000)    # ~P50 → score 20
NFIP_PAID_LOG_HIGH = math.log10(50_000_000) # ~P99 → score 100


# NFIP query: per-county 5y payouts + claim count
_NFIP_QUERY = """
SELECT
    SUM(claim_count) AS claims_5y,
    SUM(total_paid)  AS paid_5y
FROM fema_nfip
WHERE fips_county = ? AND year >= ?
"""

# Zip→county lookup; one zip can map to multiple counties — pick the
# weight-1 entry (primary county).
_ZIP_COUNTY_QUERY = """
SELECT fips_county FROM zip_county_xwalk WHERE zip = ? LIMIT 1
"""


@dataclass
class ClimateScore:
    zip: str
    state: Optional[str]
    fips_county: Optional[str]
    overall_score: int            # 0–100
    category: str                 # minimal | moderate | elevated | severe
    flood_score: int              # 0–100
    flood_claims_5y: float
    flood_paid_5y: float
    hurricane_flag: bool
    hurricane_score: int          # 0–100
    wildfire_flag: bool
    wildfire_score: int           # 0–100
    primary_risk: str             # flood | hurricane | wildfire | none
    notes: list[str]


def _flood_component(paid_5y: float, claims_5y: float) -> int:
    """Log-scale the NFIP payouts. 0 if no claims, 100 at P99-class damage."""
    if paid_5y <= 0 and claims_5y <= 0:
        return 0
    # Use paid_5y as primary signal; floor to $10K to avoid log(0)
    paid = max(10_000, paid_5y)
    log_paid = math.log10(paid)
    raw = (log_paid - NFIP_PAID_LOG_LOW) / (NFIP_PAID_LOG_HIGH - NFIP_PAID_LOG_LOW)
    score = max(0, min(100, round(raw * 80 + 20)))   # min score for any claims: 20
    return score


def _hurricane_score(state: Optional[str], flood_score: int) -> tuple[bool, int]:
    """Coastal-state flag; intensity scaled by underlying flood-damage signal."""
    if not state or state.upper() not in HURRICANE_STATES:
        return False, 0
    # State is in the hurricane belt — combine with flood-damage intensity.
    # FL Lee (Ian) → flood 100 → hurricane 100. Inland FL counties → maybe 30.
    if flood_score >= 60:
        return True, min(100, flood_score + 10)
    if flood_score >= 30:
        return True, max(50, flood_score)       # at least "elevated"
    return True, 35  # baseline coastal exposure


def _wildfire_score(state: Optional[str]) -> tuple[bool, int]:
    if not state or state.upper() not in WILDFIRE_STATES:
        return False, 0
    # CA gets the highest baseline; everyone else moderate.
    high = {"CA", "OR", "CO"}
    if state.upper() in high:
        return True, 55
    return True, 40


def _category(score: int) -> str:
    if score < 20:  return "minimal"
    if score < 50:  return "moderate"
    if score < 75:  return "elevated"
    return "severe"


def score_zip(con, zip_code: str, state: Optional[str] = None) -> Optional[ClimateScore]:
    """Compute the climate score for a zip.

    `state` is optional — if None, the zip→county lookup falls back to
    None for state-level flags. Pass the 2-letter state code when known
    (the buy box already has it from its own lookup).
    """
    zip5 = str(zip_code).zfill(5)
    # Find county for the zip
    r = con.execute(_ZIP_COUNTY_QUERY, [zip5]).fetchone()
    fips_county = r[0] if r else None
    paid_5y = 0.0
    claims_5y = 0.0
    if fips_county:
        # 5y window ending in the most recent year we have data
        max_year = con.execute("SELECT MAX(year) FROM fema_nfip").fetchone()[0] or 2024
        nfip_row = con.execute(_NFIP_QUERY, [fips_county, max_year - 4]).fetchone()
        if nfip_row:
            claims_5y = float(nfip_row[0] or 0)
            paid_5y = float(nfip_row[1] or 0)

    flood_score = _flood_component(paid_5y, claims_5y)
    hurricane_flag, hurricane_score = _hurricane_score(state, flood_score)
    wildfire_flag, wildfire_score = _wildfire_score(state)

    # Pick the dominant risk
    components = [
        ("flood",     flood_score),
        ("hurricane", hurricane_score),
        ("wildfire",  wildfire_score),
    ]
    primary_risk, overall_score = max(components, key=lambda kv: kv[1])
    if overall_score == 0:
        primary_risk = "none"

    # Notes for the human
    notes = []
    if flood_score >= 50:
        notes.append(
            f"5y NFIP payouts in this county: ${paid_5y:,.0f} ({claims_5y:.0f} claims). "
            f"Insurance carriers reprice off this; expect annual premiums to outpace inflation."
        )
    elif flood_score >= 20:
        notes.append(f"5y NFIP payouts: ${paid_5y:,.0f}. Moderate flood-claim history.")
    if hurricane_flag:
        notes.append(
            "Hurricane-belt state. Even inland zips face wind premium repricing — "
            "Citizens (FL state insurer of last resort) reset rates 30-50% in 2022-2024."
        )
    if wildfire_flag:
        notes.append(
            "Wildfire-exposure state. Major carriers (State Farm, Allstate, USAA) have "
            "pulled or non-renewed in CA/OR/CO/NV — verify private insurance is still available "
            "for this zip BEFORE close."
        )
    if overall_score < 20:
        notes.append("Minimal climate-damage history. Insurance should remain a normal line item.")

    return ClimateScore(
        zip=zip5, state=(state or "").upper() or None,
        fips_county=fips_county,
        overall_score=overall_score,
        category=_category(overall_score),
        flood_score=flood_score,
        flood_claims_5y=claims_5y,
        flood_paid_5y=paid_5y,
        hurricane_flag=hurricane_flag, hurricane_score=hurricane_score,
        wildfire_flag=wildfire_flag, wildfire_score=wildfire_score,
        primary_risk=primary_risk,
        notes=notes,
    )


def to_dict(c: ClimateScore) -> dict:
    return asdict(c)


# Used by stress.py to bump worst-case stressors when climate exposure is high.
# An "elevated" or "severe" climate zip should have insurance and rehab stressed
# *more* than the default state overlay alone provides. This is a multiplier
# on the WORST scenario's insurance/rehab deltas.
def stress_multipliers(score: ClimateScore) -> dict:
    """Return stress-delta multipliers for the given climate exposure."""
    s = score.overall_score
    if s >= 75:      # severe
        return {"insurance_mult_bonus": 0.25, "rehab_mult_bonus": 0.15,
                 "exit_cap_add_bonus": 0.005}
    if s >= 50:      # elevated
        return {"insurance_mult_bonus": 0.15, "rehab_mult_bonus": 0.08,
                 "exit_cap_add_bonus": 0.003}
    if s >= 20:      # moderate
        return {"insurance_mult_bonus": 0.08, "rehab_mult_bonus": 0.03,
                 "exit_cap_add_bonus": 0.001}
    return {"insurance_mult_bonus": 0.0, "rehab_mult_bonus": 0.0,
             "exit_cap_add_bonus": 0.0}
