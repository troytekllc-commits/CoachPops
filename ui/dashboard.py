"""CoachPops Fantasy Football Manager -- Streamlit dashboard.

Run with (from the project root, inside the activated venv):
    streamlit run app.py

Data source: mock data shaped to mimic Yahoo Fantasy's real ``/players``
endpoint response (nested dict/list columns for ``name``,
``eligible_positions``, etc., instead of a flat schema), OR real data
pulled live via ``YahooAuthManager`` + ``api/player_mapper.py`` once
``private.json`` exists at the project root. A sidebar toggle lets you pick
between them; see the module docstring in ``api/player_mapper.py`` for
which calculators work fully against real (unbackfilled) Yahoo data today
vs. which need extra enrichment.

IMPORTANT: run ``python scripts/yahoo_login.py`` once from a terminal
*before* selecting "Live Yahoo data" here -- that's where the one-time
interactive browser OAuth handshake belongs, not inside a Streamlit rerun.
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from typing import Optional

import altair as alt
import pandas as pd
import streamlit as st

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.append(str(PROJECT_ROOT))

from data.calculators import (  # noqa: E402
    DEFAULT_ROSTER_REQUIREMENTS,
    DEFAULT_SCORING_SETTINGS,
    apply_rookie_bump,
    assess_team_needs,
    build_priority_board,
    calculate_qb_floor,
    compute_replacement_level,
    evaluate_wr_scarcity,
    find_breakout_signals,
    find_free_agent_upgrades,
    find_ir_stashes,
    find_te_difference_makers,
    find_trade_candidates,
)


# --- Mock league (Free Agent Suggestions & Trade Finder) -------------------
# Both features need EVERY team's roster, not just the waiver-wire pool
# build_mock_players_df() provides -- something only real Yahoo access can
# give (via get_rosters() for every team_id), so this generates a
# procedural mock league instead. Deliberately procedural rather than
# hand-authored personas like build_mock_players_df()'s single pool:
# hand-writing ~10 full rosters would dwarf the actual feature code, and
# these two calculators care about relative team strength per position,
# not any one player's story.
MOCK_LEAGUE_NUM_TEAMS = 10
MOCK_LEAGUE_MY_TEAM_ID = "team_1"
MOCK_LEAGUE_TEAM_NAMES = [
    "Gridiron Gurus", "Waiver Wizards", "Blitz Brigade", "End Zone Elites",
    "Fantasy Fanatics", "Touchdown Titans", "Playoff Predators", "Redzone Raiders",
    "Comeback Kids", "Dynasty Dominators",
]
MOCK_LEAGUE_ROSTER_COUNTS = {"QB": 2, "RB": 4, "WR": 5, "TE": 2}

# Per-position, per-tier baseline stat lines (tier 0 = best on a team at
# that position, descending). Deliberately simple and monotonic -- this
# is demo data to exercise the need/surplus/trade-matching logic, not
# meant to resemble any real player.
_MOCK_LEAGUE_TIER_STATS = {
    "QB": [
        {"passing_yards": 4200, "passing_touchdowns": 32, "interceptions": 9, "rushing_yards": 250, "rushing_touchdowns": 3},
        {"passing_yards": 3300, "passing_touchdowns": 20, "interceptions": 12, "rushing_yards": 80, "rushing_touchdowns": 1},
    ],
    "RB": [
        {"rushing_yards": 1300, "rushing_touchdowns": 11, "receptions": 40, "receiving_yards": 350},
        {"rushing_yards": 950, "rushing_touchdowns": 7, "receptions": 30, "receiving_yards": 220},
        {"rushing_yards": 600, "rushing_touchdowns": 4, "receptions": 20, "receiving_yards": 150},
        {"rushing_yards": 300, "rushing_touchdowns": 2, "receptions": 10, "receiving_yards": 70},
    ],
    "WR": [
        {"receptions": 90, "receiving_yards": 1250, "receiving_touchdowns": 9},
        {"receptions": 70, "receiving_yards": 950, "receiving_touchdowns": 6},
        {"receptions": 55, "receiving_yards": 700, "receiving_touchdowns": 4},
        {"receptions": 35, "receiving_yards": 450, "receiving_touchdowns": 2},
        {"receptions": 20, "receiving_yards": 250, "receiving_touchdowns": 1},
    ],
    "TE": [
        {"receptions": 65, "receiving_yards": 800, "receiving_touchdowns": 7},
        {"receptions": 30, "receiving_yards": 320, "receiving_touchdowns": 2},
    ],
}
_STAT_KEYS = (
    "passing_yards", "passing_touchdowns", "interceptions", "rushing_yards", "rushing_touchdowns",
    "receptions", "receiving_yards", "receiving_touchdowns", "two_point_conversions", "fumbles_lost",
)
# Every enrichment field build_priority_board()'s underlying calculators
# check for -- defaulted to None/False here (no enrichment story for this
# mock league; team strength comes entirely from the tier-based stats
# above, which is the axis Free Agent Suggestions/Trade Finder actually
# care about) so nothing downstream hits a missing-column KeyError.
_MOCK_LEAGUE_ENRICHMENT_DEFAULTS = {
    "is_rookie": False, "draft_capital": None, "target_share": None, "deep_target_share": None,
    "red_zone_share": None, "te_snap_share": None, "team_pass_rate": None, "team_pass_epa": None,
    "game_script_spread": None, "game_script_total": None, "game_script_implied_team_total": None,
    "ngs_separation": None, "ngs_yac_above_expectation": None, "ngs_time_to_throw": None, "ngs_cpoe": None,
    "game_wind_mph": None, "game_precip_probability": None, "game_is_dome": None, "sleeper_trending_adds": None,
    "injury_opportunity": False, "injury_opportunity_ahead_player": None, "injury_opportunity_ahead_status": None,
    "weeks_flagged_out": None,
    "team_new_head_coach": False, "team_head_coach_name": None, "team_new_offensive_coordinator": False,
    "team_offensive_coordinator_name": None, "qb_year2_regression_caution": False, "qb_year1_ppg": None,
    "percent_owned": None, "percent_owned_delta": None,
}


def build_mock_league_rosters(num_teams: int = MOCK_LEAGUE_NUM_TEAMS, seed: int = 42) -> pd.DataFrame:
    """A full mock league -- `num_teams` teams, each with a roster -- for
    exercising Free Agent Suggestions and Trade Finder. `MOCK_LEAGUE_MY_TEAM_ID`
    ("team_1") is always "your" team.

    Team strength is deliberately varied by shuffling each team's tier
    assignment per position with a fixed seed (reproducible across
    reruns -- not a different mock league every time the page loads), so
    some teams end up positionally stacked and others thin, giving the
    need/surplus matching something real to find.
    """
    import random

    rng = random.Random(seed)
    rows = []
    player_counter = 0

    # Deliberate imbalance for team_1 ("your" team) and team_2, so Free
    # Agent Suggestions/Trade Finder have a guaranteed, clear scenario to
    # find -- with every team drawing the same roster COUNTS from the
    # same tier pool, a pure random shuffle clusters every team's surplus/
    # need within about +/-0.5 of neutral (confirmed while testing this),
    # rarely producing the kind of clear two-way fit worth demoing. team_1
    # is RB-stacked/WR-thin; team_2 is the mirror image (WR-stacked/
    # RB-thin) -- a textbook complementary trade. Every other team still
    # uses the random shuffle below for variety.
    tier_overrides = {
        MOCK_LEAGUE_MY_TEAM_ID: {"RB": [0, 0, 1, 1], "WR": [3, 4, 4, 4, 4]},
        "team_2": {"RB": [3, 3, 3, 2], "WR": [0, 0, 1, 1, 1]},
    }

    for team_num in range(1, num_teams + 1):
        team_id = f"team_{team_num}"
        team_name = MOCK_LEAGUE_TEAM_NAMES[(team_num - 1) % len(MOCK_LEAGUE_TEAM_NAMES)]

        for position, count in MOCK_LEAGUE_ROSTER_COUNTS.items():
            forced_tiers = tier_overrides.get(team_id, {}).get(position)
            if forced_tiers:
                tiers = forced_tiers
            else:
                tiers = list(range(len(_MOCK_LEAGUE_TIER_STATS[position])))
                rng.shuffle(tiers)  # this team's players don't all land in tier order
            for slot in range(count):
                player_counter += 1
                tier = tiers[slot % len(tiers)]
                jitter = rng.uniform(0.85, 1.15)  # a little spread so same-tier players aren't identical
                stats = {k: round(v * jitter) for k, v in _MOCK_LEAGUE_TIER_STATS[position][tier].items()}
                for key in _STAT_KEYS:
                    stats.setdefault(key, 0)

                row = {
                    "player_key": f"449.p.mock{player_counter}",
                    "player_id": f"mock{player_counter}",
                    "team_id": team_id,
                    "team_name": team_name,
                    "name": {
                        "full": f"{team_name.split()[0]} {position}{slot + 1}",
                        "first": team_name.split()[0], "last": f"{position}{slot + 1}",
                    },
                    "editorial_team_abbr": rng.choice(["KC", "SF", "BUF", "PHI", "DAL", "MIA", "DET", "BAL"]),
                    "display_position": position,
                    "eligible_positions": [{"position": position}],
                    "status": "",
                    "stats": stats,
                    "projected_points_by_week": {},
                }
                row.update(_MOCK_LEAGUE_ENRICHMENT_DEFAULTS)
                rows.append(row)

    return pd.DataFrame(rows)


def _weekly_projection(early_avg: float, late_avg: float) -> dict:
    """Deterministic weeks 1-18 projection dict: `early_avg` through week 9,
    `late_avg` from week 10 (back half) on, with a small fixed wiggle so the
    numbers don't look perfectly flat. Matches the real 18-week NFL regular
    season -- see api/nfl_enrichment.py's PROJECTION_WEEKS."""
    projection = {}
    for week in range(1, 19):
        base = late_avg if week >= 10 else early_avg
        wiggle = (week % 3) * 0.4 - 0.4
        projection[str(week)] = round(base + wiggle, 1)
    return projection


