"""Backfills the enrichment fields Yahoo's API doesn't provide -- rookie
status/draft capital, target share, deep-target share, red-zone share,
injury-opened opportunity, team coaching changes, Vegas game script, Next
Gen Stats, forecasted game-day weather (see ``api/weather.py``), a QB
"sophomore slump" caution flag, and a rough weekly projection baseline
(see ``ENRICHMENT_FIELDS`` for the full, authoritative list) -- using
``nfl_data_py``, joined onto Yahoo players via nflverse's own ``yahoo_id``
crosswalk column.

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

Coaching-change caveat: head-coach changes (``team_new_head_coach``) are
real, free data -- diffed year-over-year from ``import_schedules()``'s own
``home_coach``/``away_coach`` columns. Offensive/defensive coordinator
changes are NOT available from any free structured dataset (confirmed via
web search -- every source is a page built for humans, not an API), so
``team_new_offensive_coordinator`` only populates from the manually
maintained ``data/coaching_changes.csv`` -- see
``load_manual_coordinator_changes()``. Absent that file, it's always False.

Not yet built (documented rather than faked): year-over-year positional
*competition* change (who left/arrived at the same position on a team) --
a real signal from the sophomore-slump research this module supports, but
one that needs careful two-season role-matching to avoid noise. Left as a
future enhancement rather than shipped half-reliable.
"""

from __future__ import annotations

import datetime
import logging
import re
from functools import lru_cache
from pathlib import Path
from typing import Dict, Optional

import pandas as pd

logger = logging.getLogger(__name__)

# Manually maintained OC/DC-hire CSV -- see load_manual_coordinator_changes()
# below for why this is manual instead of pulled from an API.
DEFAULT_COORDINATOR_CHANGES_CSV = Path(__file__).resolve().parent.parent / "data" / "coaching_changes.csv"

# Weeks used to build the flat rest-of-season baseline (see module docstring).
# The modern NFL regular season runs weeks 1-18 (verified against
# import_schedules()/import_weekly_data() -- both report week values up to
# 18) -- range(1, 19) to actually include week 18, not range(1, 18).
PROJECTION_WEEKS = range(1, 19)


def default_nfl_season() -> int:
    """Best-guess current NFL season year. The season named "2026" runs
    from around September 2026 through the Super Bowl in February 2027,
    so January/February still belong to the *previous* year's season.

    This is the right default for ROSTER/draft-capital context (which
    team a player is on, is_rookie, draft_capital) -- those exist the
    moment a season's rosters/draft class are set, months before a single
    game is played. It is the WRONG default for anything that needs
    actual played games (target_share, red_zone_share, team_pass_rate,
    O-Line rankings, snap counts, ...) -- use `default_stats_season()`
    for those instead. Verified directly: in August 2026,
    `import_seasonal_rosters([2026])` returns real data, but
    `import_pbp_data([2026])` 404s -- there's nothing to aggregate yet.
    """
    today = datetime.date.today()
    return today.year - 1 if today.month <= 2 else today.year


def default_stats_season() -> int:
    """Best-guess season year for which nfl_data_py actually has
    play-by-play/weekly/snap-count data -- i.e. games have actually been
    played. The NFL regular season always starts after Labor Day (early
    September), so before September 1st of `default_nfl_season()`'s
    year, fall back to the prior (fully-played) season instead of the
    upcoming one. Use this for O-Line rankings, team change context, TE
    snap share, target share, and any other performance-based function --
    `default_nfl_season()` for roster/draft-capital context instead.
    """
    today = datetime.date.today()
    season = default_nfl_season()
    if today.year == season and today.month < 9:
        return season - 1
    return season


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

    # `fantasy_points` (not `fantasy_points_ppr`) -- this league scores
    # standard, not PPR, so the flat rest-of-season baseline in
    # estimate_projected_points_by_week() shouldn't credit a full point
    # per reception it wouldn't actually score.
    return nfl.import_weekly_data(
        [season],
        columns=["player_id", "player_display_name", "recent_team", "week", "position", "target_share", "fantasy_points"],
    )


# The raw box-score columns calculate_custom_value() actually needs --
# load_weekly_data() above deliberately pulls a narrower set (it only
# ever needed target_share/fantasy_points), so this is a separate loader
# rather than widening that one and slowing down every other caller.
_SEASON_STAT_RAW_COLUMNS = (
    "passing_yards", "passing_tds", "interceptions", "rushing_yards", "rushing_tds",
    "receptions", "receiving_yards", "receiving_tds", "sack_fumbles_lost",
    "rushing_fumbles_lost", "receiving_fumbles_lost", "passing_2pt_conversions",
    "rushing_2pt_conversions", "receiving_2pt_conversions",
)


@lru_cache(maxsize=8)
def load_season_stat_totals(season: int) -> pd.DataFrame:
    """Full-season raw stat totals per player (real column names
    confirmed directly against nfl_data_py's actual output, not guessed),
    reshaped to this project's internal stat names (``passing_touchdowns``,
    ``fumbles_lost`` summed across sack/rush/receiving fumbles,
    ``two_point_conversions`` summed across all three conversion types)
    so the result can go straight into ``calculate_custom_value()``.

    Built for ``build_draft_board_pool()`` -- everywhere else in this
    project gets a player's value from ``fantasy_points`` (nfl_data_py's
    own pre-computed total) or Yahoo's own stats; the draft board needs
    the raw categories so *this league's own scoring weights* -- not
    nfl_data_py's PPR-flavored default -- decide the value.
    """
    import nfl_data_py as nfl

    weekly = nfl.import_weekly_data(
        [season],
        columns=["player_id", "player_display_name", "recent_team", "week", "position"]
        + list(_SEASON_STAT_RAW_COLUMNS),
    )

    totals = weekly.groupby("player_id")[list(_SEASON_STAT_RAW_COLUMNS)].sum().reset_index()
    # A player's team/position can appear to change row-to-row after an
    # in-season trade or a data quirk -- take their LATEST week's info,
    # not whatever groupby happens to pick, so the totals above (correctly
    # summed across every team they played for) aren't fragmented by a
    # naive multi-column groupby.
    latest_info = (
        weekly.sort_values("week")
        .groupby("player_id")[["player_display_name", "recent_team", "position"]]
        .last()
        .reset_index()
    )
    merged = totals.merge(latest_info, on="player_id")

    merged["fumbles_lost"] = (
        merged["sack_fumbles_lost"] + merged["rushing_fumbles_lost"] + merged["receiving_fumbles_lost"]
    )
    merged["two_point_conversions"] = (
        merged["passing_2pt_conversions"] + merged["rushing_2pt_conversions"] + merged["receiving_2pt_conversions"]
    )
    return merged.rename(columns={
        "passing_tds": "passing_touchdowns",
        "rushing_tds": "rushing_touchdowns",
        "receiving_tds": "receiving_touchdowns",
    })


