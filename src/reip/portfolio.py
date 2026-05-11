"""Portfolio aggregation — roll up a list of deals into the LP-grade view.

What an investor actually wants when they have ≥3 saved deals:

  1. Totals: equity deployed, annual cash flow (pre- and post-tax),
     weighted IRR (pre- and post-tax).
  2. Concentration: by state, by archetype, by verdict, by price band.
     Surfaces "you're 70% Florida" before you add the 4th FL deal.
  3. Per-deal table: pre-tax IRR vs post-tax IRR, side-by-side.
  4. Warnings: climate-correlated concentration, leverage on weak base
     DSCR, single-state exposure.

The portfolio aggregate is pure: it takes deal dicts (as the frontend
sends them from localStorage) and returns the rolled-up payload. No
mutation, no DB calls, no I/O. Easy to test, cheap to call on every
deal-list change.
"""
from __future__ import annotations
from typing import Optional

from . import tax as tax_mod


def _equity_invested(d: dict) -> float:
    """Reconstruct equity from saved inputs."""
    inputs = d.get("inputs") or {}
    price = float(inputs.get("purchase_price") or 0)
    rehab = float(inputs.get("rehab_cost") or 0)
    ltv = float(inputs.get("ltv") or 0.75)
    closing_pct = 0.03                                  # default in Assumptions
    if price <= 0:
        return 0.0
    return price * (1 - ltv) + price * closing_pct + rehab


def _base_scenario(d: dict) -> dict:
    s = (d.get("stress") or {}).get("scenarios") or []
    return s[0] if s else {}


def _worst_scenario(d: dict) -> dict:
    s = (d.get("stress") or {}).get("scenarios") or []
    return s[2] if len(s) >= 3 else {}


def _weighted_mean(pairs: list[tuple[float, float]]) -> Optional[float]:
    """pairs = [(weight, value)]; ignore zero/None weights."""
    pairs = [(w, v) for w, v in pairs if w and v is not None]
    if not pairs:
        return None
    total_w = sum(w for w, _ in pairs)
    if total_w <= 0:
        return None
    return sum(w * v for w, v in pairs) / total_w


def _bucket(deals: list[dict], key_fn, label_fn=None):
    """Group equity + counts by an arbitrary key function. Returns a sorted list."""
    label_fn = label_fn or (lambda k: str(k) if k is not None else "—")
    buckets: dict = {}
    for d in deals:
        k = key_fn(d)
        eq = _equity_invested(d)
        if k not in buckets:
            buckets[k] = {"label": label_fn(k), "equity": 0.0, "deals": 0}
        buckets[k]["equity"] += eq
        buckets[k]["deals"] += 1
    total = sum(b["equity"] for b in buckets.values())
    out = []
    for k, b in buckets.items():
        out.append({
            "key":    k,
            "label":  b["label"],
            "equity": round(b["equity"], 2),
            "pct":    round(b["equity"] / total, 4) if total > 0 else 0,
            "deals":  b["deals"],
        })
    out.sort(key=lambda r: -r["equity"])
    return out


# State-level historical-volatility classification derived from the 50-year
# FHFA HPI analysis (see docs/STRATEGY.md). These are states whose MAJOR
# metros are Boom-Bust tier (worst-decile drawdowns, multi-decade recovery
# from peaks). A portfolio heavily concentrated here ran -50% drawdowns
# during the GFC and took 12-16 years to recover.
BOOM_BUST_STATES = {"CA", "NV", "FL", "AZ"}


