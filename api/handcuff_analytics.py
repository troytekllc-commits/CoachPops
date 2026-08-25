"""Identifies real "next man up" opportunities: for each team's current
starting-caliber RB/WR who's carrying a real, CURRENT injury designation
(Sleeper's live status, gsis-keyed -- see `api/external_sources.py`), who
the backup is and how much they stand to inherit if the starter actually
misses time.

Two real, separate signals combined
-------------------------------------
1. **Who's the starter, who's the backup** -- nflverse's own depth-chart
   data has a real, confirmed schema break for 2025+ (no `week`/
   `depth_position` columns at all; see `build_injury_opportunity_lookup()`
   in `api/nfl_enrichment.py`), so this uses last season's real usage
   share instead: RB carry/touch/snap share (`compute_rb_workload()`,
   reused from `api/run_game_analytics.py`) and WR target share
   (`_compute_wr_target_share()` below, same method as
   `api/team_change_analytics.py`'s `compute_target_competition()`).
   Within each (current team, position), the highest-share player is
   treated as the starter, the next-highest as the primary backup.
2. **Is the starter actually hurt right now** -- Sleeper's real,
   current `status` (via `attach_sleeper_status_and_trending()`'s same
   restyled Yahoo-vocabulary codes: Q/D/O/IR/PUP), not last season's
   injury history.

Real, disclosed limitations
------------------------------
- Last season's usage share is a proxy for "who's the current starter,"
  not a live depth chart -- a genuine offseason shakeup (a free-agent
  signing, a rookie who beats out an incumbent in camp) can make this
  wrong in exactly the way `api/run_game_analytics.py`'s own
  `workhorse_status` ("retained"/"departed") already discloses for the
  same reason. Team assignment itself is remapped to each player's real
  CURRENT roster (`roster_season`) before ranking, so a player who
  changed teams this offseason is correctly grouped with their new
  teammates, not their old ones.
- A true rookie or new-to-the-NFL backup with zero prior-season usage
  can't appear as a "backup" at all -- there's no real usage data to
  rank them by yet. Rookie Radar is the complementary tool for that
  exact case.
- Only surfaces RB/WR (TE isn't covered -- a real, disclosed scope
  choice, not an oversight).
"""

from __future__ import annotations

from typing import Optional

import pandas as pd

from api.external_sources import build_sleeper_injury_status_lookup
from api.nfl_enrichment import _normalize_team_abbr, default_nfl_season, load_seasonal_rosters, load_weekly_data
from api.run_game_analytics import compute_rb_workload

WR_POSITION = "WR"

# Sleeper-restyled Yahoo-vocabulary codes (see api/external_sources.py)
# that count as a real, current injury designation worth surfacing a
# handcuff for. "Q" (Questionable) is included but weighted lowest below
# -- a Questionable player usually DOES play.
STARTER_AT_RISK_STATUSES = ("Q", "D", "O", "IR", "PUP")
_INJURY_SEVERITY_WEIGHT = {"Q": 0.3, "D": 0.6, "O": 0.9, "IR": 1.0, "PUP": 1.0}


def _compute_wr_target_share(season: int) -> pd.DataFrame:
    """Real season-average target share per WR, keyed by (player_id,
    team) -- same method as `api/team_change_analytics.py`'s
    `compute_target_competition()`, but per-player rather than summed
    team-wide, since this needs to rank individual receivers against
    their own teammates."""
    weekly = load_weekly_data(season)
    wrs = weekly[weekly["position"] == WR_POSITION].copy()
    wrs["team"] = wrs["recent_team"].apply(_normalize_team_abbr)

    usage = wrs.groupby(["player_id", "team"])["target_share"].mean().reset_index()
    usage = usage.rename(columns={"target_share": "usage_share"})
    names = wrs.sort_values("week").groupby("player_id")["player_display_name"].last()
    usage["player_name"] = usage["player_id"].map(names)
    # A mid-season trade means a player can appear under more than one
    # team above -- keep the team they logged the most usage for.
    usage = usage.sort_values("usage_share", ascending=False).drop_duplicates("player_id")
    return usage[["player_id", "team", "player_name", "usage_share"]]


def _compute_rb_usage_share(season: int) -> pd.DataFrame:
    """RB usage, restyled to the same (player_id, team, player_name,
    usage_share) shape as `_compute_wr_target_share()` -- `bell_cow_score`
    (mean of carry/touch/snap share) already IS a real, comparable usage
    share, same idea as WR target share, just built from run-game volume
    instead of passing-game volume."""
    workload = compute_rb_workload(season)
    workload = workload.rename(columns={"bell_cow_score": "usage_share"})
    return workload[["player_id", "team", "player_name", "usage_share"]]


