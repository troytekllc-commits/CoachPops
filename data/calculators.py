"""Custom league analytics engines.

Eight pandas-based calculators tuned for a 3-WR / 6-bench / 2-IR league
format -- the last, `build_priority_board()`, blends the other seven's
output into one cross-position rank (see its section docstring below).
Every function takes (and returns) a DataFrame of player records
shaped like the nested structure Yahoo's Fantasy API actually returns for
players, extended with a set of enrichment fields our app layers on top
(projections, draft capital, target share, coaching changes, etc. -- in
production these come from ``get_waiver_wire_players()``/
``get_league_settings()`` in ``api/yahoo_auth.py`` plus
``api/nfl_enrichment.py``'s nfl_data_py backfill; see the mock data in
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
- ``red_zone_share``             (float 0-1) share of the *team's* red-zone
                                  (opponent's 10-yard line or closer)
                                  touches (rush attempts + targets) this player got
- ``injury_opportunity``         (bool) a same-team/position player ranked
                                  ahead of them on the depth chart is
                                  Questionable/Doubtful/Out
- ``injury_opportunity_ahead_player`` / ``injury_opportunity_ahead_status`` (str)
- ``team_new_head_coach`` / ``team_new_offensive_coordinator`` (bool)
- ``team_head_coach_name`` / ``team_offensive_coordinator_name`` (str)
- ``qb_year2_regression_caution`` (bool) rookie QB last season who
                                  finished top-15 in PPG -- historically a
                                  Year 2 decline risk
- ``qb_year1_ppg``               (float) that rookie season's PPG
- ``percent_owned`` / ``percent_owned_delta`` (float) Yahoo's league-wide
                                  ownership % and its week-over-week change
- ``game_script_spread`` / ``game_script_total`` / ``game_script_implied_team_total``
                                  (float) Vegas context for this week's game
                                  (positive spread = that team is favored)
- ``ngs_separation`` / ``ngs_yac_above_expectation`` (float) Next Gen Stats
                                  receiving skill metrics
- ``ngs_time_to_throw`` / ``ngs_cpoe`` (float) Next Gen Stats passing
                                  metrics (QBs only)
- ``sleeper_trending_adds``       (int) recent add count across all of
                                  Sleeper (not just this league) -- see
                                  ``api/external_sources.py``
- ``game_wind_mph`` / ``game_precip_probability`` (float) forecasted
                                  conditions for this week's game;
                                  ``game_is_dome`` (bool) -- see
                                  ``api/weather.py``

Not every calculator needs every column -- see each docstring below.
"""

from __future__ import annotations

import logging
from typing import Dict, Optional

import pandas as pd

logger = logging.getLogger(__name__)

# Back-half-of-season boundary. A typical fantasy regular season runs weeks
# 1-14 (byes done, playoffs starting ~week 15), so week 10 is a reasonable
# "back half" cutoff for stash/variance plays. Adjust to taste.
BACK_HALF_START_WEEK = 10

# Fallback custom scoring weights, used only if the caller doesn't pass its
# own `scoring_settings` (normally pulled live from
# `YahooAuthManager().get_league_settings()`). Set to this league's real
# format: standard (non-PPR) scoring, no bonus categories -- confirmed
# directly by the league's owner. `interceptions` is Yahoo's commonly-cited
# out-of-the-box standard default (-1); double check this specific number
# against your league's actual Settings > Scoring page once you have live
# access, since it's the one value here that wasn't explicitly confirmed.
DEFAULT_SCORING_SETTINGS: Dict[str, float] = {
    "passing_yards": 0.04,
    "passing_touchdowns": 4,
    "interceptions": -1,
    "rushing_yards": 0.1,
    "rushing_touchdowns": 6,
    "receptions": 0,          # standard scoring -- no PPR
    "receiving_yards": 0.1,
    "receiving_touchdowns": 6,
    "two_point_conversions": 2,
    "fumbles_lost": -2,
}

