"""Maps yfpy `Player` objects (real Yahoo Fantasy data, via `YahooAuthManager`)
into the DataFrame shape `data/calculators.py` and `ui/dashboard.py` expect --
the same shape `ui/dashboard.py`'s mock data uses.

Known gaps vs. the mock data
-----------------------------
Yahoo's API does not expose everything our calculators want:

- ``stats``: NOT included in the basic player list -- this module fetches
  each player's season stat line separately via
  ``get_player_stats_for_season()`` and remaps Yahoo's ``stat_id``-keyed
  values to the human-readable names ``calculators.py`` expects, using this
  league's own ``stat_categories`` (from ``get_league_settings()``). That
  remap is a best-effort name/abbreviation match (see
  ``STAT_NAME_ALIASES`` below) -- double check it against your league's
  actual scoring settings if numbers look off.
- ``is_rookie`` / ``draft_capital`` (NFL draft round/pick), ``target_share``
  / ``deep_target_share``, ``injury_opportunity`` (+ who's hurt ahead of
  them), and ``projected_points_by_week``: Yahoo doesn't expose any of
  these. When ``enrich=True`` (the default), this module backfills them
  from nfl_data_py via ``api/nfl_enrichment.py`` -- see that module's
  docstring for exactly how (a real ``yahoo_id`` crosswalk, not name
  matching) and its limits (~half of rostered players are covered; the
  weekly projection is a flat points-per-game baseline, not a true
  projection). Pass ``enrich=False`` to skip it and get Yahoo-only data
  with those fields at their empty defaults.
- ``percent_owned`` / ``percent_owned_delta``: Yahoo *does* provide these
  (league-wide ownership % and its week-over-week change), requested via
  ``out=percent_owned`` in ``get_waiver_wire_players()`` -- not yet
  verified against a live league (see the comment at that call site).

Net effect on the six calculators:
- With ``enrich=True`` (default): all six calculators can produce results,
  bounded by the coverage/accuracy caveats in ``api/nfl_enrichment.py``.
- With ``enrich=False``: only ``calculate_custom_value()`` and
  ``calculate_qb_floor()`` work fully (both only need ``stats``);
  ``apply_rookie_bump()``, ``find_ir_stashes()``, and
  ``evaluate_wr_scarcity()`` return empty, and ``find_breakout_signals()``
  can only fire on the ``percent_owned``-based signal.

Fetching season stats is one extra Yahoo API call *per player*, so
`build_players_dataframe` takes a `count_limit` to keep it fast/well under
Yahoo's rate limits during testing.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

import pandas as pd

from api.yahoo_auth import YahooAuthManager

logger = logging.getLogger(__name__)

# Best-effort match from a Yahoo Stat's `name`/`display_name`/`abbr` (lowercased,
# substring match) to the stat keys `data/calculators.py` looks for. Yahoo lets
# each league customize stat category labels, so verify against your own
# league's `get_league_settings().stat_categories` if a value looks wrong.
STAT_NAME_ALIASES: Dict[str, List[str]] = {
    "passing_yards": ["passing yards"],
    "passing_touchdowns": ["passing touchdowns"],
    "interceptions": ["interceptions"],
    "rushing_yards": ["rushing yards"],
    "rushing_touchdowns": ["rushing touchdowns"],
    "receptions": ["receptions"],
    "receiving_yards": ["receiving yards"],
    "receiving_touchdowns": ["receiving touchdowns"],
    "two_point_conversions": ["2-point conversions", "two-point conversions"],
    "fumbles_lost": ["fumbles lost"],
}


def build_stat_id_name_map(league_settings) -> Dict[int, str]:
    """Build a ``{stat_id: our_internal_name}`` map from this league's
    ``stat_categories``, using ``STAT_NAME_ALIASES`` for best-effort matching.

    Stats that don't match any alias are skipped (they simply won't
    contribute to ``calculate_custom_value()``'s total, same as any stat
    missing from ``DEFAULT_SCORING_SETTINGS``).
    """
    stat_id_map: Dict[int, str] = {}
    stats = getattr(getattr(league_settings, "stat_categories", None), "stats", []) or []

    for stat in stats:
        stat_id = getattr(stat, "stat_id", None)
        if stat_id is None:
            continue
        label = f"{getattr(stat, 'name', '')} {getattr(stat, 'display_name', '')}".lower()
        for internal_name, aliases in STAT_NAME_ALIASES.items():
            if any(alias in label for alias in aliases):
                stat_id_map[int(stat_id)] = internal_name
                break

    return stat_id_map


def _extract_stats_dict(player, stat_id_map: Dict[int, str]) -> Dict[str, float]:
    """Pull a player's season `Stat` list (from `player_stats`) into our
    `{internal_name: value}` dict, defaulting every known stat to 0."""
    stats_dict = {name: 0.0 for name in STAT_NAME_ALIASES}

    player_stats = getattr(player, "player_stats", None)
    raw_stats = getattr(player_stats, "stats", []) if player_stats else []
    for stat in raw_stats:
        stat_id = getattr(stat, "stat_id", None)
        internal_name = stat_id_map.get(int(stat_id)) if stat_id is not None else None
        if internal_name:
            try:
                stats_dict[internal_name] = float(getattr(stat, "value", 0) or 0)
            except (TypeError, ValueError):
                pass

    return stats_dict


def player_to_row(player, stat_id_map: Optional[Dict[int, str]] = None) -> Dict[str, Any]:
    """Convert a single yfpy `Player` into one row matching the schema
    `data/calculators.py` / `ui/dashboard.py` expect."""
    name = getattr(player, "name", None)
    full_name = getattr(name, "full", None) or getattr(player, "full_name", "") or ""
    first_name = getattr(name, "first", None) or getattr(player, "first_name", "") or ""
    last_name = getattr(name, "last", None) or getattr(player, "last_name", "") or ""

    eligible_positions = getattr(player, "eligible_positions", []) or []

    # `percent_owned` is Yahoo's league-wide ownership % for this player, requested
    # via `out=percent_owned` in YahooAuthManager.get_waiver_wire_players(). Its
    # `delta` is the week-over-week change -- used by find_breakout_signals()'s
    # "rising ownership" signal (the wider Yahoo market catching on before your
    # own league does).
    percent_owned_obj = getattr(player, "percent_owned", None)
    percent_owned = getattr(percent_owned_obj, "value", None) if percent_owned_obj else getattr(
        player, "percent_owned_value", None
    )
    percent_owned_delta = getattr(percent_owned_obj, "delta", None) if percent_owned_obj else None

    return {
        "player_key": getattr(player, "player_key", ""),
        "player_id": getattr(player, "player_id", ""),
        "name": {"full": full_name, "first": first_name, "last": last_name},
        "editorial_team_abbr": getattr(player, "editorial_team_abbr", ""),
        "display_position": getattr(player, "display_position", ""),
        "eligible_positions": [{"position": pos} for pos in eligible_positions],
        "status": getattr(player, "status", "") or "",
        "percent_owned": percent_owned,
        "percent_owned_delta": percent_owned_delta,
        # --- Enrichment fields Yahoo doesn't provide -- see api/nfl_enrichment.py's
        # ENRICHMENT_FIELDS for the authoritative list; defaults here are the
        # "no match in the yahoo_id crosswalk" fallback values. ---
        "is_rookie": False,
        "draft_capital": None,
        "target_share": None,
        "deep_target_share": None,
        "red_zone_share": None,
        "te_snap_share": None,
        "team_pass_rate": None,
        "team_pass_epa": None,
        "game_script_spread": None,
        "game_script_total": None,
        "game_script_implied_team_total": None,
        "ngs_separation": None,
        "ngs_yac_above_expectation": None,
        "ngs_time_to_throw": None,
        "ngs_cpoe": None,
        "sleeper_trending_adds": None,
        "injury_opportunity": False,
        "injury_opportunity_ahead_player": None,
        "injury_opportunity_ahead_status": None,
        "team_new_head_coach": False,
        "team_head_coach_name": None,
        "team_new_offensive_coordinator": False,
        "team_offensive_coordinator_name": None,
        "qb_year2_regression_caution": False,
        "qb_year1_ppg": None,
        "stats": _extract_stats_dict(player, stat_id_map) if stat_id_map else {},
        "projected_points_by_week": {},
    }


def build_players_dataframe(
    auth: YahooAuthManager,
    players: List[Any],
    fetch_stats: bool = True,
    count_limit: Optional[int] = 50,
    enrich: bool = True,
    season: Optional[int] = None,
    through_week: Optional[int] = None,
    include_sleeper_trending: bool = True,
) -> pd.DataFrame:
    """Convert a list of yfpy `Player` objects (e.g. from
    `auth.get_waiver_wire_players()`) into the DataFrame shape the
    calculators/dashboard expect.

    Args:
        auth: An authenticated `YahooAuthManager` (used to fetch season
            stats per player and this league's stat_categories).
        players: Players to convert (e.g. the output of
            `auth.get_waiver_wire_players()`).
        fetch_stats: If True, fetch each player's season stat line via one
            extra Yahoo API call per player. Set False to skip (faster,
            but every row's `stats` dict will be all zeros).
        count_limit: Caps how many players get stats fetched, to stay well
            under Yahoo's rate limits. `None` fetches all -- use with a
            small waiver-wire pool only.
        enrich: If True (default), backfill `is_rookie`/`draft_capital`/
            `target_share`/`deep_target_share`/`projected_points_by_week`
            from nfl_data_py via `api/nfl_enrichment.py`. See that module's
            docstring for coverage/accuracy caveats. Set False to skip and
            leave those fields at their Yahoo-only defaults.
        season: NFL season year for enrichment data (only used if
            `enrich=True`). Defaults to `nfl_enrichment.default_nfl_season()`.
        through_week: Only use enrichment data through this week (only used
            if `enrich=True`). Defaults to all available weeks.
        include_sleeper_trending: If True (default), backfill
            `sleeper_trending_adds` from Sleeper's free public API (see
            `api/external_sources.py`). A failed/unreachable request
            (e.g. no network) is logged and skipped rather than raised --
            this is a nice-to-have signal, not core functionality.

    Returns:
        pd.DataFrame: One row per player, ready for `data/calculators.py`.
    """
    stat_id_map = build_stat_id_name_map(auth.get_league_settings()) if fetch_stats else None
    limited_players = players[:count_limit] if count_limit is not None else players

    rows = []
    for player in limited_players:
        if fetch_stats:
            try:
                player = auth.query.get_player_stats_for_season(player.player_key)
            except Exception as exc:
                logger.warning("Couldn't fetch season stats for %s: %s", getattr(player, "player_key", "?"), exc)
        rows.append(player_to_row(player, stat_id_map))

    df = pd.DataFrame(rows)

    if enrich and not df.empty:
        from api.nfl_enrichment import default_nfl_season, enrich_players_dataframe

        df = enrich_players_dataframe(df, season or default_nfl_season(), through_week)

    if include_sleeper_trending and not df.empty:
        try:
            from api.external_sources import build_sleeper_trending_lookup

            trending = build_sleeper_trending_lookup()
            df["sleeper_trending_adds"] = df["player_id"].apply(lambda pid: trending.get(str(pid)))
        except Exception as exc:
            logger.warning("Couldn't fetch Sleeper trending data (skipping): %s", exc)

    return df
