# CoachPops — Fantasy Football Manager Dashboard

A Python-based dashboard for managing a fantasy football team, built with
[Streamlit](https://streamlit.io/), [pandas](https://pandas.pydata.org/),
[yfpy](https://github.com/uberfastman/yfpy) (Yahoo Fantasy Sports API
wrapper), and [nfl_data_py](https://github.com/nflverse/nfl_data_py).

## Project Structure

- `api/` — Yahoo Fantasy Sports integration
  - `yahoo_auth.py` — `YahooAuthManager`: wraps `yfpy`'s `YahooFantasySportsQuery`,
    reading credentials from `private.json` (see Setup below). Exposes
    `get_league_settings()`, `get_waiver_wire_players()`, and `get_rosters()`.
  - `player_mapper.py` — converts yfpy `Player` objects into the DataFrame
    shape the calculators/dashboard expect, remapping Yahoo's stat IDs to
    stat names via this league's scoring settings, and (by default) calling
    `nfl_enrichment.py` to backfill what Yahoo doesn't provide.
  - `nfl_enrichment.py` — backfills what Yahoo doesn't provide from
    `nfl_data_py`, joined onto Yahoo players via nflverse's own `yahoo_id`
    crosswalk column (a real ID match, not name/team guessing): rookie
    status/draft capital, target share, deep-target share, red-zone share,
    injury-opened opportunity (depth chart + injury report), team coaching
    changes (real for head coaches via `import_schedules()`; manual/opt-in
    for coordinators — see `data/coaching_changes.csv.example`), a QB
    "sophomore slump" caution flag, and a rough `projected_points_by_week`
    baseline. See its module docstring for coverage (~half of rostered
    players) and every accuracy caveat.
- `data/` — analytics engines and local data/caches
  - `calculators.py` — seven pandas-based scoring engines tuned for this
    league's 3-WR / 6-bench / 2-IR format: `calculate_custom_value()`,
    `apply_rookie_bump()`, `calculate_qb_floor()`, `find_ir_stashes()`,
    `evaluate_wr_scarcity()`, `find_breakout_signals()`,
    `find_te_difference_makers()` (target share, red-zone share, TE snap
    share/receiving-role split, and team pass volume/efficiency — the
    pre-box-score signals that predict a jump into TE's thin top tier).
  - `coaching_changes.csv.example` — template for the manually maintained
    OC/DC-hire data `nfl_enrichment.py` reads (copy to `coaching_changes.csv`
    and fill in from public reporting each offseason — no API exists for
    this, see the caveat above).
  - `cache/` — on-disk cache for pulled data (gitignored; kept via `.gitkeep`)
- `api/oline_analytics.py` — **team-level** O-Line Power Rankings (no
  Yahoo data needed): pass protection and run blocking from play-by-play
  data, starting-five continuity from snap counts, and draft investment,
  combined into a 0-100 score per team, plus year-over-year rank change,
  new-starter turnover, and coaching-change flags.
- `api/team_change_analytics.py` — tracks QB/RB/WR/TE players who changed
  teams since last season and lays out the context that determines
  whether it helps or hurts (team pass rate/efficiency, O-Line strength,
  new team's WR/TE target competition, coaching changes) — deliberately
  no single "value went up/down" score, since that's genuinely
  position-dependent; see its module docstring.
- `ui/` — Streamlit dashboard
  - `dashboard.py` — 9-tab dashboard (League Optimizer, Rookie Radar,
    QB Konami Code, IR Stash Targets, WR3 Floor Finder, Breakout Radar,
    **TE Difference-Makers**, **O-Line Power Rankings**, **Team Change
    Impact**), with a sidebar toggle between mock data and live Yahoo
    data. The last two tabs work with zero Yahoo access — pure
    `nfl_data_py`.

### A note on season defaults

Two different "current season" concepts matter here. `default_nfl_season()`
(the roster/draft-capital season — e.g. `2026` once that season's rosters
and draft class exist, months before a game is played) and
`default_stats_season()` (the last season with actual played games — e.g.
still `2025` in August, before Week 1). The O-Line Power Rankings and Team
Change Impact tabs, and the live-data sidebar's enrichment season, all use
`default_stats_season()` — verified directly: `import_seasonal_rosters([2026])`
returns real data in August 2026, but `import_pbp_data([2026])` 404s since
no games have been played yet.
- `scripts/yahoo_login.py` — one-off CLI script that performs the initial
  Yahoo OAuth browser handshake (run this before switching the dashboard to
  live data).
- `app.py` — root entry point that runs the Streamlit dashboard.

## Setup

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### Yahoo API credentials (`private.json`)

This project stores Yahoo Developer Network credentials in a `private.json`
file at the project root (gitignored) instead of a `.env` file.

1. Register an app at https://developer.yahoo.com/apps/create/. Yahoo's
   console has been redesigned — there's no more "Installed Application"
   checkbox, and the classic bare `oob` redirect value gets rejected as
   invalid. Fill in the current form like this:
   - **Application Name / Description**: anything you want.
   - **Homepage URL**: optional, not used by this app's auth flow.
   - **Redirect URI(s)**: enter `https://localhost:8080` (any `https://`
     URL works — it never has to actually be reachable; see step 4 below
     for why). Whatever you put here **must exactly match** the
     `redirect_uri` value you'll set in `private.json` in step 3.
   - **OAuth Client Type**: "Confidential Client" (this runs locally with
     a client secret, not as a public/native/single-page-app client).
   - **API Permissions**: this section will be empty — Fantasy Sports is
     no longer a self-serve checkbox here. See step 1a.
   - Copy the generated **Client ID** and **Client Secret** — you'll need
     both in a later step.
1a. Apply for Fantasy Sports API access at
    **https://sports.yahoo.com/developer/access/** — Yahoo now manually
    reviews Fantasy Sports API access per application instead of it being
    a console checkbox. The form asks for your product/use case, the data
    you need (read access to league settings/rosters/players is enough
    here), your user base (there's an explicit "personal or single league
    use" option), and the **Client ID** from the app you just created —
    fill that in so approval attaches to it. Yahoo's Fantasy Sports team
    reviews submissions (no published turnaround time); nothing below
    this point will work until you're approved.
2. Find your league ID: open your league on Yahoo and look at the URL,
   e.g. `https://football.fantasysports.yahoo.com/f1/123456` → league ID
   is `123456`.
3. Copy the template and fill in your values:
   ```bash
   cp private.json.example private.json
   ```
   Set `consumer_key` / `consumer_secret` to the Client ID/Secret from step 1,
   `league_id` to the ID from step 2, and `redirect_uri` to exactly what you
   registered in step 1 (e.g. `https://localhost:8080`). Leave `game_code`
   as `"nfl"` and `access_token` as `null`.
4. Run the one-off login script to complete the OAuth handshake:
   ```bash
   python scripts/yahoo_login.py
   ```
   This opens a browser window for you to log into the Yahoo account that
   owns/manages your league and click "Agree". Yahoo then redirects your
   browser to the URI you registered; since nothing is actually listening
   there, the page fails to load — but the code you need is sitting right
   in the browser's address bar (`...?code=XXXX`). Copy just that value and
   paste it into the terminal prompt ("Enter verifier : "). See the module
   docstring in `api/yahoo_auth.py` for more detail. On success, the script
   prints your league name and a few sample free agents, and caches the
   resulting token back into `private.json` so you won't be prompted again
   until it's revoked or expires.

## Running the dashboard

```bash
source venv/bin/activate
streamlit run app.py
```

Streamlit will print a local URL (usually `http://localhost:8501`) —
open it in your browser to view the dashboard. Use the **"Data source"**
toggle in the sidebar to switch between mock data and live Yahoo data (the
live option only appears once `private.json` exists — run
`scripts/yahoo_login.py` first so the OAuth handshake doesn't happen mid
Streamlit-rerun).

With live data selected, a **"Backfill rookie/target-share/projection
data"** checkbox (on by default) pulls in `nfl_data_py` via
`api/nfl_enrichment.py` to fill in what Yahoo's API doesn't provide, so
**Rookie Radar**, **IR Stash Targets**, **WR3 Floor Finder**, and
**Breakout Radar** have something to show. Coverage is roughly half of
rostered players (nflverse's `yahoo_id` crosswalk doesn't cover everyone),
and the weekly projection feeding IR Stash Targets is a flat
points-per-game baseline, not a true projection — see
`api/nfl_enrichment.py`'s module docstring for the full picture. Turn the
checkbox off to see Yahoo-only data instead, in which case only **League
Optimizer** and **QB Konami Code** will show results (Breakout Radar can
still fire on Yahoo's own `percent_owned` data alone).

### Breakout Radar

The **Breakout Radar** tab scans for the predictive patterns behind fantasy's
hardest-to-see-coming performers: an injury-opened opportunity ahead of a
backup, a new offensive play-caller, target share running ahead of the box
score, the wider Yahoo market catching on early (rising/elevated
`percent_owned`), or a Day 3/undrafted rookie already out-producing their
draft slot. It also raises (without penalizing the score) a QB "sophomore
slump" caution: rookie QBs who finished top-15 in fantasy PPG have
historically declined more often than not in Year 2.

Two of those signals need the manual `data/coaching_changes.csv` (copy from
`coaching_changes.csv.example`) to do anything — **head coach** changes are
free/automatic (`import_schedules()` has real coach-name data), but there is
no clean API for **offensive/defensive coordinator** hires (confirmed via
web search — every source is a page built for humans, not a dataset), so
that file is a small, manually updated input, refreshed once per offseason
from public reporting. Without it, the OC-hire signal simply never fires.

Not yet built: whether a rookie/sophomore's positional competition
departed or arrived (a real signal from the same research, but one that
needs careful two-season role-matching to avoid noise) — documented as a
future enhancement in `api/nfl_enrichment.py` rather than shipped
unreliable.
