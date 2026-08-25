"""Real coaching-staff changes for the CURRENT real season
(`build_coaching_change_lookup()`, reused from `api/nfl_enrichment.py`),
cross-referenced with each changed team's own real offensive tendencies
(pass rate, rush rate, passing EPA/play, O-line score) from the last
real completed season -- and, where the new head coach was ALSO a head
coach somewhere else last season (a real, findable fact from nflverse's
own `import_schedules()` coach columns, not a guess), that PRIOR team's
same tendencies as a real, disclosed signal for what scheme shift might
be coming.

Real, disclosed limitation
------------------------------
A "prior scheme" signal only exists for a new HEAD COACH who was ALSO a
head coach elsewhere last season -- a first-time HC (promoted internally
or elevated from coordinator) has no such trail, and coordinator hires
(`data/coaching_changes.csv`, this project's own manually maintained
OC/DC tracker) were never tracked with a "previous team" field at all,
so an OC hire only ever shows the "new OC" flag itself, never a scheme
signal. Treat every "ran a pass-heavy/run-heavy offense at their old
team" note as one real data point informing a guess, not a certainty --
a coach can and does deliberately change scheme when taking a new job.
"""

from __future__ import annotations

from typing import Optional

import pandas as pd

from api.nfl_enrichment import (
    _team_head_coaches,
    build_coaching_change_lookup,
    compute_team_offense_context,
    default_nfl_season,
)
from api.oline_analytics import build_oline_power_rankings

# A team's own pass_rate at/above this reads as "pass-heavy" in the
# plain-language notes; at/below the mirrored threshold reads "run-heavy".
PASS_HEAVY_THRESHOLD = 0.6
RUN_HEAVY_THRESHOLD = 0.4


def _find_coachs_previous_team(coach_name: str, current_team: str, previous_head_coaches: dict) -> Optional[str]:
    """Real, name-matched lookup: which team (if any) `coach_name` was
    the head coach of LAST season, per nflverse's own schedule data --
    excludes `current_team` itself (that would mean they aren't actually
    new, which `build_coaching_change_lookup()` already wouldn't flag)."""
    for team, name in previous_head_coaches.items():
        if team != current_team and name == coach_name:
            return team
    return None


def build_coaching_scheme_report(season: int, roster_season: Optional[int] = None) -> pd.DataFrame:
    """One row per team with a real, current new head coach and/or
    offensive coordinator, showing that team's own last-real-season
    offensive tendencies as the "before" baseline, plus -- for a new HC
    who was a head coach elsewhere last season -- that prior team's same
    tendencies as a real signal for the incoming scheme.

    `season`/`roster_season` split is the same idea as elsewhere in this
    app: `season` is the last real completed season to pull tendencies
    from, `roster_season` is the real current season the coaching change
    itself is reported for (defaults to `default_nfl_season()`) -- real
    coaching-staff data is published well before any game is played,
    confirmed live elsewhere in this app (see
    `api/team_change_analytics.py`'s module docstring).
    """
    roster_season = roster_season if roster_season is not None else default_nfl_season()

    coaching = build_coaching_change_lookup(roster_season)
    changed_teams = [
        team for team, info in coaching.items()
        if info.get("new_head_coach") or info.get("new_offensive_coordinator")
    ]
    if not changed_teams:
        return pd.DataFrame()

    offense = compute_team_offense_context(season).set_index("team")
    offense["rush_rate"] = 1 - offense["pass_rate"]
    oline = build_oline_power_rankings(season)[["team", "oline_score"]].set_index("team")

    try:
        previous_head_coaches = _team_head_coaches(roster_season - 1)
    except Exception:
        previous_head_coaches = {}

    rows = []
    for team in changed_teams:
        info = coaching[team]
        prior_team = None
        if info.get("new_head_coach") and info.get("head_coach_name"):
            prior_team = _find_coachs_previous_team(info["head_coach_name"], team, previous_head_coaches)

        rows.append({
            "team": team,
            "new_head_coach": info.get("new_head_coach", False),
            "head_coach_name": info.get("head_coach_name"),
            "new_offensive_coordinator": info.get("new_offensive_coordinator", False),
            "offensive_coordinator_name": info.get("offensive_coordinator_name"),
            "pass_rate": offense["pass_rate"].get(team),
            "rush_rate": offense["rush_rate"].get(team),
            "pass_epa": offense["pass_epa"].get(team),
            "oline_score": oline["oline_score"].get(team),
            "prior_team": prior_team,
            "prior_pass_rate": offense["pass_rate"].get(prior_team) if prior_team else None,
            "prior_rush_rate": offense["rush_rate"].get(prior_team) if prior_team else None,
            "prior_pass_epa": offense["pass_epa"].get(prior_team) if prior_team else None,
        })

    df = pd.DataFrame(rows)
    df["context_notes"] = df.apply(_context_notes, axis=1)
    return df.sort_values("team").reset_index(drop=True)


def _style(pass_rate: Optional[float]) -> str:
    if pd.isna(pass_rate):
        return "unknown-scheme"
    if pass_rate >= PASS_HEAVY_THRESHOLD:
        return "pass-heavy"
    if pass_rate <= RUN_HEAVY_THRESHOLD:
        return "run-heavy"
    return "balanced"


def _context_notes(row: pd.Series) -> str:
    notes = []
    if row.get("new_head_coach"):
        notes.append(f"new HC {row.get('head_coach_name')}")
    if row.get("new_offensive_coordinator"):
        notes.append(f"new OC {row.get('offensive_coordinator_name')}")

    if row.get("prior_team") and pd.notna(row.get("prior_pass_rate")):
        notes.append(
            f"ran a {_style(row['prior_pass_rate'])} offense at {row['prior_team']} last season "
            f"({row['prior_pass_rate']:.0%} pass rate) -- a real signal for the incoming scheme, "
            f"not a guarantee"
        )
    elif row.get("new_head_coach"):
        notes.append("no prior HC data available (first head-coaching job, or promoted internally)")

    if row.get("new_offensive_coordinator") and not row.get("new_head_coach"):
        notes.append("no scheme-history data for coordinator-only hires (not tracked)")

    return "; ".join(notes) if notes else "New staff, no further real signal available"
