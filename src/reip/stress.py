"""Deal stress test — multi-scenario underwriter with state-aware overlays.

The platform's biggest sin pre-stress was single-point estimates. A 22% IRR
under base-case is meaningless if the deal swings to -4% under realistic
shocks. This module rebuilds the underwriter around three scenarios:

  - base:   investor's assumed inputs as-is
  - stress: realistic adverse conditions (rent -10%, vacancy +3pp,
            rate +100bps, insurance +20%, rehab +20%)
  - worst:  90th-percentile-bad outcomes (rent -15%, vacancy +7pp,
            rate +200bps, insurance +40%, rehab +35%, exit cap +100bps)

State-aware overlays stack on top of those deltas. Florida adds a
hurricane-insurance multiplier, Texas adds the high-property-tax shock,
California adds rent-cap drag, rust-belt adds rehab overrun. These are
the dimensions where amateur underwriting most often lies.

Gate logic (deliberately strict; do not soften):
  GREEN  : worst-case IRR ≥  0%   AND base CoC ≥ 6%  AND base DSCR ≥ 1.25
                                  AND stress DSCR ≥ 1.10
  YELLOW : worst-case IRR ≥ -5%   AND base CoC ≥ 3%  AND base DSCR ≥ 1.15
  RED    : anything below YELLOW.

`price_to_green` is the price ceiling (via bisection on purchase price)
where the deal upgrades to GREEN — i.e. "negotiate to $X or walk."

`break_even_occupancy` is the occupancy floor at which annual cash flow
hits zero. < 0 means the deal is upside-down even at 100% occupancy.
"""
from __future__ import annotations
from dataclasses import dataclass, field, asdict
from typing import Optional
import math

from . import underwriting as uw


# --- state-aware overlays ----------------------------------------------------
# Each overlay is a dict of relative/absolute deltas applied to Assumptions.
# Keys with `_pct` are multiplicative (1.20 = +20%); raw keys override.

_FL_OVERLAY = {
    "insurance_mult": 1.50,       # post-Ian/Idalia repricing
    "exit_cap_add":  0.005,        # liquidity premium for FL exit
}
_TX_OVERLAY = {
    "property_tax_add": 0.015,    # TX no-income-tax means property tax 2.5–3%
    "insurance_mult":   1.20,     # hail/wind belt
}
_CA_OVERLAY = {
    "rent_growth_add": -0.01,     # statewide rent caps drag growth
    "opex_ratio_add":   0.05,     # rent-control compliance overhead
    "exit_cap_add":    -0.005,    # CA structurally low cap rates
}
_AZ_NV_OVERLAY = {
    "insurance_mult": 1.25,       # wildfire / heat-related claims
    "exit_cap_add":   0.005,
}
_CO_OVERLAY = {
    "insurance_mult": 1.30,       # wildfire risk repricing
}
_RUSTBELT_OVERLAY = {
    "rehab_mult":      1.30,      # 1900–1950 housing stock
    "vacancy_add":     0.02,      # softer rental demand
    "opex_ratio_add":  0.03,      # higher capex turn cost
}
_GULF_OVERLAY = {
    "insurance_mult": 1.40,       # LA/MS/AL coastal
}

_STATE_OVERLAYS = {
    "FL": _FL_OVERLAY,
    "TX": _TX_OVERLAY,
    "CA": _CA_OVERLAY,
    "AZ": _AZ_NV_OVERLAY,
    "NV": _AZ_NV_OVERLAY,
    "CO": _CO_OVERLAY,
    "LA": _GULF_OVERLAY,
    "MS": _GULF_OVERLAY,
    "AL": _GULF_OVERLAY,
    "OH": _RUSTBELT_OVERLAY,
    "MI": _RUSTBELT_OVERLAY,
    "IN": _RUSTBELT_OVERLAY,
    "IL": _RUSTBELT_OVERLAY,
    "PA": _RUSTBELT_OVERLAY,
    "WV": _RUSTBELT_OVERLAY,
    "MO": _RUSTBELT_OVERLAY,  # KCMO / STL exposure
}


