"""FastAPI backend — Phase 4 of the platform spec.

Serves:
  - GET  /api/msas                          ranked MSAs + archetypes
  - GET  /api/msas/{cbsa_id}                full breakdown for one MSA
  - GET  /api/avm                           zip-level AVM mispricing signal
  - POST /api/remarks                       parse free-text MLS remarks
  - POST /api/underwritings                 pro forma + sensitivity + recommendation
  - POST /api/underwritings/mitigations     re-run gate with mitigations applied
  - GET  /api/coverage-map                  county-level coverage status (stub)

The scoring + underwriting + recommendation packages are pure Python and
imported directly. The route handlers contain no business logic.

Static SPA mounted at `/` from src/reip/static/.
"""
from __future__ import annotations
from pathlib import Path
from typing import Optional
from dataclasses import asdict
import math

import pandas as pd
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .store import connect
from . import msa_score, avm as avm_mod, remarks as remarks_mod
from . import underwriting as uw_mod
from . import recommendation as rec_mod
from . import property_ingest as ingest_mod
from . import listings_search as listings_mod
from . import projection as proj_mod
from . import decision as decision_mod

app = FastAPI(title="reip", version="0.4.0",
              description="Real estate investment platform — deal-screening + underwriting")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


def _clean(d: dict) -> dict:
    """Replace NaN/Inf with None for JSON."""
    out = {}
    for k, v in d.items():
        if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
            out[k] = None
        else:
            out[k] = v
    return out


# ---- pydantic IO models -----------------------------------------------------

class MSARow(BaseModel):
    cbsa_code: str
    cbsa_name: Optional[str] = None
    archetype: Optional[str] = None
    pop: Optional[float] = None
    pop_cagr_5yr: Optional[float] = None
    emp_cagr_5yr: Optional[float] = None
    income_cagr_5yr: Optional[float] = None
    net_migration_pct_pop: Optional[float] = None
    permits_per_1000_hh: Optional[float] = None
    gross_yield: Optional[float] = None
    elasticity: Optional[float] = None
    wrluri: Optional[float] = None
    appreciation_score: Optional[float] = None
    cashflow_score: Optional[float] = None
    total_return_score: Optional[float] = None
    completeness: Optional[float] = None


class UnderwriteRequest(BaseModel):
    purchase_price: float
    rehab_cost: float = 0.0
    arv: Optional[float] = None
    monthly_rent: float
    mortgage_rate: float = 0.07
    ltv: float = 0.75
    vacancy: float = 0.05
    opex_ratio: float = 0.40
    property_tax_rate: float = 0.012
    insurance_annual: float = 1500.0
    hoa_monthly: float = 0.0
    exit_cap: float = 0.06
    hold_years: int = 5
    rent_growth: float = 0.03
    expense_growth: float = 0.03
    selling_cost_pct: float = 0.07
    closing_cost_pct: float = 0.03
    refi_ltv: float = 0.75
    # Recommendation-gate inputs (optional — caller may pass to drive the gate)
    insurance_trend_pct: Optional[float] = None
    climate_pct: Optional[float] = None
    alpha_stack_count: Optional[int] = None
    msa_blended_percentile: Optional[float] = None
    rehab_overrun_risk: bool = False
    financing_concentration_risk: bool = False
    exit_risk_no_ltr_fallback: bool = False


class UnderwriteResponse(BaseModel):
    proforma: dict
    brrrr_refi: dict
    irr: dict
    sensitivity: list[dict]
    deal_inputs: dict
    recommendation: dict


class MitigationRequest(BaseModel):
    deal: dict   # round-trip the deal_inputs from a prior underwriting response
    mitigations: dict


class RemarksRequest(BaseModel):
    text: str


class IngestRequest(BaseModel):
    url: str = Field(..., description="Redfin / Zillow / Realtor.com listing URL")


# ---- routes -----------------------------------------------------------------

@app.get("/api/health")
def health():
    return {"ok": True, "version": app.version}


