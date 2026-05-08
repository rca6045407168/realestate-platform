"""Public-school overlay per zip via Urban Institute's Education Data API.

The Urban Institute proxies NCES Common Core of Data (the canonical school
directory) and exposes a free, key-less REST API. Fields per school: name,
zip, school_level (elementary/middle/high), enrollment, charter, st_ratio.

We pull at the *county* level (one HTTP call per county) and aggregate to
zip. Counties are derived from county_cbsa_xwalk for the requested CBSAs.

What we get per zip:
  - school_count                total schools serving that zip
  - elementary_count            … by level
  - middle_count
  - high_count
  - charter_count               charter schools (proxy for school choice)
  - total_enrollment            sum of enrollment across all schools
  - avg_student_teacher_ratio   weighted by enrollment

What we don't get from this source:
  - test-score-based ratings  (separate paid GreatSchools / Niche)
  - per-school proficiency    (state-level NAEP only at this tier)
"""
from __future__ import annotations
import json
import pandas as pd
import duckdb
from ._http import download
from ..store import upsert_df

BASE = "https://educationdata.urban.org/api/v1/schools/ccd/directory"

# Most recent year for which Urban Institute has the directory complete.
# Bump this once a year.
DEFAULT_YEAR = 2022


def _default_cbsas() -> tuple[str, ...]:
    """Pull the full MARKETS list from listings_search so the schools
    overlay covers every CBSA the screener can search."""
    try:
        from .. import listings_search
        return tuple(listings_search.MARKETS.keys())
    except Exception:
        # Fallback: original 6 launch markets
        return ("32820", "26900", "28140", "13820", "17460", "38300")


def _state_url(year: int, fips_state: str) -> str:
    """Urban Institute's `county_code` param isn't honored on this endpoint;
    we pull at the state level (fips=XX, ~1–2k records each) and filter
    counties locally. Page size 500 → large states need pagination."""
    return f"{BASE}/{year}/?fips={int(fips_state)}&page_size=500"


def _level_label(level_code) -> str:
    """NCES school_level codes: 1=elementary 2=middle 3=high 4=other."""
    return {1: "elementary", 2: "middle", 3: "high"}.get(int(level_code) if level_code else -1, "other")


def _fetch_state(year: int, fips_state: str, refresh: bool = False) -> list[dict]:
    """Hit Urban Institute, follow pagination, return all school records for
    one state."""
    out: list[dict] = []
    url = _state_url(year, fips_state)
    page = 1
    while url:
        try:
            path = download(url, suffix=f".schools.fips{fips_state}.p{page}.json", refresh=refresh)
        except Exception:
            break
        try:
            data = json.loads(path.read_text())
        except Exception:
            break
        out.extend(data.get("results") or [])
        url = data.get("next")
        page += 1
        if page > 30:
            break
    return out


def _aggregate(records: list[dict]) -> pd.DataFrame:
    if not records:
        return pd.DataFrame()
    df = pd.DataFrame(records)
    # Normalize columns
    df["zip"] = df.get("zip_location", df.get("zip", pd.Series(dtype=str))).astype(str).str.zfill(5)
    # Drop missing / sentinel zips. "00000" appears when the source has an
    # empty zip_location; "99999" / "00001" are NCES placeholders.
    df = df[df["zip"].str.match(r"^[0-9]{5}$")]
    df = df[~df["zip"].isin({"00000", "99999", "00001"})]
    if df.empty:
        return pd.DataFrame()
    df["level"] = df.get("school_level", pd.Series(dtype=int)).map(_level_label)
    df["is_charter"] = (df.get("charter", pd.Series(dtype=int)).fillna(0).astype(int) == 1).astype(int)
    df["enrollment"] = pd.to_numeric(df.get("enrollment"), errors="coerce").fillna(0)
    # Some Urban Institute years expose teachers_fte; not all do. Tolerate.
    df["teachers_fte"] = pd.to_numeric(df.get("teachers_fte"), errors="coerce")

    # Operate without a SQL store — plain pandas group-by.
    grp = df.groupby("zip")
    out = pd.DataFrame({
        "school_count":      grp.size().astype(int),
        "elementary_count":  grp["level"].apply(lambda s: int((s == "elementary").sum())),
        "middle_count":      grp["level"].apply(lambda s: int((s == "middle").sum())),
        "high_count":        grp["level"].apply(lambda s: int((s == "high").sum())),
        "charter_count":     grp["is_charter"].sum().astype(int),
        "total_enrollment":  grp["enrollment"].sum().astype("int64"),
    }).reset_index()

    # Weighted student/teacher ratio. Tolerate missing teachers_fte.
    def _ratio(g: pd.DataFrame) -> float:
        e, t = g["enrollment"].sum(), g["teachers_fte"].sum()
        return float(e / t) if t and t > 0 else float("nan")
    out["avg_student_teacher_ratio"] = grp.apply(_ratio).reindex(out["zip"]).values

    return out


def load(
    con: duckdb.DuckDBPyConnection,
    cbsas: tuple[str, ...] | list[str] | None = None,
    year: int = DEFAULT_YEAR,
    refresh: bool = False,
) -> int:
    if cbsas is None:
        cbsas = _default_cbsas()
    """Pull schools for every county in the listed CBSAs and aggregate to zip."""
    counties = con.execute(
        "SELECT DISTINCT fips_county FROM county_cbsa_xwalk WHERE cbsa_code IN ("
        + ",".join(["?"] * len(cbsas)) + ")",
        list(cbsas),
    ).fetchall()
    if not counties:
        print("  schools: no counties found in xwalk — run cbsa_xwalk ingest first")
        return 0
    target_counties = {str(c[0]).zfill(5) for c in counties}
    target_states = {c[:2] for c in target_counties}

    all_records: list[dict] = []
    for fips_state in sorted(target_states):
        recs = _fetch_state(year, fips_state, refresh=refresh)
        # Filter to the counties we care about (Urban Institute returns the
        # full state, ~1–3k schools per state)
        for r in recs:
            cc = str(r.get("county_code") or "").zfill(5)
            if cc in target_counties:
                all_records.append(r)
        print(f"  schools fips={fips_state}: {sum(1 for r in recs if str(r.get('county_code') or '').zfill(5) in target_counties)} of {len(recs)} schools matched target counties")

    agg = _aggregate(all_records)
    if agg.empty:
        return 0
    return upsert_df(con, "schools_zip", agg)
