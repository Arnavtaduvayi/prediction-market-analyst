"""
GFS Ensemble Weather Model

Fetches the 31-member GFS ensemble forecast from Open-Meteo (free, no API key)
and computes the probability that max temperature exceeds each threshold.

This is the core edge: GFS ensemble often disagrees with Kalshi market prices
by 8-15%, which is exploitable with limit orders.
"""

import time
from dataclasses import dataclass
from datetime import date, timedelta

import requests

OPEN_METEO_URL = "https://ensemble-api.open-meteo.com/v1/ensemble"

# Kalshi KXHIGH city mapping: city code → (lat, lon, timezone)
CITIES = {
    "NYC": (40.7128, -74.0060, "America/New_York"),
    "CHI": (41.8781, -87.6298, "America/Chicago"),
    "MIA": (25.7617, -80.1918, "America/New_York"),
    "LAX": (34.0522, -118.2437, "America/Los_Angeles"),
    "DEN": (39.7392, -104.9903, "America/Denver"),
    "ATL": (33.7490, -84.3880, "America/New_York"),
    "SEA": (47.6062, -122.3321, "America/Los_Angeles"),
    "PHX": (33.4484, -112.0740, "America/Phoenix"),
    "DAL": (32.7767, -96.7970, "America/Chicago"),
    "BOS": (42.3601, -71.0589, "America/New_York"),
}


@dataclass
class CityForecast:
    city: str
    target_date: date
    ensemble_highs: list[float]   # 31 predicted max temps in °F
    mean: float
    std: float

    def prob_above(self, threshold: float) -> float:
        """Fraction of ensemble members predicting temp > threshold."""
        above = sum(1 for t in self.ensemble_highs if t > threshold)
        return above / len(self.ensemble_highs)

    def prob_below(self, threshold: float) -> float:
        return 1.0 - self.prob_above(threshold)


def celsius_to_fahrenheit(c: float) -> float:
    return c * 9 / 5 + 32


def fetch_ensemble(city: str, target_date: date) -> CityForecast | None:
    """
    Fetch GFS ensemble for one city on one date.
    Returns CityForecast or None if data unavailable.
    """
    if city not in CITIES:
        raise ValueError(f"Unknown city: {city}. Valid: {list(CITIES)}")

    lat, lon, tz = CITIES[city]

    # GFS goes out ~16 days; request target_date ± 1 day for safety
    start = (target_date - timedelta(days=1)).isoformat()
    end = (target_date + timedelta(days=1)).isoformat()

    try:
        r = requests.get(OPEN_METEO_URL, params={
            "latitude": lat,
            "longitude": lon,
            "models": "gfs_seamless",
            "daily": "temperature_2m_max",
            "temperature_unit": "celsius",
            "timezone": tz,
            "start_date": start,
            "end_date": end,
        }, timeout=20)
        r.raise_for_status()
        data = r.json()
    except requests.RequestException as e:
        print(f"  Open-Meteo error for {city}: {e}")
        return None

    daily = data.get("daily", {})
    dates = daily.get("time", [])
    target_str = target_date.isoformat()

    # Collect all ensemble member values for target date
    highs_celsius = []
    for key, values in daily.items():
        if key == "time" or not isinstance(values, list):
            continue
        try:
            idx = dates.index(target_str)
            val = values[idx]
            if val is not None:
                highs_celsius.append(val)
        except (ValueError, IndexError):
            pass

    if not highs_celsius:
        return None

    highs_f = [celsius_to_fahrenheit(c) for c in highs_celsius]
    mean = sum(highs_f) / len(highs_f)
    variance = sum((x - mean) ** 2 for x in highs_f) / len(highs_f)
    std = variance ** 0.5

    return CityForecast(
        city=city,
        target_date=target_date,
        ensemble_highs=highs_f,
        mean=mean,
        std=std,
    )


def fetch_all_cities(target_date: date, delay: float = 0.5) -> dict[str, CityForecast]:
    """Fetch ensemble for all tracked cities. Returns {city: CityForecast}."""
    results = {}
    for city in CITIES:
        fc = fetch_ensemble(city, target_date)
        if fc:
            results[city] = fc
            print(f"  {city}: mean={fc.mean:.1f}°F  std={fc.std:.1f}°F  members={len(fc.ensemble_highs)}")
        time.sleep(delay)
    return results
