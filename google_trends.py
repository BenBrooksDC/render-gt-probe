"""
google_trends.py — Phase B: Google Trends composite signal for crowd prediction.

Key insight: Search volume for "[Park] tickets" is intent. People searching
"[Park] hours / map / parking" have committed to go. Different lag profiles:
  - Ticket queries: 2-4 week lead time (intent before commitment)
  - Hours/map/parking: 1-3 day lead time (day-of logistics)

Critical normalization: Google normalizes Trends data 0-100 within each
requested window. You CANNOT stitch overlapping windows naively — you must
compute a scalar multiplier from the overlap period to create a continuous
true-volume time series.

Rate-limiting: Google bans IPs on aggressive polling. This module:
  1. Fetches once per week (not daily)
  2. Uses 5-second delay between requests
  3. Caches results aggressively
  4. Gracefully returns 0.0 on any 429 or connection error

Usage (via signal_fetcher.py):
  trends = fetch_google_trends_for_park(park_name, date_range)
  # Returns dict: date_str → {"gt_ticket_z": float, "gt_logistics_z": float, ...}
"""
from __future__ import annotations

import datetime as dt
import json
import os
import time
import warnings
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")

# On Render the container fs is ephemeral. CACHE_DIR is overridable via
# env var so the same code runs on VPS (persistent cache) and Render
# (tmp scratch). VPS handles the real caching layer; Render just executes
# fetches.
CACHE_DIR = Path(os.environ.get("GT_CACHE_DIR",
                                 str(Path(__file__).parent / "cache" / "google_trends")))
CACHE_DIR.mkdir(parents=True, exist_ok=True)

# Cache TTL in days — refresh weekly to avoid rate-limiting
CACHE_TTL_DAYS = 7

# ── Per-park query definitions ────────────────────────────────────────────────
# Intent queries: 2-4 week lag (people planning trips)
# Logistics queries: 1-3 day lag (people confirmed to go)

