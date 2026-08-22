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
  - `nfl_enrichment.py` — backfills `is_rookie`/`draft_capital`/
    `target_share`/`deep_target_share`/a rough `projected_points_by_week`
    baseline from `nfl_data_py`, joined onto Yahoo players via nflverse's
    own `yahoo_id` crosswalk column (a real ID match, not name/team
    guessing). See its module docstring for coverage (~half of rostered
    players) and accuracy caveats (the projection is a flat points-per-game
    baseline, not a true weekly projection).
- `data/` — analytics engines and local data/caches
  - `calculators.py` — five pandas-based scoring engines tuned for this
    league's 3-WR / 6-bench / 2-IR format: `calculate_custom_value()`,
    `apply_rookie_bump()`, `calculate_qb_floor()`, `find_ir_stashes()`,
    `evaluate_wr_scarcity()`.
  - `cache/` — on-disk cache for pulled data (gitignored; kept via `.gitkeep`)
- `ui/` — Streamlit dashboard
  - `dashboard.py` — 5-tab dashboard (League Optimizer, Rookie Radar,
    QB Konami Code, IR Stash Targets, WR3 Floor Finder), with a sidebar
    toggle between mock data and live Yahoo data.
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

1. Register an app at https://developer.yahoo.com/apps/create/
   - **Application Type**: "Installed Application" is simplest for local
     use — it gives you Yahoo's out-of-band (OOB) flow, where the auth code
     is displayed directly on the page for you to copy/paste, instead of
     needing a working redirect URI.
   - **Redirect URI**: if prompted anyway, `https://localhost:8080` works
     for local development.
   - **API Permissions**: check "Fantasy Sports" (Read is enough unless you
     plan to submit waiver claims / lineup changes through the API).
   - Copy the generated **Client ID** and **Client Secret** — you'll need
     both in the next step.
2. Find your league ID: open your league on Yahoo and look at the URL,
   e.g. `https://football.fantasysports.yahoo.com/f1/123456` → league ID
   is `123456`.
3. Copy the template and fill in your values:
   ```bash
   cp private.json.example private.json
   ```
   Set `consumer_key` / `consumer_secret` to the Client ID/Secret from step 1,
   and `league_id` to the ID from step 2. Leave `game_code` as `"nfl"` and
   `access_token` as `null`.
4. Run the one-off login script to complete the OAuth handshake:
   ```bash
   python scripts/yahoo_login.py
   ```
   This opens a browser window (or prints a URL if no browser is available)
   for you to log into the Yahoo account that owns/manages your league and
   click "Agree". If you registered an "Installed Application", Yahoo shows
   the verification code directly on the page — copy it and paste it into
   the terminal prompt. See the module docstring in `api/yahoo_auth.py` for
   more detail. On success, the script prints your league name and a few
   sample free agents, and caches the resulting token back into
   `private.json` so you won't be prompted again until it's revoked or
   expires.

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
**Rookie Radar**, **IR Stash Targets**, and **WR3 Floor Finder** have
something to show. Coverage is roughly half of rostered players (nflverse's
`yahoo_id` crosswalk doesn't cover everyone), and the weekly projection
feeding IR Stash Targets is a flat points-per-game baseline, not a true
projection — see `api/nfl_enrichment.py`'s module docstring for the full
picture. Turn the checkbox off to see Yahoo-only data instead, in which
case only **League Optimizer** and **QB Konami Code** will show results.
