"""Custom league analytics engines.

Five pandas-based calculators tuned for a 3-WR / 6-bench / 2-IR league
format. Every function takes (and returns) a DataFrame of player records
shaped like the nested structure Yahoo's Fantasy API actually returns for
players, extended with a few enrichment fields our app layers on top
(projections, draft capital, target share, etc. -- in production these
would come from ``get_league_settings()``/``get_waiver_wire_players()`` in
``api/yahoo_auth.py`` plus an external stats source; see the mock data in
``ui/dashboard.py`` for the exact shape expected here).

Expected columns on the input DataFrame
----------------------------------------
- ``player_key``, ``player_id`` (str)
- ``name``                       nested dict: ``{"full", "first", "last"}``
- ``editorial_team_abbr``        (str) NFL team
- ``display_position``           (str) e.g. "QB", "RB", "WR", "TE"
- ``eligible_positions``         nested list[dict]: ``[{"position": "WR"}, ...]``
- ``status``                     (str) Yahoo injury designation:
                                  ``""``, ``"Q"``, ``"D"``, ``"O"``, ``"IR"``, ``"PUP"``
- ``stats``                      nested dict of *raw season-to-date* stat
                                  totals, keyed by stat name (a stand-in for
                                  Yahoo's real ``stat_id``-keyed structure --
                                  in production, map stat_id -> name using
                                  this league's ``stat_categories`` from
                                  ``get_league_settings()``)
- ``projected_points_by_week``   nested dict: ``{"1": 11.2, "2": 14.0, ...}``
- ``is_rookie``                  (bool)
- ``draft_capital``              nested dict: ``{"round": int, "pick": int}``
                                  (NFL draft round/pick, not Yahoo draft data)
- ``target_share``               (float 0-1) share of team's air targets
- ``deep_target_share``          (float 0-1) share of the player's own
                                  targets that traveled 20+ air yards

Not every calculator needs every column -- see each docstring below.
"""

from __future__ import annotations

from typing import Dict, Optional

import pandas as pd

# Back-half-of-season boundary. A typical fantasy regular season runs weeks
# 1-14 (byes done, playoffs starting ~week 15), so week 10 is a reasonable
# "back half" cutoff for stash/variance plays. Adjust to taste.
BACK_HALF_START_WEEK = 10

# Fallback custom scoring weights, used only if the caller doesn't pass its
# own `scoring_settings` (normally pulled live from
# `YahooAuthManager().get_league_settings()`).
DEFAULT_SCORING_SETTINGS: Dict[str, float] = {
    "passing_yards": 0.04,
    "passing_touchdowns": 4,
    "interceptions": -2,
    "rushing_yards": 0.1,
    "rushing_touchdowns": 6,
    "receptions": 1,          # full-PPR
    "receiving_yards": 0.1,
    "receiving_touchdowns": 6,
    "two_point_conversions": 2,
    "fumbles_lost": -2,
}

# Rookies drafted earlier get a bigger bench-stash bump: Day 1 (round 1) and
# Day 2 (rounds 2-3) picks have far better historical hit rates than Day 3 /
# undrafted rookies, so their high-variance bench upside is worth more.
ROOKIE_DRAFT_DAY_WEIGHTS: Dict[int, float] = {
    1: 1.5,   # Day 1 (Round 1)
    2: 1.25,  # Day 2 (Rounds 2-3)
    3: 1.25,
    4: 1.0,   # Day 3 (Rounds 4-7)
    5: 1.0,
    6: 1.0,
    7: 1.0,
}
UNDRAFTED_WEIGHT = 0.75


def _stat(row_stats, key: str) -> float:
    """Safely pull a numeric stat out of a player's nested `stats` dict."""
    if not isinstance(row_stats, dict):
        return 0.0
    try:
        return float(row_stats.get(key, 0) or 0)
    except (TypeError, ValueError):
        return 0.0