def build_mock_players_df() -> pd.DataFrame:
    """Mock waiver-wire player pool, schema-matched to Yahoo's `/players` payload."""
    players = [
        {
            "player_key": "449.p.100001",
            "player_id": "100001",
            "name": {"full": "Justin Scrambler", "first": "Justin", "last": "Scrambler"},
            "editorial_team_abbr": "CHI",
            "display_position": "QB",
            "eligible_positions": [{"position": "QB"}],
            "status": "",
            "is_rookie": False,
            "draft_capital": None,
            "target_share": None,
            "deep_target_share": None,
            "stats": {
                "passing_yards": 2850, "passing_touchdowns": 17, "interceptions": 9,
                "rushing_yards": 657, "rushing_touchdowns": 8, "receptions": 0,
                "receiving_yards": 0, "receiving_touchdowns": 0,
                "two_point_conversions": 1, "fumbles_lost": 3,
            },
            "projected_points_by_week": _weekly_projection(15.0, 16.0),
        },
        {
            "player_key": "449.p.100002",
            "player_id": "100002",
            "name": {"full": "Pocket Palmer", "first": "Pocket", "last": "Palmer"},
            "editorial_team_abbr": "LAC",
            "display_position": "QB",
            "eligible_positions": [{"position": "QB"}],
            "status": "",
            "is_rookie": False,
            "draft_capital": None,
            "target_share": None,
            "deep_target_share": None,
            "stats": {
                "passing_yards": 4100, "passing_touchdowns": 28, "interceptions": 10,
                "rushing_yards": 45, "rushing_touchdowns": 0, "receptions": 0,
                "receiving_yards": 0, "receiving_touchdowns": 0,
                "two_point_conversions": 0, "fumbles_lost": 1,
            },
            "projected_points_by_week": _weekly_projection(18.0, 17.0),
        },
        {
            "player_key": "449.p.100003",
            "player_id": "100003",
            "name": {"full": "Marcus Freshman", "first": "Marcus", "last": "Freshman"},
            "editorial_team_abbr": "DET",
            "display_position": "RB",
            "eligible_positions": [{"position": "RB"}],
            "status": "",
            "is_rookie": True,
            "draft_capital": {"round": 1, "pick": 9},
            "target_share": None,
            "deep_target_share": None,
            "stats": {
                "passing_yards": 0, "passing_touchdowns": 0, "interceptions": 0,
                "rushing_yards": 310, "rushing_touchdowns": 3, "receptions": 18,
                "receiving_yards": 120, "receiving_touchdowns": 1,
                "two_point_conversions": 0, "fumbles_lost": 1,
            },
            "projected_points_by_week": _weekly_projection(6.0, 13.0),
        },
        {
            "player_key": "449.p.100004",
            "player_id": "100004",
            "name": {"full": "Cole Sophomore", "first": "Cole", "last": "Sophomore"},
            "editorial_team_abbr": "NYG",
            "display_position": "RB",
            "eligible_positions": [{"position": "RB"}],
            "status": "",
            "is_rookie": True,
            "draft_capital": {"round": 3, "pick": 22},
            "target_share": None,
            "deep_target_share": None,
            "stats": {
                "passing_yards": 0, "passing_touchdowns": 0, "interceptions": 0,
                "rushing_yards": 180, "rushing_touchdowns": 1, "receptions": 9,
                "receiving_yards": 70, "receiving_touchdowns": 0,
                "two_point_conversions": 0, "fumbles_lost": 0,
            },
            "projected_points_by_week": _weekly_projection(4.0, 9.0),
        },
        {
            "player_key": "449.p.100005",
            "player_id": "100005",
            "name": {"full": "Dusty Udfa", "first": "Dusty", "last": "Udfa"},
            "editorial_team_abbr": "ARI",
            "display_position": "RB",
            "eligible_positions": [{"position": "RB"}],
            "status": "",
            "is_rookie": True,
            "draft_capital": None,
            "target_share": None,
            "deep_target_share": None,
            "stats": {
                "passing_yards": 0, "passing_touchdowns": 0, "interceptions": 0,
                "rushing_yards": 95, "rushing_touchdowns": 1, "receptions": 3,
                "receiving_yards": 20, "receiving_touchdowns": 0,
                "two_point_conversions": 0, "fumbles_lost": 0,
            },
            "projected_points_by_week": _weekly_projection(3.0, 4.5),
        },
        {
            "player_key": "449.p.100006",
            "player_id": "100006",
            "name": {"full": "Rico Rookie", "first": "Rico", "last": "Rookie"},
            "editorial_team_abbr": "NE",
            "display_position": "WR",
            "eligible_positions": [{"position": "WR"}],
            "status": "",
            "is_rookie": True,
            "draft_capital": {"round": 1, "pick": 4},
            "target_share": 0.14,
            "deep_target_share": 0.35,
            "stats": {
                "passing_yards": 0, "passing_touchdowns": 0, "interceptions": 0,
                "rushing_yards": 0, "rushing_touchdowns": 0, "receptions": 22,
                "receiving_yards": 310, "receiving_touchdowns": 2,
                "two_point_conversions": 0, "fumbles_lost": 0,
            },
            "projected_points_by_week": _weekly_projection(5.0, 11.5),
        },
        {
            "player_key": "449.p.100007",
            "player_id": "100007",
            "name": {"full": "Steady Eddie", "first": "Steady", "last": "Eddie"},
            "editorial_team_abbr": "MIN",
            "display_position": "WR",
            "eligible_positions": [{"position": "WR"}],
            "status": "",
            "is_rookie": False,
            "draft_capital": None,
            "target_share": 0.24,
            "deep_target_share": 0.08,
            "stats": {
                "passing_yards": 0, "passing_touchdowns": 0, "interceptions": 0,
                "rushing_yards": 0, "rushing_touchdowns": 0, "receptions": 58,
                "receiving_yards": 620, "receiving_touchdowns": 3,
                "two_point_conversions": 0, "fumbles_lost": 0,
            },
            "projected_points_by_week": _weekly_projection(11.0, 11.0),
        },
        {
            "player_key": "449.p.100008",
            "player_id": "100008",
            "name": {"full": "Boom Bust Barry", "first": "Boom Bust", "last": "Barry"},
            "editorial_team_abbr": "LV",
            "display_position": "WR",
            "eligible_positions": [{"position": "WR"}],
            "status": "",
            "is_rookie": False,
            "draft_capital": None,
            "target_share": 0.22,
            "deep_target_share": 0.42,
            "stats": {
                "passing_yards": 0, "passing_touchdowns": 0, "interceptions": 0,
                "rushing_yards": 0, "rushing_touchdowns": 0, "receptions": 34,
                "receiving_yards": 540, "receiving_touchdowns": 6,
                "two_point_conversions": 0, "fumbles_lost": 0,
            },
            "projected_points_by_week": _weekly_projection(10.5, 10.5),
        },
        {
            "player_key": "449.p.100009",
            "player_id": "100009",
            "name": {"full": "Marginal Moe", "first": "Marginal", "last": "Moe"},
            "editorial_team_abbr": "JAX",
            "display_position": "WR",
            "eligible_positions": [{"position": "WR"}],
            "status": "",
            "is_rookie": False,
            "draft_capital": None,
            "target_share": 0.10,
            "deep_target_share": 0.15,
            "stats": {
                "passing_yards": 0, "passing_touchdowns": 0, "interceptions": 0,
                "rushing_yards": 0, "rushing_touchdowns": 0, "receptions": 20,
                "receiving_yards": 210, "receiving_touchdowns": 1,
                "two_point_conversions": 0, "fumbles_lost": 0,
            },
            "projected_points_by_week": _weekly_projection(4.0, 4.0),
        },
        {
            "player_key": "449.p.100010",
            "player_id": "100010",
            "name": {"full": "Ironman Ivan", "first": "Ironman", "last": "Ivan"},
            "editorial_team_abbr": "KC",
            "display_position": "RB",
            "eligible_positions": [{"position": "RB"}],
            "status": "IR",
            "is_rookie": False,
            "draft_capital": None,
            "target_share": None,
            "deep_target_share": None,
            "stats": {
                "passing_yards": 0, "passing_touchdowns": 0, "interceptions": 0,
                "rushing_yards": 220, "rushing_touchdowns": 2, "receptions": 12,
                "receiving_yards": 90, "receiving_touchdowns": 0,
                "two_point_conversions": 0, "fumbles_lost": 1,
            },
            "projected_points_by_week": _weekly_projection(0.0, 11.0),
        },
        {
            "player_key": "449.p.100011",
            "player_id": "100011",
            "name": {"full": "Wounded Wes", "first": "Wounded", "last": "Wes"},
            "editorial_team_abbr": "PHI",
            "display_position": "WR",
            "eligible_positions": [{"position": "WR"}],
            "status": "O",
            "is_rookie": False,
            "draft_capital": None,
            "target_share": 0.19,
            "deep_target_share": 0.12,
            "stats": {
                "passing_yards": 0, "passing_touchdowns": 0, "interceptions": 0,
                "rushing_yards": 0, "rushing_touchdowns": 0, "receptions": 30,
                "receiving_yards": 410, "receiving_touchdowns": 3,
                "two_point_conversions": 0, "fumbles_lost": 0,
            },
            "projected_points_by_week": _weekly_projection(0.0, 10.5),
        },
        {
            "player_key": "449.p.100012",
            "player_id": "100012",
            "name": {"full": "PUP Pete", "first": "PUP", "last": "Pete"},
            "editorial_team_abbr": "CIN",
            "display_position": "TE",
            "eligible_positions": [{"position": "TE"}],
            "status": "PUP",
            "is_rookie": False,
            "draft_capital": None,
            "target_share": None,
            "deep_target_share": None,
            "stats": {
                "passing_yards": 0, "passing_touchdowns": 0, "interceptions": 0,
                "rushing_yards": 0, "rushing_touchdowns": 0, "receptions": 8,
                "receiving_yards": 90, "receiving_touchdowns": 1,
                "two_point_conversions": 0, "fumbles_lost": 0,
            },
            "projected_points_by_week": _weekly_projection(0.0, 5.0),
        },
        {
            "player_key": "449.p.100013",
            "player_id": "100013",
            "name": {"full": "Waiver Wendell", "first": "Waiver", "last": "Wendell"},
            "editorial_team_abbr": "CAR",
            "display_position": "RB",
            "eligible_positions": [{"position": "RB"}],
            "status": "",
            "is_rookie": False,
            "draft_capital": None,
            "target_share": None,
            "deep_target_share": None,
            "stats": {
                "passing_yards": 0, "passing_touchdowns": 0, "interceptions": 0,
                "rushing_yards": 410, "rushing_touchdowns": 4, "receptions": 22,
                "receiving_yards": 150, "receiving_touchdowns": 1,
                "two_point_conversions": 0, "fumbles_lost": 1,
            },
            "projected_points_by_week": _weekly_projection(8.0, 8.5),
        },
        {
            "player_key": "449.p.100014",
            "player_id": "100014",
            "name": {"full": "Solid Sam", "first": "Solid", "last": "Sam"},
            "editorial_team_abbr": "BUF",
            "display_position": "TE",
            "eligible_positions": [{"position": "TE"}],
            "status": "",
            "is_rookie": False,
            "draft_capital": None,
            "target_share": 0.12,
            "deep_target_share": 0.05,
            "stats": {
                "passing_yards": 0, "passing_touchdowns": 0, "interceptions": 0,
                "rushing_yards": 0, "rushing_touchdowns": 0, "receptions": 40,
                "receiving_yards": 380, "receiving_touchdowns": 3,
                "two_point_conversions": 0, "fumbles_lost": 0,
            },
            "projected_points_by_week": _weekly_projection(7.0, 7.5),
        },
    ]

    # Fill in the Breakout Radar / enrichment fields every record needs, so
    # find_breakout_signals() (and the other calculators) never hit a
    # missing-column error -- then layer in a few deliberate scenarios below
    # so every signal has at least one mock player that lights it up.
    for player in players:
        player.setdefault("red_zone_share", None)
        player.setdefault("te_snap_share", None)
        player.setdefault("team_pass_rate", None)
        player.setdefault("team_pass_epa", None)
        player.setdefault("game_script_spread", None)
        player.setdefault("game_script_total", None)
        player.setdefault("game_script_implied_team_total", None)
        player.setdefault("ngs_separation", None)
        player.setdefault("ngs_yac_above_expectation", None)
        player.setdefault("ngs_time_to_throw", None)
        player.setdefault("ngs_cpoe", None)
        player.setdefault("game_wind_mph", None)
        player.setdefault("game_precip_probability", None)
        player.setdefault("game_is_dome", None)
        player.setdefault("sleeper_trending_adds", None)
        player.setdefault("injury_opportunity", False)
        player.setdefault("injury_opportunity_ahead_player", None)
        player.setdefault("injury_opportunity_ahead_status", None)
        player.setdefault("weeks_flagged_out", None)
        player.setdefault("team_new_head_coach", False)
        player.setdefault("team_head_coach_name", None)
        player.setdefault("team_new_offensive_coordinator", False)
        player.setdefault("team_offensive_coordinator_name", None)
        player.setdefault("qb_year2_regression_caution", False)
        player.setdefault("qb_year1_ppg", None)
        player.setdefault("percent_owned", None)
        player.setdefault("percent_owned_delta", None)

    by_id = {p["player_id"]: p for p in players}

    # New offensive coordinator: Steady Eddie's team got a new play-caller.
    by_id["100007"]["team_new_offensive_coordinator"] = True
    by_id["100007"]["team_offensive_coordinator_name"] = "Pat Freshstart"

    # New head coach (no OC signal): Justin Scrambler's team fired its HC.
    by_id["100001"]["team_new_head_coach"] = True
    by_id["100001"]["team_head_coach_name"] = "Ben Rebuild"

    # Draft-capital-undersold rookie: Dusty Udfa went undrafted but is
    # already earning real red-zone work.
    by_id["100005"]["red_zone_share"] = 0.16

    # Earned red-zone role, stacking on top of the existing rookie-bump signal.
    by_id["100003"]["red_zone_share"] = 0.24

    # Rising ownership vs. already-meaningfully-owned-elsewhere.
    by_id["100013"]["percent_owned"] = 9.0
    by_id["100013"]["percent_owned_delta"] = 6.0  # climbing fast
    by_id["100009"]["percent_owned"] = 21.0
    by_id["100009"]["percent_owned_delta"] = 0.5  # already meaningfully owned, flat

    # QB "sophomore slump" caution: Pocket Palmer was a top-15-PPG rookie last year.
    by_id["100002"]["qb_year2_regression_caution"] = True
    by_id["100002"]["qb_year1_ppg"] = 19.4

    # TE Difference-Maker Finder scenarios: give the existing two TEs a
    # snap-share/team-context profile, and add a third so the >=3-TE
    # percentile threshold has something to rank against.
    by_id["100014"]["te_snap_share"] = 0.62  # Solid Sam: real but modest receiving role
    by_id["100014"]["team_pass_rate"] = 0.56
    by_id["100014"]["team_pass_epa"] = 0.05
    by_id["100012"]["te_snap_share"] = 0.30  # PUP Pete: mostly a blocking TE, on top of being hurt
    by_id["100012"]["team_pass_rate"] = 0.52
    by_id["100012"]["team_pass_epa"] = -0.05
    by_id["100014"]["ngs_separation"] = 2.4  # Solid Sam: modest separation
    by_id["100012"]["ngs_separation"] = 1.6  # PUP Pete: below-average separation

    # Favorable Vegas game script: Waiver Wendell's team is implied for a big day.
    by_id["100013"]["game_script_spread"] = 6.5
    by_id["100013"]["game_script_total"] = 48.0
    by_id["100013"]["game_script_implied_team_total"] = 27.25

    # Trending across Sleeper (market-wide signal, independent of this Yahoo league).
    by_id["100009"]["sleeper_trending_adds"] = 8200  # Marginal Moe -- the market disagrees with his name

    # QB Konami Code: give both mock QBs a CPOE reading.
    by_id["100001"]["ngs_cpoe"] = 1.8   # Justin Scrambler: solid accuracy over expectation
    by_id["100002"]["ngs_cpoe"] = -2.1  # Pocket Palmer: high volume, but below expectation

    # High-wind caution: Boom Bust Barry's game has a rough forecast --
    # exactly the kind of added variance his profile doesn't need.
    by_id["100008"]["game_wind_mph"] = 22.0
    by_id["100008"]["game_precip_probability"] = 0.65
    by_id["100008"]["game_is_dome"] = False

    # Dome game, for contrast: Rico Rookie's game has no weather risk at all.
    by_id["100006"]["game_wind_mph"] = None
    by_id["100006"]["game_precip_probability"] = None
    by_id["100006"]["game_is_dome"] = True

    # Injury-opened opportunity: a backup WR behind the already-injured Wounded Wes.
    players.append({
        "player_key": "449.p.100015",
        "player_id": "100015",
        "name": {"full": "Backup Barney", "first": "Backup", "last": "Barney"},
        "editorial_team_abbr": "PHI",
        "display_position": "WR",
        "eligible_positions": [{"position": "WR"}],
        "status": "",
        "is_rookie": False,
        "draft_capital": None,
        "target_share": 0.08,
        "deep_target_share": 0.10,
        "red_zone_share": None,
        "te_snap_share": None,
        "team_pass_rate": None,
        "team_pass_epa": None,
        "game_script_spread": None,
        "game_script_total": None,
        "game_script_implied_team_total": None,
        "ngs_separation": None,
        "ngs_yac_above_expectation": None,
        "ngs_time_to_throw": None,
        "ngs_cpoe": None,
        "game_wind_mph": None,
        "game_precip_probability": None,
        "game_is_dome": None,
        "sleeper_trending_adds": None,
        "injury_opportunity": True,
        "injury_opportunity_ahead_player": "Wounded Wes",
        "injury_opportunity_ahead_status": "O",
        "weeks_flagged_out": None,
        "team_new_head_coach": False,
        "team_head_coach_name": None,
        "team_new_offensive_coordinator": False,
        "team_offensive_coordinator_name": None,
        "qb_year2_regression_caution": False,
        "qb_year1_ppg": None,
        "percent_owned": 2.0,
        "percent_owned_delta": 0.0,
        "stats": {
            "passing_yards": 0, "passing_touchdowns": 0, "interceptions": 0,
            "rushing_yards": 0, "rushing_touchdowns": 0, "receptions": 12,
            "receiving_yards": 140, "receiving_touchdowns": 1,
            "two_point_conversions": 0, "fumbles_lost": 0,
        },
        "projected_points_by_week": _weekly_projection(3.0, 6.0),
    })

    # A clear TE Difference-Maker Finder example: real target share, a real
    # red-zone role, a true receiving-role snap share, and a pass-heavy,
    # efficient offense to work in.
    players.append({
        "player_key": "449.p.100016",
        "player_id": "100016",
        "name": {"full": "Rising Ryder", "first": "Rising", "last": "Ryder"},
        "editorial_team_abbr": "DET",
        "display_position": "TE",
        "eligible_positions": [{"position": "TE"}],
        "status": "",
        "is_rookie": False,
        "draft_capital": None,
        "target_share": 0.21,
        "deep_target_share": 0.10,
        "red_zone_share": 0.15,
        "te_snap_share": 0.83,
        "team_pass_rate": 0.61,
        "team_pass_epa": 0.18,
        "game_script_spread": None,
        "game_script_total": None,
        "game_script_implied_team_total": None,
        "ngs_separation": 3.6,  # top-quartile route-running separation -- the difference-maker signal
        "ngs_yac_above_expectation": None,
        "ngs_time_to_throw": None,
        "ngs_cpoe": None,
        "game_wind_mph": None,
        "game_precip_probability": None,
        "game_is_dome": None,
        "sleeper_trending_adds": None,
        "injury_opportunity": False,
        "injury_opportunity_ahead_player": None,
        "injury_opportunity_ahead_status": None,
        "weeks_flagged_out": None,
        "team_new_head_coach": False,
        "team_head_coach_name": None,
        "team_new_offensive_coordinator": False,
        "team_offensive_coordinator_name": None,
        "qb_year2_regression_caution": False,
        "qb_year1_ppg": None,
        "percent_owned": 34.0,
        "percent_owned_delta": 4.0,
        "stats": {
            "passing_yards": 0, "passing_touchdowns": 0, "interceptions": 0,
            "rushing_yards": 0, "rushing_touchdowns": 0, "receptions": 48,
            "receiving_yards": 560, "receiving_touchdowns": 5,
            "two_point_conversions": 0, "fumbles_lost": 0,
        },
        "projected_points_by_week": _weekly_projection(9.0, 12.0),
    })

    return pd.DataFrame(players)