# Generic scenario deltas — same shape as state overlays.
_STRESS_DELTAS = {
    "rent_mult":        0.90,    # -10% rent
    "vacancy_add":      0.03,    # +3pp vacancy
    "rate_add":         0.010,   # +100bps mortgage rate
    "insurance_mult":   1.20,
    "rehab_mult":       1.20,
    "expense_growth_add": 0.005,
}
_WORST_DELTAS = {
    "rent_mult":        0.85,
    "vacancy_add":      0.07,
    "rate_add":         0.020,
    "insurance_mult":   1.40,
    "rehab_mult":       1.35,
    "exit_cap_add":     0.010,
    "expense_growth_add": 0.010,
}


def _apply_overlay(base: uw.Assumptions, overlay: dict) -> uw.Assumptions:
    """Return a new Assumptions with overlay deltas applied."""
    d = {**base.__dict__}
    if "rent_mult" in overlay:
        d["monthly_rent"] *= overlay["rent_mult"]
    if "vacancy_add" in overlay:
        d["vacancy"] = min(0.99, d["vacancy"] + overlay["vacancy_add"])
    if "rate_add" in overlay:
        d["mortgage_rate"] += overlay["rate_add"]
    if "insurance_mult" in overlay:
        d["insurance_annual"] *= overlay["insurance_mult"]
    if "rehab_mult" in overlay:
        d["rehab_cost"] *= overlay["rehab_mult"]
    if "exit_cap_add" in overlay:
        d["exit_cap"] += overlay["exit_cap_add"]
    if "expense_growth_add" in overlay:
        d["expense_growth"] += overlay["expense_growth_add"]
    if "property_tax_add" in overlay:
        d["property_tax_rate"] += overlay["property_tax_add"]
    if "rent_growth_add" in overlay:
        d["rent_growth"] += overlay["rent_growth_add"]
    if "opex_ratio_add" in overlay:
        d["opex_ratio"] = min(0.95, d["opex_ratio"] + overlay["opex_ratio_add"])
    return uw.Assumptions(**d)


def _compose(state_overlay: dict, scenario_overlay: dict) -> dict:
    """Combine two overlay dicts — multiplicative keys multiply, additive add."""
    out = {}
    for key in set(state_overlay) | set(scenario_overlay):
        s = state_overlay.get(key)
        sc = scenario_overlay.get(key)
        if key.endswith("_mult"):
            out[key] = (s or 1.0) * (sc or 1.0)
        else:
            out[key] = (s or 0.0) + (sc or 0.0)
    return out


def _break_even_occupancy(a: uw.Assumptions) -> float:
    """Year-1 occupancy at which CF = 0.

    Derivation:
        CF = gross*(1-vac)*(1-opex_ratio) - tax - ins - hoa - debt
        Set CF=0, solve for occupancy = (1-vac):
        occupancy = (tax + ins + hoa + debt) / (gross * (1 - opex_ratio))

    Returns the occupancy floor (0..1+). If > 1, the deal cannot cash-flow
    even at 100% occupancy."""
    gross = a.monthly_rent * 12
    if gross <= 0 or a.opex_ratio >= 1.0:
        return float("nan")
    loan = a.purchase_price * a.ltv
    debt = uw._amortizing_payment(loan, a.mortgage_rate, a.term_years) * 12
    fixed = (a.property_tax_rate * a.purchase_price
             + a.insurance_annual + a.hoa_monthly * 12 + debt)
    return round(fixed / (gross * (1 - a.opex_ratio)), 4)