def _back_half_points(projections: Optional[dict], start_week: int = BACK_HALF_START_WEEK) -> float:
    """Sum a player's `projected_points_by_week` dict for weeks >= start_week."""
    if not isinstance(projections, dict):
        return 0.0
    total = 0.0
    for week, points in projections.items():
        try:
            if int(week) >= start_week:
                total += float(points)
        except (TypeError, ValueError):
            continue
    return total


def calculate_custom_value(
    players_df: pd.DataFrame,
    scoring_settings: Optional[Dict[str, float]] = None,
) -> pd.DataFrame:
    """Recalculate total fantasy points using this league's custom scoring.

    Ignores whatever generic point total Yahoo/any other source attached to
    a player and rebuilds it strictly from raw ``stats`` x this league's
    per-stat weights, so waiver wire rankings actually reflect *your*
    scoring settings (e.g. full-PPR, 6pt passing TDs, etc.) rather than a
    site-wide default.

    Args:
        players_df: Player records with a nested ``stats`` dict per row.
        scoring_settings: Mapping of stat name -> point weight. Defaults to
            ``DEFAULT_SCORING_SETTINGS``; pass the league's real weights
            (derived from ``get_league_settings()``) for accurate results.

    Returns:
        Copy of ``players_df`` with a new ``custom_value`` column, sorted
        descending (highest custom value first).
    """
    weights = scoring_settings or DEFAULT_SCORING_SETTINGS
    df = players_df.copy()

    def score_row(stats) -> float:
        return sum(_stat(stats, stat_name) * weight for stat_name, weight in weights.items())

    df["custom_value"] = df["stats"].apply(score_row)
    return df.sort_values("custom_value", ascending=False).reset_index(drop=True)


def apply_rookie_bump(
    players_df: pd.DataFrame,
    bump_pct: float = 0.15,
    start_week: int = BACK_HALF_START_WEEK,
    draft_day_weights: Optional[Dict[int, float]] = None,
) -> pd.DataFrame:
    """Boost rookie RB/WR back-half-of-season projections to surface
    high-variance bench stashes.

    Rookies -- especially those with real NFL draft capital -- often start
    slow and take over a starting role midseason (rookie wall in reverse:
    usage climbs as they earn trust / injuries open a path). This multiplies
    each rookie's *back-half* weekly projections by
    ``1 + (bump_pct * draft_day_weight)``, where earlier NFL draft picks
    (Day 1/2) get the full weight and Day 3 / undrafted rookies get a
    smaller one.

    Args:
        players_df: Player records with ``is_rookie``, ``draft_capital``,
            ``display_position``, and ``projected_points_by_week``.
        bump_pct: Base bump percentage applied to back-half weeks (default 15%).
        start_week: First week considered "back half" (default: module-level
            ``BACK_HALF_START_WEEK``).
        draft_day_weights: Optional override for ``ROOKIE_DRAFT_DAY_WEIGHTS``.

    Returns:
        DataFrame of just the rookie RBs/WRs, with ``back_half_points_base``
        (unbumped) and ``back_half_points_bumped`` columns, sorted descending
        by the bumped total.
    """
    weights = draft_day_weights or ROOKIE_DRAFT_DAY_WEIGHTS
    df = players_df.copy()

    is_rookie_skill_player = df["is_rookie"] & df["display_position"].isin(["RB", "WR"])
    rookies = df[is_rookie_skill_player].copy()

    def draft_weight(draft_capital) -> float:
        if not isinstance(draft_capital, dict) or not draft_capital.get("round"):
            return UNDRAFTED_WEIGHT
        return weights.get(int(draft_capital["round"]), UNDRAFTED_WEIGHT)

    rookies["draft_day_weight"] = rookies["draft_capital"].apply(draft_weight)
    rookies["back_half_points_base"] = rookies["projected_points_by_week"].apply(
        lambda proj: _back_half_points(proj, start_week)
    )
    rookies["back_half_points_bumped"] = rookies["back_half_points_base"] * (
        1 + bump_pct * rookies["draft_day_weight"]
    )

    return rookies.sort_values("back_half_points_bumped", ascending=False).reset_index(drop=True)