PARK_TREND_QUERIES: dict[str, dict[str, list[str]]] = {
    # hotel: 7-14 day committed-visitor signal — people booking hotels have committed to the trip.
    # Use the most specific 1-keyword hotel query per park for clean GT normalization.
    "Kings Dominion": {
        "intent": ["Kings Dominion tickets", "Kings Dominion rides"],
        "logistics": ["Kings Dominion hours", "Kings Dominion map", "Kings Dominion parking"],
        "hotel":    ["hotels near Kings Dominion"],
    },
    "Carowinds": {
        "intent": ["Carowinds tickets", "Carowinds rides"],
        "logistics": ["Carowinds hours", "Carowinds map"],
        "hotel":    ["hotels near Carowinds"],
    },
    "Cedar Point": {
        "intent": ["Cedar Point tickets", "Cedar Point fast lane"],
        "logistics": ["Cedar Point hours", "Cedar Point map", "Cedar Point parking"],
        "hotel":    ["Sandusky Ohio hotel"],
    },
    "Kings Island": {
        "intent": ["Kings Island tickets"],
        "logistics": ["Kings Island hours", "Kings Island map"],
        "hotel":    ["hotels near Kings Island"],
    },
    "Dollywood": {
        "intent": ["Dollywood tickets"],
        "logistics": ["Dollywood hours", "Dollywood map"],
        "hotel":    ["Pigeon Forge hotel"],
    },
    "Kennywood": {
        "intent": ["Kennywood tickets"],
        "logistics": ["Kennywood hours"],
        "hotel":    ["hotels near Kennywood"],
    },
    "Hersheypark": {
        "intent": ["Hersheypark tickets"],
        "logistics": ["Hersheypark hours", "Hersheypark map"],
        "hotel":    ["Hershey Pennsylvania hotel"],
    },
    "Knoebels Amusement Resort": {
        "intent": ["Knoebels tickets", "Knoebels rides"],
        "logistics": ["Knoebels hours"],
        "hotel":    ["hotels near Knoebels"],
    },
    "Silver Dollar City": {
        "intent": ["Silver Dollar City tickets"],
        "logistics": ["Silver Dollar City hours"],
        "hotel":    ["Branson Missouri hotel"],
    },
    "Walt Disney World - Magic Kingdom": {
        "intent": ["Disney World tickets", "Magic Kingdom tickets", "Disney World lightning lane"],
        "logistics": ["Disney World hours", "Magic Kingdom hours", "Disney World map"],
        "hotel":    ["Disney World hotel"],
    },
    "Walt Disney World - Epcot": {
        "intent": ["Disney World tickets", "Epcot tickets"],
        "logistics": ["Epcot hours", "Epcot map"],
        "hotel":    ["Disney World hotel"],
    },
    "Walt Disney World - Disney's Hollywood Studios": {
        "intent": ["Hollywood Studios tickets", "Disney World tickets"],
        "logistics": ["Hollywood Studios hours"],
        "hotel":    ["Disney World hotel"],
    },
    "Walt Disney World - Disney's Animal Kingdom": {
        "intent": ["Animal Kingdom tickets", "Disney World tickets"],
        "logistics": ["Animal Kingdom hours"],
        "hotel":    ["Disney World hotel"],
    },
    "Disneyland": {
        "intent": ["Disneyland tickets", "Disneyland lightning lane"],
        "logistics": ["Disneyland hours", "Disneyland map", "Disneyland parking"],
        "hotel":    ["Anaheim hotel near Disneyland"],
    },
    "Disney California Adventure": {
        "intent": ["Disney California Adventure tickets", "Disneyland tickets"],
        "logistics": ["Disney California Adventure hours", "Disneyland hours"],
        "hotel":    ["Anaheim hotel near Disneyland"],
    },
    "Universal Studios Florida": {
        "intent": ["Universal Orlando tickets", "Universal Studios Florida tickets",
                   "Universal express pass"],
        "logistics": ["Universal Orlando hours", "Universal Studios hours"],
        "hotel":    ["Universal Orlando hotel"],
    },
    "Universal Studios Islands of Adventure": {
        "intent": ["Universal Orlando tickets", "Islands of Adventure tickets"],
        "logistics": ["Islands of Adventure hours"],
        "hotel":    ["Universal Orlando hotel"],
    },
    "Universal Epic Universe": {
        "intent": ["Epic Universe tickets", "Universal Epic Universe", "Epic Universe rides"],
        "logistics": ["Epic Universe hours", "Epic Universe map"],
        "hotel":    ["Universal Orlando hotel"],
    },
    "Universal Studios Hollywood": {
        "intent": ["Universal Studios Hollywood tickets"],
        "logistics": ["Universal Studios Hollywood hours", "Universal Hollywood map"],
        "hotel":    ["hotels near Universal Studios Hollywood"],
    },
    "Six Flags Magic Mountain": {
        "intent": ["Six Flags Magic Mountain tickets", "Six Flags flash pass"],
        "logistics": ["Six Flags Magic Mountain hours"],
        "hotel":    ["hotels near Six Flags Magic Mountain"],
    },
    "Six Flags Great Adventure": {
        "intent": ["Six Flags Great Adventure tickets"],
        "logistics": ["Six Flags Great Adventure hours"],
        "hotel":    ["hotels near Six Flags Great Adventure"],
    },
    "Six Flags Great America": {
        "intent": ["Six Flags Great America tickets"],
        "logistics": ["Six Flags Great America hours"],
        "hotel":    ["hotels near Six Flags Great America"],
    },
    "Six Flags Over Georgia": {
        "intent": ["Six Flags Over Georgia tickets"],
        "logistics": ["Six Flags Over Georgia hours"],
        "hotel":    ["hotels near Six Flags Over Georgia"],
    },
    "Six Flags Over Texas": {
        "intent": ["Six Flags Over Texas tickets"],
        "logistics": ["Six Flags Over Texas hours"],
        "hotel":    ["hotels near Six Flags Over Texas"],
    },
    "Six Flags Fiesta Texas": {
        "intent": ["Fiesta Texas tickets", "Six Flags Fiesta Texas"],
        "logistics": ["Fiesta Texas hours"],
        "hotel":    ["hotels near Fiesta Texas"],
    },
    "Six Flags New England": {
        "intent": ["Six Flags New England tickets"],
        "logistics": ["Six Flags New England hours"],
        "hotel":    ["hotels near Six Flags New England"],
    },
    "Six Flags Discovery Kingdom": {
        "intent": ["Six Flags Discovery Kingdom tickets"],
        "logistics": ["Six Flags Discovery Kingdom hours"],
        "hotel":    ["hotels near Six Flags Discovery Kingdom"],
    },
    "Busch Gardens Tampa": {
        "intent": ["Busch Gardens Tampa tickets"],
        "logistics": ["Busch Gardens Tampa hours"],
        "hotel":    ["Tampa hotel near Busch Gardens"],
    },
    "Busch Gardens Williamsburg": {
        "intent": ["Busch Gardens Williamsburg tickets"],
        "logistics": ["Busch Gardens hours", "Busch Gardens Williamsburg hours"],
        "hotel":    ["Williamsburg Virginia hotel"],
    },
    "SeaWorld Orlando": {
        "intent": ["SeaWorld Orlando tickets"],
        "logistics": ["SeaWorld Orlando hours"],
        "hotel":    ["Orlando hotel near SeaWorld"],
    },
    "Seaworld Orlando": {
        "intent": ["SeaWorld Orlando tickets"],
        "logistics": ["SeaWorld Orlando hours"],
        "hotel":    ["Orlando hotel near SeaWorld"],
    },
    "Seaworld San Antonio": {
        "intent": ["SeaWorld San Antonio tickets"],
        "logistics": ["SeaWorld San Antonio hours"],
        "hotel":    ["hotels near SeaWorld San Antonio"],
    },
    "SeaWorld San Antonio": {  # capital-W variant from thrill supplement (304 days)
        "intent": ["SeaWorld San Antonio tickets"],
        "logistics": ["SeaWorld San Antonio hours"],
        "hotel":    ["hotels near SeaWorld San Antonio"],
    },
    "Seaworld San Diego": {
        "intent": ["SeaWorld San Diego tickets"],
        "logistics": ["SeaWorld San Diego hours"],
        "hotel":    ["hotels near SeaWorld San Diego"],
    },
    "SeaWorld San Diego": {  # capital-W variant from thrill supplement (743 days)
        "intent": ["SeaWorld San Diego tickets"],
        "logistics": ["SeaWorld San Diego hours"],
        "hotel":    ["hotels near SeaWorld San Diego"],
    },
    "Knott's Berry Farm": {
        "intent": ["Knotts Berry Farm tickets", "Knott's Berry Farm tickets"],
        "logistics": ["Knotts hours", "Knotts Berry Farm hours"],
        "hotel":    ["Buena Park hotel near Knotts"],
    },
    "Legoland California": {
        "intent": ["Legoland California tickets"],
        "logistics": ["Legoland California hours"],
        "hotel":    ["hotels near Legoland California"],
    },
    "LEGOLAND California": {  # all-caps variant from thrill supplement (704 days)
        "intent": ["Legoland California tickets", "LEGOLAND California tickets"],
        "logistics": ["Legoland California hours"],
        "hotel":    ["hotels near Legoland California"],
    },
    "Legoland Florida": {
        "intent": ["Legoland Florida tickets"],
        "logistics": ["Legoland Florida hours"],
        "hotel":    ["hotels near Legoland Florida"],
    },
    "LEGOLAND Florida": {  # all-caps variant from thrill supplement (715 days)
        "intent": ["Legoland Florida tickets", "LEGOLAND Florida tickets"],
        "logistics": ["Legoland Florida hours"],
        "hotel":    ["hotels near Legoland Florida"],
    },
    "Legoland New York": {
        "intent": ["Legoland New York tickets"],
        "logistics": ["Legoland New York hours"],
        "hotel":    ["hotels near Legoland New York"],
    },
    "LEGOLAND New York": {  # all-caps variant from thrill supplement (289 days)
        "intent": ["Legoland New York tickets", "LEGOLAND New York tickets"],
        "logistics": ["Legoland New York hours"],
        "hotel":    ["hotels near Legoland New York"],
    },
    "Peppa Pig Theme Park Florida": {
        "intent": ["Legoland Florida tickets"],  # shared property
        "logistics": ["Legoland Florida hours"],
        "hotel":    ["hotels near Legoland Florida"],
    },
    "Dorney Park & Wildwater Kingdom": {
        "intent": ["Dorney Park tickets"],
        "logistics": ["Dorney Park hours"],
        "hotel":    ["hotels near Dorney Park"],
    },
    "Lake Compounce": {
        "intent": ["Lake Compounce tickets"],
        "logistics": ["Lake Compounce hours"],
        "hotel":    ["hotels near Lake Compounce"],
    },
    "Adventureland Resort": {
        "intent": ["Adventureland Iowa tickets"],
        "logistics": ["Adventureland Iowa hours"],
        "hotel":    ["hotels near Adventureland Iowa"],
    },
    "Frontier City": {
        "intent": ["Frontier City tickets"],
        "logistics": ["Frontier City hours"],
        "hotel":    ["hotels near Frontier City Oklahoma"],
    },
    "California's Great America": {
        "intent": ["Great America tickets California"],
        "logistics": ["Great America hours California"],
        "hotel":    ["hotels near Great America California"],
    },
    "Six Flags St. Louis": {
        "intent": ["Six Flags St Louis tickets"],
        "logistics": ["Six Flags St Louis hours"],
        "hotel":    ["hotels near Six Flags St Louis"],
    },
    "Valleyfair": {
        "intent": ["Valleyfair tickets"],
        "logistics": ["Valleyfair hours"],
        "hotel":    ["hotels near Valleyfair"],
    },
    "Worlds of Fun": {
        "intent": ["Worlds of Fun tickets"],
        "logistics": ["Worlds of Fun hours"],
        "hotel":    ["hotels near Worlds of Fun"],
    },
    "Michigan's Adventure": {
        "intent": ["Michigans Adventure tickets"],
        "logistics": ["Michigans Adventure hours"],
        "hotel":    ["hotels near Michigans Adventure"],
    },
    "Six Flags America": {
        "intent": ["Six Flags America tickets"],
        "logistics": ["Six Flags America hours"],
        "hotel":    ["hotels near Six Flags America"],
    },
    "Adventureland": {
        "intent": ["Adventureland Iowa tickets"],
        "logistics": ["Adventureland Iowa hours"],
        "hotel":    ["hotels near Adventureland Iowa"],
    },
    "Darien Lake": {
        "intent": ["Darien Lake tickets"],
        "logistics": ["Darien Lake hours"],
        "hotel":    ["hotels near Darien Lake"],
    },
    "Holiday World": {
        "intent": ["Holiday World tickets"],
        "logistics": ["Holiday World hours"],
        "hotel":    ["hotels near Holiday World Indiana"],
    },
    # Added 2026-06-19 — previously in KNOWN_NO_GT. Keep render-side and
    # VPS-side PARK_TREND_QUERIES in sync; the Render service fetches GT
    # on behalf of the VPS, so both files must list the same parks.
    "La Ronde": {
        "intent": ["La Ronde Montreal tickets", "La Ronde billets"],
        "logistics": ["La Ronde hours", "La Ronde Montreal"],
        "hotel":    ["Montreal hotel"],
    },
    "Sesame Place": {
        "intent": ["Sesame Place tickets", "Sesame Place Philadelphia"],
        "logistics": ["Sesame Place hours"],
        "hotel":    ["hotels near Sesame Place"],
    },
    "Six Flags Great Escape": {
        "intent": ["Six Flags Great Escape tickets", "Great Escape Lake George"],
        "logistics": ["Six Flags Great Escape hours"],
        "hotel":    ["Great Escape Lodge"],
    },
}


