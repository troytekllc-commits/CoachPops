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

import sys
from pathlib import Path

import altair as alt
import pandas as pd
import streamlit as st

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.append(str(PROJECT_ROOT))

from data.calculators import (  # noqa: E402
    DEFAULT_SCORING_SETTINGS,
    apply_rookie_bump,
    calculate_custom_value,
    calculate_qb_floor,
    evaluate_wr_scarcity,
    find_breakout_signals,
    find_ir_stashes,
    find_te_difference_makers,
)


def _weekly_projection(early_avg: float, late_avg: float) -> dict:
    """Deterministic weeks 1-17 projection dict: `early_avg` through week 9,
    `late_avg` from week 10 (back half) on, with a small fixed wiggle so the
    numbers don't look perfectly flat."""
    projection = {}
    for week in range(1, 18):
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
        player.setdefault("sleeper_trending_adds", None)
        player.setdefault("injury_opportunity", False)
        player.setdefault("injury_opportunity_ahead_player", None)
        player.setdefault("injury_opportunity_ahead_status", None)
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
        "sleeper_trending_adds": None,
        "injury_opportunity": True,
        "injury_opportunity_ahead_player": "Wounded Wes",
        "injury_opportunity_ahead_status": "O",
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
        "sleeper_trending_adds": None,
        "injury_opportunity": False,
        "injury_opportunity_ahead_player": None,
        "injury_opportunity_ahead_status": None,
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