def calculate_qb_floor(players_df: pd.DataFrame) -> pd.DataFrame:
    """Isolate each QB's rushing production and compute a rushing-only
    "Base Floor Score" -- the points a QB banks even on a bad passing day.

    Formula: ``rushing_yards / 10 + rushing_touchdowns * 6``

    A high score identifies dual-threat QBs (the "Konami Code" archetype)
    whose weekly floor doesn't collapse with the passing game, as opposed
    to pure pocket passers who live and die by the box score.

    Args:
        players_df: Player records with ``display_position`` and a nested
            ``stats`` dict containing ``rushing_yards``/``rushing_touchdowns``.

    Returns:
        DataFrame filtered to QBs only, with a new ``qb_floor_score`` column,
        sorted descending.
    """
    qbs = players_df[players_df["display_position"] == "QB"].copy()

    def floor_score(stats) -> float:
        return _stat(stats, "rushing_yards") / 10 + _stat(stats, "rushing_touchdowns") * 6

    qbs["qb_floor_score"] = qbs["stats"].apply(floor_score)
    return qbs.sort_values("qb_floor_score", ascending=False).reset_index(drop=True)


def find_ir_stashes(
    players_df: pd.DataFrame,
    back_half_points_threshold: float = 80.0,
    statuses: tuple = ("O", "IR", "PUP"),
) -> pd.DataFrame:
    """Surface injured players worth stashing in one of this league's 2 IR
    slots: currently out (``O``/``IR``/``PUP``) but projected for a strong
    back half of the season once healthy.

    Args:
        players_df: Player records with ``status`` and
            ``projected_points_by_week``.
        back_half_points_threshold: Minimum summed back-half projection
            (weeks >= ``BACK_HALF_START_WEEK``) to qualify as a stash target.
        statuses: Injury statuses considered IR-eligible.

    Returns:
        DataFrame filtered to qualifying players, with a
        ``projected_back_half_points`` column, sorted descending.
    """
    df = players_df.copy()
    df["projected_back_half_points"] = df["projected_points_by_week"].apply(_back_half_points)

    candidates = df[
        df["status"].isin(statuses) & (df["projected_back_half_points"] >= back_half_points_threshold)
    ]
    return candidates.sort_values("projected_back_half_points", ascending=False).reset_index(drop=True)


def evaluate_wr_scarcity(
    players_df: pd.DataFrame,
    min_target_share: float = 0.18,
    deep_target_penalty: float = 50.0,
) -> pd.DataFrame:
    """Find "safe" WR3 floor plays: receivers who see enough volume to be
    reliable, without leaning on low-probability deep-ball touchdowns to hit
    their weekly number.

    Formula: ``wr_floor_score = (target_share * 100) - (deep_target_share * deep_target_penalty)``

    Filters out receivers below ``min_target_share`` (not enough volume to
    trust), then ranks the rest by the floor score -- so a possession
    receiver with heavy target share and a low deep-target rate outranks a
    boom/bust deep threat with a similar target share.

    Args:
        players_df: Player records with ``display_position``,
            ``target_share``, and ``deep_target_share``.
        min_target_share: Minimum target share (0-1) required to qualify.
        deep_target_penalty: Penalty multiplier applied to ``deep_target_share``.

    Returns:
        DataFrame filtered to qualifying WRs, with a ``wr_floor_score``
        column, sorted descending (safest floor plays first).
    """
    wrs = players_df[players_df["display_position"] == "WR"].copy()
    wrs = wrs[wrs["target_share"] >= min_target_share]

    wrs["wr_floor_score"] = (wrs["target_share"] * 100) - (wrs["deep_target_share"] * deep_target_penalty)
    return wrs.sort_values("wr_floor_score", ascending=False).reset_index(drop=True)