# ── Overlapping-window normalization ─────────────────────────────────────────

def _normalize_overlapping_windows(
    df_a: pd.DataFrame,
    df_b: pd.DataFrame,
    overlap_col: str,
) -> pd.Series:
    """
    Stitch two Trends windows into a continuous series.

    Google normalizes each window independently to 0-100 relative to that
    window's maximum. To create a continuous series:
    1. Identify the overlap period (dates present in both DataFrames)
    2. Compute scalar: df_a_overlap_mean / df_b_overlap_mean
    3. Scale df_b by that scalar → continuous series

    Returns a pd.Series indexed by date with continuous 0-100+ values.
    """
    if df_a is None or df_b is None or df_a.empty or df_b.empty:
        if df_a is not None and not df_a.empty:
            return df_a[overlap_col]
        if df_b is not None and not df_b.empty:
            return df_b[overlap_col]
        return pd.Series(dtype=float)

    # Find overlap
    overlap_dates = df_a.index.intersection(df_b.index)
    if len(overlap_dates) >= 3:
        a_overlap = df_a.loc[overlap_dates, overlap_col].mean()
        b_overlap = df_b.loc[overlap_dates, overlap_col].mean()
        if b_overlap > 0:
            scalar = a_overlap / b_overlap
        else:
            scalar = 1.0
    else:
        scalar = 1.0  # not enough overlap — can't normalize

    # Scale df_b and combine
    df_b_scaled = df_b[overlap_col] * scalar

    # Union: prefer df_a values in overlap, df_b_scaled elsewhere
    combined = df_a[overlap_col].combine_first(df_b_scaled)
    return combined


