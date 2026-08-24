"""Game-day weather forecast (wind/precipitation) for a team's next
upcoming game -- a real, well-documented driver of game script (sustained
wind meaningfully suppresses passing efficiency and volume) that nothing
else in this project surfaces.

Why this needs its own data source
------------------------------------
``import_schedules()`` (see ``api/nfl_enrichment.py``) does ship ``temp``/
``wind`` columns, but they are NOT a forecast -- confirmed empirically: for
the fully-future 2026 season schedule, 0 of 272 games have a non-null
``temp``/``wind`` value. Those columns only get filled in with the
*actual* recorded conditions once a game has been played (a boxscore
field, not a pregame prediction). Predicting an upcoming game's weather --
the only time it's useful for a roster decision -- needs a real forecast,
which is what this module adds via a free OpenWeatherMap API key.

Setup
------
Get a free key at https://openweathermap.org/price (free tier covers the
5-day/3-hour forecast this module uses). Set it as the ``OPENWEATHERMAP_API_KEY``
environment variable, or add an ``"openweathermap_api_key"`` field to
``private.json`` (same "credentials in private.json, not a .env file"
pattern this project already uses for Yahoo credentials -- see
``api/yahoo_auth.py``). Neither is required -- everything in this module
degrades to returning ``None``/skipping rather than raising if no key is
configured, same as every other optional enrichment source in this
project (e.g. ``api/external_sources.py``'s Sleeper integration).

Known gaps
-----------
- Permanent domes (per the schedule's own ``roof`` column) are skipped
  outright -- correctly: weather can't affect an indoor game.
- Retractable-roof stadiums where nflverse hasn't classified ``roof`` yet
  (this far ahead of the season, that column is often still null) fall
  back to ``RETRACTABLE_ROOF_USUALLY_CLOSED`` -- an assumption (these
  teams close the roof the large majority of the time), not a guarantee.
  Once the schedule's own ``roof`` value is populated, it's always
  trusted over this list.
- International/neutral-site games (e.g. a London or Melbourne game) are
  skipped rather than guessing coordinates -- detected by checking the
  schedule's own ``stadium`` name against that team's real home venue.
- The free OpenWeatherMap tier only forecasts 5 days out, so this only
  ever returns real data for a game within that window (which matches
  what this project actually needs -- the "next upcoming week" game
  script/breakout signals look at) -- by design, not an oversight.
- ``gametime`` is treated as US/Eastern (nflverse's documented convention
  for this column) and converted to UTC via ``zoneinfo`` (handles DST
  correctly). If that convention is ever wrong for a specific game, the
  worst case is picking an adjacent 3-hour forecast block -- a minor
  approximation, not a wrong stadium or wrong day.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Dict, Optional
from zoneinfo import ZoneInfo

import pandas as pd
import requests

from api.nfl_enrichment import _normalize_team_abbr, load_schedules

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
OPENWEATHER_FORECAST_URL = "https://api.openweathermap.org/data/2.5/forecast"
FORECAST_STALENESS_LIMIT_HOURS = 24  # see module docstring's "5 days out" gap

# (lat, lon, real home-stadium name -- the name is used only to detect/skip
# neutral-site and international games, where these coordinates would be
# wrong for that particular game).
TEAM_STADIUM_INFO: Dict[str, tuple] = {
    "ARI": (33.5276, -112.2626, "State Farm Stadium"),
    "ATL": (33.7554, -84.4008, "Mercedes-Benz Stadium"),
    "BAL": (39.2780, -76.6227, "M&T Bank Stadium"),
    "BUF": (42.7738, -78.7870, "Highmark Stadium"),
    "CAR": (35.2258, -80.8528, "Bank of America Stadium"),
    "CHI": (41.8623, -87.6167, "Soldier Field"),
    "CIN": (39.0954, -84.5160, "Paycor Stadium"),
    "CLE": (41.5061, -81.6995, "Cleveland Browns Stadium"),
    "DAL": (32.7473, -97.0945, "AT&T Stadium"),
    "DEN": (39.7439, -105.0201, "Empower Field at Mile High"),
    "DET": (42.3400, -83.0456, "Ford Field"),
    "GB": (44.5013, -88.0622, "Lambeau Field"),
    "HOU": (29.6847, -95.4107, "NRG Stadium"),
    "IND": (39.7601, -86.1639, "Lucas Oil Stadium"),
    "JAX": (30.3239, -81.6373, "EverBank Stadium"),
    "KC": (39.0489, -94.4839, "GEHA Field at Arrowhead Stadium"),
    "LAC": (33.9535, -118.3392, "SoFi Stadium"),
    "LAR": (33.9535, -118.3392, "SoFi Stadium"),
    "LV": (36.0909, -115.1833, "Allegiant Stadium"),
    "MIA": (25.9580, -80.2389, "Hard Rock Stadium"),
    "MIN": (44.9738, -93.2581, "U.S. Bank Stadium"),
    "NE": (42.0909, -71.2643, "Gillette Stadium"),
    "NO": (29.9511, -90.0812, "Caesars Superdome"),
    "NYG": (40.8135, -74.0745, "MetLife Stadium"),
    "NYJ": (40.8135, -74.0745, "MetLife Stadium"),
    "PHI": (39.9008, -75.1675, "Lincoln Financial Field"),
    "PIT": (40.4468, -80.0158, "Acrisure Stadium"),
    "SEA": (47.5952, -122.3316, "Lumen Field"),
    "SF": (37.4032, -121.9698, "Levi's Stadium"),
    "TB": (27.9759, -82.5033, "Raymond James Stadium"),
    "TEN": (36.1665, -86.7713, "Nissan Stadium"),
    "WAS": (38.9077, -76.8645, "Northwest Stadium"),
}

# See module docstring's "Known gaps" -- fallback only, always overridden by
# the schedule's own `roof` value when that's populated.
RETRACTABLE_ROOF_USUALLY_CLOSED = {"HOU", "IND", "ATL", "ARI", "LV", "DAL"}


def _load_api_key() -> Optional[str]:
    """``OPENWEATHERMAP_API_KEY`` env var first, else an optional
    ``"openweathermap_api_key"`` field in ``private.json``. Returns None
    (never raises) if neither is set or private.json can't be read."""
    env_key = os.environ.get("OPENWEATHERMAP_API_KEY")
    if env_key:
        return env_key

    private_json = PROJECT_ROOT / "private.json"
    if private_json.is_file():
        try:
            with open(private_json) as f:
                key = json.load(f).get("openweathermap_api_key")
            if key:
                return key
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Couldn't read private.json for a weather API key: %s", exc)

    return None