@lru_cache(maxsize=8)
def load_pbp(season: int) -> pd.DataFrame:
    """Shared play-by-play loader for deep-target-share, red-zone-share,
    api/oline_analytics.py's pass-protection/run-blocking metrics, and
    api/team_change_analytics.py's team pace/efficiency context -- avoids
    downloading the same season's pbp data more than once."""
    import nfl_data_py as nfl

    return nfl.import_pbp_data(
        [season],
        columns=[
            "game_id", "week", "posteam", "rusher_player_id", "receiver_player_id",
            "yardline_100", "air_yards", "rush_attempt", "pass_attempt",
            "sack", "qb_hit", "qb_dropback", "yards_gained", "epa",
        ],
        downcast=True,
    )


def compute_team_offense_context(season: int) -> pd.DataFrame:
    """Per-team pass rate (share of rush+pass plays that were passes) and
    passing efficiency (EPA/play on pass attempts) -- a proxy for offense
    pace/aggressiveness and QB+scheme quality. Lives here (rather than in
    api/team_change_analytics.py, its main consumer) so it can also feed
    per-player enrichment (team_pass_rate/team_pass_epa, used by the TE
    Difference-Maker calculator) without a circular import."""
    pbp = load_pbp(season)
    plays = pbp[(pbp["pass_attempt"] == 1) | (pbp["rush_attempt"] == 1)].copy()
    plays["team"] = plays["posteam"].apply(_normalize_team_abbr)

    pass_rate = plays.groupby("team")["pass_attempt"].mean().rename("pass_rate")
    pass_epa = plays[plays["pass_attempt"] == 1].groupby("team")["epa"].mean().rename("pass_epa")
    return pd.concat([pass_rate, pass_epa], axis=1).reset_index()


@lru_cache(maxsize=8)
def load_snap_counts(season: int) -> pd.DataFrame:
    """Shared snap-count loader for api/oline_analytics.py's continuity
    metric and this module's TE snap-share lookup."""
    import nfl_data_py as nfl

    return nfl.import_snap_counts([season])


def build_te_snap_share_lookup(season: int) -> Dict[str, float]:
    """Each TE's average offensive snap share this season -- the
    receiving-role-vs-blocking-role signal the TE Difference-Maker
    calculator needs. Keyed by `gsis_id`, joined via `import_seasonal_rosters()`'s
    `pfr_id` column (the same format as `import_snap_counts()`'s
    `pfr_player_id` -- verified directly, e.g. Travis Kelce is `KelcTr00`
    in both)."""
    snaps = load_snap_counts(season)
    te_snaps = snaps[(snaps["position"] == "TE") & (snaps["game_type"] == "REG")]
    avg_share_by_pfr_id = te_snaps.groupby("pfr_player_id")["offense_pct"].mean()

    rosters = load_seasonal_rosters(season)
    te_rosters = rosters[(rosters["position"] == "TE") & rosters["pfr_id"].notna()]

    lookup: Dict[str, float] = {}
    for _, row in te_rosters.iterrows():
        share = avg_share_by_pfr_id.get(row["pfr_id"])
        if share is not None:
            lookup[row["player_id"]] = share
    return lookup


@lru_cache(maxsize=8)
def load_deep_target_share_by_gsis(season: int) -> Dict[str, float]:
    """Share of each receiver's targets that traveled 20+ air yards
    ("deep" targets), computed from play-by-play data.

    A "target" here is any pass attempt with an identified intended
    receiver, completed or not.
    """
    pbp = load_pbp(season)
    targets = pbp[(pbp["pass_attempt"] == 1) & pbp["receiver_player_id"].notna()]
    total_targets = targets.groupby("receiver_player_id").size()
    deep_targets = targets[targets["air_yards"] >= 20].groupby("receiver_player_id").size()
    deep_share = (deep_targets / total_targets).fillna(0.0)
    return deep_share.to_dict()


@lru_cache(maxsize=8)
def load_red_zone_share_by_gsis(season: int) -> Dict[str, float]:
    """Each player's share of their own TEAM's red-zone (opponent's 10-yard
    line or closer) rush attempts + targets, computed from play-by-play data.

    This is the "sophomore watch" red-zone/goal-line signal: a rookie whose
    touchdowns came from a real, earned share of the team's goal-line work
    is a much safer bet to repeat them than one who got hot on a handful of
    opportunistic scores -- see the module docstring on `find_breakout_signals`
    usage in `data/calculators.py` for how this gets applied.
    """
    pbp = load_pbp(season)
    red_zone = pbp[pbp["yardline_100"] <= 10]

    rush_touches = red_zone[(red_zone["rush_attempt"] == 1) & red_zone["rusher_player_id"].notna()][
        ["posteam", "rusher_player_id"]
    ].rename(columns={"rusher_player_id": "player_id"})
    target_touches = red_zone[(red_zone["pass_attempt"] == 1) & red_zone["receiver_player_id"].notna()][
        ["posteam", "receiver_player_id"]
    ].rename(columns={"receiver_player_id": "player_id"})
    touches = pd.concat([rush_touches, target_touches], ignore_index=True)

    team_totals = touches.groupby("posteam").size()
    player_totals = touches.groupby(["posteam", "player_id"]).size()

    shares: Dict[str, float] = {}
    for (team, player_id), count in player_totals.items():
        team_total = team_totals.get(team, 0)
        if team_total:
            shares[player_id] = count / team_total
    return shares


def estimate_projected_points_by_week(ppg_baseline: Optional[float]) -> Dict[str, float]:
    """Flat rest-of-season baseline projection -- see module docstring's
    "Projection caveat"."""
    if ppg_baseline is None or pd.isna(ppg_baseline):
        return {}
    return {str(week): round(float(ppg_baseline), 1) for week in PROJECTION_WEEKS}


@lru_cache(maxsize=8)
def load_depth_charts(season: int) -> pd.DataFrame:
    import nfl_data_py as nfl

    return nfl.import_depth_charts([season])


@lru_cache(maxsize=8)
def load_injuries(season: int) -> pd.DataFrame:
    import nfl_data_py as nfl

    return nfl.import_injuries([season])


# Statuses nflverse's weekly injury report uses. "IR"/"PUP" are roster
# designations, not weekly report statuses -- a player already on IR simply
# stops appearing on the report at all, so this signal only catches
# starters banged up enough to be Questionable/Doubtful/Out, not ones
# already shelved for the season.
INJURY_OPPORTUNITY_STATUSES = {"Questionable", "Doubtful", "Out"}


