"""Macro series: 30-year mortgage rate (Freddie Mac PMMS), 10-year Treasury,
fed funds, CPI shelter, Case-Shiller HPI, real median income.

Three paths in order of preference:
  1. Freddie Mac PMMS direct CSV (no auth, reliable) — for MORTGAGE30US only.
  2. FRED API (with FRED_API_KEY) — JSON, rich metadata, full history.
  3. fredgraph.csv (no auth, often throttled) — CSV mirror fallback.

The Freddie Mac PMMS path is what FRED's MORTGAGE30US series is built from,
so we cut out the middleman for the rate-context display the platform cares
about most. Other series come from FRED if the key is set, else best-effort
fredgraph.csv.
"""
from __future__ import annotations
import io
import pandas as pd
import httpx
import duckdb
from ._http import get_json
from ..store import upsert_df
from ..config import FRED_API_KEY

SERIES = [
    "MORTGAGE30US",   # 30-yr fixed mortgage rate (weekly, Freddie Mac PMMS)
    "DGS10",          # 10-year Treasury yield (daily, FRB H.15)
    "FEDFUNDS",       # Effective federal funds rate (monthly)
    "CPIHOSSL",       # CPI shelter (monthly)
    "CSUSHPISA",      # Case-Shiller national HPI (monthly)
    "MEHOINUSA672N",  # Real median household income (annual)
]

_CSV_BASE = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={sid}&cosd={cosd}"
_API_BASE = "https://api.stlouisfed.org/fred/series/observations"
_FREDDIE_PMMS_URL = "https://www.freddiemac.com/pmms/docs/PMMS_history.csv"


def _load_mortgage30us_via_freddie() -> pd.DataFrame:
    """Public Freddie Mac PMMS CSV — the upstream source of FRED's MORTGAGE30US.
    No auth, no throttle. Goes back to 1971."""
    r = httpx.get(_FREDDIE_PMMS_URL, timeout=30.0, follow_redirects=True,
                  headers={"User-Agent": "reip-data-loader/1.0"})
    r.raise_for_status()
    df = pd.read_csv(io.StringIO(r.text))
    # Columns: date, pmms30, pmms30p, pmms15, pmms15p, pmms51, ...
    df["period"] = pd.to_datetime(df["date"], format="mixed", errors="coerce").dt.date
    df["value"] = pd.to_numeric(df["pmms30"], errors="coerce")
    df = df.dropna(subset=["period", "value"])
    df["series_id"] = "MORTGAGE30US"
    return df[["series_id", "period", "value"]]


_BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/121.0.0.0 Safari/537.36"
)


def _load_via_csv(sid: str, since: str = "2010-01-01",
                  retries: int = 3, timeout: float = 60.0) -> pd.DataFrame:
    """No-auth CSV path. fredgraph.csv occasionally times out — retry
    with backoff. Returns DataFrame [series_id, period, value]."""
    import time
    url = _CSV_BASE.format(sid=sid, cosd=since)
    last_exc = None
    for i in range(retries):
        try:
            r = httpx.get(url, timeout=timeout, follow_redirects=True,
                          headers={"User-Agent": _BROWSER_UA,
                                   "Accept": "text/csv,*/*"})
            r.raise_for_status()
            df = pd.read_csv(io.StringIO(r.text))
            df.columns = ["period", "value"]
            df["period"] = pd.to_datetime(df["period"]).dt.date
            df["value"] = pd.to_numeric(df["value"], errors="coerce")
            df = df.dropna(subset=["value"])
            df["series_id"] = sid
            return df[["series_id", "period", "value"]]
        except (httpx.TimeoutException, httpx.RemoteProtocolError) as e:
            last_exc = e
            time.sleep(2 * (i + 1))   # 2s, 4s, 6s backoff
    raise last_exc


def _load_via_api(sid: str) -> pd.DataFrame:
    url = f"{_API_BASE}?series_id={sid}&api_key={FRED_API_KEY}&file_type=json"
    data = get_json(url)
    obs = data.get("observations", [])
    if not obs:
        return pd.DataFrame(columns=["series_id", "period", "value"])
    df = pd.DataFrame(obs)
    df["series_id"] = sid
    df["period"] = pd.to_datetime(df["date"]).dt.date
    df["value"] = pd.to_numeric(df["value"], errors="coerce")
    df = df.dropna(subset=["value"])
    return df[["series_id", "period", "value"]]


def load(con: duckdb.DuckDBPyConnection, series: list[str] | None = None) -> int:
    import time
    series = series or SERIES
    total = 0
    for i, sid in enumerate(series):
        if i > 0:
            time.sleep(1.0)   # polite delay between series
        try:
            # MORTGAGE30US has a dedicated no-auth path
            if sid == "MORTGAGE30US":
                df = _load_mortgage30us_via_freddie()
            elif FRED_API_KEY:
                df = _load_via_api(sid)
            else:
                df = _load_via_csv(sid)
        except Exception as e:
            print(f"  fred {sid}: skipped ({type(e).__name__}: {e})")
            continue
        if df.empty:
            continue
        total += upsert_df(con, "fred_macro", df)
        print(f"  fred {sid}: {len(df)} rows")
    return total