def _make_trend_req(cookie_dict: dict) -> Optional["TrendReq"]:  # type: ignore[name-defined]
    """
    Create a single TrendReq instance with browser cookies pre-applied.
    Should be created ONCE per park and reused across all windows to avoid
    GetGoogleCookie() making an extra HTTP request on each instantiation.
    """
    try:
        from pytrends.request import TrendReq
    except ImportError:
        return None

    pt = TrendReq(hl="en-US", tz=360, timeout=(10, 30))
    # Override the cookie pytrends fetched (GetGoogleCookie) with the full
    # browser session cookie dict — this is what bypasses 429 bot detection.
    if cookie_dict:
        pt.cookies = cookie_dict
    return pt


def _safe_pytrends_fetch(
    pt,                        # pre-created TrendReq instance (reuse across windows)
    kw_list: list[str],
    start_date: str,
    end_date: str,
    geo: str = "US",
    sleep_sec: float = 25.0,  # raised from 15 — Google is more lenient with longer gaps
    max_retries: int = 4,
) -> Optional[pd.DataFrame]:
    """
    Fetch a single Google Trends window with error handling and 429 backoff.

    `pt` must be a pre-created TrendReq instance — do NOT create one per call.
    Reusing pt avoids the extra GetGoogleCookie() HTTP request on each window.

    Sleep AFTER each attempt (not before) so the first request goes immediately.
    On 429: exponential backoff starting at 90s (90 → 180 → 360 → 720).
    Returns DataFrame indexed by date, or None on hard failure / exhausted retries.
    """
    import random

    if pt is None:
        return None

    backoff = 90.0  # seconds to wait on first 429
    for attempt in range(max_retries + 1):
        try:
            timeframe = f"{start_date} {end_date}"
            pt.build_payload(kw_list[:1], cat=0, timeframe=timeframe, geo=geo, gprop="")
            # Use only the first keyword — multi-kw queries normalize relative to each
            # other which complicates stitching. One-keyword windows are cleaner.
            df = pt.interest_over_time()
            if df is None or df.empty:
                return None
            df = df.drop(columns=["isPartial"], errors="ignore")
            # Polite delay after a successful fetch before the next call
            jitter = random.uniform(0.8, 1.4)
            time.sleep(sleep_sec * jitter)
            return df

        except Exception as e:
            err = str(e)
            if "429" in err or "TooManyRequests" in err:
                if attempt < max_retries:
                    wait = backoff * random.uniform(0.9, 1.2)
                    print(f"      [GT] 429 rate-limit — waiting {wait:.0f}s "
                          f"(attempt {attempt+1}/{max_retries})", flush=True)
                    time.sleep(wait)
                    backoff *= 2
                    continue
                else:
                    print(f"      [GT] 429 exhausted retries for {kw_list[:1]}", flush=True)
                    return None
            # Non-429 error: bail immediately
            return None

    return None