def render_league_optimizer(players_df: pd.DataFrame) -> None:
    st.subheader("Waiver Wire, Ranked by Your League's Custom Scoring")
    st.caption(
        "Ignores generic point totals and rebuilds each player's value strictly from "
        "this league's scoring weights (see `DEFAULT_SCORING_SETTINGS` in "
        "`data/calculators.py` -- swap in `get_league_settings()` for the real thing)."
    )
    ranked = _with_display_name(calculate_custom_value(players_df))
    st.dataframe(
        ranked[["player_name", "editorial_team_abbr", "display_position", "status", "custom_value"]]
        .rename(columns={"editorial_team_abbr": "team", "display_position": "pos", "custom_value": "custom_value_pts"}),
        use_container_width=True,
        hide_index=True,
    )


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
    st.dataframe(
        bumped[
            ["player_name", "editorial_team_abbr", "display_position", "draft_capital_label",
             "back_half_points_base", "back_half_points_bumped"]
        ].rename(columns={
            "editorial_team_abbr": "team", "display_position": "pos",
            "draft_capital_label": "draft_capital",
            "back_half_points_base": "back_half_pts_base", "back_half_points_bumped": "back_half_pts_bumped",
        }),
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
    st.dataframe(
        qbs[["player_name", "editorial_team_abbr", "passing_points", "qb_floor_score", "ngs_cpoe"]]
        .rename(columns={
            "editorial_team_abbr": "team", "qb_floor_score": "rushing_floor_score", "ngs_cpoe": "CPOE",
        }),
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
    st.dataframe(
        stashes[
            ["player_name", "editorial_team_abbr", "display_position", "status", "projected_back_half_points"]
        ].rename(columns={"editorial_team_abbr": "team", "display_position": "pos"}),
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
    st.dataframe(
        safe_wrs[
            ["player_name", "editorial_team_abbr", "target_share", "deep_target_share", "wr_floor_score"]
        ].rename(columns={"editorial_team_abbr": "team"}),
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
        "Yahoo league) catching on. Also flags (but doesn't score against) a QB \"sophomore slump\" "
        "caution -- rookie QBs who finished top-15 in PPG have historically declined more often than "
        "not in Year 2."
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
    st.dataframe(display, use_container_width=True, hide_index=True)


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
    st.dataframe(display, use_container_width=True, hide_index=True)


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

    st.dataframe(
        display[[
            "oline_rank", "team", "oline_score", "trend", "new_starters_count", "coaching_change",
            "sack_rate", "qb_hit_rate", "avg_time_to_throw", "yards_per_carry", "stuff_rate", "continuity_share",
        ]].rename(columns={
            "oline_rank": "rank", "oline_score": "score", "new_starters_count": "new starters",
            "sack_rate": "sack rate", "qb_hit_rate": "QB hit rate", "avg_time_to_throw": "QB time to throw (s)",
            "yards_per_carry": "YPC", "stuff_rate": "stuff rate", "continuity_share": "lineup continuity",
        }),
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
        st.error(f"Couldn't build the team change report for {season}: {exc}")
        return

    if report.empty:
        st.info(f"No team changes found for {season} (or {season - 1} data isn't available).")
        return

    filtered = report[report["position"].isin(position_filter)]
    if filtered.empty:
        st.info("No movers match the selected position filter.")
        return

    st.dataframe(
        filtered[
            ["player_name", "position", "team_old", "team_new", "pass_rate_old", "pass_rate_new",
             "oline_score_old", "oline_score_new", "wr_te_target_share_sum", "context_notes"]
        ].rename(columns={
            "player_name": "player", "team_old": "from", "team_new": "to",
            "pass_rate_old": "pass rate (old)", "pass_rate_new": "pass rate (new)",
            "oline_score_old": "O-line (old)", "oline_score_new": "O-line (new)",
            "wr_te_target_share_sum": "new team WR/TE competition",
            "context_notes": "context",
        }),
        use_container_width=True,
        hide_index=True,
    )


def load_live_players_df(count_limit: int, enrich: bool, season: int) -> pd.DataFrame:
    """Pull real waiver-wire players via YahooAuthManager + player_mapper,
    optionally backfilled with nfl_data_py via nfl_enrichment.

    Raises whatever YahooAuthManager/player_mapper raises (missing
    private.json, auth failure, etc.) -- the caller in main() decides how
    to surface that to the user.
    """
    from api.player_mapper import build_players_dataframe
    from api.yahoo_auth import YahooAuthManager

    auth = YahooAuthManager()
    free_agents = auth.get_waiver_wire_players(count_limit=count_limit)
    return build_players_dataframe(
        auth, free_agents, count_limit=count_limit, enrich=enrich, season=season
    )


def main() -> None:
    from api.nfl_enrichment import default_stats_season

    st.set_page_config(page_title="CoachPops Fantasy Manager", page_icon="🏈", layout="wide")
    st.title("🏈 CoachPops Fantasy Football Manager")

    private_json_exists = (PROJECT_ROOT / "private.json").is_file()

    with st.sidebar:
        st.header("Data source")
        data_source = st.radio(
            "Waiver wire data",
            options=["Mock data", "Live Yahoo data"],
            index=0,
            disabled=not private_json_exists,
            help=None if private_json_exists else "Create private.json first (see README).",
        )
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
            disabled=data_source != "Live Yahoo data" or not enrich_with_nfl_data,
        )

    if data_source == "Live Yahoo data":
        try:
            with st.spinner("Fetching live waiver wire data from Yahoo..."):
                players_df = load_live_players_df(live_count_limit, enrich_with_nfl_data, int(nfl_season))
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
    else:
        st.warning(
            "Showing **mock data** shaped to match Yahoo's real `/players` response. "
            "Create `private.json` (see README) and select \"Live Yahoo data\" in the "
            "sidebar to see your real waiver wire.",
            icon="⚠️",
        )
        players_df = build_mock_players_df()

    tab1, tab2, tab3, tab4, tab5, tab6, tab7, tab8, tab9 = st.tabs(
        [
            "League Optimizer",
            "Rookie Radar",
            "QB Konami Code",
            "IR Stash Targets",
            "WR3 Floor Finder",
            "Breakout Radar",
            "TE Difference-Makers",
            "O-Line Power Rankings",
            "Team Change Impact",
        ]
    )
    with tab1:
        render_league_optimizer(players_df)
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


if __name__ == "__main__":
    main()