def _latest_available_week(df: pd.DataFrame, week_col: str = "week") -> Optional[int]:
    weeks = df[week_col].dropna()
    return int(weeks.max()) if not weeks.empty else None


def build_injury_opportunity_lookup(season: int, week: Optional[int] = None) -> Dict[str, dict]:
    """For each backup (depth chart rank > "1") at RB/WR/TE, flag whether
    any rank-"1" player at the same team+position is Questionable,
    Doubtful, or Out per that week's official injury report.

    Simplification: nflverse's depth chart sometimes lists more than one
    player at rank "1" for a position (e.g. two starting WRs both ranked
    "1") -- this treats *any* rank-1 player at that team+position being
    hurt as an opportunity signal for every backup behind them, rather
    than trying to match specific starter-to-backup slots 1:1.

    Args:
        season: NFL season year.
        week: Depth chart / injury report week to use. Defaults to the
            latest week with injury-report data available for that season.

    Returns:
        dict[gsis_id, {"injury_opportunity": True, "injury_opportunity_ahead_player": str,
        "injury_opportunity_ahead_status": str}] -- only for backups with a live signal;
        everyone else is simply absent from the dict.
    """
    injuries = load_injuries(season)

    if week is None:
        week = _latest_available_week(injuries)
        if week is None:
            return {}

    depth = load_depth_charts(season)
    depth = depth[
        (depth["week"] == week)
        & depth["position"].isin(["RB", "WR", "TE"])
        & (depth["depth_position"] == depth["position"])
    ]
    injuries = injuries[injuries["week"] == week]

    injured_by_team_pos: Dict[tuple, list] = {}
    for _, row in injuries.iterrows():
        if row["report_status"] in INJURY_OPPORTUNITY_STATUSES:
            key = (row["team"], row["position"])
            injured_by_team_pos.setdefault(key, []).append((row["full_name"], row["report_status"]))

    lookup: Dict[str, dict] = {}
    for _, row in depth.iterrows():
        if str(row["depth_team"]) == "1":
            continue  # only backups are "opportunity" candidates
        ahead = injured_by_team_pos.get((row["club_code"], row["position"]))
        if ahead:
            name, status = ahead[0]
            lookup[row["gsis_id"]] = {
                "injury_opportunity": True,
                "injury_opportunity_ahead_player": name,
                "injury_opportunity_ahead_status": status,
            }
    return lookup


def build_season_injury_durability_lookup(season: int) -> Dict[str, int]:
    """Per `gsis_id`, how many DISTINCT weeks this season the player was
    listed "Out" on the NFL's official injury report -- a real durability-
    HISTORY signal, not a live "are they injured right now" one (there's no
    such thing once a season's over). This is the only injury-related
    signal available to Yahoo-independent tools (Draft Board/Run Game
    Outlook) -- they have no live Yahoo `status` field to draw on the way
    every other tab does, so a player's real in-season injury report is the
    next best thing for flagging a fragile profile.

    Deliberately counts only "Out" (not Questionable/Doubtful) -- those
    lighter tags usually still mean the player suited up, so counting them
    would overstate how often someone actually missed a game.
    """
    injuries = load_injuries(season)
    out_weeks = injuries[injuries["report_status"] == "Out"]
    return out_weeks.groupby("gsis_id")["week"].nunique().to_dict()


@lru_cache(maxsize=8)
def load_schedules(season: int) -> pd.DataFrame:
    import nfl_data_py as nfl

    return nfl.import_schedules([season])


def build_game_script_lookup(season: int, week: Optional[int] = None) -> Dict[str, dict]:
    """Per-team Vegas context for one week: the spread (positive = that
    team is favored by that many points -- verified empirically via its
    correlation with actual home-team margin, not assumed) and each
    team's own implied point total, derived from `import_schedules()`'s
    `spread_line`/`total_line` columns (works for the current season --
    unlike `import_sc_lines()`, which stops around 2020).

    Implied team total: for the home team, ``(total_line + spread_line) / 2``;
    for the away team, ``(total_line - spread_line) / 2`` (the two add up
    to `total_line`, and the favored team -- higher `spread_line` side --
    gets the larger share).

    Args:
        season: NFL season year.
        week: Week to use. Defaults to the earliest week in the schedule
            with no result recorded yet (i.e. the next upcoming game) --
            falls back to the latest played week if the whole season is
            already final.
    """
    games = load_schedules(season)

    if week is None:
        # Determined from every game this season, BEFORE dropping rows
        # missing Vegas lines below -- otherwise, if the true next week's
        # lines haven't posted yet while a later week's have, this would
        # silently report the later week's context as if it were "next."
        upcoming = games[games["home_score"].isna()]
        week = int(upcoming["week"].min()) if not upcoming.empty else int(games["week"].max())

    games = games[games["week"] == week]
    games = games.dropna(subset=["spread_line", "total_line"])

    lookup: Dict[str, dict] = {}
    for _, row in games.iterrows():
        home, away = _normalize_team_abbr(row["home_team"]), _normalize_team_abbr(row["away_team"])
        spread, total = row["spread_line"], row["total_line"]
        home_implied = (total + spread) / 2
        away_implied = (total - spread) / 2
        lookup[home] = {"game_script_spread": spread, "game_script_total": total, "game_script_implied_team_total": home_implied}
        lookup[away] = {"game_script_spread": -spread, "game_script_total": total, "game_script_implied_team_total": away_implied}
    return lookup


@lru_cache(maxsize=8)
def load_ngs_receiving(season: int) -> pd.DataFrame:
    """Season-aggregate (week=0, REG) Next Gen Stats receiving data --
    average separation, cushion, and YAC-over-expectation per player."""
    import nfl_data_py as nfl

    ngs = nfl.import_ngs_data("receiving", [season])
    return ngs[(ngs["week"] == 0) & (ngs["season_type"] == "REG")]


@lru_cache(maxsize=8)
def load_ngs_passing(season: int) -> pd.DataFrame:
    """Season-aggregate (week=0, REG) Next Gen Stats passing data --
    average time to throw and completion % above expectation (CPOE) per QB."""
    import nfl_data_py as nfl

    ngs = nfl.import_ngs_data("passing", [season])
    return ngs[(ngs["week"] == 0) & (ngs["season_type"] == "REG")]


def build_ngs_receiving_lookup(season: int) -> Dict[str, dict]:
    """{gsis_id: {avg_separation, avg_yac_above_expectation, catch_percentage}}
    -- a route-running/hands skill signal independent of scheme or volume."""
    ngs = load_ngs_receiving(season)
    return {
        row["player_gsis_id"]: {
            "ngs_separation": row["avg_separation"],
            "ngs_yac_above_expectation": row["avg_yac_above_expectation"],
        }
        for _, row in ngs.dropna(subset=["player_gsis_id"]).iterrows()
    }


