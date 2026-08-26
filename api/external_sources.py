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

import time
from typing import Dict

import requests

SLEEPER_BASE_URL = "https://api.sleeper.app/v1"
REQUEST_TIMEOUT_SECONDS = 30

# How long to trust one fetch of Sleeper's player database before
# re-fetching -- matches every caller's own st.cache_data(ttl=3600), so
# real, current injury designations actually keep refreshing hourly
# rather than going stale for the app's entire process lifetime (see
# the real bug this fixed: a plain @lru_cache(maxsize=1), with no time
# component at all, fetched Sleeper's data exactly ONCE per running
# process and never again -- on Streamlit Cloud, where a process can
# stay alive for days between restarts, that meant a real, current
# injury designation reported on Sleeper AFTER the process last
# restarted would never show up in this app at all until the next
# redeploy/reboot, no matter how many hours passed).
_SLEEPER_PLAYERS_CACHE_TTL_SECONDS = 3600
_sleeper_players_cache: Dict[str, tuple] = {}


def _load_sleeper_players() -> dict:
    """Sleeper's full player database, keyed by Sleeper's own player_id.
    Manually TTL-cached (not `st.cache_data` -- this module has no
    Streamlit dependency, deliberately, so it stays usable from a plain
    script) at `_SLEEPER_PLAYERS_CACHE_TTL_SECONDS`, so a real, current
    injury designation Sleeper reports mid-day actually shows up here
    within about an hour, not only after the next app restart."""
    cached = _sleeper_players_cache.get("players")
    now = time.monotonic()
    if cached is not None and (now - cached[1]) < _SLEEPER_PLAYERS_CACHE_TTL_SECONDS:
        return cached[0]

    resp = requests.get(f"{SLEEPER_BASE_URL}/players/nfl", timeout=REQUEST_TIMEOUT_SECONDS)
    resp.raise_for_status()
    data = resp.json()
    _sleeper_players_cache["players"] = (data, now)
    return data


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

# Real, discovered mismatch: Sleeper's own `injury_status` field (checked
# first, below) reports full English words -- confirmed live: "Questionable"
# (430 players league-wide as of this check), "IR" (104), "NA" (92), "PUP"
# (44), "Out" (11), "Sus" (9), "Doubtful" (3), "DNR" (3), "COV" (2) -- while
# every calculator in this project that reads a `status` column
# (`find_ir_stashes()`, `UNAVAILABLE_INJURY_STATUSES`, ...) expects Yahoo's
# own short-code convention (`INJURY_STATUS_LABELS` in
# `data/calculators.py`: "Q"/"D"/"O"/"IR"/"PUP"/"NA"/"SUSP"). "IR"/"PUP"/"NA"
# happen to already match by coincidence -- "Out" and "Sus" do not, which
# silently zeroed out IR Stash Targets' "O" filter (and the SUSP-exclusion
# in the free-agent/roster availability filters) even when real players
# carried that exact status. "DNR" ("Did Not Report") and "COV" have no
# Yahoo-vocabulary equivalent and are passed through unchanged rather than
# guessed at.
SLEEPER_INJURY_STATUS_TO_YAHOO_STYLE = {
    "Questionable": "Q",
    "Doubtful": "D",
    "Out": "O",
    "IR": "IR",
    "PUP": "PUP",
    "NA": "NA",
    "Sus": "SUSP",
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
    PUP/Sus/etc. -- a real weekly-report-style tag), restyled via
    `SLEEPER_INJURY_STATUS_TO_YAHOO_STYLE`, when set; falls back to a
    restyled `status` (Sleeper's broader Active/Inactive/Injured
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
        lookup[str(gsis_id).strip()] = _restyle_sleeper_status(info)
    return lookup


def _restyle_sleeper_status(info: dict) -> dict:
    """Shared by `build_sleeper_injury_status_lookup()` (gsis-keyed) and
    `build_sleeper_name_team_injury_fallback()` (name+team-keyed) --
    restyles one Sleeper player record's status the same way either
    path."""
    injury_status = info.get("injury_status")
    if injury_status:
        status = SLEEPER_INJURY_STATUS_TO_YAHOO_STYLE.get(injury_status, injury_status)
    else:
        status = SLEEPER_STATUS_TO_YAHOO_STYLE.get(info.get("status"), info.get("status") or "")
    return {"status": status, "injury_body_part": info.get("injury_body_part")}


def _normalize_full_name_for_match(name: str) -> str:
    """Lowercase, strip periods/hyphens/apostrophes -- same spirit as
    `api/nfl_enrichment.py`'s `_normalize_last_name_for_match()`, applied
    to a full name instead of just a last name. Does NOT strip a
    trailing Jr/Sr/III the way that function does -- a real, disclosed
    gap (see `build_sleeper_name_team_injury_fallback()`'s docstring)."""
    import re

    name = name.lower()
    name = re.sub(r"[.\-'’]", "", name)
    return name.strip()


def build_sleeper_name_team_injury_fallback() -> Dict[tuple, dict]:
    """``{(normalized_full_name, team): {"status": ..., "injury_body_part": ...}}``
    -- a real, disclosed fallback for players Sleeper's own data hasn't
    linked a `gsis_id` for. Confirmed live: 192 real, active skill-
    position (QB/RB/WR/TE) players carry a real, current `injury_status`
    in Sleeper's data right now with `gsis_id` set to `None` --
    including real fantasy starters (Ja'Marr Chase, Malik Nabers, Xavier
    Worthy, Puka Nacua, Breece Hall, Sam LaPorta among them as of this
    writing), not just deep-bench names. Without this fallback, every
    caller of the gsis-keyed lookup above silently treats all of them as
    healthy.

    Keyed on Sleeper's own `team` field directly (confirmed live: uses
    the same abbreviations as nflverse's play-by-play data -- e.g. "ARI"
    for Arizona, not the "AZ" nflverse's roster data uses -- so a caller
    joining against nflverse-sourced team codes should normalize via
    `api/nfl_enrichment.py`'s `_normalize_team_abbr()` first).

    Built from EVERY Sleeper record with a name and team (not just the
    gsis-less ones), so a genuine collision -- two different real
    players sharing the same normalized full name AND team -- can be
    detected and dropped rather than silently guessed at, same
    defensive pattern as `api/nfl_enrichment.py`'s
    `build_yahoo_adp_lookup()`. A real, disclosed limitation this
    doesn't handle: a name variant with a suffix (Jr/Sr/III) that
    nflverse's own full-name field includes but Sleeper's doesn't (or
    vice versa) won't match -- `_normalize_full_name_for_match()`
    doesn't strip suffixes the way the last-name-only normalizer
    elsewhere in this project does, since stripping a suffix from a
    FULL name risks colliding two real relatives who share a team (e.g.
    a real Sr./Jr. duo) -- an unlikely but real enough risk that this
    stays conservative instead.
    """
    players = _load_sleeper_players()
    by_name_team: Dict[tuple, list] = {}
    for info in players.values():
        full_name = info.get("full_name")
        team = info.get("team")
        if not full_name or not team:
            continue
        key = (_normalize_full_name_for_match(full_name), team)
        by_name_team.setdefault(key, []).append(info)

    result: Dict[tuple, dict] = {}
    for key, entries in by_name_team.items():
        if len(entries) > 1:
            continue  # genuine collision -- never guess, see docstring
        result[key] = _restyle_sleeper_status(entries[0])
    return result


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
