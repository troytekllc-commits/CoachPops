"""Tracks skill-position players (QB/RB/WR/TE) who changed teams between
two seasons -- via trade, free agency, or release-and-sign -- and lays out
the context factors that matter for judging whether the move helps or
hurts their fantasy value: team pass rate and passing efficiency (offense
pace + a QB/scheme quality proxy), O-line strength (reused from
``api/oline_analytics.py``), how crowded the new team's WR/TE target
competition already is, and whether the new team has a new head coach or
offensive coordinator (reused from ``api/nfl_enrichment.py``).

Why this doesn't compute a single "value went up/down" score
----------------------------------------------------------------
Unlike this project's other calculators, how much a team change helps or
hurts is genuinely position- and context-dependent in a way a single
number would flatten: a run-first team with an elite O-line is great news
for an incoming RB and mediocre news for an incoming WR, in the very same
offseason move. So ``build_team_change_report()`` surfaces the delta on
each factor and a plain-language ``context_notes`` string weighted by the
player's own position, rather than forcing everything into one score you'd
have to second-guess anyway.

Known gaps
-----------
- "Target competition" only looks at the new team's WR/TE target share
  (the classic "crowded receiver room" concern) -- it doesn't model RB
  carry-share competition or TE-specific blocking-vs-receiving role
  splits.
- QB quality is proxied by team-level passing EPA/play, which conflates
  QB play with scheme and supporting cast -- it's a reasonable team-level
  signal, not an isolated QB grade.
- A player who changed teams AFTER a season already started (in-season
  trade) will show that season's stats blended across both teams in
  nfl_data_py's weekly data; this module compares team-season contexts,
  not exact split-team stat lines.
"""

from __future__ import annotations

from typing import Optional

import pandas as pd

from api.nfl_enrichment import (
    _normalize_team_abbr,
    build_coaching_change_lookup,
    compute_team_offense_context,
    load_seasonal_rosters,
    load_weekly_data,
)
from api.oline_analytics import build_oline_power_rankings

FANTASY_RELEVANT_POSITIONS = ("QB", "RB", "WR", "TE")
TARGET_COMPETITION_POSITIONS = ("WR", "TE")


def compute_target_competition(season: int) -> pd.DataFrame:
    """Per-team total target share currently claimed by WR/TE pass-catchers
    (summed average target_share per player) -- higher means a more
    crowded target competition for anyone new arriving at that position."""
    weekly = load_weekly_data(season)
    catchers = weekly[weekly["position"].isin(TARGET_COMPETITION_POSITIONS)].copy()
    catchers["team"] = catchers["recent_team"].apply(_normalize_team_abbr)

    per_player = catchers.groupby(["team", "player_id"])["target_share"].mean()
    return per_player.groupby("team").sum().rename("wr_te_target_share_sum").reset_index()


def find_team_changes(season: int) -> pd.DataFrame:
    """Skill-position players whose team this season differs from last
    season's, per ``import_seasonal_rosters()``."""
    current = load_seasonal_rosters(season)
    previous = load_seasonal_rosters(season - 1)

    current = current[current["position"].isin(FANTASY_RELEVANT_POSITIONS)]
    merged = current.merge(
        previous[["player_id", "team"]], on="player_id", suffixes=("_new", "_old")
    )
    merged["team_new"] = merged["team_new"].apply(_normalize_team_abbr)
    merged["team_old"] = merged["team_old"].apply(_normalize_team_abbr)
    changed = merged[merged["team_new"] != merged["team_old"]]

    return changed[["player_id", "player_name", "position", "team_old", "team_new"]].reset_index(drop=True)


def _context_notes(row: pd.Series) -> str:
    """Plain-language summary of the move's context, weighted by position."""
    notes = []
    pass_rate_delta = row.get("pass_rate_new") - row.get("pass_rate_old") if pd.notna(row.get("pass_rate_new")) and pd.notna(row.get("pass_rate_old")) else None
    oline_delta = row.get("oline_score_new") - row.get("oline_score_old") if pd.notna(row.get("oline_score_new")) and pd.notna(row.get("oline_score_old")) else None
    position = row.get("position")

    if position in ("WR", "TE"):
        if pass_rate_delta is not None:
            notes.append(
                f"{'More' if pass_rate_delta > 0 else 'Less'} pass-heavy offense "
                f"({pass_rate_delta:+.0%} pass rate)"
            )
        competition = row.get("wr_te_target_share_sum")
        if pd.notna(competition):
            crowding = "crowded" if competition >= 2.2 else ("open" if competition <= 1.7 else "moderate")
            notes.append(f"{crowding} WR/TE target competition at new team ({competition:.2f} combined share)")
    if position in ("RB", "QB"):
        if oline_delta is not None:
            notes.append(f"O-line {'upgrade' if oline_delta > 0 else 'downgrade'} ({oline_delta:+.0f} pts)")
    if position == "QB" and pass_rate_delta is not None:
        notes.append(f"{'More' if pass_rate_delta > 0 else 'Less'} pass volume ({pass_rate_delta:+.0%})")

    if row.get("new_head_coach"):
        notes.append("new head coach at new team")
    if row.get("new_offensive_coordinator"):
        notes.append("new offensive coordinator at new team")

    return "; ".join(notes) if notes else "Insufficient data to assess context"


def build_team_change_report(season: int) -> pd.DataFrame:
    """Full team-change report: every skill-position mover, their old vs.
    new team's offensive context, and a plain-language summary of whether
    the move looks favorable, unfavorable, or mixed for their position.
    """
    movers = find_team_changes(season)
    if movers.empty:
        return movers

    offense_new = compute_team_offense_context(season).add_suffix("_new").rename(columns={"team_new": "team"})
    offense_old = compute_team_offense_context(season - 1).add_suffix("_old").rename(columns={"team_old": "team"})
    oline_new = build_oline_power_rankings(season)[["team", "oline_score"]].rename(columns={"oline_score": "oline_score_new"})
    oline_old = build_oline_power_rankings(season - 1)[["team", "oline_score"]].rename(columns={"oline_score": "oline_score_old"})
    target_competition = compute_target_competition(season)
    coaching = build_coaching_change_lookup(season)

    df = movers.merge(offense_new, left_on="team_new", right_on="team", how="left").drop(columns=["team"])
    df = df.merge(offense_old, left_on="team_old", right_on="team", how="left").drop(columns=["team"])
    df = df.merge(oline_new, left_on="team_new", right_on="team", how="left").drop(columns=["team"])
    df = df.merge(oline_old, left_on="team_old", right_on="team", how="left").drop(columns=["team"])
    df = df.merge(target_competition, left_on="team_new", right_on="team", how="left").drop(columns=["team"])

    df["new_head_coach"] = df["team_new"].map(lambda t: coaching.get(t, {}).get("new_head_coach", False))
    df["new_offensive_coordinator"] = df["team_new"].map(
        lambda t: coaching.get(t, {}).get("new_offensive_coordinator", False)
    )

    df["context_notes"] = df.apply(_context_notes, axis=1)
    return df