def build_ngs_passing_lookup(season: int) -> Dict[str, dict]:
    """{gsis_id: {avg_time_to_throw, completion_percentage_above_expectation}}
    -- CPOE is a well-regarded "hidden QB skill" metric independent of raw
    box score; time-to-throw helps separate "bad O-line" from "QB holds
    the ball too long" when reading sack rate (see api/oline_analytics.py's
    documented caveat about conflating the two)."""
    ngs = load_ngs_passing(season)
    return {
        row["player_gsis_id"]: {
            "ngs_time_to_throw": row["avg_time_to_throw"],
            "ngs_cpoe": row["completion_percentage_above_expectation"],
        }
        for _, row in ngs.dropna(subset=["player_gsis_id"]).iterrows()
    }


# Team codes differ across nflverse's own datasets, let alone Yahoo's
# `editorial_team_abbr` -- normalize everything to this convention before
# joining team-level signals. `import_draft_picks()`'s `team` column uses
# PFR-style long codes (GNB/KAN/LVR/NOR/NWE/SFO/TAM) that don't match
# `import_schedules()`/play-by-play's short codes (GB/KC/LV/NO/NE/SF/TB) at
# all -- confirmed by a real duplicate-team bug in api/oline_analytics.py's
# rankings (New England showed up as both "NWE" and "NE") before this map
# was extended to cover them.
TEAM_ABBR_NORMALIZATION = {
    "LA": "LAR", "OAK": "LV", "SD": "LAC", "STL": "LAR", "WSH": "WAS", "AZ": "ARI",
    "GNB": "GB", "KAN": "KC", "LVR": "LV", "NOR": "NO", "NWE": "NE", "SFO": "SF", "TAM": "TB",
}


def _normalize_team_abbr(abbr: Optional[str]) -> Optional[str]:
    if not abbr:
        return abbr
    return TEAM_ABBR_NORMALIZATION.get(abbr, abbr)


def _team_head_coaches(season: int) -> Dict[str, str]:
    """Most recent home/away coach name on record per team for a season
    (a coach fired mid-season is overwritten by their successor, since we
    just want "who's coaching them now")."""
    games = load_schedules(season)
    games_sorted = games.sort_values("week") if "week" in games.columns else games

    coaches: Dict[str, str] = {}
    for _, row in games_sorted.iterrows():
        for team_col, coach_col in (("home_team", "home_coach"), ("away_team", "away_coach")):
            team, coach = row.get(team_col), row.get(coach_col)
            if team and isinstance(coach, str) and coach.strip():
                coaches[_normalize_team_abbr(team)] = coach
    return coaches


def build_head_coach_change_lookup(season: int) -> Dict[str, dict]:
    """Flag teams whose head coach this season differs from last season's,
    using `import_schedules()`'s real `home_coach`/`away_coach` columns --
    free, structured data, no manual input needed (unlike coordinator
    changes below).

    Returns {team_abbr: {"new_head_coach": bool, "head_coach_name": str}}.
    A team with no prior-season baseline (expansion/relocation) never
    reads as "new" -- there's nothing to compare against.
    """
    current = _team_head_coaches(season)
    try:
        previous = _team_head_coaches(season - 1)
    except Exception:
        previous = {}

    return {
        team: {
            "new_head_coach": team in previous and previous[team] != coach,
            "head_coach_name": coach,
        }
        for team, coach in current.items()
    }


def load_manual_coordinator_changes(season: int, csv_path: Optional[Path] = None) -> Dict[str, dict]:
    """Load offensive/defensive coordinator hire info from a manually
    maintained CSV (`data/coaching_changes.csv` by default) -- see
    `data/coaching_changes.csv.example` for the exact format.

    Why manual: unlike head coaches (covered for free above), there is no
    clean structured dataset for OC/DC hires -- every source (Wikipedia,
    Pro Football Reference, beat-writer trackers) is built for human
    reading, not an API, and scraping them is fragile. This file is
    intentionally a small, low-maintenance manual input: ~32 rows, updated
    once per offseason from public reporting. Returns {} (not an error) if
    the file doesn't exist yet -- this signal is opt-in.
    """
    path = csv_path or DEFAULT_COORDINATOR_CHANGES_CSV
    if not path.is_file():
        return {}

    df = pd.read_csv(path)
    df = df[df["season"] == season]

    lookup: Dict[str, dict] = {}
    for _, row in df.iterrows():
        team = _normalize_team_abbr(row["team"])
        lookup[team] = {
            "new_offensive_coordinator": bool(row.get("new_offensive_coordinator", False)),
            "offensive_coordinator_name": row.get("offensive_coordinator_name"),
            "new_defensive_coordinator": bool(row.get("new_defensive_coordinator", False)),
            "defensive_coordinator_name": row.get("defensive_coordinator_name"),
        }
    return lookup


def build_coaching_change_lookup(season: int, csv_path: Optional[Path] = None) -> Dict[str, dict]:
    """Combine the free head-coach-change signal with the optional,
    manually maintained OC/DC CSV into one per-team lookup."""
    head_coach = build_head_coach_change_lookup(season)
    coordinators = load_manual_coordinator_changes(season, csv_path)

    combined: Dict[str, dict] = {}
    for team in set(head_coach) | set(coordinators):
        hc = head_coach.get(team, {})
        oc = coordinators.get(team, {})
        combined[team] = {
            "new_head_coach": hc.get("new_head_coach", False),
            "head_coach_name": hc.get("head_coach_name"),
            "new_offensive_coordinator": oc.get("new_offensive_coordinator", False),
            "offensive_coordinator_name": oc.get("offensive_coordinator_name"),
        }
    return combined


# Historical base rate this flag is built on: of the last 14 rookie QBs who
# averaged a top-15-among-QBs fantasy PPG in Year 1, 11 got WORSE in Year 2
# (defenses have a full year of film; a rookie's own improvement apparently
# doesn't outweigh that). This is a CAUTION signal, not a penalty --
# data/calculators.py decides how to weigh it against positive catalysts
# (new OC, better weapons, etc.).
QB_YEAR2_REGRESSION_TOP_N = 15


