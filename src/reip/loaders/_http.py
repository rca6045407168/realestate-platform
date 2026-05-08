"""Shared HTTP + cache helpers."""
from __future__ import annotations
import hashlib
from pathlib import Path
import httpx
from tenacity import retry, stop_after_attempt, wait_exponential
from ..config import CACHE_DIR, USER_AGENT


def _cache_path(url: str, suffix: str = "") -> Path:
    h = hashlib.sha1(url.encode()).hexdigest()[:16]
    return CACHE_DIR / f"{h}{suffix}"


@retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=10))
def download(url: str, suffix: str = "", refresh: bool = False, **kwargs) -> Path:
    """Download URL to cache and return the local path. Cached by URL hash."""
    p = _cache_path(url, suffix)
    if p.exists() and not refresh:
        return p
    headers = kwargs.pop("headers", {})
    headers.setdefault("User-Agent", USER_AGENT)
    with httpx.stream("GET", url, headers=headers, follow_redirects=True, timeout=120, **kwargs) as r:
        r.raise_for_status()
        with p.open("wb") as f:
            for chunk in r.iter_bytes():
                f.write(chunk)
    return p


def get_json(url: str, **kwargs):
    headers = kwargs.pop("headers", {})
    headers.setdefault("User-Agent", USER_AGENT)
    r = httpx.get(url, headers=headers, follow_redirects=True, timeout=60, **kwargs)
    r.raise_for_status()
    return r.json()