# ── Feature computation ───────────────────────────────────────────────────────

def compute_trends_features(
    values: pd.Series,
    date: dt.date,
    intent_lag_days: int = 14,
    logistics_lag_days: int = 3,
) -> dict[str, float]:
    """
    Compute features from a raw trends series for a target date.

    Features:
      _7d_mean    : mean over the 7 days ending at (date - lag_days)
      _7d_z       : z-score of that 7-day mean vs 60-day rolling std
      _14d_slope  : slope of the 7-day smoothed series over the prior 14 days
      _spike      : is the last point >1.5 std above the 60-day mean?
    """
    if values is None or values.empty:
        return {"_7d_mean": 0.0, "_7d_z": 0.0, "_14d_slope": 0.0, "_spike": 0.0}

    values = values.astype(float).fillna(0.0)

    # Reference point: (date - lag_days)
    ref_date = date - dt.timedelta(days=intent_lag_days)

    # Convert index to comparable type (pytrends returns Timestamp index)
    try:
        import pandas as pd
        ref_ts = pd.Timestamp(ref_date)
        hist = values[values.index <= ref_ts]
    except Exception:
        ref_date_str = str(ref_date)
        hist = values[values.index <= ref_date_str]

    if len(hist) < 7:
        return {"_7d_mean": 0.0, "_7d_z": 0.0, "_14d_slope": 0.0, "_spike": 0.0}

    mean_7d = float(hist.iloc[-7:].mean())
    std_60d = float(hist.iloc[-60:].std()) if len(hist) >= 30 else float(hist.std())
    mean_60d = float(hist.iloc[-60:].mean()) if len(hist) >= 30 else float(hist.mean())

    z_7d = (mean_7d - mean_60d) / max(std_60d, 1.0)

    # 14-day slope (linear regression)
    vals_14d = hist.iloc[-14:].values
    if len(vals_14d) >= 7:
        x = np.arange(len(vals_14d))
        slope = float(np.polyfit(x, vals_14d, 1)[0])
    else:
        slope = 0.0

    # Spike flag
    last_val = float(hist.iloc[-1])
    spike = 1.0 if (last_val - mean_60d) > 1.5 * max(std_60d, 1.0) else 0.0

    return {
        "_7d_mean": mean_7d / 100.0,  # normalize to [0,1]
        "_7d_z": float(np.clip(z_7d, -3, 3)),
        "_14d_slope": float(np.clip(slope / 5.0, -2, 2)),  # normalize
        "_spike": spike,
    }