def build_qb_year2_regression_flags(season: int) -> Dict[str, dict]:
    """Flag players who were a rookie QB last season AND finished top-15
    among QBs in fantasy points per game -- the "sophomore slump" QB
    caution group. Keyed by `gsis_id` (this looks at the *prior* season's
    rookie class, so there's no `yahoo_id` to key on directly yet at the
    time this function runs -- `enrich_players_dataframe` re-keys it via
    each player's stable `gsis_id` from this season's roster).
    """
    prior_season = season - 1
    rosters_prior = load_seasonal_rosters(prior_season)
    weekly_prior = load_weekly_data(prior_season)

    qb_ppg = weekly_prior[weekly_prior["position"] == "QB"].groupby("player_id")["fantasy_points"].mean()
    top_n_gsis = set(qb_ppg.sort_values(ascending=False).head(QB_YEAR2_REGRESSION_TOP_N).index)

    rookie_qbs_prior_year = rosters_prior[
        (rosters_prior["position"] == "QB") & (rosters_prior["rookie_year"] == prior_season)
    ]

    flags: Dict[str, dict] = {}
    for _, row in rookie_qbs_prior_year.iterrows():
        gsis_id = row["player_id"]
        if gsis_id in top_n_gsis:
            flags[gsis_id] = {
                "qb_year2_regression_caution": True,
                "qb_year1_ppg": round(float(qb_ppg.get(gsis_id, 0.0)), 1),
            }
    return flags


def build_enrichment_lookup(
    season: int,
    through_week: Optional[int] = None,
    injury_week: Optional[int] = None,
    coordinator_csv_path: Optional[Path] = None,
    game_script_week: Optional[int] = None,
    key_by: str = "yahoo_id",
    roster_season: Optional[int] = None,
) -> Dict[str, dict]:
    """Build a ``{yahoo_id: {...enrichment fields...}}`` lookup for one season
    (or ``{gsis_id: {...}}`` if ``key_by="player_id"`` -- see that arg below).

    Args:
        season: The STATS season -- NFL season year (e.g. 2024) whose real
            played games back everything here that can only come from
            actual games (target_share, red_zone_share, snap share, game
            script, weather, injury reports/opportunity/durability). During
            a season, this is simply the current year -- but in January/
            February you're still in the *previous* year's season, so pass
            that explicitly.
        through_week: If given, only weeks up to and including this one are
            used to compute ``target_share``/points-per-game averages
            (useful for excluding future/unplayed weeks). Defaults to all
            available weeks.
        game_script_week: Week to pull Vegas game-script context for.
            Defaults (via ``build_game_script_lookup``) to the next
            upcoming game.
        key_by: ``"yahoo_id"`` (default, for joining onto real Yahoo
            player data -- only ~half of rostered players have one) or
            ``"player_id"`` (nflverse's own gsis ID, covering EVERY
            rostered player with zero Yahoo dependency -- see
            ``build_draft_board_pool()``, built for pre-draft research
            while Yahoo API access is still pending).
        roster_season: The CONTEXT season -- which team a player is on,
            whether they're a rookie THIS year, their real draft capital,
            and the current coaching situation. Defaults to `season` (the
            live, in-season case, where the two are naturally the same
            year) -- but a Yahoo-free tool built from a stale stats season
            (e.g. Draft Board falling back to 2024 stats because nflverse
            hasn't published 2025's yet) should pass the REAL current
            season here instead (`default_nfl_season()`), so a player's
            team/rookie status/draft slot and the league's coaching
            situation reflect today's real roster -- available immediately,
            unlike box scores -- rather than a year-plus-stale one. Real,
            concrete bug this fixes: without it, a genuine 2026 rookie
            checked against 2024's rookie class would never be flagged
            `is_rookie` at all, and "new offensive coordinator" would be
            answering "new for 2024" instead of "new for 2026."
        """
    roster_season = roster_season if roster_season is not None else season
    rosters = load_seasonal_rosters(roster_season)
    draft_picks = load_draft_picks(roster_season)
    weekly = load_weekly_data(season)
    if through_week is not None:
        weekly = weekly[weekly["week"] <= through_week]
    deep_share_by_gsis = load_deep_target_share_by_gsis(season)
    red_zone_share_by_gsis = load_red_zone_share_by_gsis(season)
    injury_opportunity_by_gsis = build_injury_opportunity_lookup(season, injury_week)
    injury_durability_by_gsis = build_season_injury_durability_lookup(season)
    coaching_change_by_team = build_coaching_change_lookup(roster_season, coordinator_csv_path)
    qb_year2_flags_by_gsis = build_qb_year2_regression_flags(season)
    te_snap_share_by_gsis = build_te_snap_share_lookup(season)
    offense_context_by_team = compute_team_offense_context(season).set_index("team")
    game_script_by_team = build_game_script_lookup(season, game_script_week)
    ngs_receiving_by_gsis = build_ngs_receiving_lookup(season)
    ngs_passing_by_gsis = build_ngs_passing_lookup(season)

    # Deferred import: api/weather.py imports load_schedules/_normalize_team_abbr
    # from this module at load time, so importing it back at module level here
    # would be circular. See api/weather.py's module docstring for why this
    # needs its own forecast lookup instead of reusing the schedule's own
    # temp/wind columns.
    from api.weather import build_game_weather_lookup

    weather_by_team = build_game_weather_lookup(season, game_script_week)

    # A real, discovered ID gap for the FRESHEST draft class: nflverse's
    # import_draft_picks()'s own `gsis_id` column lags behind
    # import_seasonal_rosters()'s `player_id` for players drafted this
    # year -- confirmed directly (every one of the 2026 class's 257 picks
    # carries a temp PFR-style ID like "LOV121782" in `gsis_id`, not the
    # standard "00-00XXXXX" gsis format rosters uses, while the prior
    # year's class is already fully reconciled: 256/257). Left as-is, this
    # would mean the newest, most roster_season-relevant rookies -- the
    # exact ones this project's `is_rookie`/`draft_capital` split from the
    # stats season was built to correctly flag -- never actually got a
    # `draft_capital` match. Both tables agree on a real, stable
    # `pfr_player_id`/`pfr_id` (confirmed directly: "LoveJe00" for
    # Jeremiyah Love in both), so bridge through that first and only fall
    # back to the draft table's own `gsis_id` when there's no pfr_id match
    # (older classes, or a player missing a pfr_id in one table).
    pfr_id_to_gsis = (
        rosters.dropna(subset=["pfr_id"]).drop_duplicates("pfr_id").set_index("pfr_id")["player_id"].to_dict()
    )
    draft_by_gsis: Dict[str, dict] = {}
    for _, row in draft_picks.iterrows():
        gsis_id_for_pick = pfr_id_to_gsis.get(row.get("pfr_player_id")) or row.get("gsis_id")
        if not gsis_id_for_pick or pd.isna(gsis_id_for_pick):
            continue
        draft_by_gsis[gsis_id_for_pick] = {"round": int(row["round"]), "pick": int(row["pick"])}
    target_share_by_gsis = weekly.groupby("player_id")["target_share"].mean().to_dict()
    ppg_by_gsis = weekly.groupby("player_id")["fantasy_points"].mean().to_dict()

    lookup: Dict[str, dict] = {}
    roster_rows = rosters if key_by == "player_id" else rosters.dropna(subset=["yahoo_id"])
    for _, row in roster_rows.iterrows():
        key = row["player_id"] if key_by == "player_id" else str(row["yahoo_id"])
        gsis_id = row["player_id"]
        team = _normalize_team_abbr(row.get("team"))
        injury_opp = injury_opportunity_by_gsis.get(gsis_id, {})
        coaching_change = coaching_change_by_team.get(team, {})
        qb_year2 = qb_year2_flags_by_gsis.get(gsis_id, {})
        offense_context = offense_context_by_team.loc[team] if team in offense_context_by_team.index else None
        game_script = game_script_by_team.get(team, {})
        ngs_rec = ngs_receiving_by_gsis.get(gsis_id, {})
        ngs_pass = ngs_passing_by_gsis.get(gsis_id, {})
        weather = weather_by_team.get(team, {})

        lookup[key] = {
            "is_rookie": bool(row.get("rookie_year") == roster_season),
            "draft_capital": draft_by_gsis.get(gsis_id),
            "target_share": target_share_by_gsis.get(gsis_id),
            "deep_target_share": deep_share_by_gsis.get(gsis_id),
            "red_zone_share": red_zone_share_by_gsis.get(gsis_id),
            "te_snap_share": te_snap_share_by_gsis.get(gsis_id),
            "team_pass_rate": offense_context["pass_rate"] if offense_context is not None else None,
            "team_pass_epa": offense_context["pass_epa"] if offense_context is not None else None,
            "game_script_spread": game_script.get("game_script_spread"),
            "game_script_total": game_script.get("game_script_total"),
            "game_script_implied_team_total": game_script.get("game_script_implied_team_total"),
            "ngs_separation": ngs_rec.get("ngs_separation"),
            "ngs_yac_above_expectation": ngs_rec.get("ngs_yac_above_expectation"),
            "ngs_time_to_throw": ngs_pass.get("ngs_time_to_throw"),
            "ngs_cpoe": ngs_pass.get("ngs_cpoe"),
            "game_wind_mph": weather.get("wind_mph"),
            "game_precip_probability": weather.get("precip_probability"),
            "game_is_dome": weather.get("game_is_dome"),
            "projected_points_by_week": estimate_projected_points_by_week(ppg_by_gsis.get(gsis_id)),
            "injury_opportunity": injury_opp.get("injury_opportunity", False),
            "injury_opportunity_ahead_player": injury_opp.get("injury_opportunity_ahead_player"),
            "injury_opportunity_ahead_status": injury_opp.get("injury_opportunity_ahead_status"),
            "weeks_flagged_out": injury_durability_by_gsis.get(gsis_id),
            "team_new_head_coach": coaching_change.get("new_head_coach", False),
            "team_head_coach_name": coaching_change.get("head_coach_name"),
            "team_new_offensive_coordinator": coaching_change.get("new_offensive_coordinator", False),
            "team_offensive_coordinator_name": coaching_change.get("offensive_coordinator_name"),
            "qb_year2_regression_caution": qb_year2.get("qb_year2_regression_caution", False),
            "qb_year1_ppg": qb_year2.get("qb_year1_ppg"),
        }
    return lookup