def _scored_msas() -> pd.DataFrame:
    con = connect()
    raw = msa_score.features(con)
    if raw.empty:
        return raw
    return msa_score.with_archetype(msa_score.score(raw))


@app.get("/api/msas")
def list_msas(
    archetype: Optional[str] = None,
    min_pop: int = 250_000,
    sort_by: str = Query("total", pattern="^(total|appreciation|cashflow)$"),
    limit: int = 100,
):
    df = _scored_msas()
    if df.empty:
        return []
    if archetype:
        df = df[df["archetype"] == archetype]
    df = df[df["pop"] >= min_pop]
    sort_col = {"total": "total_return_score", "appreciation": "appreciation_score",
                "cashflow": "cashflow_score"}[sort_by]
    df = df.sort_values(sort_col, ascending=False).head(limit)
    cols = [c for c in MSARow.model_fields if c in df.columns]
    return [_clean(r) for r in df[cols].to_dict("records")]


@app.get("/api/msas/{cbsa_code}")
def get_msa(cbsa_code: str):
    df = _scored_msas()
    if df.empty:
        raise HTTPException(404, "No MSAs scored. Run `reip ingest` first.")
    m = df[df["cbsa_code"].astype(str) == cbsa_code]
    if m.empty:
        raise HTTPException(404, f"No MSA matching {cbsa_code}")
    row = m.iloc[0].to_dict()
    # Add a percentile rank for primary scoring metrics
    for col, label in (("appreciation_score", "appreciation_pct"),
                       ("cashflow_score",     "cashflow_pct"),
                       ("total_return_score", "total_return_pct")):
        row[label] = float(df[col].rank(pct=True).loc[m.index[0]]) if col in df.columns else None
    return _clean(row)


@app.get("/api/avm")
def list_avm(
    direction: str = Query("cold", pattern="^(cold|hot|aligned|all)$"),
    min_price: float = 0,
    max_price: float = 2_000_000,
    limit: int = 50,
):
    con = connect()
    avm_mod.persist(con)
    df = con.execute(
        "SELECT * FROM zip_avm_signal WHERE zhvi BETWEEN ? AND ?",
        [min_price, max_price],
    ).df()
    if direction != "all":
        df = df[df["direction"] == direction]
    df = df.sort_values("divergence_z", ascending=(direction == "cold")).head(limit)
    return [_clean(r) for r in df.to_dict("records")]


@app.post("/api/remarks")
def parse_remarks(req: RemarksRequest):
    s = remarks_mod.parse(req.text)
    return {
        "motivated": s.motivated, "distressed": s.distressed,
        "use_change": s.use_change, "assumable": s.assumable,
        "price_cut": s.price_cut, "short_sale": s.short_sale, "probate": s.probate,
        "score": round(s.score, 3),
        "matched_terms": list(s.matched_terms),
    }


