"""Team-level offensive line power rankings, year-over-year trend, and
"what changed" context -- built entirely from nfl_data_py. No Yahoo data
involved, so this works today regardless of Yahoo API access.

How the composite score is built
-----------------------------------
Four inputs, each converted to a 0-1 percentile rank *across the league*
before averaging (so no single metric's raw scale dominates):

- ``pass_protection_pct``: average of (1 - sack rate) and (1 - QB hit
  rate), both per dropback, from play-by-play data.
- ``run_blocking_pct``: average of yards-per-carry and (1 - stuff rate)
  (share of rushes gaining 0 or fewer yards), from play-by-play data.
- ``continuity_pct``: share of the team's total O-line (C/G/T) snaps
  commanded by that team's top-5 snap leaders at those positions -- a
  stable, healthy starting five is a real, repeatable predictor of
  sustained O-line performance, independent of raw talent.
- ``draft_investment_pct``: trailing 4-draft-class capital spent on
  O-line positions (C/G/OT/OL), weighted toward earlier picks.

``oline_score`` is the mean of those four percentiles, scaled to 0-100.
``build_oline_rankings_with_trend()`` adds the prior season's rank/score,
the season-over-season delta, how many of the current top-5 snap leaders
are new faces vs. last season (roster turnover), and team coaching-change
flags reused from ``api/nfl_enrichment.py``.

Known gaps
-----------
This is missing true pass-block-win-rate / PFF-style per-lineman grading
-- that's proprietary and not available via nfl_data_py. Sack rate and
QB hits are a real but incomplete proxy: a scrambling, extend-the-play
QB (or a run-heavy offense with few dropbacks) can distort these
independent of actual blocking quality. Treat ``oline_score`` as
directional -- useful for spotting real trends and outliers -- not as an
authoritative grade.

Forward-looking use: there's no play-by-play data for a season that
hasn't happened yet, so "predicting" the *upcoming* season's strongest
lines really means: take last season's rankings, then weight up teams
with high continuity (same core five returning), meaningful recent draft
investment, and coaching stability -- and weight down teams with heavy
turnover or a new offensive coordinator/scheme. ``build_oline_rankings_with_trend()``
surfaces exactly those signals rather than fabricating a single fake
"projected" number for a season with zero actual snaps played.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Optional

import pandas as pd

from api.nfl_enrichment import (
    _normalize_team_abbr,
    build_coaching_change_lookup,
    load_draft_picks,
    load_pbp,
)

OLINE_POSITIONS_SNAP = ("C", "G", "T")
OLINE_POSITIONS_DRAFT = ("C", "G", "OT", "OL")
DRAFT_TRAILING_YEARS = 4
STUFF_RATE_YARDS_THRESHOLD = 0  # a rush gaining <= this many yards counts as "stuffed"
TOP_N_OLINE_STARTERS = 5


@lru_cache(maxsize=8)
def load_snap_counts(season: int) -> pd.DataFrame:
    import nfl_data_py as nfl

    return nfl.import_snap_counts([season])


def compute_pass_protection(season: int) -> pd.DataFrame:
    """Per-team sack rate and QB hit rate, both per dropback."""
    pbp = load_pbp(season)
    dropback_col = "qb_dropback" if "qb_dropback" in pbp.columns else "pass_attempt"
    plays = pbp[pbp[dropback_col] == 1].copy()
    plays["team"] = plays["posteam"].apply(_normalize_team_abbr)

    grouped = (
        plays.groupby("team")
        .agg(dropbacks=(dropback_col, "sum"), sacks=("sack", "sum"), qb_hits=("qb_hit", "sum"))
        .reset_index()
    )
    grouped["sack_rate"] = grouped["sacks"] / grouped["dropbacks"]
    grouped["qb_hit_rate"] = grouped["qb_hits"] / grouped["dropbacks"]
    return grouped[["team", "dropbacks", "sack_rate", "qb_hit_rate"]]


def compute_run_blocking(season: int) -> pd.DataFrame:
    """Per-team yards-per-carry and stuff rate (share of runs gaining
    STUFF_RATE_YARDS_THRESHOLD or fewer yards)."""
    pbp = load_pbp(season)
    runs = pbp[pbp["rush_attempt"] == 1].copy()
    runs["team"] = runs["posteam"].apply(_normalize_team_abbr)

    grouped = (
        runs.groupby("team")
        .agg(
            rush_attempts=("rush_attempt", "sum"),
            rush_yards=("yards_gained", "sum"),
            stuffed=("yards_gained", lambda y: (y <= STUFF_RATE_YARDS_THRESHOLD).sum()),
        )
        .reset_index()
    )
    grouped["yards_per_carry"] = grouped["rush_yards"] / grouped["rush_attempts"]
    grouped["stuff_rate"] = grouped["stuffed"] / grouped["rush_attempts"]
    return grouped[["team", "rush_attempts", "yards_per_carry", "stuff_rate"]]


def compute_oline_continuity(season: int) -> pd.DataFrame:
    """Share of a team's total O-line (C/G/T) snaps commanded by that
    team's top-5 snap leaders at those positions, plus who those 5 are
    (for year-over-year turnover comparison)."""
    snaps = load_snap_counts(season)
    oline = snaps[snaps["position"].isin(OLINE_POSITIONS_SNAP) & (snaps["game_type"] == "REG")].copy()
    oline["team"] = oline["team"].apply(_normalize_team_abbr)

    team_totals = oline.groupby("team")["offense_snaps"].sum()
    player_totals = (
        oline.groupby(["team", "pfr_player_id", "player"])["offense_snaps"].sum().reset_index()
    )

    rows = []
    for team, group in player_totals.groupby("team"):
        top5 = group.sort_values("offense_snaps", ascending=False).head(TOP_N_OLINE_STARTERS)
        team_total = team_totals.get(team, 0)
        continuity_share = (top5["offense_snaps"].sum() / team_total) if team_total else 0.0
        rows.append(
            {
                "team": team,
                "continuity_share": continuity_share,
                "top5_player_ids": tuple(top5["pfr_player_id"]),
                "top5_players": ", ".join(top5["player"]),
            }
        )
    return pd.DataFrame(rows)


def compute_draft_investment(current_season: int, trailing_years: int = DRAFT_TRAILING_YEARS) -> pd.DataFrame:
    """Sum of draft-value points spent on O-line positions (C/G/OT/OL)
    across the last `trailing_years` draft classes, weighted toward
    earlier picks (higher pick number = later = less value)."""
    frames = []
    for year in range(current_season - trailing_years + 1, current_season + 1):
        try:
            picks = load_draft_picks(year)
        except Exception:
            continue
        picks = picks[picks["position"].isin(OLINE_POSITIONS_DRAFT)].copy()
        if picks.empty:
            continue
        picks["value"] = (260 - picks["pick"]).clip(lower=1)
        picks["team"] = picks["team"].apply(_normalize_team_abbr)
        frames.append(picks[["team", "value"]])

    if not frames:
        return pd.DataFrame(columns=["team", "draft_investment"])

    combined = pd.concat(frames, ignore_index=True)
    return combined.groupby("team")["value"].sum().reset_index().rename(columns={"value": "draft_investment"})


def _percentile_rank(series: pd.Series, higher_is_better: bool = True) -> pd.Series:
    ranked = series.rank(pct=True)
    return ranked if higher_is_better else (1 - ranked)


def build_oline_power_rankings(season: int) -> pd.DataFrame:
    """One row per team, ranked by composite `oline_score` (0-100, higher
    is better). See module docstring for exactly what goes into it."""
    df = compute_pass_protection(season)
    df = df.merge(compute_run_blocking(season), on="team", how="outer")
    df = df.merge(compute_oline_continuity(season), on="team", how="outer")
    df = df.merge(compute_draft_investment(season), on="team", how="outer")

    df["pass_protection_pct"] = (
        _percentile_rank(df["sack_rate"], higher_is_better=False)
        + _percentile_rank(df["qb_hit_rate"], higher_is_better=False)
    ) / 2
    df["run_blocking_pct"] = (
        _percentile_rank(df["yards_per_carry"], higher_is_better=True)
        + _percentile_rank(df["stuff_rate"], higher_is_better=False)
    ) / 2
    df["continuity_pct"] = _percentile_rank(df["continuity_share"], higher_is_better=True)
    df["draft_investment_pct"] = _percentile_rank(df["draft_investment"], higher_is_better=True)

    df["oline_score"] = 100 * df[
        ["pass_protection_pct", "run_blocking_pct", "continuity_pct", "draft_investment_pct"]
    ].mean(axis=1)

    df = df.sort_values("oline_score", ascending=False).reset_index(drop=True)
    df["oline_rank"] = df.index + 1
    return df


def build_oline_rankings_with_trend(season: int) -> pd.DataFrame:
    """`build_oline_power_rankings(season)` plus year-over-year context:
    prior-season rank/score, the delta, how many of the current top-5
    O-line snap leaders are new vs. last season, and coaching-change
    flags (reused from api/nfl_enrichment.py)."""
    current = build_oline_power_rankings(season)

    try:
        previous = build_oline_power_rankings(season - 1)
    except Exception:
        previous = pd.DataFrame(columns=["team", "oline_rank", "oline_score", "top5_player_ids"])

    prev_by_team = previous.set_index("team") if not previous.empty else previous
    coaching = build_coaching_change_lookup(season)

    def compute_changes(row: pd.Series) -> pd.Series:
        team = row["team"]
        prev_row = prev_by_team.loc[team] if team in getattr(prev_by_team, "index", []) else None

        new_starters = None
        if (
            prev_row is not None
            and isinstance(row.get("top5_player_ids"), tuple)
            and isinstance(prev_row.get("top5_player_ids"), tuple)
        ):
            new_starters = len(set(row["top5_player_ids"]) - set(prev_row["top5_player_ids"]))

        coach_info = coaching.get(team, {})
        return pd.Series(
            {
                "prior_oline_rank": prev_row["oline_rank"] if prev_row is not None else None,
                "rank_change": (prev_row["oline_rank"] - row["oline_rank"]) if prev_row is not None else None,
                "score_change": (row["oline_score"] - prev_row["oline_score"]) if prev_row is not None else None,
                "new_starters_count": new_starters,
                "new_head_coach": coach_info.get("new_head_coach", False),
                "new_offensive_coordinator": coach_info.get("new_offensive_coordinator", False),
            }
        )

    changes = current.apply(compute_changes, axis=1)
    return pd.concat([current, changes], axis=1)
