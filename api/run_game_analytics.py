"""Team-level run-game outlook (which offenses look built to run the ball
next season) and RB workload/"bell cow" identification -- both built
entirely from nfl_data_py. No Yahoo data involved, so this works today
regardless of Yahoo API access, same as O-Line Power Rankings/Team
Change Impact.

Two real, separate questions this module answers
--------------------------------------------------
1. **Which teams project as the strongest running teams?**
   `build_run_game_outlook()` starts from last season's real run-game
   volume (rush rate: rushes as a share of rush+dropback plays) and
   efficiency (yards per carry, rushing EPA/play), then adjusts using the
   same forward-looking philosophy as `api/oline_analytics.py`: real
   O-line strength/continuity (reused directly from that module), real
   coaching stability (reused from `api/nfl_enrichment.py` -- a new head
   coach or offensive coordinator is a real scheme-change risk), and
   whether last season's lead back is still on the roster (a retained
   "bell cow" is a strong continuity signal; a departed one with no clear
   heir is a real red flag for the run game's floor).

   Like `oline_score`, `run_outlook_score` is a blend of real percentile
   ranks, not a fabricated projection for a season with zero snaps played
   yet -- treat it as directional, and read `context_notes` for why a
   team landed where it did.

2. **Which RBs are the most heavily used, and who's a true "bell cow"?**
   `build_rb_workload_report()` computes each RB's real season workload
   -- carries, targets, receptions, receiving yards, total touches -- AND
   their *share* of their own team's RB-room usage on three independent
   axes (carry share, touch share, snap share), since raw counts alone
   don't distinguish a bell cow on a run-heavy team from a committee back
   getting decent volume on a pass-heavy one. `bell_cow_score` averages
   those three shares; `usage_tier` buckets it into a plain-language read
   (Bell Cow / Lead Back / Committee Lead / Depth-Committee).

Known gaps
-----------
- Rush rate/efficiency, like O-line grading, can't be computed for a
  season that hasn't been played -- see `build_run_game_outlook()`'s
  docstring for exactly what "prediction" means here (real last-season
  data + real, explainable adjustments, not a synthetic season sim).
- "Notable RB arrival" (an incoming free agent/trade/rookie who could
  replace a departed bell cow) is a real signal reused from
  `api/team_change_analytics.py`'s team-change detection, but it's a
  listing, not a graded projection -- an incoming rookie with zero NFL
  touches has no real workload data yet to judge them by.
- Snap share requires `import_snap_counts()`, which (like the rest of
  this project's snap-based signals) only goes back a handful of
  seasons; teams/players missing from it fall back to carry/touch share
  alone (a `bell_cow_score` from fewer inputs, not a penalized one).
"""

from __future__ import annotations

from typing import Optional

import pandas as pd

from api.nfl_enrichment import (
    _normalize_team_abbr,
    build_coaching_change_lookup,
    load_pbp,
    load_seasonal_rosters,
    load_snap_counts,
)
from api.oline_analytics import build_oline_rankings_with_trend
from api.team_change_analytics import find_team_changes

RB_POSITION = "RB"

# bell_cow_score thresholds (mean of carry/touch/snap share, each 0-1)
BELL_COW_THRESHOLD = 0.70
LEAD_BACK_THRESHOLD = 0.50
COMMITTEE_LEAD_THRESHOLD = 0.30

# Minimum touches (carries + receptions) for an RB to appear in the
# workload report at all -- cuts deep-bench/practice-squad noise without
# hiding anyone who had a real role for even part of a season.
MIN_TOUCHES_FOR_REPORT = 20


def compute_team_run_rate(season: int) -> pd.DataFrame:
    """Per-team rush rate (rushes as a share of rush+dropback plays),
    rush yards/game, yards per carry, and rushing EPA/play -- the real
    volume + efficiency signals `build_run_game_outlook()` starts from."""
    pbp = load_pbp(season)
    plays = pbp[(pbp["rush_attempt"] == 1) | (pbp["qb_dropback"] == 1)].copy()
    plays["team"] = plays["posteam"].apply(_normalize_team_abbr)

    games_played = plays.groupby("team")["game_id"].nunique().rename("games_played")
    rush_rate = plays.groupby("team")["rush_attempt"].mean().rename("rush_rate")

    runs = plays[plays["rush_attempt"] == 1]
    run_stats = runs.groupby("team").agg(
        rush_attempts=("rush_attempt", "sum"),
        rush_yards=("yards_gained", "sum"),
        rushing_epa_total=("epa", "sum"),
    )
    run_stats["yards_per_carry"] = run_stats["rush_yards"] / run_stats["rush_attempts"]
    run_stats["rushing_epa_per_play"] = run_stats["rushing_epa_total"] / run_stats["rush_attempts"]

    df = pd.concat([games_played, rush_rate, run_stats], axis=1).reset_index()
    df["rush_yards_per_game"] = df["rush_yards"] / df["games_played"]
    return df[[
        "team", "rush_rate", "rush_yards_per_game", "yards_per_carry", "rushing_epa_per_play",
    ]]