def _remap_to_current_team(usage: pd.DataFrame, roster_season: int) -> pd.DataFrame:
    """Overrides `team` with each player's REAL CURRENT roster team
    (`roster_season`) rather than trusting last season's team -- catches
    an offseason trade/free-agent move so a player is ranked against
    their new, actual teammates, not their old ones. Players no longer on
    any real roster this season (retired, out of the league) are
    dropped -- there's no current team to rank them within."""
    current_rosters = load_seasonal_rosters(roster_season)
    current_team_by_id = current_rosters.drop_duplicates("player_id").set_index("player_id")["team"]
    current_team_by_id = current_team_by_id.apply(_normalize_team_abbr)

    df = usage.copy()
    df["team"] = df["player_id"].map(current_team_by_id)
    return df.dropna(subset=["team"])


def find_next_man_up(season: int, roster_season: Optional[int] = None) -> pd.DataFrame:
    """One row per (team, position) where the real, current top-usage
    RB/WR carries a real, CURRENT Sleeper injury designation -- paired
    with the next-highest-usage teammate at that position (the "next man
    up") and an `opportunity_score` weighted by the starter's own usage
    magnitude and the severity of their injury status.

    `season`/`roster_season` split is the same idea as elsewhere in this
    app (see `api/team_change_analytics.py`'s module docstring):
    `season` is the last real completed season to compute usage shares
    from, `roster_season` is the real current season's rosters those
    shares get remapped onto (defaults to `default_nfl_season()`).
    """
    roster_season = roster_season if roster_season is not None else default_nfl_season()

    rb_usage = _compute_rb_usage_share(season)
    rb_usage["position"] = "RB"
    wr_usage = _compute_wr_target_share(season)
    wr_usage["position"] = WR_POSITION

    usage = pd.concat([rb_usage, wr_usage], ignore_index=True)
    usage = usage.dropna(subset=["usage_share"])
    usage = _remap_to_current_team(usage, roster_season)
    if usage.empty:
        return usage

    usage["depth_rank"] = usage.groupby(["team", "position"])["usage_share"].rank(
        ascending=False, method="first"
    )

    starters = usage[usage["depth_rank"] == 1]
    backups = usage[usage["depth_rank"] == 2]
    merged = starters.merge(
        backups, on=["team", "position"], suffixes=("_starter", "_backup")
    )
    if merged.empty:
        return merged

    status_lookup = build_sleeper_injury_status_lookup()
    merged["starter_status"] = merged["player_id_starter"].map(
        lambda pid: status_lookup.get(str(pid), {}).get("status", "")
    )
    merged["backup_status"] = merged["player_id_backup"].map(
        lambda pid: status_lookup.get(str(pid), {}).get("status", "")
    )

    at_risk = merged[merged["starter_status"].isin(STARTER_AT_RISK_STATUSES)].copy()
    if at_risk.empty:
        return at_risk

    # Don't recommend a backup who's themselves out for the year/hurt --
    # no real opportunity there.
    at_risk = at_risk[~at_risk["backup_status"].isin(("IR", "PUP", "O"))]
    if at_risk.empty:
        return at_risk

    at_risk["injury_severity_weight"] = at_risk["starter_status"].map(_INJURY_SEVERITY_WEIGHT).fillna(0.3)
    at_risk["opportunity_score"] = (
        at_risk["usage_share_starter"] * at_risk["injury_severity_weight"] * 100
    ).round(1)

    at_risk["context_notes"] = at_risk.apply(_context_notes, axis=1)
    return at_risk.sort_values("opportunity_score", ascending=False).reset_index(drop=True)


_STATUS_LABELS = {"Q": "Questionable", "D": "Doubtful", "O": "Out", "IR": "on IR", "PUP": "on PUP"}


def _context_notes(row: pd.Series) -> str:
    label = _STATUS_LABELS.get(row["starter_status"], row["starter_status"])
    starter_share_pct = row["usage_share_starter"] * 100 if pd.notna(row["usage_share_starter"]) else None
    backup_share_pct = row["usage_share_backup"] * 100 if pd.notna(row["usage_share_backup"]) else None

    notes = [f"{row['player_name_starter']} is {label}"]
    if starter_share_pct is not None:
        notes.append(f"held {starter_share_pct:.0f}% real usage share last season")
    if backup_share_pct is not None and backup_share_pct > 5:
        notes.append(f"{row['player_name_backup']} already carries a real {backup_share_pct:.0f}% share")
    else:
        notes.append(f"{row['player_name_backup']} had minimal usage last season -- a real unknown if elevated")
    return "; ".join(notes)