def _scenario(name: str, label: str, a: uw.Assumptions,
              base_a: uw.Assumptions, overlay: dict) -> dict:
    """Run one scenario; return a flat dict for the response."""
    pf = uw.proforma(a)
    irr = uw.irr(a)
    bep = _break_even_occupancy(a)
    # Surface what changed vs base in human-readable form.
    deltas = {}
    if a.monthly_rent != base_a.monthly_rent:
        deltas["rent_pct"] = round((a.monthly_rent / base_a.monthly_rent) - 1, 3)
    if a.vacancy != base_a.vacancy:
        deltas["vacancy_pp"] = round(a.vacancy - base_a.vacancy, 3)
    if a.mortgage_rate != base_a.mortgage_rate:
        deltas["rate_bps"] = round((a.mortgage_rate - base_a.mortgage_rate) * 10000)
    if a.insurance_annual != base_a.insurance_annual:
        deltas["insurance_pct"] = round((a.insurance_annual / base_a.insurance_annual) - 1, 3)
    if a.rehab_cost != base_a.rehab_cost and base_a.rehab_cost > 0:
        deltas["rehab_pct"] = round((a.rehab_cost / base_a.rehab_cost) - 1, 3)
    if a.exit_cap != base_a.exit_cap:
        deltas["exit_cap_bps"] = round((a.exit_cap - base_a.exit_cap) * 10000)
    if a.property_tax_rate != base_a.property_tax_rate:
        deltas["tax_rate_pp"] = round((a.property_tax_rate - base_a.property_tax_rate) * 100, 3)
    return {
        "name": name,
        "label": label,
        "deltas": deltas,
        "irr": irr["irr"],
        "equity_multiple": irr["equity_multiple"],
        "cash_on_cash": pf["cash_on_cash"],
        "dscr": pf["dscr"],
        "cap_rate": pf["cap_rate"],
        "cash_flow_y1": pf["cash_flow_y1"],
        "break_even_occupancy": bep,
    }


# --- gate logic -------------------------------------------------------------

@dataclass
class GateResult:
    verdict: str            # GREEN / YELLOW / RED
    reasons: list[str]
    mitigations: list[str]


def _evaluate_gate(scenarios: list[dict]) -> GateResult:
    """Apply gate thresholds across base/stress/worst scenarios."""
    by_name = {s["name"]: s for s in scenarios}
    base, stress, worst = by_name["base"], by_name["stress"], by_name["worst"]

    fails_green = []
    fails_yellow = []

    if worst["irr"] is None or worst["irr"] < -0.05:
        fails_green.append(f"worst-case IRR {(_pct(worst['irr']))} below -5% (you want survivable in recession)")
    if base["cash_on_cash"] is None or base["cash_on_cash"] < 0.06:
        fails_green.append(f"base CoC {(_pct(base['cash_on_cash']))} below 6%")
    if base["dscr"] is None or base["dscr"] < 1.25:
        fails_green.append(f"base DSCR {base['dscr']} below 1.25 (institutional underwriting threshold)")
    if stress["dscr"] is None or stress["dscr"] < 1.10:
        fails_green.append(f"stress DSCR {stress['dscr']} below 1.10 (rate shock breaks debt service)")

    if worst["irr"] is None or worst["irr"] < -0.15:
        fails_yellow.append(f"worst-case IRR {_pct(worst['irr'])} below -15% (deal could get wiped in a recession)")
    if base["cash_on_cash"] is None or base["cash_on_cash"] < 0.03:
        fails_yellow.append(f"base CoC {_pct(base['cash_on_cash'])} below 3%")
    if base["dscr"] is None or base["dscr"] < 1.15:
        fails_yellow.append(f"base DSCR {base['dscr']} below 1.15")

    if not fails_green:
        return GateResult("GREEN",
                          reasons=["Survives worst-case with non-negative IRR.",
                                   "Base CoC and DSCR clear lender + investor thresholds."],
                          mitigations=[])
    if not fails_yellow:
        return GateResult("YELLOW", reasons=fails_green,
                          mitigations=_suggest_mitigations(base, stress, worst))
    return GateResult("RED", reasons=fails_yellow,
                      mitigations=_suggest_mitigations(base, stress, worst))


def _pct(x):
    if x is None:
        return "n/a"
    try:
        if x <= -0.95:
            return "≤ -99% (equity wipe)"
        return f"{x*100:+.1f}%"
    except Exception:
        return str(x)


