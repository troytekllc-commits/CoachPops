"""Backfills the enrichment fields Yahoo's API doesn't provide -- rookie
status/draft capital, target share, deep-target share, red-zone share,
injury-opened opportunity, team coaching changes, a QB "sophomore slump"
caution flag, and a rough weekly projection baseline (see
``ENRICHMENT_FIELDS`` for the full, authoritative list) -- using
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
from functools import lru_cache
from pathlib import Path
from typing import Dict, Optional

import pandas as pd

# Manually maintained OC/DC-hire CSV -- see load_manual_coordinator_changes()
# below for why this is manual instead of pulled from an API.
DEFAULT_COORDINATOR_CHANGES_CSV = Path(__file__).resolve().parent.parent / "data" / "coaching_changes.csv"

# Weeks used to build the flat rest-of-season baseline (see module docstring).
PROJECTION_WEEKS = range(1, 18)


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

    return nfl.import_weekly_data(
        [season],
        columns=["player_id", "player_display_name", "recent_team", "week", "position", "target_share", "fantasy_points_ppr"],
    )


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


@lru_cache(maxsize=8)
def load_schedules(season: int) -> pd.DataFrame:
    import nfl_data_py as nfl

    return nfl.import_schedules([season])


# Team codes differ across nflverse's own datasets, let alone Yahoo's
# `editorial_team_abbr` -- normalize everything to this convention before
# joining team-level signals. `import_draft_picks()`'s `team` column uses
# PFR-style long codes (GNB/KAN/LVR/NOR/NWE/SFO/TAM) that don't match
# `import_schedules()`/play-by-play's short codes (GB/KC/LV/NO/NE/SF/TB) at
# all -- confirmed by a real duplicate-team bug in api/oline_analytics.py's
# rankings (New England showed up as both "NWE" and "NE") before this map
# was extended to cover them.
TEAM_ABBR_NORMALIZATION = {
    "LA": "LAR", "OAK": "LV", "SD": "LAC", "STL": "LAR", "WSH": "WAS",
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

    qb_ppg = weekly_prior[weekly_prior["position"] == "QB"].groupby("player_id")["fantasy_points_ppr"].mean()
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
) -> Dict[str, dict]:
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
    red_zone_share_by_gsis = load_red_zone_share_by_gsis(season)
    injury_opportunity_by_gsis = build_injury_opportunity_lookup(season, injury_week)
    coaching_change_by_team = build_coaching_change_lookup(season, coordinator_csv_path)
    qb_year2_flags_by_gsis = build_qb_year2_regression_flags(season)
    te_snap_share_by_gsis = build_te_snap_share_lookup(season)
    offense_context_by_team = compute_team_offense_context(season).set_index("team")

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
        team = _normalize_team_abbr(row.get("team"))
        injury_opp = injury_opportunity_by_gsis.get(gsis_id, {})
        coaching_change = coaching_change_by_team.get(team, {})
        qb_year2 = qb_year2_flags_by_gsis.get(gsis_id, {})
        offense_context = offense_context_by_team.loc[team] if team in offense_context_by_team.index else None

        lookup[yahoo_id] = {
            "is_rookie": bool(row.get("rookie_year") == season),
            "draft_capital": draft_by_gsis.get(gsis_id),
            "target_share": target_share_by_gsis.get(gsis_id),
            "deep_target_share": deep_share_by_gsis.get(gsis_id),
            "red_zone_share": red_zone_share_by_gsis.get(gsis_id),
            "te_snap_share": te_snap_share_by_gsis.get(gsis_id),
            "team_pass_rate": offense_context["pass_rate"] if offense_context is not None else None,
            "team_pass_epa": offense_context["pass_epa"] if offense_context is not None else None,
            "projected_points_by_week": estimate_projected_points_by_week(ppg_by_gsis.get(gsis_id)),
            "injury_opportunity": injury_opp.get("injury_opportunity", False),
            "injury_opportunity_ahead_player": injury_opp.get("injury_opportunity_ahead_player"),
            "injury_opportunity_ahead_status": injury_opp.get("injury_opportunity_ahead_status"),
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
    "projected_points_by_week", "injury_opportunity", "injury_opportunity_ahead_player",
    "injury_opportunity_ahead_status", "team_new_head_coach", "team_head_coach_name",
    "team_new_offensive_coordinator", "team_offensive_coordinator_name",
    "qb_year2_regression_caution", "qb_year1_ppg",
)


def enrich_players_dataframe(
    players_df: pd.DataFrame,
    season: int,
    through_week: Optional[int] = None,
    injury_week: Optional[int] = None,
    coordinator_csv_path: Optional[Path] = None,
) -> pd.DataFrame:
    """Fill in every field listed in ``ENRICHMENT_FIELDS`` on a Yahoo
    players DataFrame (as produced by ``api/player_mapper.py``), using
    nflverse data joined via the ``yahoo_id`` crosswalk (plus a manual
    coordinator-change CSV, if present -- see
    ``load_manual_coordinator_changes()``).

    Players whose ``player_id`` has no match in that crosswalk are left
    with whatever defaults they already had (see module docstring).
    """
    lookup = build_enrichment_lookup(season, through_week, injury_week, coordinator_csv_path)
    df = players_df.copy()

    for field in ENRICHMENT_FIELDS:
        if field not in df.columns:
            df[field] = None
        df[field] = df.apply(
            lambda row, f=field: lookup.get(str(row["player_id"]), {}).get(f, row.get(f)),
            axis=1,
        )

    return df