def _with_display_name(df: pd.DataFrame) -> pd.DataFrame:
    """Add a plain `player_name` column for display, without touching the
    underlying nested `name` column."""
    df = df.copy()
    df["player_name"] = df["name"].apply(lambda n: n.get("full") if isinstance(n, dict) else n)
    return df


# Matches `.streamlit/config.toml`'s theme -- `backgroundColor` / `secondaryBackgroundColor`
# for zebra banding, `primaryColor` as the dark end of the gradient below.
_ZEBRA_ROW_COLORS = ("#FFFFFF", "#EFF6FF")
_GRADIENT_LIGHT_RGB = (239, 246, 255)  # #EFF6FF
_GRADIENT_DARK_RGB = (37, 99, 235)  # #2563EB (theme primaryColor)


def _blue_shade(t: float) -> str:
    """Linear-interpolate between the theme's light and primary blue for a
    0-1 value `t`, returning a hex color."""
    t = max(0.0, min(1.0, t))
    r, g, b = (
        round(low + (high - low) * t) for low, high in zip(_GRADIENT_LIGHT_RGB, _GRADIENT_DARK_RGB)
    )
    return f"#{r:02x}{g:02x}{b:02x}"


def _style_table(df: pd.DataFrame, highlight_col: Optional[str] = None, higher_is_better: bool = True):
    """Style a table for `st.dataframe` using this app's blue theme instead
    of Streamlit's flat default: light zebra-striped row banding (so
    adjacent rows are easy to tell apart at a glance) plus, optionally, a
    blue-intensity gradient on one key ranking column -- darker/bolder blue
    for a better value, alongside the raw number.

    Doesn't depend on matplotlib (which `Styler.background_gradient()`
    needs and this project doesn't otherwise use) -- colors are
    hand-interpolated between the theme's two blues via `_blue_shade()`.

    Args:
        highlight_col: Column to apply the gradient to (skipped if absent
            or entirely null -- e.g. a tab with no single "score" column).
        higher_is_better: Whether a higher raw value in `highlight_col`
            should get the darker shade (True for a score, False for a
            rank or a rate where lower is better, e.g. sack rate).
    """
    df = df.copy()

    # The highlight gradient needs to rank highlight_col's REAL numeric
    # values -- compute that before the formatting step below replaces the
    # column with display strings.
    highlight_ranked = None
    if highlight_col and highlight_col in df.columns and df[highlight_col].notna().any():
        highlight_ranked = df[highlight_col].rank(pct=True, ascending=higher_is_better)

    # Styler's default number rendering shows full float precision (e.g.
    # "282.700000") -- nothing else in this app does that, so trim it back
    # to a clean, comma-grouped, trailing-zero-free number to match how
    # st.dataframe rendered these same columns before styling. This is done
    # by baking the formatted string directly into the DataFrame (rather
    # than via Styler.format(na_rep=...)) because Streamlit's actual
    # dataframe widget shows its own literal "None" for a null cell
    # regardless of what na_rep a Styler specifies -- confirmed directly:
    # na_rep is honored in the Styler's own to_html() but not in the
    # rendered app. Baking the placeholder into the cell's real content is
    # the only way to control what actually displays.
    numeric_cols = df.select_dtypes(include="number").columns.tolist()
    for col in numeric_cols:
        df[col] = df[col].apply(lambda v: "–" if pd.isna(v) else f"{v:,.4g}")

    def _zebra(row: pd.Series) -> list:
        color = _ZEBRA_ROW_COLORS[row.name % 2]
        return [f"background-color: {color}"] * len(row)

    styler = df.style.apply(_zebra, axis=1)

    if highlight_ranked is not None:
        def _gradient(_col: pd.Series) -> list:
            styles = []
            for value in highlight_ranked:
                if pd.isna(value):
                    styles.append("")
                else:
                    text_color = "white" if value >= 0.55 else "#0F172A"
                    styles.append(f"background-color: {_blue_shade(value)}; color: {text_color}; font-weight: 600")
            return styles

        styler = styler.apply(_gradient, subset=[highlight_col])

    return styler


