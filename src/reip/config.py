import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = ROOT / "data"
CACHE_DIR = Path(os.getenv("REIP_CACHE") or DATA_DIR / "cache")
DB_PATH = Path(os.getenv("REIP_DB") or DATA_DIR / "reip.duckdb")

CACHE_DIR.mkdir(parents=True, exist_ok=True)
DATA_DIR.mkdir(parents=True, exist_ok=True)

HUD_API_TOKEN = os.getenv("HUD_API_TOKEN", "").strip()
FRED_API_KEY = os.getenv("FRED_API_KEY", "").strip()
CENSUS_API_KEY = os.getenv("CENSUS_API_KEY", "").strip()
FIRSTSTREET_API_KEY = os.getenv("FIRSTSTREET_API_KEY", "").strip()

USER_AGENT = "reip/0.1 (real-estate-investment-platform; richard.chen@flexhaul.ai)"
