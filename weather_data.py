"""
weather_data.py — National Weather Service forecast client + probability model.

Free, no API key (api.weather.gov just wants a User-Agent, set in botlib).
Two-hop flow: /points/{lat,lon} -> the gridpoint forecast URL -> daily periods.
We read each daytime period's forecast high (already °F) keyed by date.

The probability model turns a point forecast into a distribution over the daily
high so we can price a temperature bracket:

    high ~ Normal(mu = forecast_high, sigma = f(lead_time))

Forecast skill decays with lead time, so sigma grows with how many days out the
target date is. A half-degree continuity correction is applied because Kalshi
settles on the integer °F.
"""

import math
from datetime import date, datetime, timezone

from botlib import get_json

# Kalshi daily-high series -> (label, NWS station coordinates).
# Coordinates follow the station each Kalshi series settles on; cross-check a
# market's rules_primary if a city's numbers look off.
STATIONS = {
    "KXHIGHNY":  ("NYC Central Park", 40.78, -73.97),
    "KXHIGHCHI": ("Chicago Midway",   41.786, -87.752),
    "KXHIGHLAX": ("Los Angeles LAX",  33.938, -118.389),
    "KXHIGHMIA": ("Miami Intl",       25.79, -80.29),
    "KXHIGHAUS": ("Austin Camp Mabry", 30.32, -97.76),
}

# Forecast-high error (°F) by lead time in days. Same-day forecasts are tight;
# skill degrades further out. Conservative round numbers.
SIGMA_BY_LEAD = {0: 2.0, 1: 3.0, 2: 4.0, 3: 5.0}
SIGMA_FAR = 6.0

_grid_cache: dict[tuple, str] = {}
_high_cache: dict[str, dict] = {}


def norm_cdf(z: float) -> float:
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def sigma_for_lead(lead_days: int) -> float:
    return SIGMA_BY_LEAD.get(max(0, lead_days), SIGMA_FAR)


def _grid_forecast_url(lat: float, lon: float) -> str | None:
    key = (round(lat, 3), round(lon, 3))
    if key in _grid_cache:
        return _grid_cache[key]
    data = get_json(f"https://api.weather.gov/points/{lat},{lon}")
    url = (data.get("properties") or {}).get("forecast")
    if url:
        _grid_cache[key] = url
    return url


def daily_highs(lat: float, lon: float) -> dict[str, float]:
    """{ 'YYYY-MM-DD': forecast_high_F } for the daytime periods available."""
    cache_key = f"{round(lat,3)},{round(lon,3)}"
    if cache_key in _high_cache:
        return _high_cache[cache_key]

    url = _grid_forecast_url(lat, lon)
    if not url:
        return {}
    data = get_json(url)
    periods = (data.get("properties") or {}).get("periods", [])
    highs: dict[str, float] = {}
    for p in periods:
        if not p.get("isDaytime"):
            continue
        start = p.get("startTime", "")
        try:
            d = datetime.fromisoformat(start).date().isoformat()
        except (ValueError, TypeError):
            continue
        temp = p.get("temperature")
        unit = (p.get("temperatureUnit") or "F").upper()
        if temp is None:
            continue
        temp_f = float(temp) if unit == "F" else float(temp) * 9 / 5 + 32
        highs[d] = temp_f
    _high_cache[cache_key] = highs
    return highs


def forecast_high(series_prefix: str, target: date) -> tuple[float | None, float]:
    """Returns (forecast_high_F or None, sigma) for a city on a target date."""
    station = STATIONS.get(series_prefix)
    if not station:
        return None, 0.0
    _, lat, lon = station
    highs = daily_highs(lat, lon)
    mu = highs.get(target.isoformat())
    lead = (target - datetime.now(timezone.utc).date()).days
    return mu, sigma_for_lead(lead)


def bracket_probability(lo: float | None, hi: float | None,
                        mu: float, sigma: float) -> float:
    """
    P(high in [lo, hi]) under Normal(mu, sigma), with a 0.5°F continuity
    correction. lo=None -> '... or below hi'; hi=None -> 'lo or above'.
    """
    if sigma <= 0:
        return 0.0
    lo_p = 0.0 if lo is None else norm_cdf((lo - 0.5 - mu) / sigma)
    hi_p = 1.0 if hi is None else norm_cdf((hi + 0.5 - mu) / sigma)
    return max(0.0, min(1.0, hi_p - lo_p))