def render_rookie_radar(players_df: pd.DataFrame) -> None:
    st.subheader("Rookie RBs & WRs, Ranked by Bumped Back-Half Upside")
    st.caption(
        "Back-half-of-season projections bumped by NFL draft capital -- Day 1/2 picks "
        "get the biggest boost. High-variance bench stashes worth rostering now."
    )
    bumped = _with_display_name(apply_rookie_bump(players_df))
    if bumped.empty:
        st.info("No rookie RB/WR bench stashes found in the current pool.")
        return

    def draft_label(dc):
        if not isinstance(dc, dict) or not dc.get("round"):
            return "Undrafted"
        return f"Round {dc['round']}, Pick {dc['pick']}"

    bumped["draft_capital_label"] = bumped["draft_capital"].apply(draft_label)
    display = bumped[
        ["player_name", "editorial_team_abbr", "display_position", "draft_capital_label",
         "back_half_points_base", "back_half_points_bumped"]
    ].rename(columns={
        "editorial_team_abbr": "team", "display_position": "pos",
        "draft_capital_label": "draft_capital",
        "back_half_points_base": "back_half_pts_base", "back_half_points_bumped": "back_half_pts_bumped",
    })
    st.dataframe(
        _style_table(display, highlight_col="back_half_pts_bumped"),
        use_container_width=True,
        hide_index=True,
    )


def render_qb_konami_code(players_df: pd.DataFrame) -> None:
    st.subheader("QB Konami Code: Passing Points vs. Rushing Floor")
    st.caption(
        "Dual-threat QBs sit up and to the right -- their weekly floor doesn't collapse "
        "on a bad passing day the way a pure pocket passer's does. CPOE (completion % over "
        "expectation, Next Gen Stats) is shown as a \"hidden skill\" signal independent of the box score."
    )
    qbs = _with_display_name(calculate_qb_floor(players_df))
    if qbs.empty:
        st.info("No QBs found in the current pool.")
        return

    passing_weight_yards = DEFAULT_SCORING_SETTINGS["passing_yards"]
    passing_weight_td = DEFAULT_SCORING_SETTINGS["passing_touchdowns"]
    interception_weight = DEFAULT_SCORING_SETTINGS["interceptions"]

    qbs["passing_points"] = qbs["stats"].apply(
        lambda s: s.get("passing_yards", 0) * passing_weight_yards
        + s.get("passing_touchdowns", 0) * passing_weight_td
        + s.get("interceptions", 0) * interception_weight
    )

    chart = (
        alt.Chart(qbs)
        .mark_circle(size=180)
        .encode(
            x=alt.X("passing_points", title="Passing Points"),
            y=alt.Y("qb_floor_score", title="Rushing Floor Score"),
            tooltip=["player_name", "editorial_team_abbr", "passing_points", "qb_floor_score"],
            color=alt.Color("editorial_team_abbr", legend=None),
        )
        .properties(height=400)
        .interactive()
    )
    st.altair_chart(chart, use_container_width=True)
    display = qbs[["player_name", "editorial_team_abbr", "passing_points", "qb_floor_score", "ngs_cpoe"]].rename(
        columns={"editorial_team_abbr": "team", "qb_floor_score": "rushing_floor_score", "ngs_cpoe": "CPOE"}
    )
    st.dataframe(
        _style_table(display, highlight_col="rushing_floor_score"),
        use_container_width=True,
        hide_index=True,
    )


def render_ir_stash_targets(players_df: pd.DataFrame) -> None:
    st.subheader("IR Stash Targets")
    st.caption(
        "Currently out (O / IR / PUP) but projected for a strong back half once healthy -- "
        "worth one of this league's 2 IR slots."
    )
    stashes = _with_display_name(find_ir_stashes(players_df))
    if stashes.empty:
        st.info("No IR-eligible stash targets clear the back-half projection threshold right now.")
        return
    display = stashes[
        ["player_name", "editorial_team_abbr", "display_position", "status", "projected_back_half_points"]
    ].rename(columns={"editorial_team_abbr": "team", "display_position": "pos"})
    st.dataframe(
        _style_table(display, highlight_col="projected_back_half_points"),
        use_container_width=True,
        hide_index=True,
    )


def render_wr3_floor_finder(players_df: pd.DataFrame) -> None:
    st.subheader("WR3 Floor Finder")
    st.caption(
        "Receivers with enough target volume to trust, ranked down for over-reliance on "
        "low-probability deep-ball touchdowns."
    )
    safe_wrs = _with_display_name(evaluate_wr_scarcity(players_df))
    if safe_wrs.empty:
        st.info("No WRs clear the minimum target share threshold right now.")
        return
    display = safe_wrs[
        ["player_name", "editorial_team_abbr", "target_share", "deep_target_share", "wr_floor_score"]
    ].rename(columns={"editorial_team_abbr": "team"})
    st.dataframe(
        _style_table(display, highlight_col="wr_floor_score"),
        use_container_width=True,
        hide_index=True,
    )


