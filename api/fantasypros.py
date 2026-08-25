"""Real, live FantasyPros data -- season-long/weekly projections and expert
consensus rankings -- fetched from FantasyPros' public v2 API. Built as a
"real data now" source while Yahoo's fantasy API approval is pending (see
README's "Additional data sources" notes): unlike this project's other
Yahoo-free tools (Draft Board, Run Game Outlook, O-Line Power Rankings),
which infer next season's outlook from LAST season's real box scores, this
is an actual third-party projection for the season that hasn't been played
yet, plus a real, independent expert-consensus ranking to cross-check
against this project's own model.

Real, discovered limitation -- read before trusting the coverage
------------------------------------------------------------------
The API key behind this integration is FantasyPros' FREE public-API tier,
which caps every request at exactly 10 players (`"public_api_limited":
true` in the raw response -- confirmed directly against the live API, not
documented anywhere obvious) regardless of how many total players exist
for that query (the response's own `count` field reports the REAL total --
e.g. 599 across QB/RB/WR/TE/K/DST combined for a 2026 season projection
request, but only 10 are ever actually returned in `players`).

Fetching one position at a time (instead of `position=ALL`) works around
this partially -- each of the 6 positions gets its OWN 10-player cap, so
`fetch_projections()`/`fetch_consensus_rankings()` fetch QB/RB/WR/TE/K/DST
separately and concatenate, yielding up to *60* real players total (the
top ~10 at each position by FantasyPros' own ordering) -- not the full
player pool. Treat this as a real, elite-tier-only supplement: a live
second opinion at the very top of the draft board, where it happens to
have full coverage, not a full replacement for this app's own Draft Board
(built from complete real stat history, covering every rostered player).

Upgrading the FantasyPros API plan (paid tiers remove this cap) would let
this same code return the full player pool with no changes beyond
removing the per-position fetch loop.

Real ID crosswalk, not name-matching
--------------------------------------
The consensus-rankings endpoint's `player_yahoo_id` field is a REAL,
direct Yahoo player ID (confirmed live against this project's own mock/
real player IDs) -- unlike `data/yahoo_draft_reference.csv` (this
project's other Yahoo-ADP source, manually transcribed from a rendered
PDF with no ID to join on), this is a proper ID join, not a name guess.
The projections endpoint's own player ID (`fpid`) matches
consensus-rankings' `player_id` field exactly for the same real player
(confirmed live: Jahmyr Gibbs is `22968` in both) -- so
`attach_fantasypros_data()` bridges projections onto Yahoo's own ID space
via that shared FantasyPros ID, then joins onto `players_df["player_id"]`
(which already holds Yahoo's ID for every Yahoo-backed player in this
project -- see `api/player_mapper.py`).
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Dict, Optional

import pandas as pd

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
FANTASYPROS_BASE_URL = "https://api.fantasypros.com/public/v2/json/nfl"
FANTASYPROS_POSITIONS = ("QB", "RB", "WR", "TE", "K", "DST")
REQUEST_TIMEOUT_SECONDS = 15
# Confirmed live: firing all 6 positions back-to-back with no delay trips a
# real 429 ("Too Many Requests") on the free tier partway through -- this
# spacing is a real, discovered requirement, not a guess. A cold cache
# (first load of the day) still costs ~6 seconds per fetch; @st.cache_data
# at the call site (see ui/dashboard.py) means that only happens once per
# TTL window, not on every page view.
REQUEST_SPACING_SECONDS = 1.0


def _load_api_key() -> Optional[str]:
    """Same fallback chain as `api/weather.py`'s `_load_api_key()`:
    ``FANTASYPROS_API_KEY`` env var first, then an optional
    ``"fantasypros_api_key"`` field in ``private.json``, then (for a
    hosted deployment with no local filesystem) Streamlit's own secrets
    manager. Returns None (never raises) if none of the three are set."""
    env_key = os.environ.get("FANTASYPROS_API_KEY")
    if env_key:
        return env_key

    private_json = PROJECT_ROOT / "private.json"
    if private_json.is_file():
        try:
            with open(private_json) as f:
                key = json.load(f).get("fantasypros_api_key")
            if key:
                return key
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Couldn't read private.json for a FantasyPros API key: %s", exc)

    try:
        import streamlit as st

        key = st.secrets.get("fantasypros_api_key")
        if key:
            return key
    except Exception:
        pass  # not running under Streamlit, or no secrets.toml configured -- not an error

    return None


def _get(path: str, params: dict) -> dict:
    import requests

    api_key = _load_api_key()
    if not api_key:
        raise RuntimeError(
            "No FantasyPros API key configured -- set FANTASYPROS_API_KEY, add "
            "\"fantasypros_api_key\" to private.json, or add it to Streamlit secrets."
        )
    response = requests.get(
        f"{FANTASYPROS_BASE_URL}/{path}",
        headers={"x-api-key": api_key},
        params=params,
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    return response.json()


def fetch_projections(season: int, week: int = 0, scoring: str = "STD") -> pd.DataFrame:
    """Real FantasyPros projections for `season` (``week=0`` = full-season,
    otherwise that week's projection) -- one row per player, flattened
    from the API's nested ``stats`` dict. Fetches each position
    separately to work around the free tier's 10-player-per-request cap
    (see module docstring) -- up to 60 real players total, not the full
    pool. Raises only if EVERY position's fetch fails; a single failed
    position is logged and skipped so a transient error doesn't blank out
    the other five.
    """
    rows = []
    successes = 0
    for i, position in enumerate(FANTASYPROS_POSITIONS):
        if i > 0:
            time.sleep(REQUEST_SPACING_SECONDS)
        try:
            payload = _get(f"{season}/projections", {"position": position, "scoring": scoring, "week": week})
        except Exception as exc:
            logger.warning("FantasyPros projections fetch failed for position=%s: %s", position, exc)
            continue
        successes += 1
        for player in payload.get("players", []):
            row = {
                "fpid": player.get("fpid"),
                "name": player.get("name"),
                "position_id": player.get("position_id"),
                "team_id": player.get("team_id"),
            }
            row.update(player.get("stats", {}))
            rows.append(row)

    if successes == 0:
        raise RuntimeError(f"FantasyPros projections fetch failed for every position (season={season}).")
    return pd.DataFrame(rows)


def fetch_consensus_rankings(season: int, ranking_type: str = "ST", week: int = 0) -> pd.DataFrame:
    """Real FantasyPros expert-consensus rankings for `season`
    (``ranking_type="ST"`` = standard season-long draft rankings) -- one
    row per player, including the real ``player_yahoo_id`` crosswalk (see
    module docstring) and ``rank_ecr``/``pos_rank``/``tier``/
    ``player_bye_week``. Same per-position fetch loop and partial-failure
    handling as `fetch_projections()`.
    """
    rows = []
    successes = 0
    for i, position in enumerate(FANTASYPROS_POSITIONS):
        if i > 0:
            time.sleep(REQUEST_SPACING_SECONDS)
        try:
            payload = _get(
                f"{season}/consensus-rankings",
                {"type": ranking_type, "position": position, "week": week},
            )
        except Exception as exc:
            logger.warning("FantasyPros consensus-rankings fetch failed for position=%s: %s", position, exc)
            continue
        successes += 1
        rows.extend(payload.get("players", []))

    if successes == 0:
        raise RuntimeError(f"FantasyPros consensus-rankings fetch failed for every position (season={season}).")
    return pd.DataFrame(rows)


FANTASYPROS_ENRICHMENT_FIELDS = (
    "fantasypros_projected_points", "fantasypros_rank_ecr",
    "fantasypros_pos_rank", "fantasypros_tier", "fantasypros_bye_week",
)


def fetch_fantasypros_bundle(season: int, week: int = 0) -> Dict[str, pd.DataFrame]:
    """Both real FantasyPros feeds needed for `join_fantasypros_data()`,
    fetched together with the request spacing this free tier needs
    between them (see `REQUEST_SPACING_SECONDS`). Split out from the join
    step so a caller (e.g. `ui/dashboard.py`'s `@st.cache_data`) can cache
    the network round-trip independently of whatever players DataFrame
    it'll eventually be joined onto -- that DataFrame changes with every
    season/position-filter tweak, but the raw FantasyPros data for a given
    season doesn't need re-fetching just because of that.

    Returns ``{"rankings": ..., "projections": ...}``. Raises if EITHER
    feed's fetch fails entirely (see `fetch_projections()`/
    `fetch_consensus_rankings()` for their own per-position partial-failure
    handling) -- callers should catch and degrade gracefully, same as
    `attach_fantasypros_data()` does.
    """
    rankings = fetch_consensus_rankings(season, week=week)
    time.sleep(REQUEST_SPACING_SECONDS)
    projections = fetch_projections(season, week=week)
    return {"rankings": rankings, "projections": projections}


def join_fantasypros_data(
    players_df: pd.DataFrame,
    rankings: pd.DataFrame,
    projections: pd.DataFrame,
    yahoo_id_column: str = "player_id",
) -> pd.DataFrame:
    """Joins already-fetched FantasyPros `rankings`/`projections` (see
    `fetch_fantasypros_bundle()`) onto `players_df` by Yahoo ID (see
    module docstring for the real ID crosswalk this uses -- not name-
    matching). Pure/no network calls -- safe to call on every rerun.

    Adds `FANTASYPROS_ENRICHMENT_FIELDS`, all ``None`` where no match was
    found (the player isn't in FantasyPros' free-tier top ~10 at their
    position).

    Args:
        yahoo_id_column: Which column on `players_df` already holds
            Yahoo's own player ID. Every Yahoo-backed players_df in this
            project keys its own `player_id` column with Yahoo's ID
            directly (the default here) -- but `build_draft_board_pool()`'s
            Yahoo-free pool keys `player_id` with nflverse's own gsis ID
            instead, carrying Yahoo's ID (only ~half of players have one)
            in a separate `yahoo_id` column -- pass that column name
            explicitly for that pool.
    """
    df = players_df.copy()
    for field in FANTASYPROS_ENRICHMENT_FIELDS:
        df[field] = None

    rankings = rankings.dropna(subset=["player_yahoo_id"]).drop_duplicates("player_yahoo_id").copy()
    rankings["player_yahoo_id"] = rankings["player_yahoo_id"].astype(str)
    rankings_by_yahoo = rankings.set_index("player_yahoo_id")

    fpid_to_yahoo: Dict[int, str] = dict(zip(rankings["player_id"], rankings["player_yahoo_id"]))
    projections = projections.dropna(subset=["fpid"]).drop_duplicates("fpid").copy()
    projections["yahoo_id"] = projections["fpid"].map(fpid_to_yahoo)
    points_by_yahoo = projections.dropna(subset=["yahoo_id"]).set_index("yahoo_id")["points"]

    player_ids = df[yahoo_id_column].astype(str)
    df["fantasypros_projected_points"] = player_ids.map(points_by_yahoo)
    df["fantasypros_rank_ecr"] = player_ids.map(rankings_by_yahoo.get("rank_ecr", pd.Series(dtype=float)))
    df["fantasypros_pos_rank"] = player_ids.map(rankings_by_yahoo.get("pos_rank", pd.Series(dtype=object)))
    df["fantasypros_tier"] = player_ids.map(rankings_by_yahoo.get("tier", pd.Series(dtype=float)))
    df["fantasypros_bye_week"] = player_ids.map(rankings_by_yahoo.get("player_bye_week", pd.Series(dtype=object)))
    return df


def attach_fantasypros_data(
    players_df: pd.DataFrame, season: int, week: int = 0, yahoo_id_column: str = "player_id"
) -> pd.DataFrame:
    """Convenience wrapper: `fetch_fantasypros_bundle()` +
    `join_fantasypros_data()` in one call, for a non-Streamlit caller (or
    ad-hoc/test use) that doesn't need to cache the fetch separately from
    the join. Never raises -- a fetch failure degrades to "no FantasyPros
    context" (all `FANTASYPROS_ENRICHMENT_FIELDS` left ``None``) rather
    than breaking the caller.
    """
    try:
        bundle = fetch_fantasypros_bundle(season, week=week)
    except Exception as exc:
        logger.warning("FantasyPros fetch failed (%s) -- leaving fantasypros_* columns empty.", exc)
        df = players_df.copy()
        for field in FANTASYPROS_ENRICHMENT_FIELDS:
            df[field] = None
        return df

    return join_fantasypros_data(players_df, bundle["rankings"], bundle["projections"], yahoo_id_column)