# Fields backfilled onto the players DataFrame by enrich_players_dataframe.
# Kept as a module constant so player_mapper.py's defaults and this list
# can't silently drift apart.
ENRICHMENT_FIELDS = (
    "is_rookie", "draft_capital", "target_share", "deep_target_share", "red_zone_share",
    "te_snap_share", "team_pass_rate", "team_pass_epa",
    "game_script_spread", "game_script_total", "game_script_implied_team_total",
    "ngs_separation", "ngs_yac_above_expectation", "ngs_time_to_throw", "ngs_cpoe",
    "game_wind_mph", "game_precip_probability", "game_is_dome",
    "projected_points_by_week", "injury_opportunity", "injury_opportunity_ahead_player",
    "injury_opportunity_ahead_status", "weeks_flagged_out", "team_new_head_coach", "team_head_coach_name",
    "team_new_offensive_coordinator", "team_offensive_coordinator_name",
    "qb_year2_regression_caution", "qb_year1_ppg",
)


def enrich_players_dataframe(
    players_df: pd.DataFrame,
    season: int,
    through_week: Optional[int] = None,
    injury_week: Optional[int] = None,
    coordinator_csv_path: Optional[Path] = None,
    game_script_week: Optional[int] = None,
    key_by: str = "yahoo_id",
    roster_season: Optional[int] = None,
) -> pd.DataFrame:
    """Fill in every field listed in ``ENRICHMENT_FIELDS`` on a players
    DataFrame (as produced by ``api/player_mapper.py``, or
    ``build_draft_board_pool()``'s Yahoo-independent pool), using
    nflverse data (plus a manual coordinator-change CSV, if present --
    see ``load_manual_coordinator_changes()``).

    Args:
        key_by: Passed straight through to ``build_enrichment_lookup()``
            -- ``"yahoo_id"`` (default) expects ``players_df["player_id"]``
            to hold Yahoo's player_id (yfpy's convention); ``"player_id"``
            expects it to hold nflverse's own gsis ID instead (what
            ``build_draft_board_pool()`` populates it with).
        roster_season: Passed straight through to
            ``build_enrichment_lookup()`` -- see its docstring for why a
            stale stats season (e.g. Draft Board falling back to 2024)
            shouldn't also decide team/rookie/draft-capital/coaching
            context, which real, current data already exists for.

    Players whose ``player_id`` has no match in that crosswalk are left
    with whatever defaults they already had (see module docstring).
    """
    lookup = build_enrichment_lookup(
        season, through_week, injury_week, coordinator_csv_path, game_script_week, key_by, roster_season
    )
    df = players_df.copy()

    for field in ENRICHMENT_FIELDS:
        if field not in df.columns:
            df[field] = None
        df[field] = df.apply(
            lambda row, f=field: lookup.get(str(row["player_id"]), {}).get(f, row.get(f)),
            axis=1,
        )

    return df


# --- Draft Board (zero Yahoo dependency) -------------------------------
DRAFT_BOARD_POSITIONS = ("QB", "RB", "WR", "TE")
_DRAFT_BOARD_STAT_KEYS = (
    "passing_yards", "passing_touchdowns", "interceptions", "rushing_yards", "rushing_touchdowns",
    "receptions", "receiving_yards", "receiving_touchdowns", "two_point_conversions", "fumbles_lost",
)


