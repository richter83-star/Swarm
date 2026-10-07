"""
MODEL agent (weather): daily high-temperature markets (KXHIGH*).

Pulls the forecast daily max for the settlement station from several
numerical weather models (Open-Meteo), treats the multi-model mean as the
centre and widens the spread by a lead-time error floor, then integrates
the normal over the market's strike interval (integer degrees).

Assumptions to calibrate in shadow mode (the reliability store will show
whether they hold):
* ``base_sigma_f`` - typical forecast error by lead day.
* Station coordinates - verify each against the market's rules text.
* Local calendar day - the climate report day may run on standard time.
"""

from __future__ import annotations

import math
import statistics
from datetime import date, datetime, timedelta, timezone
from typing import Dict, Optional

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None  # type: ignore[assignment]

from consensus.agents.base import Agent, Estimate, MarketContext
from consensus.agents.strikes import prob_normal, yes_interval
from consensus.data_sources import OpenMeteo

DEFAULT_STATIONS: Dict[str, Dict] = {
    "KXHIGHNY":   {"name": "NYC Central Park",  "lat": 40.7789, "lon": -73.9692,  "tz": "America/New_York"},
    "KXHIGHCHI":  {"name": "Chicago Midway",    "lat": 41.7868, "lon": -87.7522,  "tz": "America/Chicago"},
    "KXHIGHMIA":  {"name": "Miami Intl",        "lat": 25.7959, "lon": -80.2870,  "tz": "America/New_York"},
    "KXHIGHAUS":  {"name": "Austin Bergstrom",  "lat": 30.1831, "lon": -97.6799,  "tz": "America/Chicago"},
    "KXHIGHDEN":  {"name": "Denver Intl",       "lat": 39.8466, "lon": -104.6562, "tz": "America/Denver"},
    "KXHIGHLAX":  {"name": "Los Angeles LAX",   "lat": 33.9382, "lon": -118.3866, "tz": "America/Los_Angeles"},
    "KXHIGHPHIL": {"name": "Philadelphia Intl", "lat": 39.8733, "lon": -75.2268,  "tz": "America/New_York"},
    "KXHIGHTDC":  {"name": "DC Reagan National", "lat": 38.8483, "lon": -77.0342, "tz": "America/New_York"},
    "KXHIGHTSFO": {"name": "San Francisco SFO", "lat": 37.6197, "lon": -122.3647, "tz": "America/Los_Angeles"},
    "KXHIGHTSEA": {"name": "Seattle Sea-Tac",   "lat": 47.4447, "lon": -122.3144, "tz": "America/Los_Angeles"},
    "KXHIGHTHOU": {"name": "Houston Hobby",     "lat": 29.6375, "lon": -95.2825,  "tz": "America/Chicago"},
}

DEFAULT_MODELS = ["gfs_seamless", "ecmwf_ifs025", "icon_seamless", "gem_seamless"]
DEFAULT_SIGMA = {0: 1.8, 1: 2.5, 2: 3.2, 3: 3.8}

# Standard-time UTC offsets, used only if the OS has no tz database
# (Windows without the ``tzdata`` package).  DST approximated Mar-Nov.
_STD_OFFSETS = {"America/New_York": -5, "America/Chicago": -6,
                "America/Denver": -7, "America/Los_Angeles": -8}


def local_now(now: float, tz_name: str) -> datetime:
    utc = datetime.fromtimestamp(now, tz=timezone.utc)
    if ZoneInfo is not None:
        try:
            return utc.astimezone(ZoneInfo(tz_name))
        except Exception:
            pass
    offset = _STD_OFFSETS.get(tz_name, 0)
    if 3 <= utc.month <= 11:
        offset += 1
    return (utc + timedelta(hours=offset)).replace(tzinfo=None)


def local_date(now: float, tz_name: str) -> date:
    return local_now(now, tz_name).date()


class WeatherAgent(Agent):
    name = "weather"
    family = "weather_model"
    label = "MODEL"

    def __init__(self, config=None, source: Optional[OpenMeteo] = None) -> None:
        super().__init__(config)
        self.source = source or OpenMeteo()
        self.stations = {**DEFAULT_STATIONS, **(self.config.get("stations") or {})}
        self.models = list(self.config.get("models") or DEFAULT_MODELS)
        sig = self.config.get("base_sigma_f") or DEFAULT_SIGMA
        self.base_sigma = {int(k): float(v) for k, v in sig.items()}
        self.max_lead_days = int(self.config.get("max_lead_days", 3))
        self.bias_f = {k: float(v) for k, v in (self.config.get("bias_f") or {}).items()}
        self._cache: Dict[tuple, tuple] = {}
        self.cache_ttl_s = float(self.config.get("cache_ttl_s", 1800))
        self.error_ttl_s = float(self.config.get("error_ttl_s", 120))
        # After this local hour the day's observed temperatures dominate and a
        # forecast-only view is stale, so same-day markets are skipped.
        self.same_day_cutoff_hour = int(self.config.get("same_day_cutoff_local_hour", 11))

    def applies(self, ctx: MarketContext) -> bool:
        return ctx.series in self.stations and ctx.event_date is not None and bool(ctx.strike_type)

    def _forecast(self, series: str, date_iso: str, now: float) -> Dict[str, float]:
        key = (series, date_iso)
        hit = self._cache.get(key)
        if hit:
            ts, values, err = hit
            if err and now - ts < self.error_ttl_s:
                raise RuntimeError(f"cached failure: {err}")
            if not err and now - ts < self.cache_ttl_s:
                return values
        st = self.stations[series]
        try:
            values = self.source.daily_max_f(st["lat"], st["lon"], st["tz"], date_iso, self.models)
        except Exception as exc:
            self._cache[key] = (now, {}, f"{type(exc).__name__}: {exc}"[:200])
            raise
        self._cache[key] = (now, values, "")
        return values

    def estimate(self, ctx: MarketContext) -> Optional[Estimate]:
        st = self.stations[ctx.series]
        target = ctx.event_date
        local = local_now(ctx.now, st["tz"])
        lead = (target - local.date()).days
        if lead < 0 or lead > self.max_lead_days:
            return None
        if lead == 0 and local.hour >= self.same_day_cutoff_hour:
            return None
        interval = yes_interval(ctx.strike_type, ctx.floor_strike, ctx.cap_strike, integer_outcome=True)
        if interval is None:
            return None
        values = self._forecast(ctx.series, target.isoformat(), ctx.now)
        if not values:
            return None
        temps = list(values.values())
        mu = statistics.fmean(temps) + self.bias_f.get(ctx.series, 0.0)
        spread = statistics.stdev(temps) if len(temps) > 1 else 0.0
        base = self.base_sigma.get(lead, self.base_sigma[max(self.base_sigma)])
        sigma = math.sqrt(spread ** 2 + base ** 2)
        p = prob_normal(interval, mu, sigma)
        conf = max(0.2, 0.7 - 0.12 * lead - min(0.2, spread / 10.0))
        detail = ", ".join(f"{k} {v:.1f}" for k, v in sorted(values.items()))
        return Estimate(
            p_yes=p,
            confidence=conf,
            rationale=f"{st['name']} {target} lead {lead}d: mean {mu:.1f}F sd {sigma:.1f}F [{detail}]",
            sources=tuple(f"open-meteo:{m}" for m in sorted(values)),
        )