# This league's real roster construction (confirmed by the league's owner):
# standard Yahoo roster plus a third starting WR. K/DEF are intentionally
# left out -- nothing in this project models them (no scoring weights, no
# calculators), matching the app's existing skill-position-only scope.
DEFAULT_ROSTER_REQUIREMENTS: Dict[str, int] = {
    "QB": 1,
    "RB": 2,
    "WR": 3,
    "TE": 1,
    "FLEX": 1,  # W/R/T -- see _position_demand() for how this gets split
    "BENCH": 6,
    "IR": 2,
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

    A qualifying WR with a real ``target_share`` but a missing
    ``deep_target_share`` (the roughly half of rostered players outside
    nflverse's ``yahoo_id`` crosswalk) is treated as having *no* known
    deep-ball dependency -- benefit of the doubt, 0 -- rather than
    propagating ``NaN`` into ``wr_floor_score``. Un-fixed, that ``NaN``
    would sort to the bottom via ``sort_values``'s default
    ``na_position="last"``, silently burying a receiver with the *best*
    target share in the qualifying pool as if they were the worst option.

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

    deep_target_share = wrs["deep_target_share"].fillna(0)
    wrs["wr_floor_score"] = (wrs["target_share"] * 100) - (deep_target_share * deep_target_penalty)
    return wrs.sort_values("wr_floor_score", ascending=False).reset_index(drop=True)


# --- Breakout Radar ---------------------------------------------------------
# Signal weights are tunable; see find_breakout_signals()'s docstring for
# what each signal measures and where its data comes from.
BREAKOUT_SIGNAL_WEIGHTS: Dict[str, float] = {
    "injury_opportunity": 2.0,
    "new_offensive_coordinator": 1.5,
    "new_head_coach": 1.0,  # only scored if there's no OC signal already counted for that team
    "efficiency_ahead_of_production": 1.5,
    "rising_ownership": 1.0,
    "meaningful_ownership_elsewhere": 0.5,
    "draft_capital_undersold": 1.5,
    "earned_red_zone_role": 1.0,
    "favorable_game_script": 1.0,
    "sleeper_trending": 0.75,
}

RISING_OWNERSHIP_DELTA_THRESHOLD = 3.0  # percentage points, week over week
MEANINGFUL_OWNERSHIP_THRESHOLD = 15.0  # percent owned across all of Yahoo
UNDERSOLD_ROOKIE_ROUND_THRESHOLD = 4  # Day 3 (round 4+) or undrafted
UNDERSOLD_ROOKIE_TARGET_SHARE = 0.12
UNDERSOLD_ROOKIE_RED_ZONE_SHARE = 0.10
EARNED_RED_ZONE_SHARE_THRESHOLD = 0.20
EFFICIENCY_GAP_THRESHOLD = 0.30  # percentile-rank gap within position
MIN_POSITION_GROUP_FOR_EFFICIENCY_CHECK = 3
FAVORABLE_IMPLIED_TEAM_TOTAL = 24.0  # Vegas-implied team point total (api/nfl_enrichment.py's game_script_*)
SLEEPER_TRENDING_ADD_THRESHOLD = 5000  # sleeper_trending_adds -- Sleeper's own scale, not a percentage
HIGH_WIND_CAUTION_MPH = 15.0  # sustained wind -- the point sports-weather research shows passing volume/efficiency drops off
WIND_CAUTION_POSITIONS = {"QB", "WR", "TE"}


def _efficiency_ahead_of_production_gap(players_df: pd.DataFrame) -> pd.Series:
    """Within each position group, how much higher a player's target-share
    percentile rank is than their custom-scoring percentile rank -- a proxy
    for "the underlying opportunity (targets) hasn't shown up in the box
    score yet" (the Chris Olave / Alec Pierce pattern: advanced metrics
    outpacing the raw stat line).

    Needs at least `MIN_POSITION_GROUP_FOR_EFFICIENCY_CHECK` players at a
    position with non-null `target_share` to compute a meaningful
    percentile -- returns NaN for players in too-small groups rather than a
    misleading rank of 1.
    """
    custom_value = players_df["stats"].apply(
        lambda stats: sum(_stat(stats, name) * weight for name, weight in DEFAULT_SCORING_SETTINGS.items())
    )
    gap = pd.Series(float("nan"), index=players_df.index)

    for _, group_index in players_df.groupby("display_position").groups.items():
        group = players_df.loc[group_index]
        # Bug fix: this used to check the group's total SIZE, not how many
        # of them actually have a real (non-null) target_share -- so a
        # group of 3 with only 2 real values still computed a percentile
        # off just those 2, exactly the "misleading rank" this docstring
        # says is guarded against.
        if group["target_share"].notna().sum() < MIN_POSITION_GROUP_FOR_EFFICIENCY_CHECK:
            continue
        target_share_percentile = group["target_share"].rank(pct=True)
        production_percentile = custom_value.loc[group_index].rank(pct=True)
        gap.loc[group_index] = target_share_percentile - production_percentile

    return gap


def find_breakout_signals(players_df: pd.DataFrame, min_score: float = 1.0) -> pd.DataFrame:
    """Scan the pool for players showing the same predictive patterns behind
    last season's hardest-to-see-coming fantasy performers: an injury-opened
    opportunity, a new offensive play-caller, efficiency metrics running
    ahead of the box score, the wider Yahoo market catching on before your
    own league does, a Day 3/UDFA rookie already earning more volume
    than their draft slot implied, a favorable Vegas-implied game script
    (`api/nfl_enrichment.py`'s `game_script_implied_team_total`), or the
    wider fantasy market (not just this Yahoo league) catching on via
    Sleeper's trending-adds data (`api/external_sources.py`).

    Also raises (but doesn't score against) two cautions: a QB "sophomore
    slump" caution -- rookie QBs who finished top-15 in fantasy PPG have
    historically *declined* more often than not in Year 2 (roughly 11 of
    the last 14) -- a defense's film advantage apparently outweighing the
    QB's own growth (see `api/nfl_enrichment.py`'s
    `build_qb_year2_regression_flags()`) -- and a high-wind caution for
    QB/WR/TE: sustained wind above `HIGH_WIND_CAUTION_MPH` historically
    suppresses passing volume/efficiency (see `api/weather.py`).

    Known gap: a real signal from the same research this is built on --
    whether a rookie/sophomore's positional competition departed or
    arrived (who they're now blocking or being blocked by) -- isn't
    implemented. It needs careful two-season role-matching to avoid noise,
    so it's documented as a future enhancement rather than shipped
    half-reliable; see `api/nfl_enrichment.py`'s module docstring.

    Requires the enrichment fields from `api/nfl_enrichment.py`
    (`injury_opportunity`, `team_new_offensive_coordinator`/`team_new_head_coach`,
    `target_share`, `red_zone_share`, `is_rookie`/`draft_capital`,
    `qb_year2_regression_caution`, `game_script_implied_team_total`,
    `game_wind_mph`) plus `sleeper_trending_adds` (`api/external_sources.py`)
    and Yahoo's own `percent_owned`/`percent_owned_delta`. A signal simply
    doesn't fire for players missing
    that data (e.g. the roughly half of rostered players outside nflverse's
    `yahoo_id` crosswalk) -- this never guesses at a missing value.

    Args:
        players_df: Player records, enriched via `api/nfl_enrichment.py`.
        min_score: Minimum `breakout_score` required to appear in the results.

    Returns:
        DataFrame filtered to players clearing `min_score`, with
        `breakout_score` (float), `breakout_signals` (list[str]), and
        `caution_flags` (list[str]) columns, sorted descending by score.
    """
    df = players_df.copy()
    efficiency_gap = _efficiency_ahead_of_production_gap(df)

    scores, signals_col, cautions_col = [], [], []

    for idx, row in df.iterrows():
        score = 0.0
        signals: list = []
        cautions: list = []

        if row.get("injury_opportunity"):
            score += BREAKOUT_SIGNAL_WEIGHTS["injury_opportunity"]
            signals.append(
                f"Opportunity: {row.get('injury_opportunity_ahead_player')} is "
                f"{row.get('injury_opportunity_ahead_status')}"
            )

        if row.get("team_new_offensive_coordinator"):
            score += BREAKOUT_SIGNAL_WEIGHTS["new_offensive_coordinator"]
            signals.append(f"New offensive coordinator: {row.get('team_offensive_coordinator_name')}")
        elif row.get("team_new_head_coach"):
            score += BREAKOUT_SIGNAL_WEIGHTS["new_head_coach"]
            signals.append(f"New head coach: {row.get('team_head_coach_name')}")

        gap = efficiency_gap.get(idx)
        if gap is not None and not pd.isna(gap) and gap >= EFFICIENCY_GAP_THRESHOLD:
            score += BREAKOUT_SIGNAL_WEIGHTS["efficiency_ahead_of_production"]
            signals.append("Target share running ahead of box-score production")

        delta = row.get("percent_owned_delta")
        owned = row.get("percent_owned")
        if delta is not None and delta >= RISING_OWNERSHIP_DELTA_THRESHOLD:
            score += BREAKOUT_SIGNAL_WEIGHTS["rising_ownership"]
            signals.append(f"Rising ownership (+{delta:.0f} pts this week)")
        elif owned is not None and owned >= MEANINGFUL_OWNERSHIP_THRESHOLD:
            score += BREAKOUT_SIGNAL_WEIGHTS["meaningful_ownership_elsewhere"]
            signals.append(f"Already {owned:.0f}% owned across Yahoo")

        if row.get("is_rookie"):
            draft_capital = row.get("draft_capital")
            undersold_capital = (
                draft_capital is None or draft_capital.get("round", 0) >= UNDERSOLD_ROOKIE_ROUND_THRESHOLD
            )
            has_real_role = (row.get("target_share") or 0) >= UNDERSOLD_ROOKIE_TARGET_SHARE or (
                row.get("red_zone_share") or 0
            ) >= UNDERSOLD_ROOKIE_RED_ZONE_SHARE
            if undersold_capital and has_real_role:
                score += BREAKOUT_SIGNAL_WEIGHTS["draft_capital_undersold"]
                signals.append("Day 3/UDFA rookie already earning real volume")

        if (row.get("red_zone_share") or 0) >= EARNED_RED_ZONE_SHARE_THRESHOLD:
            score += BREAKOUT_SIGNAL_WEIGHTS["earned_red_zone_role"]
            signals.append(f"Earned red-zone role ({row['red_zone_share'] * 100:.0f}% of team share)")

        implied_total = row.get("game_script_implied_team_total")
        if implied_total is not None and pd.notna(implied_total) and implied_total >= FAVORABLE_IMPLIED_TEAM_TOTAL:
            score += BREAKOUT_SIGNAL_WEIGHTS["favorable_game_script"]
            signals.append(f"Favorable game script (Vegas-implied {implied_total:.1f} team points)")

        trending = row.get("sleeper_trending_adds")
        if trending is not None and pd.notna(trending) and trending >= SLEEPER_TRENDING_ADD_THRESHOLD:
            score += BREAKOUT_SIGNAL_WEIGHTS["sleeper_trending"]
            signals.append(f"Trending across Sleeper ({int(trending):,} adds recently)")

        if row.get("qb_year2_regression_caution"):
            cautions.append(
                f"Sophomore-slump caution: top-15 QB PPG as a rookie ({row.get('qb_year1_ppg')} pts/gm) -- "
                "historically ~79% of that group declines in Year 2"
            )

        wind = row.get("game_wind_mph")
        if (
            row.get("display_position") in WIND_CAUTION_POSITIONS
            and wind is not None
            and pd.notna(wind)
            and wind >= HIGH_WIND_CAUTION_MPH
        ):
            cautions.append(f"High wind forecast ({wind:.0f} mph) -- historically suppresses passing volume/efficiency")

        scores.append(score)
        signals_col.append(signals)
        cautions_col.append(cautions)

    df["breakout_score"] = scores
    df["breakout_signals"] = signals_col
    df["caution_flags"] = cautions_col

    candidates = df[df["breakout_score"] >= min_score]
    return candidates.sort_values("breakout_score", ascending=False).reset_index(drop=True)


# --- TE Difference-Maker Finder ---------------------------------------------
# Tight end is unusually top-heavy: a handful of "must-start" weekly options,
# then a canyon, then touchdown-dependent streamers -- there's almost no
# usable middle tier the way there is at RB/WR. The signals below are the
# ones that predict a jump into that top tier *before* the box score shows
# it: real target volume, a real red-zone role, actually running receiving
# routes rather than mostly blocking (`te_snap_share` alone doesn't
# distinguish these -- it's `te_snap_share` combined with `target_share`
# that does), a good, high-volume passing offense to work in, and real
# route-running separation (Next Gen Stats' `ngs_separation` -- getting
# open is a skill signal independent of scheme or volume).
TE_SIGNAL_WEIGHTS: Dict[str, float] = {
    "target_share": 0.25,
    "red_zone_share": 0.20,
    "te_snap_share": 0.15,
    "team_pass_rate": 0.15,
    "team_pass_epa": 0.05,
    "ngs_separation": 0.10,
    "opportunity_bonus": 0.10,  # injury_opportunity / new offensive coordinator, from Breakout Radar's fields
}
MIN_TE_GROUP_FOR_PERCENTILE = 3


def find_te_difference_makers(players_df: pd.DataFrame, min_score: float = 0.0) -> pd.DataFrame:
    """Rank TEs by how likely they are to become a top-tier weekly starter,
    using the pre-box-score signals research shows actually predict it,
    rather than raw fantasy points scored so far.

    Requires the enrichment fields from `api/nfl_enrichment.py`
    (`target_share`, `red_zone_share`, `te_snap_share`, `team_pass_rate`,
    `team_pass_epa`, `ngs_separation`, `injury_opportunity`,
    `team_new_offensive_coordinator`). With too few TEs in the pool to
    compute a meaningful percentile (fewer than
    `MIN_TE_GROUP_FOR_PERCENTILE`), returns an empty DataFrame rather than
    a misleading rank of 1.

    Args:
        players_df: Player records, enriched via `api/nfl_enrichment.py`.
        min_score: Minimum `te_score` (0-100) required to appear in results.

    Returns:
        DataFrame filtered to TEs clearing `min_score`, with a `te_score`
        column and a `te_signals` (list[str]) column explaining why, sorted
        descending.
    """
    tes = players_df[players_df["display_position"] == "TE"].copy()
    if len(tes) < MIN_TE_GROUP_FOR_PERCENTILE:
        return tes.iloc[0:0]

    def pct(col: str, higher_is_better: bool = True) -> pd.Series:
        # A missing value (common with real, partial Yahoo/nfl_data_py
        # data -- e.g. a TE outside nflverse's yahoo_id crosswalk for one
        # specific metric) is treated as *neutral* (0.5), not worst-case,
        # so one missing input doesn't NaN out -- and zero -- a player's
        # entire score.
        ranked = tes[col].rank(pct=True)
        ranked = ranked if higher_is_better else (1 - ranked)
        return ranked.fillna(0.5)

    opportunity_bonus = tes.apply(
        lambda row: (1.0 if row.get("injury_opportunity") else 0.0)
        + (1.0 if row.get("team_new_offensive_coordinator") else 0.0),
        axis=1,
    ).clip(upper=1.0)

    tes["te_score"] = 100 * (
        TE_SIGNAL_WEIGHTS["target_share"] * pct("target_share")
        + TE_SIGNAL_WEIGHTS["red_zone_share"] * pct("red_zone_share")
        + TE_SIGNAL_WEIGHTS["te_snap_share"] * pct("te_snap_share")
        + TE_SIGNAL_WEIGHTS["team_pass_rate"] * pct("team_pass_rate")
        + TE_SIGNAL_WEIGHTS["team_pass_epa"] * pct("team_pass_epa")
        + TE_SIGNAL_WEIGHTS["ngs_separation"] * pct("ngs_separation")
        + TE_SIGNAL_WEIGHTS["opportunity_bonus"] * opportunity_bonus
    )

    def signals(row: pd.Series) -> list:
        found = []
        if pd.notna(row.get("target_share")) and row["target_share"] >= tes["target_share"].quantile(0.75):
            found.append(f"Top-quartile target share ({row['target_share']:.0%})")
        if pd.notna(row.get("red_zone_share")) and row["red_zone_share"] >= tes["red_zone_share"].quantile(0.75):
            found.append(f"Top-quartile red-zone share ({row['red_zone_share']:.0%})")
        if pd.notna(row.get("te_snap_share")) and row["te_snap_share"] >= 0.7:
            found.append(f"True receiving-role snap share ({row['te_snap_share']:.0%})")
        if pd.notna(row.get("ngs_separation")) and row["ngs_separation"] >= tes["ngs_separation"].quantile(0.75):
            found.append(f"Top-quartile route-running separation ({row['ngs_separation']:.1f} yds)")
        if row.get("injury_opportunity"):
            found.append(f"Opportunity: {row.get('injury_opportunity_ahead_player')} is {row.get('injury_opportunity_ahead_status')}")
        if row.get("team_new_offensive_coordinator"):
            found.append(f"New offensive coordinator: {row.get('team_offensive_coordinator_name')}")
        return found

    tes["te_signals"] = tes.apply(signals, axis=1)
    candidates = tes[tes["te_score"] >= min_score]
    return candidates.sort_values("te_score", ascending=False).reset_index(drop=True)


# --- Priority Board ----------------------------------------------------
# Blends every other calculator in this module into one cross-position
# rank -- for the actual question a waiver claim / bench spot forces:
# "of these different positions all competing for the same roster spot,
# who do I actually prioritize this week?" League Optimizer, Breakout
# Radar, TE Difference-Makers, etc. each answer a narrower question well;
# this tab is the rollup, not a replacement for any of them.
#
# All four components are percentile-ranked to 0-1 before blending, so
# wildly different raw scales (custom_value in points, breakout_score's
# small signal-count scale, te_score's 0-100) combine fairly. Weights
# aren't enforced to sum to 1 -- pass your own `weights` dict if you want
# a different balance, just know priority_score's 0-100 scale assumes they do.
PRIORITY_WEIGHTS: Dict[str, float] = {
    "production": 0.40,    # calculate_custom_value(), ranked WITHIN position (a QB's raw points
                            # are never compared to a WR's) -- "how good are they, right now"
    "opportunity": 0.35,   # find_breakout_signals()'s breakout_score, ranked pool-wide -- it's
                            # already a composite of injury/coaching/game-script/Sleeper/ownership
                            # signals, so it doubles here as "how much is about to change"
    "specialist": 0.15,    # whichever position-specific engine applies (TE/rookie/QB/WR) --
                            # a bonus signal, not a primary driver; neutral 0.5 if none apply
    "team_context": 0.10,  # the player's team's O-Line Power Rankings score, if supplied --
                            # context (a strong O-line raises the floor under an RB/QB), not a
                            # primary driver; stays neutral 0.5 if oline_rankings isn't passed in
}


def _specialist_frame(sub_df: pd.DataFrame, raw_col: str, label_fmt) -> pd.DataFrame:
    """One position-specialist engine's output (e.g. `find_te_difference_makers()`)
    reduced to `{player_id, specialist_pct, specialist_label}` -- `raw_col`
    percentile-ranked within that engine's own pool (already position-filtered
    by the engine itself), with a human-readable label carrying the raw value."""
    if sub_df.empty or "player_id" not in sub_df.columns:
        return pd.DataFrame(columns=["player_id", "specialist_pct", "specialist_label"])
    out = sub_df[["player_id", raw_col]].drop_duplicates("player_id").copy()
    out["specialist_pct"] = out[raw_col].rank(pct=True)
    out["specialist_label"] = out[raw_col].apply(label_fmt)
    return out[["player_id", "specialist_pct", "specialist_label"]]


def build_priority_board(
    players_df: pd.DataFrame,
    oline_rankings: Optional[pd.DataFrame] = None,
    team_change_report: Optional[pd.DataFrame] = None,
    weights: Optional[Dict[str, float]] = None,
) -> pd.DataFrame:
    """One cross-position ranked board blending every other calculator in
    this module -- see the section docstring above for the blend and why
    each component is percentile-ranked the way it is.

    `priority_signals` merges `find_breakout_signals()`'s `breakout_signals`
    with whichever specialist engine's label applied (if any) -- one place
    to see *why* a player ranked where they did, instead of checking nine
    tabs. `priority_cautions` is `find_breakout_signals()`'s `caution_flags`
    verbatim (QB sophomore-slump, high-wind forecast) plus, if
    `team_change_report` is supplied and this player is in it, that report's
    plain-language `context_notes` -- carried through as-is, not folded into
    the score, matching `api/team_change_analytics.py`'s own explicit design
    choice not to force a team change into a single "helps/hurts" number.

    Args:
        players_df: Player records, enriched via `api/nfl_enrichment.py`
            for best results -- most components below need it.
        oline_rankings: Optional output of `api/oline_analytics.py`'s
            `build_oline_power_rankings()` (needs `team` and `oline_score`
            columns). This function takes no network dependency itself, by
            design -- the caller (`ui/dashboard.py`) fetches this
            separately and passes it in. Skipped (neutral 0.5) if omitted.
        team_change_report: Optional output of
            `api/team_change_analytics.py`'s `build_team_change_report()`
            (needs a `yahoo_id` column -- a real ID crosswalk, not name
            matching). Skipped entirely if omitted.
        weights: Optional override for `PRIORITY_WEIGHTS`.

    Returns:
        Copy of `players_df` with `priority_score` (float, roughly 0-100),
        `priority_signals` (list[str]), and `priority_cautions` (list[str])
        columns, sorted descending.
    """
    w = weights or PRIORITY_WEIGHTS
    df = players_df.copy()

    if df.empty:
        # `df.apply(func, axis=1)` on a zero-row DataFrame can't infer the
        # function's return shape (there's no row to actually call it on),
        # and raises when the result is assigned back to a single column --
        # so every column this function adds is filled in explicitly here
        # instead of falling through into the merges/applies below.
        for col in ("custom_value", "production_pct", "breakout_score", "opportunity_pct",
                    "specialist_pct", "team_context_pct", "priority_score"):
            df[col] = pd.Series(dtype=float)
        for col in ("breakout_signals", "caution_flags", "priority_signals", "priority_cautions"):
            df[col] = pd.Series(dtype=object)
        return df

    # Every merge below is keyed on player_id -- a duplicate would fan out on
    # EVERY subsequent merge (multiplicatively), silently corrupting the
    # whole board with meaningless duplicate rows. Guard against it here
    # rather than trusting every caller's input is already unique.
    if df["player_id"].duplicated().any():
        dupe_count = int(df["player_id"].duplicated().sum())
        logger.warning(
            "build_priority_board(): players_df has %d duplicate player_id row(s) -- "
            "keeping the first occurrence of each.",
            dupe_count,
        )
        df = df.drop_duplicates(subset=["player_id"], keep="first").reset_index(drop=True)

    # --- production: this league's real scoring, ranked within position ---
    value_df = calculate_custom_value(df)[["player_id", "custom_value"]].drop_duplicates("player_id")
    df = df.merge(value_df, on="player_id", how="left")
    df["production_pct"] = df.groupby("display_position")["custom_value"].rank(pct=True).fillna(0.5)

    # --- opportunity: Breakout Radar's composite, ranked pool-wide ---
    breakout_df = find_breakout_signals(df, min_score=float("-inf"))[
        ["player_id", "breakout_score", "breakout_signals", "caution_flags"]
    ].drop_duplicates("player_id")
    df = df.merge(breakout_df, on="player_id", how="left")
    df["breakout_score"] = df["breakout_score"].fillna(0.0)
    df["breakout_signals"] = df["breakout_signals"].apply(lambda s: s if isinstance(s, list) else [])
    df["caution_flags"] = df["caution_flags"].apply(lambda s: s if isinstance(s, list) else [])
    df["opportunity_pct"] = df["breakout_score"].rank(pct=True).fillna(0.5)

    # --- specialist: whichever position-specific engine applies (a bonus,
    # not a driver) -- a player could clear more than one (e.g. a rookie WR
    # who also clears the WR3 floor threshold); keep whichever percentile is
    # higher, since this is a bonus signal and the better-fitting engine
    # should win, not the first one computed.
    specialist_frames = []
    te_pool = df[df["display_position"] == "TE"]
    if len(te_pool) >= MIN_TE_GROUP_FOR_PERCENTILE:
        te_df = find_te_difference_makers(df, min_score=float("-inf"))
        specialist_frames.append(
            _specialist_frame(te_df, "te_score", lambda v: f"TE difference-maker score: {v:.0f}/100")
        )
    specialist_frames.append(
        _specialist_frame(
            apply_rookie_bump(df), "back_half_points_bumped",
            lambda v: f"Rookie back-half upside: {v:.0f} pts",
        )
    )
    specialist_frames.append(
        _specialist_frame(calculate_qb_floor(df), "qb_floor_score", lambda v: f"Rushing floor score: {v:.0f}")
    )
    specialist_frames.append(
        _specialist_frame(evaluate_wr_scarcity(df), "wr_floor_score", lambda v: f"WR3 floor score: {v:.0f}")
    )
    specialist = pd.concat(specialist_frames, ignore_index=True)
    if not specialist.empty:
        specialist = specialist.sort_values("specialist_pct", ascending=False).drop_duplicates(
            "player_id", keep="first"
        )
    df = df.merge(specialist, on="player_id", how="left")
    df["specialist_pct"] = df["specialist_pct"].fillna(0.5)

    # --- team context: O-Line Power Rankings, if supplied. Matched on
    # editorial_team_abbr (Yahoo's team code) vs. oline_rankings' team column
    # (already normalized in api/oline_analytics.py) -- these should agree
    # for all 32 teams, but isn't independently re-verified here.
    df["team_context_pct"] = 0.5
    if oline_rankings is not None and not oline_rankings.empty:
        oline_pct = oline_rankings[["team", "oline_score"]].drop_duplicates("team").copy()
        oline_pct["team_context_pct"] = oline_pct["oline_score"].rank(pct=True)
        df = df.merge(
            oline_pct[["team", "team_context_pct"]].rename(columns={"team_context_pct": "_oline_pct"}),
            left_on="editorial_team_abbr", right_on="team", how="left",
        ).drop(columns=["team"])
        df["team_context_pct"] = df["_oline_pct"].fillna(0.5)
        df = df.drop(columns=["_oline_pct"])

    df["priority_score"] = 100 * (
        w["production"] * df["production_pct"]
        + w["opportunity"] * df["opportunity_pct"]
        + w["specialist"] * df["specialist_pct"]
        + w["team_context"] * df["team_context_pct"]
    )

    # --- context-only: team-change notes, never folded into the score ---
    change_notes_by_yahoo_id: Dict[str, str] = {}
    if team_change_report is not None and not team_change_report.empty and "yahoo_id" in team_change_report.columns:
        movers = team_change_report.dropna(subset=["yahoo_id"])
        change_notes_by_yahoo_id = dict(zip(movers["yahoo_id"].astype(str), movers["context_notes"]))

    def merge_signals(row: pd.Series) -> list:
        signals = list(row["breakout_signals"])
        if pd.notna(row.get("specialist_label")):
            signals.append(row["specialist_label"])
        return signals

    def merge_cautions(row: pd.Series) -> list:
        cautions = list(row["caution_flags"])
        note = change_notes_by_yahoo_id.get(str(row["player_id"]))
        if note:
            cautions.append(f"Changed teams ({row.get('editorial_team_abbr')}): {note}")
        return cautions

    df["priority_signals"] = df.apply(merge_signals, axis=1)
    df["priority_cautions"] = df.apply(merge_cautions, axis=1)

    return df.sort_values("priority_score", ascending=False).reset_index(drop=True)


# --- Free Agent Suggestions & Trade Finder -----------------------------
# Everything below needs full-LEAGUE roster data (every team, not just
# free agents) with a `team_id` column and a `priority_score` column
# (see `build_priority_board()`) -- these are the two features that only
# become real once Yahoo API access is live and `get_rosters()` can pull
# every team, not just the waiver wire. Until then, `ui/dashboard.py`'s
# `build_mock_league_rosters()` exercises this against a procedurally
# generated mock league.
#
# The core idea, standard fantasy-analysis "replacement level": a
# player's value isn't their raw score, it's how far above the point
# where you could just plug in whoever's next-available at that position
# league-wide. A team has real tradeable SURPLUS at a position when it
# has more startable (at-or-above-replacement-level) players there than
# it has starting slots to fill; it has a real NEED when the opposite is
# true.
SKILL_POSITIONS = ("QB", "RB", "WR", "TE")
TRADE_FAIRNESS_TOLERANCE = 0.35  # max relative priority_score gap (of the larger side) to still call "fair enough to propose"


def _position_demand(roster_requirements: Dict[str, int], num_teams: int = 1) -> Dict[str, float]:
    """How many starting slots exist at each position across `num_teams`
    teams (`num_teams=1` for one team's own starting slots). FLEX (W/R/T)
    demand is split evenly across RB and WR, not TE -- TE rarely starts in
    FLEX in practice, and this app already treats TE as its own scarce,
    difference-maker position (see TE Difference-Maker Finder). A
    reasonable approximation, not an exact simulation of real lineup
    decisions.
    """
    flex = roster_requirements.get("FLEX", 0)
    return {
        "QB": num_teams * roster_requirements.get("QB", 0),
        "RB": num_teams * (roster_requirements.get("RB", 0) + flex / 2),
        "WR": num_teams * (roster_requirements.get("WR", 0) + flex / 2),
        "TE": num_teams * roster_requirements.get("TE", 0),
    }


def compute_replacement_level(
    rostered_players_df: pd.DataFrame,
    roster_requirements: Optional[Dict[str, int]] = None,
    num_teams: int = 10,
) -> Dict[str, float]:
    """The `priority_score` of the Nth-best player at each position,
    where N is how many league-wide starting slots exist there (see
    `_position_demand()`) -- computed across the WHOLE league's rostered
    players, not just one team's roster or the free-agent pool alone.
    """
    requirements = roster_requirements or DEFAULT_ROSTER_REQUIREMENTS
    demand = _position_demand(requirements, num_teams)

    levels = {}
    for position, n in demand.items():
        pool = rostered_players_df[rostered_players_df["display_position"] == position]
        if pool.empty or n <= 0:
            levels[position] = 0.0
            continue
        sorted_scores = pool["priority_score"].sort_values(ascending=False).reset_index(drop=True)
        idx = max(0, min(int(round(n)) - 1, len(sorted_scores) - 1))
        levels[position] = float(sorted_scores.iloc[idx])
    return levels


def assess_team_needs(
    team_id: str,
    rostered_players_df: pd.DataFrame,
    replacement_levels: Dict[str, float],
    roster_requirements: Optional[Dict[str, int]] = None,
) -> pd.DataFrame:
    """Per-position surplus/need for one team: how many of its players at
    a position clear replacement level (real starting-caliber depth)
    minus how many starting slots it actually has to fill there --
    positive `surplus` is real tradeable depth, negative is a real hole.
    """
    requirements = roster_requirements or DEFAULT_ROSTER_REQUIREMENTS
    own_slots = _position_demand(requirements, num_teams=1)
    team = rostered_players_df[rostered_players_df["team_id"] == team_id]

    rows = []
    for position in SKILL_POSITIONS:
        pos_players = team[team["display_position"] == position]
        level = replacement_levels.get(position, 0.0)
        startable = pos_players[pos_players["priority_score"] >= level]
        slots_needed = own_slots.get(position, 0)
        rows.append({
            "position": position,
            "startable_count": len(startable),
            "slots_needed": slots_needed,
            "surplus": len(startable) - slots_needed,
            "best_value": pos_players["priority_score"].max() if not pos_players.empty else None,
            "worst_starter_value": startable["priority_score"].min() if not startable.empty else None,
        })
    return pd.DataFrame(rows)


def _player_display_name(row: pd.Series) -> str:
    name = row.get("name")
    return name.get("full") if isinstance(name, dict) else name


def find_free_agent_upgrades(
    my_roster_df: pd.DataFrame,
    free_agents_df: pd.DataFrame,
    min_priority_score_gain: float = 10.0,
) -> pd.DataFrame:
    """For each skill position on your roster, compares your weakest
    rostered player there against the best available free agent --
    surfaces a concrete "drop X, add Y" suggestion whenever a free agent
    clearly beats your worst player at that position (by at least
    `min_priority_score_gain` `priority_score` points), rather than just
    listing free agents ranked in a vacuum with no connection to your
    actual roster.

    Both inputs need a `priority_score` column (see `build_priority_board()`).

    Returns:
        DataFrame with `position`, `drop_player`/`drop_priority_score`,
        `add_player`/`add_priority_score`, `priority_score_gain` columns,
        sorted descending by gain. Empty if nothing clears the threshold.
    """
    columns = [
        "position", "drop_player", "drop_priority_score",
        "add_player", "add_priority_score", "priority_score_gain",
    ]
    suggestions = []
    for position in SKILL_POSITIONS:
        mine = my_roster_df[my_roster_df["display_position"] == position]
        available = free_agents_df[free_agents_df["display_position"] == position]
        if mine.empty or available.empty:
            continue
        worst_mine = mine.sort_values("priority_score", ascending=True).iloc[0]
        best_fa = available.sort_values("priority_score", ascending=False).iloc[0]
        gain = best_fa["priority_score"] - worst_mine["priority_score"]
        if gain >= min_priority_score_gain:
            suggestions.append({
                "position": position,
                "drop_player": _player_display_name(worst_mine),
                "drop_priority_score": worst_mine["priority_score"],
                "add_player": _player_display_name(best_fa),
                "add_priority_score": best_fa["priority_score"],
                "priority_score_gain": gain,
            })

    if not suggestions:
        return pd.DataFrame(columns=columns)
    return pd.DataFrame(suggestions).sort_values("priority_score_gain", ascending=False).reset_index(drop=True)


def _pick_tradeable_player(
    rostered_players_df: pd.DataFrame, team_id: str, position: str, replacement_level: float
) -> Optional[pd.Series]:
    """The most giveable startable player a team has at `position`: the
    LOWEST `priority_score` player that still clears replacement level --
    a real asset to whoever receives it, but the team's most spare one at
    that position, not their best starter."""
    pool = rostered_players_df[
        (rostered_players_df["team_id"] == team_id)
        & (rostered_players_df["display_position"] == position)
        & (rostered_players_df["priority_score"] >= replacement_level)
    ]
    if pool.empty:
        return None
    return pool.sort_values("priority_score", ascending=True).iloc[0]


def find_trade_candidates(
    my_team_id: str,
    rostered_players_df: pd.DataFrame,
    roster_requirements: Optional[Dict[str, int]] = None,
    num_teams: int = 10,
    fairness_tolerance: float = TRADE_FAIRNESS_TOLERANCE,
) -> pd.DataFrame:
    """Proactively scans every other team in the league for a genuine
    two-way fit with your team: a position where you have surplus
    (tradeable depth) and they have a need, paired with a position where
    they have surplus and you have a need -- a real "we'd both actually
    want this" trade shape, not just "who has a good player."

    Known limits -- read before trusting a suggestion: this is a
    value-and-need heuristic, not a negotiation. It has no idea whether a
    manager actually wants to trade, their own roster philosophy, keeper/
    dynasty considerations, or plain stubbornness. Treat every row as a
    conversation starter to evaluate yourself, never a trade either side
    is guaranteed to accept.

    Args:
        my_team_id: Your `team_id` in `rostered_players_df`.
        rostered_players_df: Every team's roster, league-wide, with
            `team_id` and `priority_score` columns (see `build_priority_board()`).
        num_teams: Used to compute replacement level (see
            `compute_replacement_level()`) -- should match your league's
            real team count.
        fairness_tolerance: Max relative gap between the two traded
            players' `priority_score` (as a fraction of the larger one)
            to still surface the trade -- wildly lopsided "trades" aren't
            realistic proposals and are filtered out, not just flagged.

    Returns:
        DataFrame with `other_team_id`, `you_give`/`you_give_position`/
        `you_give_value`, `you_get`/`you_get_position`/`you_get_value`,
        `fairness_gap_pct`, and `fit_score` columns, sorted descending by
        fit. Empty if no genuine two-way fit exists anywhere in the league.
    """
    columns = [
        "other_team_id", "you_give", "you_give_position", "you_give_value",
        "you_get", "you_get_position", "you_get_value", "fairness_gap_pct", "fit_score",
    ]
    requirements = roster_requirements or DEFAULT_ROSTER_REQUIREMENTS
    replacement_levels = compute_replacement_level(rostered_players_df, requirements, num_teams)

    other_team_ids = [t for t in rostered_players_df["team_id"].unique() if t != my_team_id]
    my_needs = assess_team_needs(my_team_id, rostered_players_df, replacement_levels, requirements).set_index("position")

    candidates = []
    for other_team_id in other_team_ids:
        other_needs = assess_team_needs(
            other_team_id, rostered_players_df, replacement_levels, requirements
        ).set_index("position")

        for give_position in SKILL_POSITIONS:
            for get_position in SKILL_POSITIONS:
                if give_position == get_position:
                    continue  # never a real "fill a hole" trade if it's the same position both ways

                my_surplus = my_needs.loc[give_position, "surplus"]
                their_need = other_needs.loc[give_position, "surplus"]
                their_surplus = other_needs.loc[get_position, "surplus"]
                my_need = my_needs.loc[get_position, "surplus"]

                if my_surplus <= 0 or their_need >= 0 or their_surplus <= 0 or my_need >= 0:
                    continue  # not a genuine two-way fit -- one side gains nothing real

                my_player = _pick_tradeable_player(
                    rostered_players_df, my_team_id, give_position, replacement_levels[give_position]
                )
                their_player = _pick_tradeable_player(
                    rostered_players_df, other_team_id, get_position, replacement_levels[get_position]
                )
                if my_player is None or their_player is None:
                    continue

                my_value, their_value = my_player["priority_score"], their_player["priority_score"]
                gap = abs(my_value - their_value) / max(my_value, their_value, 1e-6)
                if gap > fairness_tolerance:
                    continue

                candidates.append({
                    "other_team_id": other_team_id,
                    "you_give": _player_display_name(my_player),
                    "you_give_position": give_position,
                    "you_give_value": my_value,
                    "you_get": _player_display_name(their_player),
                    "you_get_position": get_position,
                    "you_get_value": their_value,
                    "fairness_gap_pct": gap,
                    "fit_score": min(my_surplus, -their_need) + min(their_surplus, -my_need),
                })

    if not candidates:
        return pd.DataFrame(columns=columns)
    return pd.DataFrame(candidates).sort_values("fit_score", ascending=False).reset_index(drop=True)
