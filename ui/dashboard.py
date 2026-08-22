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
    find_ir_stashes,
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
        "on a bad passing day the way a pure pocket passer's does."
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
        qbs[["player_name", "editorial_team_abbr", "passing_points", "qb_floor_score"]]
        .rename(columns={"editorial_team_abbr": "team", "qb_floor_score": "rushing_floor_score"}),
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
    from api.nfl_enrichment import default_nfl_season

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
            "NFL season", min_value=2015, max_value=2035, value=default_nfl_season(), step=1,
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
                    "Backfill disabled -- `is_rookie`, `draft_capital`, `target_share`, "
                    "`deep_target_share`, and `projected_points_by_week` are all at their "
                    "empty Yahoo-only defaults, so Rookie Radar, IR Stash Targets, and "
                    "WR3 Floor Finder will show empty."
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

    tab1, tab2, tab3, tab4, tab5 = st.tabs(
        [
            "League Optimizer",
            "Rookie Radar",
            "QB Konami Code",
            "IR Stash Targets",
            "WR3 Floor Finder",
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


if __name__ == "__main__":
    main()