def build_draft_board_pool(
    season: int, positions: tuple = DRAFT_BOARD_POSITIONS, roster_season: Optional[int] = None
) -> pd.DataFrame:
    """A full draft-day player pool -- every real skill-position player on
    the CURRENT real roster (`roster_season`, defaulting to
    `default_nfl_season()` -- e.g. 2026), with `season`'s actual production
    (ready for `calculate_custom_value()` to score under THIS league's own
    weights, not nfl_data_py's PPR-flavored default) plus every enrichment
    signal this project already computes -- built with ZERO Yahoo
    dependency, keyed entirely by nflverse's own gsis `player_id` (see
    `enrich_players_dataframe(..., key_by="player_id")`).

    `season` and `roster_season` are deliberately separate: `season` is
    the last one with real played games to draw production from (real
    stats can't come from a season that hasn't happened yet), while
    `roster_season` is "who's actually on what team right now" -- a real,
    current fact available immediately, unlike box scores. These are
    routinely different years in practice: as of this writing,
    nflverse hasn't published full 2025 weekly stats yet (see
    `render_draft_board()`'s automatic one-year-back fallback in
    ui/dashboard.py), so `season` ends up being 2024 while `roster_season`
    is the real 2026 season being drafted for. Using the stale season for
    BOTH would mean showing a player's old 2024 team (wrong after any
    trade/free-agent move since) and -- more concretely -- silently
    failing to flag any real 2026 rookie as `is_rookie` at all, since
    they'd be checked against 2024's rookie class instead of 2026's. Team/
    rookie-status/draft-capital/coaching-situation all come from
    `roster_season` for exactly this reason (see
    `build_enrichment_lookup()`'s docstring); target share, red-zone
    share, and every other played-game signal still correctly come from
    `season`, since there's no substitute for that.

    Built for pre-draft research while Yahoo API access is pending. This
    is NOT a synthetic projection for the upcoming season on its own --
    see `api/fantasypros.py` for a real external projection where it's
    available. It's last completed season's real, actual performance
    under your league's scoring, adjusted by real signals about what's
    changed since (new offensive coordinators, coaching changes, injury-
    opened opportunity, rookie draft capital for players who had no NFL
    stats yet). Treat it as a serious, defensible starting point for
    ranking players -- not a finished cheat sheet that already knows
    about every offseason move (e.g. it can't model a free-agent signing
    changing a target competition -- see this module's docstring on that
    exact gap).

    Rookies with real draft capital but no NFL stats yet are still
    included, all-zero stats and all -- their signal here is
    `draft_capital` alone, which is exactly what `apply_rookie_bump()`
    is built to use.
    """
    roster_season = roster_season if roster_season is not None else default_nfl_season()
    rosters = load_seasonal_rosters(roster_season)
    rosters = rosters[rosters["position"].isin(positions)].drop_duplicates(subset=["player_id"])

    stat_totals = load_season_stat_totals(season).set_index("player_id")

    rows = []
    for _, row in rosters.iterrows():
        gsis_id = row["player_id"]
        player_stats = stat_totals.loc[gsis_id] if gsis_id in stat_totals.index else None
        stats_dict = {
            key: float(player_stats[key]) if player_stats is not None else 0.0
            for key in _DRAFT_BOARD_STAT_KEYS
        }

        rows.append({
            "player_key": f"nflverse.{gsis_id}",
            "player_id": gsis_id,
            # A real ID crosswalk (import_seasonal_rosters()'s own
            # yahoo_id column -- see this module's docstring), not a name
            # guess -- only ~half of rostered players have one. Lets
            # api/fantasypros.py's attach_fantasypros_data() join real
            # FantasyPros projections/rankings onto this Yahoo-free pool.
            "yahoo_id": row.get("yahoo_id"),
            "name": {
                "full": row.get("player_name") or gsis_id,
                # Prefer the public "football name" over the legal first
                # name -- nflverse's `first_name` is the player's legal
                # first name (e.g. "Rayne" for Dak Prescott, "DeKaylin" for
                # DK Metcalf), which doesn't match how anyone -- including
                # Yahoo's own cheat sheet -- actually refers to them. This
                # matters beyond display: `attach_yahoo_adp()` below keys
                # on this initial, and a legal-name initial silently broke
                # that match for every player with a real "football name".
                "first": row.get("football_name") or row.get("first_name") or "",
                "last": row.get("last_name") or "",
            },
            "editorial_team_abbr": _normalize_team_abbr(row.get("team")),
            "display_position": row["position"],
            "eligible_positions": [{"position": row["position"]}],
            "status": "",
            "percent_owned": None,
            "percent_owned_delta": None,
            "stats": stats_dict,
            "projected_points_by_week": {},
        })

    df = pd.DataFrame(rows)
    if df.empty:
        return df
    return enrich_players_dataframe(df, season, key_by="player_id", roster_season=roster_season)


def attach_sleeper_status_and_trending(players_df: pd.DataFrame) -> pd.DataFrame:
    """Overlays real, current `status` (Questionable/Doubtful/Out/IR/PUP/...)
    and `sleeper_trending_adds` from Sleeper's free API onto
    `build_draft_board_pool()`'s Yahoo-free pool, keyed by `player_id`
    (gsis ID -- see `api/external_sources.py`'s gsis-keyed Sleeper
    functions). This is what makes that pool usable as a genuine
    Yahoo-independent substitute for a live Yahoo `players_df`: without
    it, `status` is always `""` (IR Stash Targets would always be empty)
    and `sleeper_trending_adds` is always missing (one of Breakout Radar's
    signals never fires).

    Never raises -- a Sleeper fetch failure (network hiccup, schema
    change) degrades to leaving whatever `status`/`sleeper_trending_adds`
    the pool already had (its own `""`/``None`` defaults), logged, not
    surfaced as an error to the caller.
    """
    from api.external_sources import build_sleeper_gsis_trending_lookup, build_sleeper_injury_status_lookup

    df = players_df.copy()
    if "status" not in df.columns:
        df["status"] = ""
    if "sleeper_trending_adds" not in df.columns:
        df["sleeper_trending_adds"] = None

    try:
        status_lookup = build_sleeper_injury_status_lookup()
        trending_lookup = build_sleeper_gsis_trending_lookup()
    except Exception as exc:
        logger.warning("Sleeper fetch failed (%s) -- status/sleeper_trending_adds left as-is.", exc)
        return df

    player_ids = df["player_id"].astype(str)
    sleeper_status = player_ids.map(lambda pid: status_lookup.get(pid, {}).get("status"))
    df["status"] = sleeper_status.where(sleeper_status.notna() & (sleeper_status != ""), df["status"])
    sleeper_trending = player_ids.map(trending_lookup)
    df["sleeper_trending_adds"] = sleeper_trending.where(sleeper_trending.notna(), df["sleeper_trending_adds"])
    return df