def render_breakout_radar(players_df: pd.DataFrame) -> None:
    st.subheader("Breakout Radar")
    st.caption(
        "Scans for the same predictive patterns behind last season's hardest-to-see-coming "
        "performers: an injury-opened opportunity, a new offensive play-caller, target share "
        "running ahead of the box score, the wider Yahoo market catching on early, a Day 3/UDFA "
        "rookie already earning more volume than their draft slot implied, a favorable Vegas-implied "
        "game script, or the wider fantasy market (via Sleeper's trending-adds data, not just this "
        "Yahoo league) catching on. Also flags (but doesn't score against) two cautions: a QB "
        "\"sophomore slump\" -- rookie QBs who finished top-15 in PPG have historically declined more "
        "often than not in Year 2 -- and a high-wind game forecast for QB/WR/TE (OpenWeatherMap), "
        "which historically suppresses passing volume/efficiency."
    )
    candidates = _with_display_name(find_breakout_signals(players_df))
    if candidates.empty:
        st.info("No players clear the breakout-score threshold in the current pool.")
        return

    display = candidates[
        ["player_name", "editorial_team_abbr", "display_position", "breakout_score", "breakout_signals", "caution_flags"]
    ].rename(columns={"editorial_team_abbr": "team", "display_position": "pos"})
    display["breakout_signals"] = display["breakout_signals"].apply(lambda s: " | ".join(s) if s else "")
    display["caution_flags"] = display["caution_flags"].apply(lambda s: " | ".join(s) if s else "")
    st.dataframe(_style_table(display, highlight_col="breakout_score"), use_container_width=True, hide_index=True)


def render_te_difference_makers(players_df: pd.DataFrame) -> None:
    st.subheader("TE Difference-Maker Finder")
    st.caption(
        "TE is unusually top-heavy -- a handful of must-start options, then a canyon, then "
        "touchdown-dependent streamers. Ranks TEs by the signals that predict a jump into that "
        "top tier *before* the box score shows it: real target share, a real red-zone role, "
        "actually running receiving routes (not just blocking), real route-running separation "
        "(Next Gen Stats), and a good, high-volume passing offense to work in -- plus the same "
        "opportunity signals as Breakout Radar."
    )
    ranked = _with_display_name(find_te_difference_makers(players_df))
    if ranked.empty:
        st.info("Not enough TEs in the current pool to rank (need at least 3).")
        return

    display = ranked[
        ["player_name", "editorial_team_abbr", "te_score", "target_share", "red_zone_share",
         "te_snap_share", "ngs_separation", "team_pass_rate", "te_signals"]
    ].rename(columns={
        "editorial_team_abbr": "team", "te_score": "score", "target_share": "target share",
        "red_zone_share": "red-zone share", "te_snap_share": "snap share (receiving role)",
        "ngs_separation": "separation (yds)", "team_pass_rate": "team pass rate",
    })
    display["te_signals"] = display["te_signals"].apply(lambda s: " | ".join(s) if s else "")
    st.dataframe(_style_table(display, highlight_col="score"), use_container_width=True, hide_index=True)


def render_oline_rankings() -> None:
    """Team-level O-Line Power Rankings. Unlike every other tab, this one
    needs no Yahoo data at all -- pure nfl_data_py -- so it works the same
    whether the sidebar is set to mock or live Yahoo data."""
    from api.nfl_enrichment import default_stats_season
    from api.oline_analytics import build_oline_rankings_with_trend

    st.subheader("O-Line Power Rankings")
    st.caption(
        "Team-level, built entirely from nfl_data_py -- no Yahoo access needed. Combines pass "
        "protection (sack/QB-hit rate per dropback), run blocking (yards per carry, stuff rate), "
        "starting-five continuity (share of O-line snaps held by the top 5), and trailing 4-year "
        "O-line draft investment into one 0-100 score, plus year-over-year rank change, new-starter "
        "turnover, and coaching-change flags. There's no true PFF-style per-lineman grading "
        "available for free, so treat the score as directional -- see `api/oline_analytics.py` for "
        "the full methodology and its limits."
    )

    season = st.number_input(
        "Season", min_value=2015, max_value=2035, value=default_stats_season(), step=1, key="oline_season"
    )

    @st.cache_data(ttl=3600, show_spinner="Crunching play-by-play, snap counts, and draft data...")
    def _load(season: int) -> pd.DataFrame:
        return build_oline_rankings_with_trend(season)

    try:
        rankings = _load(int(season))
    except Exception as exc:
        st.error(f"Couldn't build O-Line rankings for {season}: {exc}")
        return

    display = rankings.copy()
    display["trend"] = display["rank_change"].apply(
        lambda d: f"▲{int(d)}" if pd.notna(d) and d > 0 else (
            f"▼{int(abs(d))}" if pd.notna(d) and d < 0 else ("–" if pd.notna(d) else "new")
        )
    )
    display["coaching_change"] = display.apply(
        lambda r: ", ".join(
            filter(None, [
                "New OC" if r.get("new_offensive_coordinator") else None,
                "New HC" if r.get("new_head_coach") else None,
            ])
        ) or "—",
        axis=1,
    )

    table = display[[
        "oline_rank", "team", "oline_score", "trend", "new_starters_count", "coaching_change",
        "sack_rate", "qb_hit_rate", "avg_time_to_throw", "yards_per_carry", "stuff_rate", "continuity_share",
    ]].rename(columns={
        "oline_rank": "rank", "oline_score": "score", "new_starters_count": "new starters",
        "sack_rate": "sack rate", "qb_hit_rate": "QB hit rate", "avg_time_to_throw": "QB time to throw (s)",
        "yards_per_carry": "YPC", "stuff_rate": "stuff rate", "continuity_share": "lineup continuity",
    })
    st.dataframe(
        _style_table(table, highlight_col="score"),
        use_container_width=True,
        hide_index=True,
    )
    st.caption(
        "\"QB time to throw\" is context, not part of the score: a high sack rate paired with a "
        "*long* time to throw points at the O-line; paired with a *short* time to throw, it points "
        "more at the QB/scheme (Next Gen Stats)."
    )

    movers = rankings.dropna(subset=["rank_change"]).copy()
    if not movers.empty:
        movers["abs_change"] = movers["rank_change"].abs()
        movers = movers.sort_values("abs_change", ascending=False).head(5)
        mover_text = ", ".join(
            f"{row.team} ({'+' if row.rank_change > 0 else ''}{int(row.rank_change)})"
            for row in movers.itertuples()
        )
        st.caption(f"Biggest rank movers vs. last season: {mover_text}")


def render_team_change_report() -> None:
    """Skill-position players who changed teams, with old-vs-new team
    context. Like O-Line Power Rankings, this is pure nfl_data_py -- no
    Yahoo data needed."""
    from api.nfl_enrichment import default_stats_season
    from api.team_change_analytics import build_team_change_report

    st.subheader("Team Change Impact")
    st.caption(
        "Every QB/RB/WR/TE who changed teams since last season, with the context that actually "
        "determines whether it helps or hurts: team pass rate/efficiency, O-Line strength "
        "(reusing the O-Line Power Rankings tab), how crowded the new team's WR/TE target "
        "competition already is, and coaching changes. No single \"value went up/down\" score -- "
        "how much a move helps is genuinely position-dependent, so read the context notes for "
        "each player. See `api/team_change_analytics.py` for the full methodology and its limits."
    )

    season = st.number_input(
        "Season", min_value=2016, max_value=2035, value=default_stats_season(), step=1, key="team_change_season"
    )
    position_filter = st.multiselect(
        "Position", options=["QB", "RB", "WR", "TE"], default=["QB", "RB", "WR", "TE"], key="team_change_position"
    )

    @st.cache_data(ttl=3600, show_spinner="Comparing team contexts across two seasons...")
    def _load(season: int) -> pd.DataFrame:
        return build_team_change_report(season)

    try:
        report = _load(int(season))
    except Exception as exc:
        fallback_season = int(season) - 1
        st.warning(
            f"Couldn't build the team change report for {int(season)} ({exc}) -- nflverse likely "
            f"hasn't published full weekly stats for that season yet (a known, real gap as of this "
            f"writing). Falling back to {fallback_season}.",
            icon="⚠️",
        )
        try:
            report = _load(fallback_season)
        except Exception as exc2:
            st.error(f"Couldn't build the team change report for {fallback_season} either ({exc2}).", icon="🚫")
            return

    if report.empty:
        st.info(f"No team changes found for {season} (or {season - 1} data isn't available).")
        return

    filtered = report[report["position"].isin(position_filter)]
    if filtered.empty:
        st.info("No movers match the selected position filter.")
        return

    display = filtered[
        ["player_name", "position", "team_old", "team_new", "pass_rate_old", "pass_rate_new",
         "oline_score_old", "oline_score_new", "wr_te_target_share_sum", "context_notes"]
    ].rename(columns={
        "player_name": "player", "team_old": "from", "team_new": "to",
        "pass_rate_old": "pass rate (old)", "pass_rate_new": "pass rate (new)",
        "oline_score_old": "O-line (old)", "oline_score_new": "O-line (new)",
        "wr_te_target_share_sum": "new team WR/TE competition",
        "context_notes": "context",
    })
    st.dataframe(
        _style_table(display, highlight_col="O-line (new)"),
        use_container_width=True,
        hide_index=True,
    )