# ── Main API ──────────────────────────────────────────────────────────────────

def fetch_google_trends_for_park(
    park_name: str,
    dates: list,
    force_refresh: bool = False,
    cache_only: bool = False,
) -> dict[str, dict[str, float]]:
    """
    Fetch and return Google Trends features for a park across the given dates.

    Returns dict: date_str → {
        "gt_intent_z":    float  (ticket/ride search intensity, z-score)
        "gt_logistics_z": float  (hours/map/parking search, z-score)
        "gt_intent_slope": float (14-day slope — accelerating or fading?)
        "gt_logistics_spike": float (day-of surge, 0 or 1)
        "gt_composite_z": float (basket average z-score)
    }

    cache_only=True: never make live pytrends API calls; return cached data
    or zeros. Use this inside parallel signal fetchers to avoid simultaneous
    429 rate-limits — live fetching is handled by the sequential fetch_gt.py step.

    Returns 0.0 for all features if pytrends is unavailable or rate-limited.
    """
    if not dates:
        return {}

    # Check cache — try canonical name first, then PARK_NAME_ALIASES reverse variants
    safe_name = park_name.replace(" ", "_").replace("/", "_").replace("'", "")
    cache_file = CACHE_DIR / f"{safe_name}.json"

    # If canonical cache doesn't exist, check for alias cache files (handles
    # "Legoland_California.json" → "LEGOLAND_California.json" mismatches)
    if not cache_file.exists() and not force_refresh:
        _REVERSE_ALIASES = {
            "LEGOLAND California": "Legoland_California",
            "LEGOLAND Florida":    "Legoland_Florida",
            "LEGOLAND New York":   "Legoland_New_York",
            "SeaWorld San Antonio": "Seaworld_San_Antonio",
            "SeaWorld San Diego":   "Seaworld_San_Diego",
        }
        if park_name in _REVERSE_ALIASES:
            alt_file = CACHE_DIR / f"{_REVERSE_ALIASES[park_name]}.json"
            if alt_file.exists():
                import shutil
                shutil.copy2(str(alt_file), str(cache_file))

    if cache_file.exists() and not force_refresh:
        age_days = (dt.date.today() - dt.date.fromtimestamp(cache_file.stat().st_mtime)).days
        if age_days < CACHE_TTL_DAYS:
            try:
                cached = json.loads(cache_file.read_text())
                # Phase WA: schema check — old caches (pre-2026-06-03) lacked
                # gt_hotel_z. If first entry is missing it, treat cache as stale.
                first_entry = next(iter(cached.values()), None) if cached else None
                schema_ok = (first_entry is None
                             or "gt_hotel_z" in first_entry)
                if not schema_ok:
                    raise ValueError("pre-WA cache schema — refetch with hotel")
                # Fill any missing dates with zeros
                result = {}
                for d in dates:
                    d_str = str(d)
                    if d_str in cached:
                        # Merge to backfill any missing keys with zeros
                        merged = _zero_trends()
                        merged.update(cached[d_str])
                        result[d_str] = merged
                    else:
                        result[d_str] = _zero_trends()
                return result
            except Exception:
                pass  # cache corrupt or pre-WA schema, re-fetch

    # cache_only: never make live API calls — return zeros for uncached dates
    if cache_only:
        return {str(d): _zero_trends() for d in dates}

    queries = PARK_TREND_QUERIES.get(park_name)
    if not queries:
        return {str(d): _zero_trends() for d in dates}

    # Determine date range needed (need 60+ days of history for z-score baseline)
    min_date = min(dt.date.fromisoformat(str(d)) for d in dates)
    fetch_start = min_date - dt.timedelta(days=90)   # 90-day history before earliest date
    fetch_end   = dt.date.today()

    intent_kws   = queries["intent"][:1]    # one keyword per window for clean normalization
    logistics_kws = queries["logistics"][:1]
    hotel_kws    = (queries.get("hotel") or [])[:1]   # Phase WA: 7-day committed-visitor signal

    # Create ONE TrendReq instance for the entire park fetch.
    # Reusing avoids an extra GetGoogleCookie() HTTP request per window.
    cookie_dict: dict = {}
    try:
        import browser_cookie3
        chrome_cookies = browser_cookie3.chrome(domain_name=".google.com")
        cookie_dict = {c.name: c.value for c in chrome_cookies if c.value}
    except Exception:
        pass
    pt = _make_trend_req(cookie_dict)

    # Fetch strategy: one request per query type covering the full range.
    # For ranges ≤ 270 days: pytrends returns daily data.
    # For ranges > 270 days: pytrends returns weekly data.
    # Weekly data is sufficient for our 7d/14d rolling features — one request
    # is far fewer HTTP calls than overlapping 270-day windows, which hit 429.
    def _fetch_single(kws: list[str]) -> pd.Series:
        """One request covering the full date range. Returns daily or weekly series."""
        df = _safe_pytrends_fetch(pt, kws, fetch_start.isoformat(), fetch_end.isoformat(),
                                  sleep_sec=35.0)
        if df is None or df.empty:
            return pd.Series(dtype=float)
        col = df.columns[0]
        return df[col].rename("v").astype(float)

    intent_series   = _fetch_single(intent_kws)
    logistics_series = _fetch_single(logistics_kws)
    # Phase WA: hotel-intent series — 7-day lag matches typical booking-to-visit
    # window. Empty series when park has no hotel query defined.
    hotel_series = _fetch_single(hotel_kws) if hotel_kws else pd.Series(dtype=float)

    # Compute features per date
    result: dict[str, dict[str, float]] = {}
    for d in dates:
        target_date = dt.date.fromisoformat(str(d))
        d_str = str(d)

        intent_feats   = compute_trends_features(intent_series, target_date,
                                                  intent_lag_days=14)
        logistics_feats = compute_trends_features(logistics_series, target_date,
                                                   intent_lag_days=3)
        hotel_feats    = compute_trends_features(hotel_series, target_date,
                                                  intent_lag_days=7)

        result[d_str] = {
            "gt_intent_z":        intent_feats["_7d_z"],
            "gt_intent_slope":    intent_feats["_14d_slope"],
            "gt_logistics_z":     logistics_feats["_7d_z"],
            "gt_logistics_spike": logistics_feats["_spike"],
            "gt_composite_z":     (intent_feats["_7d_z"] + logistics_feats["_7d_z"]) / 2.0,
            # Phase WA: hotel-intent committed-visitor signal
            "gt_hotel_z":         hotel_feats["_7d_z"],
            "gt_hotel_slope":     hotel_feats["_14d_slope"],
        }

    # Cache results
    try:
        cache_file.write_text(json.dumps(result, indent=2))
    except Exception:
        pass

    return result