def load_rb_workload_raw(season: int) -> pd.DataFrame:
    """Real per-week RB stat lines (carries, targets, receptions, rushing/
    receiving yards) -- a separate, narrow loader rather than widening
    `load_weekly_data()` (which only ever needed target_share/fantasy_points
    for its existing callers), matching this project's existing pattern
    (see `load_season_stat_totals()`)."""
    import nfl_data_py as nfl

    return nfl.import_weekly_data(
        [season],
        columns=[
            "player_id", "player_display_name", "recent_team", "week", "position",
            "carries", "targets", "receptions", "rushing_yards", "receiving_yards",
        ],
    )


def _percentile_rank(series: pd.Series, higher_is_better: bool = True) -> pd.Series:
    ranked = series.rank(pct=True)
    return ranked if higher_is_better else (1 - ranked)


def _rb_snap_share_lookup(season: int) -> dict:
    """Each RB's average offensive snap share this season -- keyed by
    `gsis_id` (nflverse's `player_id`), joined via `import_seasonal_rosters()`'s
    `pfr_id` column, same approach as `build_te_snap_share_lookup()`."""
    snaps = load_snap_counts(season)
    rb_snaps = snaps[(snaps["position"] == RB_POSITION) & (snaps["game_type"] == "REG")]
    avg_share_by_pfr_id = rb_snaps.groupby("pfr_player_id")["offense_pct"].mean()

    rosters = load_seasonal_rosters(season)
    rb_rosters = rosters[(rosters["position"] == RB_POSITION) & rosters["pfr_id"].notna()]

    lookup: dict = {}
    for _, row in rb_rosters.iterrows():
        share = avg_share_by_pfr_id.get(row["pfr_id"])
        if share is not None:
            lookup[row["player_id"]] = share
    return lookup


def compute_rb_workload(season: int) -> pd.DataFrame:
    """Real season-total workload for every RB, plus their share of their
    OWN team's RB-room usage on three independent axes (carries, total
    touches, offensive snaps) -- raw counts alone can't tell a bell cow on
    a run-heavy team from a committee back getting decent volume on a
    pass-heavy one."""
    weekly = load_rb_workload_raw(season)
    rbs = weekly[weekly["position"] == RB_POSITION].copy()
    rbs["team"] = rbs["recent_team"].apply(_normalize_team_abbr)

    totals = rbs.groupby(["player_id", "team"]).agg(
        carries=("carries", "sum"),
        targets=("targets", "sum"),
        receptions=("receptions", "sum"),
        rushing_yards=("rushing_yards", "sum"),
        receiving_yards=("receiving_yards", "sum"),
    ).reset_index()
    # A mid-season trade means a player can appear under more than one team
    # above -- keep the team they logged the most carries for, not an
    # arbitrary duplicate row per team.
    totals = totals.sort_values("carries", ascending=False).drop_duplicates("player_id")

    names = rbs.sort_values("week").groupby("player_id")["player_display_name"].last()
    totals["player_name"] = totals["player_id"].map(names)
    totals["touches"] = totals["carries"] + totals["receptions"]

    team_totals = totals.groupby("team").agg(
        team_rb_carries=("carries", "sum"),
        team_rb_touches=("touches", "sum"),
    )
    totals = totals.merge(team_totals, on="team", how="left")
    totals["carry_share"] = (totals["carries"] / totals["team_rb_carries"]).where(totals["team_rb_carries"] > 0)
    totals["touch_share"] = (totals["touches"] / totals["team_rb_touches"]).where(totals["team_rb_touches"] > 0)

    snap_share = _rb_snap_share_lookup(season)
    totals["snap_share"] = totals["player_id"].map(snap_share)

    totals["bell_cow_score"] = totals[["carry_share", "touch_share", "snap_share"]].mean(axis=1, skipna=True)
    return totals.drop(columns=["team_rb_carries", "team_rb_touches"])


def _usage_tier(score: Optional[float]) -> str:
    if pd.isna(score):
        return "Insufficient data"
    if score >= BELL_COW_THRESHOLD:
        return "Bell Cow"
    if score >= LEAD_BACK_THRESHOLD:
        return "Lead Back"
    if score >= COMMITTEE_LEAD_THRESHOLD:
        return "Committee Lead"
    return "Depth/Committee"


def build_rb_workload_report(season: int, min_touches: int = MIN_TOUCHES_FOR_REPORT) -> pd.DataFrame:
    """`compute_rb_workload()` filtered to RBs with a real role
    (>= `min_touches` touches, cutting deep-bench noise) and classified
    into a plain-language `usage_tier`, sorted by `bell_cow_score`."""
    df = compute_rb_workload(season)
    df = df[df["touches"] >= min_touches].copy()
    df["usage_tier"] = df["bell_cow_score"].apply(_usage_tier)
    return df.sort_values("bell_cow_score", ascending=False).reset_index(drop=True)