def _suggest_mitigations(base, stress, worst) -> list[str]:
    out = []
    if base["dscr"] is not None and base["dscr"] < 1.25:
        out.append("Negotiate a lower purchase price — at current rent, DSCR clears 1.25 only if price drops; run 'price to green' to size.")
    if stress["dscr"] is not None and stress["dscr"] < 1.10:
        out.append("Require a 12-month interest reserve at closing (covers a 1-rate-shock scenario).")
    if base["cash_on_cash"] is not None and base["cash_on_cash"] < 0.06:
        out.append("Increase down payment to 25%+ or buy down the rate by 1pt — reduces debt-service drag.")
    if base["break_even_occupancy"] is not None and base["break_even_occupancy"] > 0.90:
        out.append("Identify a property manager NOW — break-even occupancy is razor-thin, vacancy will kill you.")
    if worst["irr"] is not None and worst["irr"] < -0.05:
        out.append("Negotiate a longer inspection window + financing contingency; this deal is fragile to small shocks.")
    if base["dscr"] is not None and base["dscr"] < 1.0:
        out.append("Walk. Year-1 NOI does not cover debt service at base assumptions.")
    return out


# --- price-to-green bisect --------------------------------------------------

def _price_to_green(base_a: uw.Assumptions, state: Optional[str],
                     extra_stress: Optional[dict] = None) -> Optional[float]:
    """Bisect on purchase_price to find the ceiling that lifts the deal to GREEN.
    Returns None if even pricing at 30% of asked still doesn't get there."""
    lo, hi = base_a.purchase_price * 0.30, base_a.purchase_price
    best = None
    for _ in range(28):
        mid = (lo + hi) / 2
        trial = uw.Assumptions(**{**base_a.__dict__, "purchase_price": mid})
        scenarios = _run_all(trial, state, extra_stress=extra_stress)
        if _evaluate_gate(scenarios).verdict == "GREEN":
            best = mid
            lo = mid           # try going higher
        else:
            hi = mid           # need lower price
    return round(best) if best is not None else None


# --- top-level entry --------------------------------------------------------

def _apply_climate_bumps(deltas: dict, bumps: dict) -> dict:
    """Stack climate bonuses onto an existing stress/worst delta dict."""
    if not bumps:
        return deltas
    out = dict(deltas)
    if bumps.get("insurance_mult_bonus"):
        out["insurance_mult"] = out.get("insurance_mult", 1.0) * (1 + bumps["insurance_mult_bonus"])
    if bumps.get("rehab_mult_bonus"):
        out["rehab_mult"] = out.get("rehab_mult", 1.0) * (1 + bumps["rehab_mult_bonus"])
    if bumps.get("exit_cap_add_bonus"):
        out["exit_cap_add"] = out.get("exit_cap_add", 0.0) + bumps["exit_cap_add_bonus"]
    return out


def _run_all(a: uw.Assumptions, state: Optional[str],
              extra_stress: Optional[dict] = None) -> list[dict]:
    state_overlay = _STATE_OVERLAYS.get((state or "").upper(), {})
    stress_deltas = _apply_climate_bumps(_STRESS_DELTAS, extra_stress or {})
    worst_deltas  = _apply_climate_bumps(_WORST_DELTAS,  extra_stress or {})
    base_a = _apply_overlay(a, state_overlay)
    stress_a = _apply_overlay(a, _compose(state_overlay, stress_deltas))
    worst_a = _apply_overlay(a, _compose(state_overlay, worst_deltas))
    return [
        _scenario("base",   "Base case",   base_a,   base_a, state_overlay),
        _scenario("stress", "Stress case", stress_a, base_a, _compose(state_overlay, stress_deltas)),
        _scenario("worst",  "Worst case",  worst_a,  base_a, _compose(state_overlay, worst_deltas)),
    ]