def _underwrite_core(req: UnderwriteRequest) -> dict:
    a = uw_mod.Assumptions(
        purchase_price=req.purchase_price, rehab_cost=req.rehab_cost, arv=req.arv,
        monthly_rent=req.monthly_rent, vacancy=req.vacancy,
        opex_ratio=req.opex_ratio, property_tax_rate=req.property_tax_rate,
        insurance_annual=req.insurance_annual, hoa_monthly=req.hoa_monthly,
        mortgage_rate=req.mortgage_rate, ltv=req.ltv,
        rent_growth=req.rent_growth, expense_growth=req.expense_growth,
        exit_cap=req.exit_cap, selling_cost_pct=req.selling_cost_pct,
        closing_cost_pct=req.closing_cost_pct, refi_ltv=req.refi_ltv,
        hold_years=req.hold_years,
    )
    pf = uw_mod.proforma(a)
    brrrr = uw_mod.brrrr_refi(a)
    irr = uw_mod.irr(a)
    # Stress-case CoC for the rec gate: rehab +20%, ARV -5%
    stress = uw_mod.Assumptions(**{**a.__dict__,
                                   "rehab_cost": a.rehab_cost * 1.20,
                                   "arv": (a.arv or a.purchase_price) * 0.95})
    stress_pf = uw_mod.proforma(stress)
    stress_irr = uw_mod.irr(stress)
    stress_coc = stress_pf["cash_flow_y1"] / stress_pf["equity_invested"] if stress_pf["equity_invested"] > 0 else None

    deal = rec_mod.DealUnderwriting(
        stabilized_dscr=pf["dscr"],
        refi_appraisal_stress_pass=(brrrr.get("new_loan", 0) / max((a.arv or 1) * 0.95, 1) <= 0.70) if a.arv else None,
        insurance_trend_pct=req.insurance_trend_pct,
        climate_pct=req.climate_pct,
        alpha_stack_count=req.alpha_stack_count,
        stress_coc_on_residual=stress_coc,
        msa_blended_percentile=req.msa_blended_percentile,
        sensitivity_negative_cashflow=stress_pf["cash_flow_y1"] < 0,
        rehab_overrun_risk=req.rehab_overrun_risk,
        financing_concentration_risk=req.financing_concentration_risk,
        exit_risk_no_ltr_fallback=req.exit_risk_no_ltr_fallback,
    )
    rec = rec_mod.classify(deal)
    sens = uw_mod.sensitivity(a).to_dict("records")
    return {
        "proforma": pf, "brrrr_refi": brrrr, "irr": irr, "sensitivity": sens,
        "deal_inputs": asdict(deal),
        "recommendation": rec.to_dict(),
    }


@app.post("/api/underwritings")
def underwrite(req: UnderwriteRequest) -> UnderwriteResponse:
    return UnderwriteResponse(**_underwrite_core(req))


@app.post("/api/underwritings/mitigations")
def mitigations(req: MitigationRequest):
    """Re-run the recommendation gate with mitigations applied.
    Pass `deal` from a prior underwrite response and the mitigation flags."""
    deal = rec_mod.DealUnderwriting(**req.deal)
    mits = rec_mod.VerifiedMitigations(**req.mitigations)
    out = rec_mod.classify(deal, mits)
    return out.to_dict()


@app.get("/api/listings/markets")
def listings_markets():
    """Allowlist of CBSAs the screener can search."""
    return listings_mod.list_markets()