def _team_lead_back(workload: pd.DataFrame) -> pd.DataFrame:
    """The single highest-touch RB per team from a workload table --
    used to check bell-cow continuity year over year."""
    if workload.empty:
        return workload
    idx = workload.groupby("team")["touches"].idxmax()
    return workload.loc[idx, ["team", "player_id", "player_name", "bell_cow_score"]]


def _run_outlook_notes(row: pd.Series) -> str:
    notes = []
    if pd.notna(row.get("rush_rate_pct")):
        volume = "run-heavy" if row["rush_rate_pct"] >= 0.7 else ("pass-heavy" if row["rush_rate_pct"] <= 0.3 else "balanced")
        notes.append(f"{volume} last season ({row['rush_rate']:.0%} rush rate)")
    if pd.notna(row.get("oline_score")):
        tier = "strong" if row["oline_pct"] >= 0.7 else ("weak" if row["oline_pct"] <= 0.3 else "average")
        notes.append(f"{tier} O-line ({row['oline_score']:.0f}/100)")
    if row.get("new_head_coach"):
        notes.append("new head coach")
    if row.get("new_offensive_coordinator"):
        notes.append("new offensive coordinator")

    if row.get("workhorse_status") == "retained":
        notes.append(f"last season's lead back ({row.get('lead_back_name')}) is still on the roster")
    elif row.get("workhorse_status") == "departed":
        arrivals = row.get("notable_arrivals")
        if arrivals:
            notes.append(
                f"lead back ({row.get('lead_back_name')}) departed; notable arrival(s): {arrivals}"
            )
        else:
            notes.append(f"lead back ({row.get('lead_back_name')}) departed with no notable RB arrival on record")
    elif row.get("workhorse_status") == "no_clear_lead_back":
        notes.append("no clear lead back last season (committee backfield)")

    return "; ".join(notes) if notes else "Insufficient data to assess"


def build_run_game_outlook(season: int) -> pd.DataFrame:
    """One row per team, ranked by `run_outlook_score` (0-100, higher =
    more built to run the ball THIS season) -- see module docstring for
    exactly what this is (real last-season volume/efficiency + real,
    explainable adjustments) and isn't (a synthetic season simulation).
    """
    run_rate = compute_team_run_rate(season)
    oline = build_oline_rankings_with_trend(season)[[
        "team", "oline_score", "rank_change", "new_starters_count",
        "new_head_coach", "new_offensive_coordinator",
    ]]

    df = run_rate.merge(oline, on="team", how="outer")
    df["rush_rate_pct"] = _percentile_rank(df["rush_rate"], higher_is_better=True)
    df["run_efficiency_pct"] = (
        _percentile_rank(df["yards_per_carry"], higher_is_better=True)
        + _percentile_rank(df["rushing_epa_per_play"], higher_is_better=True)
    ) / 2
    df["oline_pct"] = _percentile_rank(df["oline_score"], higher_is_better=True)
    df["coaching_stability_pct"] = df.apply(
        lambda r: 0.0 if r.get("new_head_coach") else (0.5 if r.get("new_offensive_coordinator") else 1.0),
        axis=1,
    )

    # Bell-cow continuity: is last season's lead back still around?
    last_season_workload = build_rb_workload_report(season - 1, min_touches=MIN_TOUCHES_FOR_REPORT)
    lead_backs = _team_lead_back(last_season_workload).set_index("team")
    current_rosters = load_seasonal_rosters(season)
    current_teams_by_player = current_rosters.set_index("player_id")["team"].apply(_normalize_team_abbr)
    movers = find_team_changes(season)
    movers_by_team = movers[movers["position"] == RB_POSITION].groupby("team_new")["player_name"].apply(list)

    def _workhorse_signal(team: str) -> pd.Series:
        if team not in lead_backs.index:
            return pd.Series({"workhorse_status": "no_clear_lead_back", "lead_back_name": None, "notable_arrivals": None})
        lead = lead_backs.loc[team]
        if lead["bell_cow_score"] < COMMITTEE_LEAD_THRESHOLD:
            return pd.Series({"workhorse_status": "no_clear_lead_back", "lead_back_name": None, "notable_arrivals": None})
        current_team = current_teams_by_player.get(lead["player_id"])
        retained = current_team == team
        arrivals = movers_by_team.get(team)
        return pd.Series({
            "workhorse_status": "retained" if retained else "departed",
            "lead_back_name": lead["player_name"],
            "notable_arrivals": ", ".join(arrivals) if arrivals else None,
        })

    workhorse = df["team"].apply(_workhorse_signal)
    df = pd.concat([df, workhorse], axis=1)
    df["workhorse_continuity_pct"] = df["workhorse_status"].map(
        {"retained": 1.0, "departed": 0.0, "no_clear_lead_back": 0.5}
    )

    df["run_outlook_score"] = 100 * df[[
        "rush_rate_pct", "run_efficiency_pct", "oline_pct", "coaching_stability_pct", "workhorse_continuity_pct",
    ]].mean(axis=1, skipna=True)

    df["context_notes"] = df.apply(_run_outlook_notes, axis=1)
    return df.sort_values("run_outlook_score", ascending=False).reset_index(drop=True)