# --- Yahoo premium draft reference (manual, name-matched -- see caveat) -
# data/yahoo_draft_reference.csv: manually transcribed from a real Yahoo
# Fantasy Plus premium "Cheat Sheet" PDF export for this exact league (the
# league owner's own account) -- Yahoo's real ADP (average draft position)
# and tier groupings, which this project otherwise has no source for (the
# "consensus baseline" gap this project's README flags FantasyPros/paid
# services as one way to fill -- this is a free, real, one-time snapshot
# of the same idea instead). Unlike every other join in this project, this
# has NO stable ID to cross-reference (a rendered PDF export, not an API)
# -- so this is name-matched, a real, documented exception to this
# project's usual "never guess by name" rule, made only because there's
# no alternative and the match is verified, not blind.
DEFAULT_YAHOO_DRAFT_REFERENCE_CSV = Path(__file__).resolve().parent.parent / "data" / "yahoo_draft_reference.csv"


def _normalize_last_name_for_match(last_name: str) -> str:
    """Lowercase, strip periods/hyphens/apostrophes, and strip a trailing
    Jr/Sr/II/III/IV suffix -- the reference CSV's PDF-extracted text glues
    these onto the last name with no space (e.g. "PittsSr.", "WalkerIII"),
    and nflverse's own `last_name` field doesn't consistently include them
    either, so both sides are normalized the same way before comparing."""
    name = last_name.lower()
    name = re.sub(r"[.\-'’]", "", name)
    name = re.sub(r"(jr|sr|iii|ii|iv)$", "", name)
    return name.strip()


def build_yahoo_adp_lookup(csv_path: Optional[Path] = None) -> Optional[Dict[str, dict]]:
    """Two lookup dicts built from ``data/yahoo_draft_reference.csv``,
    returned as ``{"exact": ..., "by_name_position": ...}`` -- or ``None``
    (never raises) if that file doesn't exist. This is an optional,
    refresh-when-you-feel-like-it signal (re-export a fresh cheat sheet PDF
    and re-transcribe when your league's ADP has moved), not a live feed.

    ``"exact"`` keys on ``(position, first_initial, normalized_last_name,
    team)`` -- the strict, no-ambiguity match.

    ``"by_name_position"`` keys on ``(position, first_initial,
    normalized_last_name)`` alone, mapped to a *list* of that name's
    entries. This exists because the reference CSV's team column reflects
    the team a player is on *right now* (a live, current cheat sheet),
    while the rest of the draft pool's team comes from a completed-season
    roster snapshot that can be a year or more stale -- real trades and
    free-agency moves in between (e.g. a WR traded mid-season) mean the two
    sides' team codes can legitimately disagree for the same real player.
    When a name+position has exactly one candidate here, that's a safe
    match despite the team mismatch; `attach_yahoo_adp()` falls back to it
    only when the strict team-matched lookup misses.
    """
    path = csv_path or DEFAULT_YAHOO_DRAFT_REFERENCE_CSV
    if not path.is_file():
        return None

    df = pd.read_csv(path)
    exact: Dict[tuple, dict] = {}
    by_name_position: Dict[tuple, list] = {}
    for _, row in df.iterrows():
        position = str(row["position"]).upper()
        first_initial = str(row["first_initial"]).upper()
        last_key = _normalize_last_name_for_match(str(row["last_name"]))
        team_key = _normalize_team_abbr(row["team"])
        payload = {
            "yahoo_adp": float(row["adp"]) if pd.notna(row.get("adp")) else None,
            "yahoo_tier": int(row["tier"]) if pd.notna(row.get("tier")) else None,
            "yahoo_position_rank": int(row["overall_rank"]) if pd.notna(row.get("overall_rank")) else None,
        }
        exact[(position, first_initial, last_key, team_key)] = payload
        by_name_position.setdefault((position, first_initial, last_key), []).append(payload)

    return {"exact": exact, "by_name_position": by_name_position}


def attach_yahoo_adp(players_df: pd.DataFrame, csv_path: Optional[Path] = None) -> pd.DataFrame:
    """Joins ``data/yahoo_draft_reference.csv``'s real Yahoo ADP/tier data
    onto ``players_df`` by (position, first initial, normalized last name,
    team) -- falling back to (position, first initial, last name) alone
    when that name+position is unambiguous (see `build_yahoo_adp_lookup`'s
    docstring on why team alone can't always be trusted). NAME-matched,
    not an ID crosswalk (see module-level comment above for why). A real,
    known limitation: this can silently miss a real match on a name
    variant it doesn't normalize away, or (rarely) collide two different
    players sharing an initial + last name + position. Treat the added
    columns as directionally useful, not a guaranteed-correct join -- and
    expect real, unavoidable misses for rookies who weren't on any roster
    in whatever season the rest of the pool's stats come from (this
    reference is for the *upcoming* draft; a completed-season stat pool
    inherently can't contain next year's incoming rookie class).

    Adds ``yahoo_adp`` (float, lower = drafted earlier), ``yahoo_tier``
    (int), and ``yahoo_position_rank`` (int, rank within position) --
    all ``None`` where no match was found.
    """
    lookup = build_yahoo_adp_lookup(csv_path)
    df = players_df.copy()
    if not lookup:
        df["yahoo_adp"] = None
        df["yahoo_tier"] = None
        df["yahoo_position_rank"] = None
        return df

    exact = lookup["exact"]
    by_name_position = lookup["by_name_position"]

    def _match(row: pd.Series) -> dict:
        name = row.get("name")
        first = name.get("first") if isinstance(name, dict) else None
        last = name.get("last") if isinstance(name, dict) else None
        if not first or not last:
            return {}
        position = str(row.get("display_position") or "").upper()
        first_initial = first[0].upper()
        last_key = _normalize_last_name_for_match(last)
        team_key = row.get("editorial_team_abbr")

        hit = exact.get((position, first_initial, last_key, team_key))
        if hit is not None:
            return hit

        candidates = by_name_position.get((position, first_initial, last_key))
        if candidates and len(candidates) == 1:
            return candidates[0]
        return {}

    matched = df.apply(_match, axis=1)
    df["yahoo_adp"] = matched.apply(lambda m: m.get("yahoo_adp"))
    df["yahoo_tier"] = matched.apply(lambda m: m.get("yahoo_tier"))
    df["yahoo_position_rank"] = matched.apply(lambda m: m.get("yahoo_position_rank"))
    return df