@app.get("/api/listings/buy")
def listings_buy(
    cbsa: str = Query("32820", description="CBSA code (default Memphis 32820)"),
    zip: Optional[str] = Query(None, description="Filter to a single ZIP code (5 digits)"),
    limit: int = 12,
    min_price: int = 50_000,
    max_price: int = 500_000,
    min_irr: Optional[float] = Query(None, description="Minimum 5y IRR (0–1)"),
    min_dscr: Optional[float] = Query(None, description="Minimum stabilized DSCR"),
    min_cap: Optional[float] = Query(None, description="Minimum year-1 cap rate (0–1)"),
    min_school_count: Optional[int] = Query(None, description="Minimum public schools serving the zip"),
    mortgage_rate: float = 0.07,
    ltv: float = 0.75,
    sort: str = Query("irr", pattern="^(total_return|cashflow|appreciation|irr)$"),
):
    """Live buyable listings for a launch market, ranked by 5-year return.

    For each property:
      - pulls Redfin's active for-sale listings via the gis search API
      - projects 5y appreciation (zip ZHVI CAGR with archetype overlay)
      - projects 5y rental profit (NOI − debt service, with rent growth)
      - runs the recommendation gate (GREEN / YELLOW / RED)
      - generates a plain-English decision rationale

    Returns the top N ranked by chosen criterion.
    """
    listings, warnings = listings_mod.search(
        cbsa, num_homes=200, min_price=min_price, max_price=max_price,
    )
    if not listings:
        return {"results": [], "warnings": warnings}

    # Pull MSA archetype + scores once
    con = connect()
    raw = msa_score.features(con)
    archetype = None
    appr_score = cf_score = None
    msa_pct = None
    if not raw.empty:
        scored = msa_score.with_archetype(msa_score.score(raw))
        m = scored[scored["cbsa_code"].astype(str) == str(cbsa)]
        if not m.empty:
            row = m.iloc[0]
            archetype = row.get("archetype")
            appr_score = float(row.get("appreciation_score") or 0)
            cf_score = float(row.get("cashflow_score") or 0)
            msa_pct = float(scored["total_return_score"].rank(pct=True).loc[m.index[0]])

    # AVM map for the CBSA's zips (single SQL hit)
    avm_mod.persist(con)  # idempotent
    avm_rows = con.execute(
        "SELECT zip, divergence_z, direction FROM zip_avm_signal"
    ).fetchdf()
    avm_by_zip = {r.zip: (r.direction, r.divergence_z) for r in avm_rows.itertuples()}

    # Schools overlay for the zips in this CBSA
    schools_rows = con.execute(
        """SELECT s.zip, s.school_count, s.elementary_count, s.middle_count,
                  s.high_count, s.charter_count, s.total_enrollment,
                  s.avg_student_teacher_ratio
           FROM schools_zip s
           JOIN zip_county_xwalk z ON z.zip = s.zip
           JOIN county_cbsa_xwalk c ON c.fips_county = z.fips_county
           WHERE c.cbsa_code = ?""",
        [str(cbsa)],
    ).fetchdf()
    schools_by_zip = {
        r.zip: {
            "school_count":      int(r.school_count or 0),
            "elementary_count":  int(r.elementary_count or 0),
            "middle_count":      int(r.middle_count or 0),
            "high_count":        int(r.high_count or 0),
            "charter_count":     int(r.charter_count or 0),
            "total_enrollment":  int(r.total_enrollment or 0),
            "avg_st_ratio":      None if pd.isna(r.avg_student_teacher_ratio)
                                 else round(float(r.avg_student_teacher_ratio), 1),
        }
        for r in schools_rows.itertuples()
    }

    # Median household income per zip via the zip→county→ACS hop
    income_rows = con.execute(
        """SELECT z.zip, a.median_household_income
           FROM zip_county_xwalk z
           JOIN acs_county a ON a.fips_county = z.fips_county
           JOIN county_cbsa_xwalk c ON c.fips_county = z.fips_county
           WHERE c.cbsa_code = ?
             AND a.year = (SELECT MAX(year) FROM acs_county)""",
        [str(cbsa)],
    ).fetchdf()
    income_by_zip = {
        r.zip: int(r.median_household_income) if not pd.isna(r.median_household_income) else None
        for r in income_rows.itertuples()
    }

    results = []
    target_zip = (zip or "").strip().zfill(5) if zip else None
    for L in listings:
        d = L.__dict__
        # Drop incomplete cards: vacant lots / non-residential parcels usually
        # arrive with null beds + null sqft. We need both for a credible
        # rental projection. Also enforce a sane minimum price floor.
        if not d.get("listed_price") or d["listed_price"] < 30_000:
            continue
        if not d.get("zip"):
            continue
        if d.get("beds") is None and d.get("sqft") is None:
            continue
        if target_zip and str(d["zip"]).zfill(5) != target_zip:
            continue
        try:
            p = proj_mod.project(con, d, archetype=archetype,
                                 mortgage_rate=mortgage_rate, ltv=ltv)
        except Exception:
            continue
        avm_dir, avm_z = avm_by_zip.get(d["zip"], (None, None))
        # Run the rec gate using the projection's DSCR and the MSA percentile
        deal = rec_mod.DealUnderwriting(
            stabilized_dscr=p.dscr_y1,
            refi_appraisal_stress_pass=None,
            insurance_trend_pct=None,
            climate_pct=None,
            alpha_stack_count=None,
            stress_coc_on_residual=p.cash_on_cash_y1 * 0.7,  # crude stress
            msa_blended_percentile=msa_pct,
            sensitivity_negative_cashflow=p.dscr_y1 < 1.0,
        )
        rec = rec_mod.classify(deal)

        decision = decision_mod.build(
            d, p, archetype=archetype,
            msa_appreciation_score=appr_score, msa_cashflow_score=cf_score,
            avm_direction=avm_dir, avm_z=float(avm_z) if avm_z is not None else None,
            rec_verdict=rec.verdict.value, rec_reasons=rec.reasons,
            rec_primary_action=rec.primary_action,
            schools=schools_by_zip.get(d["zip"]),
            county_median_income=income_by_zip.get(d["zip"]),
        )

        # Hard threshold filters — honored before sort so the user gets
        # a clean, action-ready list.
        if min_irr  is not None and p.irr_5y       < min_irr:  continue
        if min_dscr is not None and p.dscr_y1      < min_dscr: continue
        if min_cap  is not None and p.cap_rate_y1  < min_cap:  continue

        schools = schools_by_zip.get(d["zip"])
        if min_school_count is not None and (not schools or schools.get("school_count", 0) < min_school_count):
            continue

        results.append({
            "listing":        _clean(d),
            "projection":     p.__dict__,
            "avm":            {"direction": avm_dir,
                               "z": float(avm_z) if avm_z is not None else None},
            "schools":        schools,
            "county_median_income": income_by_zip.get(d["zip"]),
            "recommendation": rec.to_dict(),
            "decision": {
                "verdict":        decision.verdict,
                "thesis_tag":     decision.thesis_tag,
                "reasons":        decision.reasons,
                "primary_action": decision.primary_action,
            },
        })

    # Sort by chosen criterion
    sort_key = {
        "total_return":  lambda r: r["projection"]["total_return_5y_dollars"],
        "cashflow":      lambda r: r["projection"]["rental_profit_5y"],
        "appreciation":  lambda r: r["projection"]["appreciation_5y_dollars"],
        "irr":           lambda r: r["projection"]["irr_5y"],
    }[sort]
    results.sort(key=sort_key, reverse=True)
    return {"results": results[:limit], "warnings": warnings,
            "market": listings_mod.MARKETS.get(cbsa, {}).get("name", cbsa),
            "archetype": archetype}


