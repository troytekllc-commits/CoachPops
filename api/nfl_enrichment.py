"""Backfills the enrichment fields Yahoo's API doesn't provide --
``is_rookie``, ``draft_capital``, ``target_share``, ``deep_target_share``,
and a rough ``projected_points_by_week`` baseline -- using ``nfl_data_py``,
joined onto Yahoo players via nflverse's own ``yahoo_id`` crosswalk column.

Why ``yahoo_id`` instead of name/team matching
------------------------------------------------
``nfl_data_py.import_seasonal_rosters()`` ships a ``yahoo_id`` column that
is the *same* numeric ID as yfpy's ``Player.player_id`` -- e.g. Aaron
Rodgers is ``yahoo_id="7200"`` there and ``player_id="7200"`` in Yahoo's own
API (verified directly against a live pull). That's a real, direct join
key, so this module never has to fuzzy-match names across formats like
"A.Rodgers" vs. "Aaron Rodgers", or reconcile team-abbreviation mismatches
(nflverse's draft-pick data uses PFR-style codes like "GNB"/"KAN"/"NWE"
that don't match Yahoo's "GB"/"KC"/"NE" at all).

Coverage caveat: only roughly half of rostered players have a ``yahoo_id``
in nflverse's crosswalk (deep bench / practice-squad / very recently signed
players often don't). Anyone missing from it simply keeps the empty/None
defaults ``api/player_mapper.py`` already sets -- this module never guesses.

Projection caveat: nfl_data_py has no true forward-looking projections
feed. ``estimate_projected_points_by_week()`` builds a rough rest-of-season
BASELINE from each player's season-to-date points-per-game average -- flat
across every remaining week, with no matchup/opponent adjustment. Treat it
as a placeholder for a real projections source, not one itself.
"""

from __future__ import annotations

import datetime
from functools import lru_cache
from typing import Dict, Optional

import pandas as pd

# Weeks used to build the flat rest-of-season baseline (see module docstring).
PROJECTION_WEEKS = range(1, 18)


def default_nfl_season() -> int:
    """Best-guess current NFL season year. The season named "2026" runs
    from around September 2026 through the Super Bowl in February 2027,
    so January/February still belong to the *previous* year's season."""
    today = datetime.date.today()
    return today.year - 1 if today.month <= 2 else today.year


@lru_cache(maxsize=8)
def load_seasonal_rosters(season: int) -> pd.DataFrame:
    import nfl_data_py as nfl

    return nfl.import_seasonal_rosters([season])


@lru_cache(maxsize=8)
def load_draft_picks(season: int) -> pd.DataFrame:
    import nfl_data_py as nfl

    return nfl.import_draft_picks([season])


@lru_cache(maxsize=8)
def load_weekly_data(season: int) -> pd.DataFrame:
    import nfl_data_py as nfl

    return nfl.import_weekly_data(
        [season],
        columns=["player_id", "week", "target_share", "fantasy_points_ppr"],
    )


@lru_cache(maxsize=8)
def load_deep_target_share_by_gsis(season: int) -> Dict[str, float]:
    """Share of each receiver's targets that traveled 20+ air yards
    ("deep" targets), computed from play-by-play data.

    A "target" here is any pass attempt with an identified intended
    receiver, completed or not.
    """
    import nfl_data_py as nfl

    pbp = nfl.import_pbp_data(
        [season],
        columns=["game_id", "week", "receiver_player_id", "air_yards", "pass_attempt"],
        downcast=True,
    )
    targets = pbp[(pbp["pass_attempt"] == 1) & pbp["receiver_player_id"].notna()]
    total_targets = targets.groupby("receiver_player_id").size()
    deep_targets = targets[targets["air_yards"] >= 20].groupby("receiver_player_id").size()
    deep_share = (deep_targets / total_targets).fillna(0.0)
    return deep_share.to_dict()


def estimate_projected_points_by_week(ppg_baseline: Optional[float]) -> Dict[str, float]:
    """Flat rest-of-season baseline projection -- see module docstring's
    "Projection caveat"."""
    if ppg_baseline is None or pd.isna(ppg_baseline):
        return {}
    return {str(week): round(float(ppg_baseline), 1) for week in PROJECTION_WEEKS}


def build_enrichment_lookup(season: int, through_week: Optional[int] = None) -> Dict[str, dict]:
    """Build a ``{yahoo_id: {...enrichment fields...}}`` lookup for one season.

    Args:
        season: NFL season year (e.g. 2024). During a season, this is
            simply the current year -- but in January/February you're
            still in the *previous* year's season, so pass that explicitly.
        through_week: If given, only weeks up to and including this one are
            used to compute ``target_share``/points-per-game averages
            (useful for excluding future/unplayed weeks). Defaults to all
            available weeks.
    """
    rosters = load_seasonal_rosters(season)
    draft_picks = load_draft_picks(season)
    weekly = load_weekly_data(season)
    if through_week is not None:
        weekly = weekly[weekly["week"] <= through_week]
    deep_share_by_gsis = load_deep_target_share_by_gsis(season)

    draft_by_gsis = {
        row["gsis_id"]: {"round": int(row["round"]), "pick": int(row["pick"])}
        for _, row in draft_picks.dropna(subset=["gsis_id"]).iterrows()
    }
    target_share_by_gsis = weekly.groupby("player_id")["target_share"].mean().to_dict()
    ppg_by_gsis = weekly.groupby("player_id")["fantasy_points_ppr"].mean().to_dict()

    lookup: Dict[str, dict] = {}
    for _, row in rosters.dropna(subset=["yahoo_id"]).iterrows():
        yahoo_id = str(row["yahoo_id"])
        gsis_id = row["player_id"]
        lookup[yahoo_id] = {
            "is_rookie": bool(row.get("rookie_year") == season),
            "draft_capital": draft_by_gsis.get(gsis_id),
            "target_share": target_share_by_gsis.get(gsis_id),
            "deep_target_share": deep_share_by_gsis.get(gsis_id),
            "projected_points_by_week": estimate_projected_points_by_week(ppg_by_gsis.get(gsis_id)),
        }
    return lookup


def enrich_players_dataframe(
    players_df: pd.DataFrame,
    season: int,
    through_week: Optional[int] = None,
) -> pd.DataFrame:
    """Fill in ``is_rookie``, ``draft_capital``, ``target_share``,
    ``deep_target_share``, and ``projected_points_by_week`` on a Yahoo
    players DataFrame (as produced by ``api/player_mapper.py``), using
    nflverse data joined via the ``yahoo_id`` crosswalk.

    Players whose ``player_id`` has no match in that crosswalk are left
    with whatever defaults they already had (see module docstring).
    """
    lookup = build_enrichment_lookup(season, through_week)
    df = players_df.copy()

    for field in ("is_rookie", "draft_capital", "target_share", "deep_target_share", "projected_points_by_week"):
        df[field] = df.apply(
            lambda row, f=field: lookup.get(str(row["player_id"]), {}).get(f, row.get(f)),
            axis=1,
        )

    return df