def _concentration_warnings(by_state, by_verdict, deals_with_tax,
                             climate_states={"FL", "TX", "CA", "AZ", "NV", "CO", "LA", "MS", "AL"}) -> list[str]:
    warnings = []
    # Single-state >60%
    for s in by_state:
        if s["pct"] >= 0.60 and s["key"]:
            warnings.append(f"{s['pct']*100:.0f}% of equity is in {s['key']} — single-state concentration.")
            break
    # Climate-correlated >50%
    climate_pct = sum(s["pct"] for s in by_state if s["key"] in climate_states)
    if climate_pct >= 0.50:
        warnings.append(f"{climate_pct*100:.0f}% of equity is in climate-stressed states "
                        f"({', '.join(s['key'] for s in by_state if s['key'] in climate_states and s['pct'] > 0)}).")
    # Historical Boom-Bust state concentration
    bb_pct = sum(s["pct"] for s in by_state if s["key"] in BOOM_BUST_STATES)
    if bb_pct >= 0.40:
        bb_states_present = [s["key"] for s in by_state if s["key"] in BOOM_BUST_STATES and s["pct"] > 0]
        warnings.append(f"{bb_pct*100:.0f}% of equity is in historically Boom-Bust states "
                        f"({', '.join(bb_states_present)}) — these saw -50% drawdowns and "
                        f"12-16 year recoveries in 2007-2022. Confirm you can hold ≥10y through "
                        f"that scenario.")
    # RED-verdict deals carrying real equity
    red_eq = sum(d["equity"] for v in by_verdict if v["key"] == "RED" for d in [v])
    if red_eq > 0:
        red_pct = red_eq / sum(v["equity"] for v in by_verdict) if by_verdict else 0
        if red_pct >= 0.20:
            warnings.append(f"{red_pct*100:.0f}% of equity is on RED-verdict deals — "
                            "either negotiate to walk-away price or pass.")
    # Thin DSCR average
    dscr_vals = [(d.get("equity") or 0, d.get("base_dscr"))
                  for d in deals_with_tax if d.get("base_dscr") is not None]
    avg_dscr = _weighted_mean(dscr_vals)
    if avg_dscr is not None and avg_dscr < 1.20:
        warnings.append(f"Weighted base DSCR is {avg_dscr:.2f} — portfolio is leverage-fragile.")
    return warnings