def render_run_game_outlook() -> None:
    """Team-level run-game outlook (which offenses look built to run the
    ball, and why) plus RB workload/"bell cow" identification. Like
    O-Line Power Rankings/Team Change Impact, this is pure nfl_data_py --
    no Yahoo data needed."""
    from api.nfl_enrichment import default_stats_season
    from api.run_game_analytics import build_rb_workload_report, build_run_game_outlook

    st.subheader("Run Game Outlook")
    st.caption(
        "**Team outlook**: last season's real rush rate/efficiency, adjusted by O-line strength "
        "(reusing O-Line Power Rankings), coaching stability, and whether last season's lead back "
        "is still on the roster -- into one 0-100 `run_outlook_score`, same real-signals-blend "
        "approach as O-Line Power Rankings/Priority Board. Not a synthetic season simulation -- "
        "see `api/run_game_analytics.py` for the full methodology. **RB workload**: every RB's "
        "real carries/targets/receptions/touches, plus their *share* of their own team's RB-room "
        "usage (carry/touch/snap share) -- that share is what actually separates a bell cow from a "
        "committee back getting decent raw volume on a pass-heavy offense."
    )

    season = st.number_input(
        "Season", min_value=2016, max_value=2035, value=default_stats_season(), step=1, key="run_game_season"
    )

    @st.cache_data(ttl=3600, show_spinner="Crunching play-by-play, snap counts, and roster data...")
    def _load(season: int) -> tuple:
        return build_run_game_outlook(season), build_rb_workload_report(season)

    try:
        outlook, workload = _load(int(season))
    except Exception as exc:
        fallback_season = int(season) - 1
        st.warning(
            f"Couldn't build the run game outlook for {int(season)} ({exc}) -- nflverse likely "
            f"hasn't published full weekly stats for that season yet. Falling back to {fallback_season}.",
            icon="⚠️",
        )
        try:
            outlook, workload = _load(fallback_season)
        except Exception as exc2:
            st.error(f"Couldn't build the run game outlook for {fallback_season} either ({exc2}).", icon="🚫")
            return

    st.markdown("#### Team Run Game Outlook")
    outlook_display = outlook.copy()
    outlook_display["lead_back"] = outlook_display.apply(
        lambda r: (
            f"{r['lead_back_name']} (retained)" if r.get("workhorse_status") == "retained"
            else f"{r['lead_back_name']} (departed)" if r.get("workhorse_status") == "departed"
            else "no clear lead back"
        ),
        axis=1,
    )
    outlook_table = outlook_display[[
        "team", "run_outlook_score", "rush_rate", "yards_per_carry", "rushing_epa_per_play",
        "oline_score", "lead_back", "notable_arrivals", "context_notes",
    ]].rename(columns={
        "run_outlook_score": "outlook score", "rush_rate": "rush rate", "yards_per_carry": "YPC",
        "rushing_epa_per_play": "rush EPA/play", "oline_score": "O-line score",
        "lead_back": "lead back (last season)", "notable_arrivals": "notable RB arrivals",
        "context_notes": "context",
    })
    st.dataframe(
        _style_table(outlook_table, highlight_col="outlook score"),
        use_container_width=True,
        hide_index=True,
    )

    st.markdown("#### RB Workload / Bell Cow Finder")
    tier_filter = st.multiselect(
        "Usage tier", options=["Bell Cow", "Lead Back", "Committee Lead", "Depth/Committee"],
        default=["Bell Cow", "Lead Back", "Committee Lead"], key="run_game_tier_filter",
    )
    filtered_workload = workload[workload["usage_tier"].isin(tier_filter)]
    if filtered_workload.empty:
        st.info("No RBs match the selected usage tier filter.")
        return

    workload_table = filtered_workload[[
        "player_name", "team", "carries", "targets", "receptions", "receiving_yards",
        "touches", "carry_share", "touch_share", "snap_share", "bell_cow_score", "usage_tier",
        "weeks_flagged_out",
    ]].rename(columns={
        "player_name": "player", "receiving_yards": "rec yards", "carry_share": "carry share",
        "touch_share": "touch share", "snap_share": "snap share", "bell_cow_score": "bell cow score",
        "usage_tier": "usage tier", "weeks_flagged_out": "weeks out (injury)",
    })
    st.dataframe(
        _style_table(workload_table, highlight_col="bell cow score"),
        use_container_width=True,
        hide_index=True,
    )
    st.caption(
        "\"Share\" columns are that RB's percentage of their OWN team's RB-room carries/touches/"
        "snaps -- not a league-wide ranking. A RB with modest raw touches but a high share is still "
        "a real bell cow on a run-light offense; see the Team Run Game Outlook table above for that "
        "team's overall volume. \"Weeks out (injury)\" is how many weeks they were listed Out on the "
        "real NFL injury report -- a real caveat for the shares above: a back who missed real time "
        "shows a season-total workload that UNDERSTATES what they command when actually healthy, so "
        "a low bell cow score paired with real weeks out can mean \"was hurt,\" not \"is a committee back.\""
    )


def render_priority_board(players_df: pd.DataFrame) -> None:
    """The rollup tab: blends every other tab's signal into one
    cross-position rank, for the question a waiver claim actually forces
    -- "of these different positions all competing for the same roster
    spot, who do I prioritize this week?" See `build_priority_board()`'s
    docstring in `data/calculators.py` for exactly how the blend works.

    O-Line Power Rankings and Team Change Impact context are optional,
    pure nfl_data_py additions fetched independently -- a failure in
    either (e.g. a season nflverse hasn't published weekly data for yet)
    degrades that one context source rather than breaking this tab."""
    from api.nfl_enrichment import default_stats_season
    from api.oline_analytics import build_oline_power_rankings
    from api.team_change_analytics import build_team_change_report

    st.subheader("Priority Board")
    st.caption(
        "Blends every other tab into one cross-position rank: this league's own custom scoring "
        "(shown here as \"league value pts\", ranked within position so a QB's raw points are "
        "never compared to a WR's), Breakout Radar's opportunity score (already a composite of "
        "injury/coaching/game-script/Sleeper/ownership signals), whichever position-specific tab "
        "applies as a bonus (TE Difference-Makers, Rookie Radar, QB Konami Code, or WR3 Floor "
        "Finder), and -- as light context, not a driver -- the player's team's O-Line Power "
        "Ranking. Team Change Impact's notes show up as context too, never folded into the "
        "score, matching that tab's own \"no single value-up/down number\" design. See "
        "`build_priority_board()` in `data/calculators.py` for the exact weights."
    )

    season = st.number_input(
        "Season (for O-Line/Team Change context)", min_value=2016, max_value=2035,
        value=default_stats_season(), step=1, key="priority_board_season",
    )

    @st.cache_data(ttl=3600, show_spinner="Loading O-Line context...")
    def _load_oline(season: int) -> pd.DataFrame:
        return build_oline_power_rankings(season)

    @st.cache_data(ttl=3600, show_spinner="Loading team-change context...")
    def _load_team_change(season: int) -> pd.DataFrame:
        return build_team_change_report(season)

    oline_rankings = None
    try:
        oline_rankings = _load_oline(int(season))
    except Exception as exc:
        st.caption(f"⚠️ O-Line context unavailable for {season} ({exc}) -- team_context stays neutral.")

    team_change_report = None
    try:
        team_change_report = _load_team_change(int(season))
    except Exception as exc:
        st.caption(f"⚠️ Team Change context unavailable for {season} ({exc}) -- skipped.")

    board = _with_display_name(
        build_priority_board(players_df, oline_rankings=oline_rankings, team_change_report=team_change_report)
    )

    display = board[
        ["player_name", "editorial_team_abbr", "display_position", "status", "priority_score",
         "custom_value", "priority_signals", "priority_cautions"]
    ].rename(columns={
        "editorial_team_abbr": "team", "display_position": "pos", "custom_value": "league value pts",
    })
    display["priority_signals"] = display["priority_signals"].apply(lambda s: " | ".join(s) if s else "")
    display["priority_cautions"] = display["priority_cautions"].apply(lambda s: " | ".join(s) if s else "")
    st.dataframe(_style_table(display, highlight_col="priority_score"), use_container_width=True, hide_index=True)


def _yahoo_credentials_from_secrets() -> Optional[dict]:
    """For a hosted deployment (e.g. Streamlit Community Cloud) with no
    `private.json` file on disk at all: paste the exact same JSON you'd
    put in `private.json` locally into the deployed app's Secrets, under
    a single `private_json` key (a raw JSON string, not a TOML table --
    avoids any TOML-nesting gymnastics for the `access_token` sub-dict).
    See README's "Deploying" section for the exact format.

    Returns None (never raises) if unset, invalid, or Streamlit secrets
    aren't configured at all (e.g. running locally with no
    `.streamlit/secrets.toml`) -- this is an optional, additional
    credential source, not a required one.
    """
    try:
        raw = st.secrets.get("private_json")
    except Exception:
        return None
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError) as exc:
        logger.warning("Couldn't parse the 'private_json' secret as JSON: %s", exc)
        return None


def load_live_players_df(
    count_limit: int, enrich: bool, season: int, credentials: Optional[dict] = None
) -> pd.DataFrame:
    """Pull real waiver-wire players via YahooAuthManager + player_mapper,
    optionally backfilled with nfl_data_py via nfl_enrichment.

    Args:
        credentials: Passed straight through to `YahooAuthManager` --
            when given (from `_yahoo_credentials_from_secrets()`, on a
            hosted deployment with no `private.json` file), skips reading
            a local file entirely.

    Raises whatever YahooAuthManager/player_mapper raises (missing
    private.json, auth failure, etc.) -- the caller in main() decides how
    to surface that to the user.
    """
    from api.player_mapper import build_players_dataframe
    from api.yahoo_auth import YahooAuthManager

    auth = YahooAuthManager(credentials=credentials) if credentials else YahooAuthManager()
    free_agents = auth.get_waiver_wire_players(count_limit=count_limit)
    return build_players_dataframe(
        auth, free_agents, count_limit=count_limit, enrich=enrich, season=season
    )


@st.cache_data(ttl=3600, show_spinner=False)
def load_real_players_df(season: int) -> pd.DataFrame:
    """A genuine, Yahoo-free player pool for the "Real data (no Yahoo)"
    sidebar option -- the same real, full-coverage pool Draft Board uses
    (`build_draft_board_pool()`), with real current injury status/trending
    adds from Sleeper layered on top (`attach_sleeper_status_and_trending()`)
    to close the one gap that pool otherwise has for these tabs: no live
    `status` field at all. Falls back one season automatically on the same
    known, real nflverse weekly-stats gap Draft Board handles."""
    from api.nfl_enrichment import attach_sleeper_status_and_trending, build_draft_board_pool

    try:
        pool = build_draft_board_pool(season)
    except Exception:
        pool = build_draft_board_pool(season - 1)
    return attach_sleeper_status_and_trending(pool)