def _zero_trends() -> dict[str, float]:
    return {
        "gt_intent_z": 0.0,
        "gt_intent_slope": 0.0,
        "gt_logistics_z": 0.0,
        "gt_logistics_spike": 0.0,
        "gt_composite_z": 0.0,
        # Phase WA: hotel-intent committed-visitor signal
        "gt_hotel_z": 0.0,
        "gt_hotel_slope": 0.0,
    }


# New signal columns added to SIGNAL_COLS
GOOGLE_TRENDS_COLS = [
    "gt_intent_z",
    "gt_intent_slope",
    "gt_logistics_z",
    "gt_logistics_spike",
    "gt_composite_z",
    # Phase WA: hotel-intent committed-visitor signal
    "gt_hotel_z",
    "gt_hotel_slope",
]


if __name__ == "__main__":
    import sys

    park = sys.argv[1] if len(sys.argv) > 1 else "Kings Dominion"
    dates = ["2026-05-17", "2026-05-18", "2026-05-19", "2026-05-20", "2026-05-21"]

    print(f"\nGoogle Trends features for {park}:")
    print(f"  {'Date':<12}  {'intent_z':>9}  {'logist_z':>9}  {'slope':>7}  {'spike':>5}")
    print("  " + "-"*60)

    result = fetch_google_trends_for_park(park, dates, force_refresh=True)
    for d_str, feats in sorted(result.items()):
        print(f"  {d_str:<12}  {feats['gt_intent_z']:>9.3f}  "
              f"{feats['gt_logistics_z']:>9.3f}  "
              f"{feats['gt_intent_slope']:>7.3f}  "
              f"{feats['gt_logistics_spike']:>5.1f}")
