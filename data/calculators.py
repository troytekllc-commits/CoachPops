"""Custom league analytics engines.

Six pandas-based calculators tuned for a 3-WR / 6-bench / 2-IR league
format. Every function takes (and returns) a DataFrame of player records
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
}

RISING_OWNERSHIP_DELTA_THRESHOLD = 3.0  # percentage points, week over week
MEANINGFUL_OWNERSHIP_THRESHOLD = 15.0  # percent owned across all of Yahoo
UNDERSOLD_ROOKIE_ROUND_THRESHOLD = 4  # Day 3 (round 4+) or undrafted
UNDERSOLD_ROOKIE_TARGET_SHARE = 0.12
UNDERSOLD_ROOKIE_RED_ZONE_SHARE = 0.10
EARNED_RED_ZONE_SHARE_THRESHOLD = 0.20
EFFICIENCY_GAP_THRESHOLD = 0.30  # percentile-rank gap within position
MIN_POSITION_GROUP_FOR_EFFICIENCY_CHECK = 3


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
        if len(group) < MIN_POSITION_GROUP_FOR_EFFICIENCY_CHECK or group["target_share"].isna().all():
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
    own league does, or a Day 3/UDFA rookie already earning more volume
    than their draft slot implied.

    Also raises (but doesn't score against) a QB "sophomore slump" caution:
    rookie QBs who finished top-15 in fantasy PPG have historically
    *declined* more often than not in Year 2 (roughly 11 of the last 14) --
    a defense's film advantage apparently outweighing the QB's own growth.
    See `api/nfl_enrichment.py`'s `build_qb_year2_regression_flags()`.

    Known gap: a real signal from the same research this is built on --
    whether a rookie/sophomore's positional competition departed or
    arrived (who they're now blocking or being blocked by) -- isn't
    implemented. It needs careful two-season role-matching to avoid noise,
    so it's documented as a future enhancement rather than shipped
    half-reliable; see `api/nfl_enrichment.py`'s module docstring.

    Requires the enrichment fields from `api/nfl_enrichment.py`
    (`injury_opportunity`, `team_new_offensive_coordinator`/`team_new_head_coach`,
    `target_share`, `red_zone_share`, `is_rookie`/`draft_capital`,
    `qb_year2_regression_caution`) plus Yahoo's own `percent_owned`/
    `percent_owned_delta`. A signal simply doesn't fire for players missing
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

        if row.get("qb_year2_regression_caution"):
            cautions.append(
                f"Sophomore-slump caution: top-15 QB PPG as a rookie ({row.get('qb_year1_ppg')} pts/gm) -- "
                "historically ~79% of that group declines in Year 2"
            )

        scores.append(score)
        signals_col.append(signals)
        cautions_col.append(cautions)

    df["breakout_score"] = scores
    df["breakout_signals"] = signals_col
    df["caution_flags"] = cautions_col

    candidates = df[df["breakout_score"] >= min_score]
    return candidates.sort_values("breakout_score", ascending=False).reset_index(drop=True)
