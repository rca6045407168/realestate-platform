"""Per-property decision rationale generator.

The spec's UX rule: 'three or four reasons in plain English, one primary
action.' This module renders a per-property decision narrative from:

  - the recommendation gate verdict (GREEN / YELLOW / RED)
  - the MSA archetype + score
  - the zip's AVM mispricing direction (hot / cold / aligned)
  - the property's pro forma (DSCR, cap rate, CoC)
  - the 5y projection (appreciation + rental profit + total return)

Returns a structured `Decision` with:
  - verdict
  - 3–5 plain-English bullet reasons
  - thesis_tag (one of 5 narrative archetypes)
  - primary_action
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Optional


@dataclass
class Decision:
    verdict: str                  # 'GREEN' | 'YELLOW' | 'RED'
    thesis_tag: str               # short narrative archetype tag
    reasons: list[str]
    primary_action: str


THESIS_BY_ARCHETYPE = {
    "Cashflow Heartland": "yield-driven",
    "Sun Belt Growth":    "appreciation-driven",
    "Coastal Gateway":    "appreciation-only (negative carry)",
    "Boom-Bust Beta":     "cyclical bet",
    "Resource & Niche":   "thematic",
    "Mixed":              "hybrid",
}


def _money(x: float | None) -> str:
    if x is None: return "—"
    if abs(x) >= 1e6:   return f"${x/1e6:.2f}M"
    if abs(x) >= 1e3:   return f"${x/1e3:.0f}k"
    return f"${x:,.0f}"


def _pct(x: float | None, d: int = 1) -> str:
    return "—" if x is None else f"{x*100:.{d}f}%"


def build(
    listing: dict,
    projection,
    archetype: Optional[str],
    msa_appreciation_score: Optional[float],
    msa_cashflow_score: Optional[float],
    avm_direction: Optional[str],
    avm_z: Optional[float],
    rec_verdict: str,
    rec_reasons: list[str],
    rec_primary_action: Optional[str],
    schools: Optional[dict] = None,
    county_median_income: Optional[float] = None,
) -> Decision:
    reasons: list[str] = []

    # 1. Anchor: the verdict + rationale chain in plain English
    p = projection
    five_y_dollars = p.total_return_5y_dollars
    five_y_pct = p.total_return_5y_pct

    # Architecture / archetype framing
    if archetype == "Cashflow Heartland":
        reasons.append(
            f"{listing.get('cbsa_name', 'this MSA')} is Cashflow Heartland — the thesis is "
            f"current yield, not appreciation. Cap rate clears at {_pct(p.cap_rate_y1, 1)}."
        )
    elif archetype == "Sun Belt Growth":
        reasons.append(
            f"{listing.get('cbsa_name', 'this MSA')} is Sun Belt Growth — 5y projected appreciation "
            f"is {_pct(p.appreciation_5y_pct, 0)} ({_money(p.appreciation_5y_dollars)}) on top of yield."
        )
    elif archetype == "Boom-Bust Beta":
        reasons.append(
            f"{listing.get('cbsa_name', 'this MSA')} is Boom-Bust Beta — strong returns mid-cycle, "
            f"40–60% drawdowns at cycle ends. Position-size carefully."
        )
    elif archetype == "Coastal Gateway":
        reasons.append(
            f"{listing.get('cbsa_name', 'this MSA')} is Coastal Gateway — negative carry, "
            f"appreciation-dominated, regulatory risk priced in."
        )
    else:
        reasons.append(
            f"{listing.get('cbsa_name', 'this MSA')} ({archetype or 'archetype unknown'}) — "
            f"5y projected total return {_money(five_y_dollars)} ({_pct(five_y_pct, 0)} on equity)."
        )

    # 2. Cashflow story
    if p.dscr_y1 >= 1.30:
        reasons.append(
            f"Cash flow is healthy: DSCR {p.dscr_y1:.2f}×, cash-on-cash {_pct(p.cash_on_cash_y1, 1)} "
            f"in year 1, 5y rental profit ~{_money(p.rental_profit_5y)}."
        )
    elif p.dscr_y1 >= 1.10:
        reasons.append(
            f"Cash flow is thin: DSCR {p.dscr_y1:.2f}× — above the 1.10× floor but below 1.30× "
            f"GREEN. 5y rental profit {_money(p.rental_profit_5y)}."
        )
    else:
        reasons.append(
            f"Cash flow won't cover debt service: DSCR {p.dscr_y1:.2f}×. 5y projected rental "
            f"profit {_money(p.rental_profit_5y)} — negative carry until exit."
        )

    # 3. Appreciation story
    reasons.append(
        f"Appreciation prior: zip ZHVI runs {_pct(p.appreciation_cagr, 1)} a year "
        f"after archetype overlay → {_pct(p.appreciation_5y_pct, 0)} over 5y, "
        f"= {_money(p.appreciation_5y_dollars)} of unleveraged price gain."
    )

    # 4. AVM mispricing signal (Information alpha)
    if avm_direction == "hot":
        reasons.append(
            f"AVM signal: zip is HOT (sales clearing {avm_z:+.1f}σ above ZHVI) — momentum "
            f"tailwind for resale, but you're paying a premium today."
        )
    elif avm_direction == "cold":
        reasons.append(
            f"AVM signal: zip is COLD (sales clearing {avm_z:+.1f}σ below ZHVI) — buying "
            f"opportunity if you trust the rent comp; verify deeper."
        )
    elif avm_direction == "aligned":
        reasons.append("AVM signal: aligned — sales clearing in line with the index. No edge.")

    # 5. Neighborhood overlay: schools + income
    if schools and schools.get("school_count"):
        ratio = schools.get("avg_st_ratio")
        ratio_str = f", student/teacher {ratio:.0f}:1" if ratio else ""
        reasons.append(
            f"Schools: {schools['school_count']} public schools serving "
            f"this zip ({schools.get('elementary_count', 0)} elementary, "
            f"{schools.get('high_count', 0)} high), "
            f"{schools.get('charter_count', 0)} charter{ratio_str}."
        )
    if county_median_income:
        # Affordability framing: rent should be ≤30% of income for sustainable demand
        annual_rent = (projection.cap_rate_y1 or 0) * (listing.get("listed_price") or 0) + 0
        # Easier: compute ZORI×12 / median_income to avoid double-pulling rent
        burden_pct = None
        if listing.get("listed_price") and projection.cap_rate_y1:
            # Approximate annual rent: NOI / (1−opex_ratio), but simpler is
            # gross_rent_annual ≈ cap_rate * price / (1−0.40) — noisy. Skip.
            pass
        reasons.append(
            f"Local median household income {_money(county_median_income)} "
            f"— a {_money(listing.get('listed_price'))} home is "
            f"{(listing.get('listed_price') or 0) / max(county_median_income, 1):.1f}× income."
        )

    # 6. Rec-gate failures (only show top 1–2 most actionable)
    if rec_reasons:
        # Show the first 1–2 — they're already in plain English from the gate
        for r in rec_reasons[:2]:
            reasons.append("Rec gate: " + r)

    # Primary action: prefer the rec-gate's recommended action; fall back
    # to a sensible verdict-based action.
    if rec_primary_action:
        primary = rec_primary_action
    elif rec_verdict == "GREEN":
        primary = f"Submit offer at modeled price ({_money(listing.get('listed_price'))}); no mitigations needed."
    elif rec_verdict == "YELLOW":
        primary = "Submit a conditional offer; close only after the listed mitigations are verified."
    else:
        primary = "Pass on this deal or restructure terms (lower price, larger rehab budget) before re-running."

    return Decision(
        verdict=rec_verdict,
        thesis_tag=THESIS_BY_ARCHETYPE.get(archetype or "Mixed", "hybrid"),
        reasons=reasons,
        primary_action=primary,
    )