def _load_league_rosters(use_live: bool, credentials: Optional[dict]):
    """Returns `(rosters_df, my_team_id)` for Free Agent Suggestions/Trade
    Finder: live Yahoo rosters (every team, via `api/player_mapper.py`'s
    `build_league_rosters_dataframe()` -- NOT YET verified against a live
    league, see that function's docstring) when `use_live`, else the
    procedural mock league (`build_mock_league_rosters()`). Any live-fetch
    failure (or Yahoo not flagging any team as yours) falls back to the
    mock league with a visible warning, rather than leaving the tab blank.
    """
    if not use_live:
        return build_mock_league_rosters(), MOCK_LEAGUE_MY_TEAM_ID

    from api.player_mapper import build_league_rosters_dataframe
    from api.yahoo_auth import YahooAuthManager

    try:
        with st.spinner("Fetching every team's roster from Yahoo..."):
            auth = YahooAuthManager(credentials=credentials) if credentials else YahooAuthManager()
            rosters_df = build_league_rosters_dataframe(auth)
        my_team_id = rosters_df.attrs.get("my_team_id")
        if my_team_id is None:
            st.warning(
                "Couldn't identify your team in the league (Yahoo didn't flag any team as "
                "yours) -- showing the mock league instead.",
                icon="⚠️",
            )
            return build_mock_league_rosters(), MOCK_LEAGUE_MY_TEAM_ID
        return rosters_df, my_team_id
    except Exception as exc:
        st.error(f"Couldn't fetch live league rosters ({exc}). Showing the mock league instead.", icon="🚫")
        return build_mock_league_rosters(), MOCK_LEAGUE_MY_TEAM_ID


def render_free_agent_suggestions(players_df: pd.DataFrame, use_live: bool, credentials: Optional[dict]) -> None:
    """Concrete drop/add recommendations, not just a ranked list: for
    each skill position, compares your weakest rostered player against
    the best available free agent, using Priority Board's blended
    `priority_score` for both sides."""
    st.subheader("Free Agent Suggestions")
    st.caption(
        "Compares your weakest rostered player at each position against the best available free "
        "agent, using the same blended `priority_score` as Priority Board -- a concrete \"drop X, "
        "add Y\" suggestion, not just free agents ranked in a vacuum with no connection to your "
        "actual roster. Never recommends adding someone currently on IR/PUP/NA/suspended (they "
        "structurally can't help you right now); a merely Questionable/Doubtful/Out player is still "
        "eligible but shown with their real status so you know the single-game risk you're taking. "
        "Needs every team's real Yahoo rosters to know what \"your weakest player\" actually is; "
        "falls back to a mock league until that's live. See `find_free_agent_upgrades()` in "
        "`data/calculators.py`."
    )

    league_rosters, my_team_id = _load_league_rosters(use_live, credentials)
    if not use_live:
        st.info("Showing a mock league (10 teams) -- switch to \"Live Yahoo data\" in the sidebar for your real roster.")

    my_roster = league_rosters[league_rosters["team_id"] == my_team_id]
    if my_roster.empty:
        st.info("Couldn't find your roster in the league data.")
        return

    min_gain = st.slider(
        "Minimum priority-score gain to suggest", min_value=0.0, max_value=50.0, value=10.0, step=5.0,
        help="Higher = only your clearest upgrades; lower = surfaces more marginal ones too.",
    )

    my_roster_scored = build_priority_board(my_roster)
    free_agents_scored = build_priority_board(players_df)
    suggestions = find_free_agent_upgrades(my_roster_scored, free_agents_scored, min_priority_score_gain=min_gain)

    if suggestions.empty:
        st.info("No free agent clears your minimum gain threshold at any position right now.")
        return

    display = suggestions.rename(columns={
        "position": "pos", "drop_player": "drop", "drop_priority_score": "drop score",
        "drop_status": "drop status", "add_player": "add", "add_priority_score": "add score",
        "add_status": "add status", "priority_score_gain": "gain",
    })
    st.dataframe(_style_table(display, highlight_col="gain"), use_container_width=True, hide_index=True)


def render_trade_finder(use_live: bool, credentials: Optional[dict]) -> None:
    """Proactively scans every other team for a genuine two-way trade
    fit with yours: a position where you have surplus and they have a
    need, paired with a position where they have surplus and you have a
    need. See `find_trade_candidates()` in `data/calculators.py` for the
    exact matching logic and its documented limits."""
    st.subheader("Trade Finder")
    st.caption(
        "Scans every other team for a genuine two-way fit: a position where you have surplus "
        "(tradeable depth) and they have a need, paired with a position where they have surplus "
        "and you have a need -- not just \"who has a good player.\" This is a value-and-need "
        "heuristic, not a negotiation: it has no idea whether a manager actually wants to trade, "
        "their own roster philosophy, or plain stubbornness. Treat every row as a conversation "
        "starter to evaluate yourself, never a trade either side is guaranteed to accept. Neither "
        "side of a proposed trade is ever someone currently on IR/PUP/NA/suspended; a "
        "Questionable/Doubtful/Out status is still shown as real single-game risk context."
    )

    league_rosters, my_team_id = _load_league_rosters(use_live, credentials)
    if not use_live:
        st.info("Showing a mock league (10 teams) -- switch to \"Live Yahoo data\" in the sidebar for your real league.")

    num_teams = st.number_input(
        "Number of teams in your league", min_value=4, max_value=20,
        value=league_rosters["team_id"].nunique(), step=1,
        help="Used to compute replacement level (see compute_replacement_level()) -- should match your real league size.",
    )

    board = build_priority_board(league_rosters)
    trades = find_trade_candidates(
        my_team_id, board, DEFAULT_ROSTER_REQUIREMENTS, num_teams=int(num_teams)
    )

    if trades.empty:
        st.info("No genuine two-way trade fit found with any other team right now.")
        return

    display = trades.rename(columns={
        "other_team_id": "team", "you_give": "you give", "you_give_position": "give pos",
        "you_give_value": "give value", "you_give_status": "give status", "you_get": "you get",
        "you_get_position": "get pos", "you_get_value": "get value", "you_get_status": "get status",
        "fairness_gap_pct": "fairness gap", "fit_score": "fit",
    })
    st.dataframe(_style_table(display, highlight_col="fit"), use_container_width=True, hide_index=True)


def render_draft_board() -> None:
    """Every real skill-position player, ranked for a snake draft -- see
    `build_draft_board_pool()`'s docstring in `api/nfl_enrichment.py` for
    exactly what this is (last season's real performance under this
    league's own scoring, adjusted by real signals) and isn't (a
    synthetic projection for the upcoming season). Built with ZERO Yahoo
    dependency, unlike every other player-level tab -- this is pure
    `nfl_data_py`, same as O-Line Power Rankings/Team Change Impact."""
    from api.fantasypros import attach_fantasypros_data
    from api.nfl_enrichment import (
        DEFAULT_YAHOO_DRAFT_REFERENCE_CSV,
        DRAFT_BOARD_POSITIONS,
        attach_yahoo_adp,
        build_draft_board_pool,
        default_nfl_season,
        default_stats_season,
    )

    st.subheader("Draft Board")
    st.caption(
        "Every real skill-position player, ranked by last season's actual performance under "
        "**this league's own scoring** -- not a generic site-wide point total -- adjusted by the "
        "same real signals as the rest of this app (rookie draft capital, coaching changes, Next "
        "Gen Stats, Vegas game script, and more; see Priority Board). This is NOT a synthetic "
        "projection for the upcoming season on its own -- see \"FantasyPros\" below for a real "
        "external projection where it's available -- it's a serious, defensible starting point for "
        "ranking players, not a finished cheat sheet that already knows about every offseason "
        "move. Needs zero Yahoo access, unlike every other player-level tab."
    )
    has_yahoo_adp_reference = DEFAULT_YAHOO_DRAFT_REFERENCE_CSV.is_file()
    if has_yahoo_adp_reference:
        st.caption(
            "Also shown: this league's real Yahoo Fantasy Plus ADP/tier (from your own premium "
            "cheat sheet export) alongside where this board ranks each player, so you can spot "
            "where the two disagree. Name-matched, not an ID join -- see README for how to refresh "
            "it. Expect real, harmless misses for this year's rookies (not in last season's stats)."
        )
    st.caption(
        "Also shown, where available: a real, live third-party check from FantasyPros -- their "
        "own season-long projected points and expert-consensus rank/tier for the upcoming season, "
        "joined by a real Yahoo ID crosswalk (not name-matched). The API key behind this is "
        "FantasyPros' free tier, which caps real coverage at roughly the top 10 players per "
        "position (~60 total) -- expect this to be populated near the top of the board and blank "
        "further down; see `api/fantasypros.py` for the exact, discovered limitation."
    )

    season = st.number_input(
        "Season (last completed season's stats)", min_value=2015, max_value=2035,
        value=default_stats_season(), step=1, key="draft_board_season",
    )
    position_filter = st.multiselect(
        "Position", options=list(DRAFT_BOARD_POSITIONS), default=list(DRAFT_BOARD_POSITIONS),
        key="draft_board_position",
    )

    @st.cache_data(ttl=3600, show_spinner="Building the draft board (every skill-position player, full-season stats)...")
    def _load(season: int) -> pd.DataFrame:
        pool = build_draft_board_pool(season)
        return build_priority_board(pool)

    try:
        board = _load(int(season))
    except Exception as exc:
        fallback_season = int(season) - 1
        st.warning(
            f"Couldn't build the draft board for {int(season)} ({exc}) -- nflverse likely hasn't "
            f"published full weekly stats for that season yet (a known, real gap as of this "
            f"writing). Falling back to {fallback_season}.",
            icon="⚠️",
        )
        try:
            board = _load(fallback_season)
        except Exception as exc2:
            st.error(f"Couldn't build the draft board for {fallback_season} either ({exc2}).", icon="🚫")
            return

    board = _with_display_name(board)
    if has_yahoo_adp_reference:
        board = attach_yahoo_adp(board)
        board["our_position_rank"] = board.groupby("display_position")["priority_score"].rank(
            ascending=False, method="min"
        )
        # Positive = we rank this player better than Yahoo's own drafters do
        # (a possible value pick); negative = Yahoo's ADP has them going
        # earlier than this board would -- a possible overdraft/avoid.
        board["value_vs_yahoo_adp"] = board["yahoo_position_rank"] - board["our_position_rank"]

    fetch_fantasypros = st.checkbox(
        "Fetch live FantasyPros projections/rankings", value=True, key="draft_board_fantasypros",
        help="A real API call against FantasyPros' free tier (~top 10 players/position -- see "
             "the caption above). Cached for 6 hours; uncheck to skip the network call entirely.",
    )
    has_fantasypros = False
    if fetch_fantasypros:
        from api.fantasypros import join_fantasypros_data

        @st.cache_data(ttl=21600, show_spinner="Fetching live FantasyPros projections/rankings...")
        def _load_fantasypros(fp_season: int) -> Optional[dict]:
            from api.fantasypros import fetch_fantasypros_bundle

            try:
                bundle = fetch_fantasypros_bundle(fp_season)
            except Exception as exc:
                return {"error": str(exc)}
            return {"rankings": bundle["rankings"], "projections": bundle["projections"]}

        fantasypros_season = default_nfl_season()
        bundle = _load_fantasypros(fantasypros_season)
        if bundle and "error" not in bundle:
            board = join_fantasypros_data(board, bundle["rankings"], bundle["projections"], yahoo_id_column="yahoo_id")
            has_fantasypros = True
        else:
            st.caption(
                f"⚠️ FantasyPros fetch failed ({bundle.get('error') if bundle else 'unknown error'}) -- "
                "skipped for this load."
            )

    filtered = board[board["display_position"].isin(position_filter)]
    if filtered.empty:
        st.info("No players match the selected position filter.")
        return

    columns = ["player_name", "editorial_team_abbr", "display_position", "priority_score"]
    rename = {"editorial_team_abbr": "team", "display_position": "pos"}
    if has_yahoo_adp_reference:
        columns += ["yahoo_adp", "yahoo_tier", "value_vs_yahoo_adp"]
        rename["value_vs_yahoo_adp"] = "value vs. Yahoo ADP"
    if has_fantasypros:
        columns += ["fantasypros_projected_points", "fantasypros_pos_rank", "fantasypros_tier"]
        rename["fantasypros_projected_points"] = "FantasyPros proj. pts"
        rename["fantasypros_pos_rank"] = "FantasyPros pos rank"
        rename["fantasypros_tier"] = "FantasyPros tier"
    columns += ["priority_signals", "priority_cautions"]

    display = filtered[columns].rename(columns=rename)
    display["priority_signals"] = display["priority_signals"].apply(lambda s: " | ".join(s) if s else "")
    display["priority_cautions"] = display["priority_cautions"].apply(lambda s: " | ".join(s) if s else "")
    st.dataframe(_style_table(display, highlight_col="priority_score"), use_container_width=True, hide_index=True)