def _is_effectively_indoors(team: str, roof: Optional[str]) -> bool:
    """Trusts the schedule's own `roof` value when populated; otherwise
    falls back to `RETRACTABLE_ROOF_USUALLY_CLOSED` (see module docstring)."""
    if isinstance(roof, str) and roof:
        return roof.lower() in {"dome", "closed"}
    return team in RETRACTABLE_ROOF_USUALLY_CLOSED


def _is_true_home_game(team: str, stadium: Optional[str]) -> bool:
    """False for a neutral-site/international game, where `team`'s usual
    home-stadium coordinates in `TEAM_STADIUM_INFO` would be wrong."""
    info = TEAM_STADIUM_INFO.get(team)
    if not info or not isinstance(stadium, str) or not stadium:
        return False
    home_name = info[2].lower()
    return home_name in stadium.lower() or stadium.lower() in home_name


def _game_datetime_utc(gameday: str, gametime: str) -> Optional[datetime]:
    """`import_schedules()`'s `gameday`/`gametime` columns as a UTC
    datetime, treating `gametime` as US/Eastern per nflverse's documented
    convention (handles DST correctly via zoneinfo). Returns None (never
    raises) if either value is missing/malformed."""
    if not gameday or not gametime or pd.isna(gameday) or pd.isna(gametime):
        return None
    try:
        naive = datetime.strptime(f"{gameday} {gametime}", "%Y-%m-%d %H:%M")
    except ValueError:
        return None
    eastern = naive.replace(tzinfo=ZoneInfo("America/New_York"))
    return eastern.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)