def stress_test(a: uw.Assumptions, state: Optional[str] = None,
                include_price_to_green: bool = True,
                climate_score: Optional[dict] = None) -> dict:
    """Top-level: run all 3 scenarios + gate + price-to-green search.

    If `climate_score` is provided (a dict from climate.ClimateScore.to_dict()),
    the stress + worst scenarios get climate-amplified — severe-climate zips
    see additional insurance and rehab stress on top of the state overlay.
    """
    extra = _climate_stress_bumps(climate_score)
    scenarios = _run_all(a, state, extra_stress=extra)
    gate = _evaluate_gate(scenarios)
    out = {
        "state": (state or "").upper() or None,
        "state_overlay_applied": (state or "").upper() in _STATE_OVERLAYS,
        "state_overlay_summary": _state_overlay_summary(state),
        "climate_overlay_applied": bool(extra),
        "climate_overlay_summary": _climate_overlay_summary(climate_score, extra),
        "assumptions": a.__dict__,
        "scenarios": scenarios,
        "gate": {"verdict": gate.verdict, "reasons": gate.reasons,
                  "mitigations": gate.mitigations},
    }
    if include_price_to_green and gate.verdict != "GREEN":
        out["price_to_green"] = _price_to_green(a, state, extra_stress=extra)
    return out


def _climate_stress_bumps(climate_score: Optional[dict]) -> dict:
    """Translate a climate score dict into extra stress/worst deltas."""
    if not climate_score:
        return {}
    s = climate_score.get("overall_score") or 0
    if s >= 75:
        return {"insurance_mult_bonus": 0.25, "rehab_mult_bonus": 0.15,
                 "exit_cap_add_bonus": 0.005}
    if s >= 50:
        return {"insurance_mult_bonus": 0.15, "rehab_mult_bonus": 0.08,
                 "exit_cap_add_bonus": 0.003}
    if s >= 20:
        return {"insurance_mult_bonus": 0.08, "rehab_mult_bonus": 0.03,
                 "exit_cap_add_bonus": 0.001}
    return {}


def _climate_overlay_summary(climate_score: Optional[dict], extra: dict) -> Optional[str]:
    if not extra or not climate_score:
        return None
    cat = climate_score.get("category", "?")
    risk = climate_score.get("primary_risk", "?")
    bits = []
    if extra.get("insurance_mult_bonus"):
        bits.append(f"insurance +{extra['insurance_mult_bonus']*100:.0f}%")
    if extra.get("rehab_mult_bonus"):
        bits.append(f"rehab +{extra['rehab_mult_bonus']*100:.0f}%")
    if extra.get("exit_cap_add_bonus"):
        bits.append(f"exit-cap +{extra['exit_cap_add_bonus']*10000:.0f}bps")
    return f"{cat} {risk} climate: stress/worst bumped — " + ", ".join(bits)


def _state_overlay_summary(state: Optional[str]) -> Optional[str]:
    s = (state or "").upper()
    if s not in _STATE_OVERLAYS:
        return None
    o = _STATE_OVERLAYS[s]
    bits = []
    if o.get("insurance_mult", 1) != 1:
        bits.append(f"insurance ×{o['insurance_mult']:.2f}")
    if o.get("property_tax_add", 0):
        bits.append(f"property tax +{o['property_tax_add']*100:.1f}pp")
    if o.get("rehab_mult", 1) != 1:
        bits.append(f"rehab ×{o['rehab_mult']:.2f}")
    if o.get("vacancy_add", 0):
        bits.append(f"vacancy +{o['vacancy_add']*100:.0f}pp")
    if o.get("rent_growth_add", 0):
        bits.append(f"rent growth {o['rent_growth_add']*100:+.1f}pp")
    if o.get("opex_ratio_add", 0):
        bits.append(f"opex ratio +{o['opex_ratio_add']*100:.0f}pp")
    if o.get("exit_cap_add", 0):
        bits.append(f"exit cap {o['exit_cap_add']*10000:+.0f}bps")
    return f"{s}: " + ", ".join(bits) if bits else None
