"""External, non-Yahoo/non-nfl_data_py data sources.

Currently just Sleeper's free, no-auth public API, used for a market-wide
"trending adds" signal that works independently of Yahoo -- a genuine
cross-platform complement to Breakout Radar's Yahoo-only `percent_owned`/
`percent_owned_delta` (which only reflects what's happening inside *this*
Yahoo league).

Why Sleeper: no API key, no OAuth, no approval process (unlike Yahoo's
Fantasy Sports API access application) -- just plain HTTP GET requests.
Verified directly: its full player database (`/v1/players/nfl`) ships a
`yahoo_id` field per player, so trending data joins onto our existing
`yahoo_id`-keyed player rows with zero fuzzy name/team matching.

Known gaps
-----------
- Sleeper's `yahoo_id` field isn't populated for every player (especially
  obscure/practice-squad names) -- the same "not every player has matching
  cross-platform data" caveat as nflverse's own `yahoo_id` crosswalk in
  `api/nfl_enrichment.py`.
- Trending counts reflect *Sleeper's* own user base adding/dropping
  players in Sleeper leagues, not Yahoo's. It's a real, directionally
  useful market signal ("is the wider fantasy community catching on to
  this player right now") -- not literally "what Yahoo players are doing."
- The full player database is a ~14 MB JSON payload (~12k players);
  cached for this process's lifetime via `lru_cache` rather than re-fetched
  per call, but it's still one real network round-trip the first time.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Dict

import requests

SLEEPER_BASE_URL = "https://api.sleeper.app/v1"
REQUEST_TIMEOUT_SECONDS = 30


@lru_cache(maxsize=1)
def _load_sleeper_players() -> dict:
    """Sleeper's full player database, keyed by Sleeper's own player_id."""
    resp = requests.get(f"{SLEEPER_BASE_URL}/players/nfl", timeout=REQUEST_TIMEOUT_SECONDS)
    resp.raise_for_status()
    return resp.json()


def build_yahoo_id_to_sleeper_id_map() -> Dict[str, str]:
    """``{yahoo_id: sleeper_player_id}`` -- only for players Sleeper has
    mapped to a `yahoo_id` (see module docstring's coverage caveat)."""
    players = _load_sleeper_players()
    return {
        str(info["yahoo_id"]): sleeper_id
        for sleeper_id, info in players.items()
        if info.get("yahoo_id")
    }


def fetch_trending_adds(lookback_hours: int = 24, limit: int = 200) -> Dict[str, int]:
    """``{sleeper_player_id: add_count}`` for the most-added players across
    all of Sleeper in the last `lookback_hours`."""
    resp = requests.get(
        f"{SLEEPER_BASE_URL}/players/nfl/trending/add",
        params={"lookback_hours": lookback_hours, "limit": limit},
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    resp.raise_for_status()
    return {row["player_id"]: row["count"] for row in resp.json()}


def build_sleeper_trending_lookup(lookback_hours: int = 24, limit: int = 200) -> Dict[str, int]:
    """``{yahoo_id: sleeper_add_count}`` -- ready to join onto our existing
    `yahoo_id`-keyed player rows. Players Sleeper's trending list surfaces
    but that don't have a `yahoo_id` mapped are simply omitted, not
    guessed at."""
    yahoo_to_sleeper = build_yahoo_id_to_sleeper_id_map()
    sleeper_to_yahoo = {v: k for k, v in yahoo_to_sleeper.items()}
    trending = fetch_trending_adds(lookback_hours, limit)
    return {
        sleeper_to_yahoo[sleeper_id]: count
        for sleeper_id, count in trending.items()
        if sleeper_id in sleeper_to_yahoo
    }