@lru_cache(maxsize=32)
def _fetch_forecast_blocks(lat: float, lon: float, api_key: str) -> tuple:
    """One 5-day/3-hour forecast API call, cached per-process -- every game
    at the same stadium in one lookup call shares this. Returns a tuple
    (hashable, for lru_cache) of raw forecast blocks; raises on a network/
    HTTP failure -- callers catch that, this function doesn't swallow it,
    so a real outage is still visible if something logs it."""
    response = requests.get(
        OPENWEATHER_FORECAST_URL,
        params={"lat": lat, "lon": lon, "appid": api_key, "units": "imperial"},
        timeout=10,
    )
    response.raise_for_status()
    return tuple(response.json().get("list", []))


def fetch_game_weather(
    team: str, roof: Optional[str], stadium: Optional[str], game_datetime_utc: Optional[datetime]
) -> Optional[dict]:
    """Best-effort forecasted conditions for one team's next home game.

    Returns ``{"wind_mph", "precip_probability", "temp_f", "game_is_dome"}``
    -- with the first three at ``None`` and ``game_is_dome=True`` for an
    indoor game (a real, useful answer: weather doesn't matter there).

    Returns ``None`` (never raises) if: the game is at a neutral/
    international site, no API key is configured, the game is further out
    than the free tier's 5-day forecast window, or the request itself
    fails (logged as a warning, same as every other optional enrichment
    source in this project).
    """
    if _is_effectively_indoors(team, roof):
        return {"wind_mph": None, "precip_probability": None, "temp_f": None, "game_is_dome": True}

    if game_datetime_utc is None or not _is_true_home_game(team, stadium):
        return None

    api_key = _load_api_key()
    info = TEAM_STADIUM_INFO.get(team)
    if not api_key or not info:
        return None
    lat, lon, _ = info

    try:
        blocks = _fetch_forecast_blocks(lat, lon, api_key)
    except Exception as exc:
        logger.warning("Couldn't fetch weather forecast for %s: %s", team, exc)
        return None

    if not blocks:
        return None

    def _block_dt(block: dict) -> datetime:
        return datetime.strptime(block["dt_txt"], "%Y-%m-%d %H:%M:%S")

    nearest = min(blocks, key=lambda b: abs((_block_dt(b) - game_datetime_utc).total_seconds()))
    hours_off = abs((_block_dt(nearest) - game_datetime_utc).total_seconds()) / 3600
    if hours_off > FORECAST_STALENESS_LIMIT_HOURS:
        return None  # game is further out than the free tier's forecast window

    return {
        "wind_mph": nearest.get("wind", {}).get("speed"),
        "precip_probability": nearest.get("pop"),
        "temp_f": nearest.get("main", {}).get("temp"),
        "game_is_dome": False,
    }


def build_game_weather_lookup(season: int, week: Optional[int] = None) -> Dict[str, dict]:
    """Per-team forecasted game conditions for one week, keyed by
    normalized team abbreviation (mirrors
    ``api/nfl_enrichment.py``'s ``build_game_script_lookup()`` -- same
    "next upcoming week" default, same per-team dict shape convention, so
    the two compose naturally in ``build_enrichment_lookup()``).

    A team missing from the returned dict just means weather couldn't be
    determined for its game this week (see ``fetch_game_weather``'s
    docstring for every reason that can happen) -- never a signal that
    something is wrong.
    """
    games = load_schedules(season)
    games = games.dropna(subset=["spread_line", "total_line"])

    if week is None:
        upcoming = games[games["home_score"].isna()]
        week = int(upcoming["week"].min()) if not upcoming.empty else int(games["week"].max())

    games = games[games["week"] == week]

    lookup: Dict[str, dict] = {}
    for _, row in games.iterrows():
        home = _normalize_team_abbr(row["home_team"])
        away = _normalize_team_abbr(row["away_team"])
        game_dt = _game_datetime_utc(row.get("gameday"), row.get("gametime"))
        weather = fetch_game_weather(home, row.get("roof"), row.get("stadium"), game_dt)
        if weather is None:
            continue
        lookup[home] = weather
        lookup[away] = weather  # same stadium, same conditions, affects both teams equally
    return lookup
