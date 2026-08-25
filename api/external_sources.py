"""External, non-Yahoo/non-nfl_data_py data sources.

Sleeper's free, no-auth public API, used for two real, Yahoo-independent
signals: a market-wide "trending adds" signal (a genuine cross-platform
complement to Breakout Radar's Yahoo-only `percent_owned`/
`percent_owned_delta`, which only reflects what's happening inside *this*
Yahoo league) and, more importantly, real CURRENT injury/roster status
(Questionable/Doubtful/Out/IR/PUP/...) for the entirely Yahoo-free player
pool (`build_draft_board_pool()`) -- the one live signal that pool
otherwise has no source for at all (nflverse's own weekly injury report
can't see roster-level IR/PUP designations; see
`build_season_injury_durability_lookup()`'s docstring).

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
    mapped to a `yahoo_id` (see module docstring's coverage caveat).

    A handful of `yahoo_id`s are claimed by more than one Sleeper player
    (confirmed live: 13 of ~6,750 mapped IDs) -- most of those collisions
    are Sleeper's own "Duplicate Player" placeholder rows
    (``first_name == "Duplicate Player"``, ``active == False``), not a
    genuine two-real-players collision. A naive last-wins dict would
    silently attribute trending data to whichever entry happened to
    iterate last, including a placeholder -- this prefers a real, active
    entry when one exists.
    """
    players = _load_sleeper_players()

    by_yahoo_id: Dict[str, list] = {}
    for sleeper_id, info in players.items():
        yahoo_id = info.get("yahoo_id")
        if yahoo_id:
            by_yahoo_id.setdefault(str(yahoo_id), []).append((sleeper_id, info))

    result: Dict[str, str] = {}
    for yahoo_id, entries in by_yahoo_id.items():
        if len(entries) == 1:
            result[yahoo_id] = entries[0][0]
            continue
        non_placeholder = [e for e in entries if e[1].get("full_name") != "Duplicate Player"]
        # Prefer active-and-real, then any real (even inactive -- still a
        # named person, not a placeholder), then fall back to the first
        # entry only if every candidate is a "Duplicate Player" placeholder.
        active_real = [e for e in non_placeholder if e[1].get("active")]
        winner = active_real[0] if active_real else (non_placeholder[0] if non_placeholder else entries[0])
        result[yahoo_id] = winner[0]

    return result


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


# --- gsis-keyed variants: for the Yahoo-free real player pool ---------------
# Sleeper's player records carry a `gsis_id` field directly (nflverse's own
# ID) -- confirmed live -- so these don't need the yahoo_id round-trip above;
# they key straight off gsis_id, matching build_draft_board_pool()'s
# `player_id` column. This is what makes Sleeper's real, current injury
# status (including roster-level "IR"/"PUP", which nflverse's own weekly
# injury report can't provide -- see build_season_injury_durability_lookup()'s
# docstring on why) usable for the Yahoo-free tools, closing the one real gap
# build_draft_board_pool() had: no live `status` field at all.
SLEEPER_STATUS_TO_YAHOO_STYLE = {
    "Injured Reserve": "IR",
    "Physically Unable to Perform": "PUP",
    "Non Football Injury": "NFI",
    "Inactive": "",  # a real person just not on an active roster right now -- not itself an injury signal
    "Active": "",
    "Practice Squad": "",
}


def build_gsis_id_to_sleeper_id_map() -> Dict[str, str]:
    """``{gsis_id: sleeper_player_id}`` -- direct, no crosswalk detour
    needed (unlike the yahoo_id version above, gsis_id is a field Sleeper
    already carries per player). Same collision handling as
    `build_yahoo_id_to_sleeper_id_map()` -- a placeholder/inactive
    duplicate never wins over a real, active entry."""
    players = _load_sleeper_players()

    by_gsis_id: Dict[str, list] = {}
    for sleeper_id, info in players.items():
        gsis_id = info.get("gsis_id")
        if gsis_id:
            by_gsis_id.setdefault(str(gsis_id).strip(), []).append((sleeper_id, info))

    result: Dict[str, str] = {}
    for gsis_id, entries in by_gsis_id.items():
        if len(entries) == 1:
            result[gsis_id] = entries[0][0]
            continue
        non_placeholder = [e for e in entries if e[1].get("full_name") != "Duplicate Player"]
        active_real = [e for e in non_placeholder if e[1].get("active")]
        winner = active_real[0] if active_real else (non_placeholder[0] if non_placeholder else entries[0])
        result[gsis_id] = winner[0]

    return result


def build_sleeper_injury_status_lookup() -> Dict[str, dict]:
    """``{gsis_id: {"status": "...", "injury_body_part": "..."}}`` --
    Sleeper's real, current injury/roster status, restyled to this
    project's existing Yahoo-status vocabulary (see
    `data/calculators.py`'s `INJURY_STATUS_LABELS`) so it drops straight
    into the same `status` column every calculator already expects.

    Prefers Sleeper's own `injury_status` (Questionable/Doubtful/Out/IR/
    PUP/Sus/etc. -- a real weekly-report-style tag) when set; falls back
    to a restyled `status` (Sleeper's broader Active/Inactive/Injured
    Reserve/... roster state) otherwise. Never fabricates a status for a
    healthy, active player -- both map to `""`, same as Yahoo's own
    convention for "nothing to report."
    """
    players = _load_sleeper_players()
    lookup: Dict[str, dict] = {}
    for info in players.values():
        gsis_id = info.get("gsis_id")
        if not gsis_id:
            continue
        gsis_id = str(gsis_id).strip()
        injury_status = info.get("injury_status")
        status = injury_status or SLEEPER_STATUS_TO_YAHOO_STYLE.get(info.get("status"), info.get("status") or "")
        lookup[gsis_id] = {"status": status, "injury_body_part": info.get("injury_body_part")}
    return lookup


def build_sleeper_gsis_trending_lookup(lookback_hours: int = 24, limit: int = 200) -> Dict[str, int]:
    """``{gsis_id: sleeper_add_count}`` -- the gsis-keyed twin of
    `build_sleeper_trending_lookup()`, for the Yahoo-free real player
    pool."""
    gsis_to_sleeper = build_gsis_id_to_sleeper_id_map()
    sleeper_to_gsis = {v: k for k, v in gsis_to_sleeper.items()}
    trending = fetch_trending_adds(lookback_hours, limit)
    return {
        sleeper_to_gsis[sleeper_id]: count
        for sleeper_id, count in trending.items()
        if sleeper_id in sleeper_to_gsis
    }