def aggregate(deals: list[dict], tax: tax_mod.TaxAssumptions = None) -> dict:
    """Roll up a list of deals into the portfolio view.

    Each deal is expected to have:
        {label, status, inputs: {purchase_price, monthly_rent, rehab_cost,
                                  state, ltv, mortgage_rate, ...},
         stress: {gate: {verdict}, scenarios: [base, stress, worst],
                   price_to_green, ...}}
    """
    tax = tax or tax_mod.TaxAssumptions()
    if not deals:
        return {
            "count": 0,
            "totals": _empty_totals(),
            "by_state": [], "by_verdict": [], "by_status": [], "by_archetype": [],
            "deals_with_tax": [],
            "concentration_warnings": [],
            "tax_assumptions": _tax_to_dict(tax),
        }

    deals_with_tax = []
    total_eq = 0.0
    total_cf_pre = 0.0
    total_cf_post = 0.0
    total_depr = 0.0
    total_tax_savings = 0.0
    irr_pretax_pairs = []
    irr_posttax_pairs = []
    worst_pretax_pairs = []
    dscr_pairs = []
    for d in deals:
        inputs = d.get("inputs") or {}
        base = _base_scenario(d)
        worst = _worst_scenario(d)
        equity = _equity_invested(d)
        pretax_cf = (base.get("cash_flow_y1") or 0.0) if base else 0.0
        depr = tax_mod.annual_depreciation(
            float(inputs.get("purchase_price") or 0),
            float(inputs.get("rehab_cost") or 0),
            land_allocation=tax.land_allocation,
            useful_life_years=tax.useful_life_years,
        )
        post_cf = tax_mod.post_tax_annual_cf(pretax_cf, depr, tax)
        tax_savings = tax_mod.annual_tax_savings(pretax_cf, depr, tax)
        pretax_irr = base.get("irr") if base else None
        posttax_irr = tax_mod.irr_uplift_estimate(
            pretax_irr or 0.0, pretax_cf, depr, equity, tax=tax,
        ) if pretax_irr is not None else None
        worst_irr = worst.get("irr") if worst else None
        base_dscr = base.get("dscr") if base else None
        deals_with_tax.append({
            "label":            d.get("label"),
            "status":           d.get("status"),
            "verdict":          (d.get("stress") or {}).get("gate", {}).get("verdict"),
            "state":            inputs.get("state"),
            "purchase_price":   inputs.get("purchase_price"),
            "monthly_rent":     inputs.get("monthly_rent"),
            "equity":           round(equity, 2),
            "annual_cf_pretax": round(pretax_cf, 2),
            "annual_cf_posttax": round(post_cf, 2),
            "annual_depreciation": round(depr, 2),
            "annual_tax_savings":  round(tax_savings, 2),
            "irr_pretax":       pretax_irr,
            "irr_posttax":      posttax_irr,
            "worst_irr":        worst_irr,
            "base_dscr":        base_dscr,
        })
        total_eq += equity
        total_cf_pre += pretax_cf
        total_cf_post += post_cf
        total_depr += depr
        total_tax_savings += tax_savings
        if pretax_irr is not None:
            irr_pretax_pairs.append((equity, pretax_irr))
        if posttax_irr is not None:
            irr_posttax_pairs.append((equity, posttax_irr))
        if worst_irr is not None:
            worst_pretax_pairs.append((equity, worst_irr))
        if base_dscr is not None:
            dscr_pairs.append((equity, base_dscr))

    by_state    = _bucket(deals, lambda d: (d.get("inputs") or {}).get("state"))
    by_verdict  = _bucket(deals, lambda d: (d.get("stress") or {}).get("gate", {}).get("verdict") or "?")
    by_status   = _bucket(deals, lambda d: d.get("status") or "?")
    by_archetype = _bucket(deals, lambda d: (d.get("inputs") or {}).get("archetype_hint"))

    return {
        "count": len(deals),
        "totals": {
            "equity_deployed":        round(total_eq, 2),
            "annual_cf_pretax":       round(total_cf_pre, 2),
            "annual_cf_posttax":      round(total_cf_post, 2),
            "annual_tax_savings":     round(total_tax_savings, 2),
            "annual_depreciation":    round(total_depr, 2),
            "weighted_irr_pretax":    _maybe_round(_weighted_mean(irr_pretax_pairs)),
            "weighted_irr_posttax":   _maybe_round(_weighted_mean(irr_posttax_pairs)),
            "weighted_worst_irr_pretax": _maybe_round(_weighted_mean(worst_pretax_pairs)),
            "weighted_base_dscr":     _maybe_round(_weighted_mean(dscr_pairs), 2),
            "monthly_cf_pretax":      round(total_cf_pre / 12, 2),
            "monthly_cf_posttax":     round(total_cf_post / 12, 2),
        },
        "by_state":     by_state,
        "by_verdict":   by_verdict,
        "by_status":    by_status,
        "by_archetype": by_archetype,
        "deals_with_tax": deals_with_tax,
        "concentration_warnings": _concentration_warnings(by_state, by_verdict, deals_with_tax),
        "tax_assumptions": _tax_to_dict(tax),
    }


def _empty_totals() -> dict:
    return {
        "equity_deployed":          0,
        "annual_cf_pretax":         0,
        "annual_cf_posttax":        0,
        "annual_tax_savings":       0,
        "annual_depreciation":      0,
        "weighted_irr_pretax":      None,
        "weighted_irr_posttax":     None,
        "weighted_worst_irr_pretax": None,
        "weighted_base_dscr":       None,
        "monthly_cf_pretax":        0,
        "monthly_cf_posttax":       0,
    }


def _tax_to_dict(t: tax_mod.TaxAssumptions) -> dict:
    return {
        "tax_bracket":       t.tax_bracket,
        "land_allocation":   t.land_allocation,
        "useful_life_years": t.useful_life_years,
        "deduction_against_ordinary": t.deduction_against_ordinary,
        "recapture_rate":    t.recapture_rate,
    }


def _maybe_round(x: Optional[float], ndigits: int = 4) -> Optional[float]:
    return round(x, ndigits) if x is not None else None