def main() -> None:
    from api.nfl_enrichment import default_stats_season

    st.set_page_config(page_title="CoachPops Fantasy Manager", page_icon="🏈", layout="wide")
    st.title("🏈 CoachPops Fantasy Football Manager")

    private_json_exists = (PROJECT_ROOT / "private.json").is_file()
    secrets_credentials = _yahoo_credentials_from_secrets()
    yahoo_available = private_json_exists or secrets_credentials is not None

    with st.sidebar:
        st.header("Data source")
        data_source = st.radio(
            "Waiver wire data",
            options=["Mock data", "Real data (no Yahoo)", "Live Yahoo data"],
            index=0,
            help=(
                "\"Real data (no Yahoo)\" builds a genuine, full-coverage player pool from "
                "nfl_data_py + Sleeper -- zero Yahoo dependency, unlike mock data's synthetic "
                "demo players. Applies to Priority Board, Rookie Radar, QB Konami Code, IR Stash "
                "Targets, WR3 Floor Finder, Breakout Radar, and TE Difference-Makers -- Free Agent "
                "Suggestions/Trade Finder still need a real multi-team roster (Yahoo or a mock "
                "league), which no player-pool API can substitute for."
            ),
        )
        if data_source == "Live Yahoo data" and not yahoo_available:
            st.caption("⚠️ No Yahoo credentials found (create `private.json` -- see README). Showing mock data instead.")
        live_count_limit = st.slider(
            "Players to fetch (live only)", min_value=5, max_value=100, value=25, step=5,
            help="Each player costs one extra Yahoo API call for season stats.",
            disabled=data_source != "Live Yahoo data",
        )
        enrich_with_nfl_data = st.checkbox(
            "Backfill rookie/target-share/projection data (nfl_data_py)",
            value=True,
            disabled=data_source != "Live Yahoo data",
            help="Joins in nflverse data via a yahoo_id crosswalk -- covers "
                 "roughly half of rostered players. See api/nfl_enrichment.py.",
        )
        nfl_season = st.number_input(
            "NFL season", min_value=2015, max_value=2035, value=default_stats_season(), step=1,
            disabled=data_source == "Mock data" or (data_source == "Live Yahoo data" and not enrich_with_nfl_data),
            help="For \"Real data\": the last season with real played games to draw production "
                 "from (team/rookie/coaching context always uses the real current season "
                 "regardless -- see README's \"A note on season defaults\").",
        )

    # A local private.json file always wins if present (matches local-dev
    # expectations); secrets are the fallback for a hosted deployment with
    # no such file at all. Computed once here since Free Agent Suggestions/
    # Trade Finder need it too, not just the waiver-wire fetch below.
    credentials = None if private_json_exists else secrets_credentials

    if data_source == "Live Yahoo data":
        try:
            with st.spinner("Fetching live waiver wire data from Yahoo..."):
                players_df = load_live_players_df(
                    live_count_limit, enrich_with_nfl_data, int(nfl_season), credentials=credentials
                )
            st.success("Connected to Yahoo -- showing live waiver wire data.", icon="✅")
            if enrich_with_nfl_data:
                st.caption(
                    "Rookie/target-share/projection data backfilled from nfl_data_py "
                    f"({int(nfl_season)} season). Coverage is roughly half of rostered "
                    "players, and the weekly projection is a flat points-per-game "
                    "baseline, not a true projection -- see `api/nfl_enrichment.py`."
                )
            else:
                st.caption(
                    "Backfill disabled -- every nfl_data_py-sourced field is at its empty "
                    "Yahoo-only default, so Rookie Radar, IR Stash Targets, WR3 Floor Finder, "
                    "and Breakout Radar will all show empty (Breakout Radar can still fire on "
                    "`percent_owned`/`percent_owned_delta` alone, since Yahoo provides those)."
                )
        except Exception as exc:
            st.error(
                f"Couldn't load live Yahoo data ({exc}). Run `python scripts/yahoo_login.py` "
                "from a terminal first to complete the OAuth handshake, then reload. "
                "Falling back to mock data for now.",
                icon="🚫",
            )
            players_df = build_mock_players_df()
    elif data_source == "Real data (no Yahoo)":
        try:
            with st.spinner("Building a real, Yahoo-free player pool (nfl_data_py + Sleeper)..."):
                players_df = load_real_players_df(int(nfl_season))
            st.success(
                f"Showing a real, Yahoo-free player pool ({int(nfl_season)} season stats, current "
                "roster/coaching context) -- zero Yahoo dependency, unlike mock data's synthetic "
                "demo players.",
                icon="✅",
            )
            st.caption(
                "Built the same way as Draft Board: every real skill-position player, real target "
                "share/red-zone share/coaching changes, plus real current injury status and "
                "trending-add data from Sleeper (see `api/external_sources.py`). Free Agent "
                "Suggestions/Trade Finder below still show mock data -- they need a real "
                "multi-team roster, which no player-pool API can substitute for."
            )
        except Exception as exc:
            st.error(f"Couldn't build the real player pool ({exc}). Falling back to mock data for now.", icon="🚫")
            players_df = build_mock_players_df()
    else:
        st.warning(
            "Showing **mock data** shaped to match Yahoo's real `/players` response. Select "
            "\"Real data (no Yahoo)\" for a genuine Yahoo-free player pool, or create "
            "`private.json` (see README) and select \"Live Yahoo data\" for your real waiver wire.",
            icon="⚠️",
        )
        players_df = build_mock_players_df()

    tab1, tab2, tab3, tab4, tab5, tab6, tab7, tab8, tab9, tab10, tab11, tab12, tab13 = st.tabs(
        [
            "Priority Board",
            "Rookie Radar",
            "QB Konami Code",
            "IR Stash Targets",
            "WR3 Floor Finder",
            "Breakout Radar",
            "TE Difference-Makers",
            "O-Line Power Rankings",
            "Team Change Impact",
            "Run Game Outlook",
            "Free Agent Suggestions",
            "Trade Finder",
            "Draft Board",
        ]
    )
    with tab1:
        render_priority_board(players_df)
    with tab2:
        render_rookie_radar(players_df)
    with tab3:
        render_qb_konami_code(players_df)
    with tab4:
        render_ir_stash_targets(players_df)
    with tab5:
        render_wr3_floor_finder(players_df)
    with tab6:
        render_breakout_radar(players_df)
    with tab7:
        render_te_difference_makers(players_df)
    with tab8:
        render_oline_rankings()
    with tab9:
        render_team_change_report()
    with tab10:
        render_run_game_outlook()
    with tab11:
        # Deliberately NOT `players_df` when that's the real Yahoo-free pool:
        # Free Agent Suggestions needs a real MULTI-TEAM roster to mean
        # anything (whose roster is "my weakest player" relative to), which
        # only Yahoo or the synthetic mock league can provide -- a real
        # free-agent pool paired with a mock roster would silently mix two
        # unrelated data sources. Falls back to the same mock players_df as
        # "Mock data" mode whenever Yahoo isn't live.
        free_agent_pool = players_df if data_source == "Live Yahoo data" else build_mock_players_df()
        render_free_agent_suggestions(free_agent_pool, use_live=(data_source == "Live Yahoo data"), credentials=credentials)
    with tab12:
        render_trade_finder(use_live=(data_source == "Live Yahoo data"), credentials=credentials)
    with tab13:
        render_draft_board()


if __name__ == "__main__":
    main()
