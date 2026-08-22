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
- `data/` — analytics engines and local data/caches
  - `calculators.py` — five pandas-based scoring engines tuned for this
    league's 3-WR / 6-bench / 2-IR format: `calculate_custom_value()`,
    `apply_rookie_bump()`, `calculate_qb_floor()`, `find_ir_stashes()`,
    `evaluate_wr_scarcity()`.
  - `cache/` — on-disk cache for pulled data (gitignored; kept via `.gitkeep`)
- `ui/` — Streamlit dashboard
  - `dashboard.py` — 5-tab dashboard (League Optimizer, Rookie Radar,
    QB Konami Code, IR Stash Targets, WR3 Floor Finder), currently running
    on mock data shaped like Yahoo's real `/players` response.
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

1. Register an app at https://developer.yahoo.com/apps/create/ with the
   "Fantasy Sports" API permission enabled.
2. Copy the template and fill in your values:
   ```bash
   cp private.json.example private.json
   ```
   Set `consumer_key` / `consumer_secret` to the app's Client ID/Secret, and
   `league_id` to your league's numeric ID (from its Yahoo URL).
3. The first call into `YahooAuthManager` (e.g. `get_waiver_wire_players()`)
   opens a browser window for you to authorize the app against your Yahoo
   account. See the module docstring in `api/yahoo_auth.py` for the full
   walkthrough, including the out-of-band flow if you registered an
   "Installed Application" instead of a web app. Once authorized, the
   resulting token is written back into `private.json` so you won't be
   prompted again until it's revoked or expires.

## Running the dashboard

```bash
source venv/bin/activate
streamlit run app.py
```

Streamlit will print a local URL (usually `http://localhost:8501`) —
open it in your browser to view the dashboard. It currently displays mock
waiver-wire data; wire up `private.json` and swap
`build_mock_players_df()` in `ui/dashboard.py` for a live
`YahooAuthManager().get_waiver_wire_players()` call to see real data.