@app.post("/api/properties/ingest")
def ingest_property(req: IngestRequest):
    """Paste a Redfin / Zillow / Realtor URL → property dict.

    Best-effort scrape: address, price, beds/baths/sqft, year built, lot,
    plus a Redfin AVM rent estimate when available. Falls back to ZORI for
    the zip if no listing-side rent comes through.
    """
    p = ingest_mod.ingest(req.url)
    out = ingest_mod.to_dict(p)
    if out.get("rent_estimate") is None and out.get("zip"):
        con = connect()
        zori = ingest_mod.rent_estimate_from_zori(con, out["zip"])
        if zori:
            out["rent_estimate"] = round(zori, 2)
            out["rent_source"] = "ZORI fallback"
            out["extracted_via"] = list(out["extracted_via"]) + ["zori:zip"]
    return out


@app.get("/api/coverage-map")
def coverage_map():
    """V1 stub: every county currently 'in_scope' until the Phase-2 coverage
    flow lands. Five Section-8.5 thresholds will populate this."""
    return {
        "version": "v1-stub",
        "in_scope": ["Memphis, TN-MS-AR", "Indianapolis-Carmel-Anderson, IN",
                     "Kansas City, MO-KS", "Birmingham, AL", "Cleveland, OH"],
        "thresholds": [
            "tax_delinquency_freshness <= 30 days",
            "recorder_filings <= 7 days",
            "parcel_GIS_available",
            "rent_comp_density >= 30 active rentals/zip",
            "mls_sold_history_depth >= 24 months",
        ],
    }


# Static SPA last so /api routes take precedence. Wrap StaticFiles to send
# `Cache-Control: no-cache` — the SPA is small (<25KB) and we ship UI
# updates often, so we'd rather make the browser revalidate every load
# than ship users a stale dropdown after every commit.
class _NoCacheStatic(StaticFiles):
    async def get_response(self, path, scope):
        resp = await super().get_response(path, scope)
        resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        resp.headers["Pragma"] = "no-cache"
        return resp

STATIC = Path(__file__).resolve().parent / "static"
if STATIC.exists():
    app.mount("/", _NoCacheStatic(directory=str(STATIC), html=True), name="static")
