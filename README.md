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
    baseline. Also pulls in two more real data sources (see "Additional data
    sources" below): Vegas game-script context (`build_game_script_lookup()`,
    from `import_schedules()`'s own `spread_line`/`total_line` columns) and
    Next Gen Stats route-running/passing metrics (`build_ngs_receiving_lookup()`/
    `build_ngs_passing_lookup()`). See its module docstring for coverage
    (~half of rostered players for the nflverse-crosswalk fields) and every
    accuracy caveat.
  - `external_sources.py` — pulls player-level trending data from
    [Sleeper](https://sleeper.com/)'s free, no-auth public API (a different
    fantasy platform, not Yahoo) via its own `yahoo_id` field, so it needs no
    name matching either: `build_sleeper_trending_lookup()` returns how many
    times each player was added across Sleeper leagues in the last 24 hours —
    a signal for "the wider fantasy market is catching on," independent of
    and often faster-moving than this one Yahoo league's `percent_owned`.
    Wrapped in a try/except everywhere it's called — a failed/unreachable
    request is logged and skipped, never raised, since this is a nice-to-have
    signal, not core functionality.
  - `weather.py` — forecasted game-day wind/precipitation for a team's next
    game, via a free OpenWeatherMap API key (see Setup below). Skips
    permanent domes and neutral-site/international games outright (weather
    doesn't apply, or this module doesn't have reliable coordinates for
    them) rather than guessing. Same "skip and log, never raise" pattern as
    `external_sources.py` — no key configured, no network, or the game is
    further out than the free tier's 5-day forecast window all degrade to
    an empty result, not an error.
- `data/` — analytics engines and local data/caches
  - `calculators.py` — seven pandas-based scoring engines tuned for this
    league's 3-WR / 6-bench / 2-IR format: `calculate_custom_value()`,
    `apply_rookie_bump()`, `calculate_qb_floor()`, `find_ir_stashes()`,
    `evaluate_wr_scarcity()`, `find_breakout_signals()` (now also credits a
    favorable Vegas-implied team total and a Sleeper trending-adds spike,
    and raises — without scoring against — a high-wind-forecast caution for
    QB/WR/TE), `find_te_difference_makers()` (target share, red-zone share,
    TE snap share/receiving-role split, team pass volume/efficiency, and
    Next Gen Stats route-running separation — the pre-box-score signals
    that predict a jump into TE's thin top tier).
  - `coaching_changes.csv.example` — template for the manually maintained
    OC/DC-hire data `nfl_enrichment.py` reads (copy to `coaching_changes.csv`
    and fill in from public reporting each offseason — no API exists for
    this, see the caveat above).
  - `cache/` — on-disk cache for pulled data (gitignored; kept via `.gitkeep`)
- `api/oline_analytics.py` — **team-level** O-Line Power Rankings (no
  Yahoo data needed): pass protection and run blocking from play-by-play
  data, starting-five continuity from snap counts, and draft investment,
  combined into a 0-100 score per team, plus year-over-year rank change,
  new-starter turnover, and coaching-change flags. Also surfaces each
  team's Next Gen Stats average QB time-to-throw as display-only context
  (`compute_team_time_to_throw()`) — not part of the composite score, but a
  read on whether a high sack rate points at the O-line (long time-to-throw)
  or the QB/scheme (short time-to-throw).
- `api/team_change_analytics.py` — tracks QB/RB/WR/TE players who changed
  teams since last season and lays out the context that determines
  whether it helps or hurts (team pass rate/efficiency, O-Line strength,
  new team's WR/TE target competition, coaching changes) — deliberately
  no single "value went up/down" score, since that's genuinely
  position-dependent; see its module docstring. Its report now also
  carries a `yahoo_id` column (a real ID crosswalk, riding along on the
  same `import_seasonal_rosters()` row as its `player_id` -- not name
  matching) so `build_priority_board()` can join a mover's context notes
  onto the right Yahoo player.
- `ui/` — Streamlit dashboard
  - `dashboard.py` — 13-tab dashboard. **Priority Board** (see below) is
    first (its table includes each player's raw league-scoring value --
    the former standalone "League Optimizer" tab was folded in here since
    it was just that same number, unblended; see below); then Rookie
    Radar, QB Konami Code, IR Stash Targets, WR3 Floor Finder, Breakout
    Radar, **TE Difference-Makers**,
    **O-Line Power Rankings**, **Team Change Impact**, **Run Game Outlook**
    (see below), **Free Agent Suggestions**, **Trade Finder** (see "Free
    Agent Suggestions & Trade Finder" below), and **Draft Board** (see
    below). A sidebar toggle switches between mock data and live Yahoo
    data; O-Line Power Rankings/Team Change Impact/**Run Game
    Outlook**/**Draft Board** (plus Priority Board's O-Line/Team Change
    context) work with zero Yahoo access — pure `nfl_data_py`. Every tab's
    table is styled via `_style_table()` to match the blue theme
    (`.streamlit/config.toml`): light zebra-striped row banding plus a
    blue-intensity gradient (darker = better) on that tab's key ranking
    column, hand-interpolated between the theme's two blues rather than
    pulling in matplotlib.
  - `api/run_game_analytics.py` — **Run Game Outlook**'s calculators (see
    below).

### Priority Board

The **Priority Board** tab (`build_priority_board()` in
`data/calculators.py`) is the rollup: it blends every other calculator
into one cross-position rank, for the question a real waiver claim or
bench spot actually forces — "of these different positions competing for
the same roster spot, who do I prioritize this week?" Every component is
percentile-ranked to 0-1 before blending (so custom_value's raw points,
breakout_score's small signal-count scale, and te_score's 0-100 all
combine fairly):

- **Production (40%)** — `calculate_custom_value()`'s output, ranked
  *within position* (a QB's raw point total is never compared to a WR's).
  The raw number itself (unblended) is also shown as its own "league
  value pts" column on the board -- this is what the standalone "League
  Optimizer" tab used to show; it's been folded in here instead of kept
  as a separate near-duplicate tab.
- **Opportunity (35%)** — `find_breakout_signals()`'s `breakout_score`,
  ranked pool-wide. It's already a composite of injury opportunity,
  coaching changes, Vegas game script, Sleeper trending, and ownership
  trends, so it doubles here as "how much is about to change" for a player.
- **Specialist (15%)** — whichever position-specific engine applies as a
  bonus: `find_te_difference_makers()` for TEs, `apply_rookie_bump()` for
  rookie RB/WR, `calculate_qb_floor()` for QBs, `evaluate_wr_scarcity()`
  for WRs. Missing/not-applicable stays neutral (0.5), never penalized.
- **Team context (10%)** — the player's team's O-Line Power Rankings
  score, if supplied. Light context (a strong O-line raises the floor
  under an RB/QB), not a primary driver.

`priority_signals` merges Breakout Radar's signals with the specialist
label that applied; `priority_cautions` carries Breakout Radar's cautions
(QB sophomore-slump, high-wind forecast) plus, if a player is in the Team
Change Impact report, that report's plain-language context notes —
carried through as-is, never folded into the score, matching that tab's
own explicit "no single value-up/down number" design. O-Line and Team
Change context are optional and fetched independently in the dashboard
layer — a failure in either (e.g. a season nflverse hasn't published
weekly data for yet) degrades that one context source rather than
breaking the whole board.

### Run Game Outlook

Two real, separate questions (`api/run_game_analytics.py`), built with
zero Yahoo access — pure `nfl_data_py`, same as O-Line Power
Rankings/Team Change Impact:

**Which teams project as the strongest running teams?**
`build_run_game_outlook()` starts from last season's real rush rate
(rushes as a share of rush+dropback plays) and efficiency (yards per
carry, rushing EPA/play), then adjusts using the same forward-looking
philosophy as O-Line Power Rankings: real O-line strength (reused
directly from that tab), real coaching stability (new head
coach/offensive coordinator flags reused from `api/nfl_enrichment.py`),
and whether last season's lead back is still on the roster — a retained
"bell cow" is a continuity signal; a departed one is flagged along with
any notable RB arrival that might replace them (reusing
`api/team_change_analytics.py`'s team-change detection). All of that
blends into one 0-100 `run_outlook_score`, same real-percentiles-blend
approach as `oline_score`/`priority_score` — directional, not a
synthetic simulation of a season with zero snaps played yet.

**Which RBs are the most heavily used, and who's a true bell cow?**
`build_rb_workload_report()` computes each RB's real season
carries/targets/receptions/receiving yards/touches, plus their *share*
of their own team's RB-room usage on three independent axes — carry
share, touch share, and offensive snap share. That share is what
actually separates a bell cow from a committee back getting decent raw
volume on a pass-heavy offense; `bell_cow_score` averages the three, and
`usage_tier` buckets it into Bell Cow (≥70%) / Lead Back (≥50%) /
Committee Lead (≥30%) / Depth-Committee.

Validated against real 2024 outcomes: the workload report's top
"Bell Cow" tier correctly surfaced Kyren Williams, Jonathan Taylor,
Saquon Barkley, and Derrick Henry; the team outlook correctly flagged
real offseason lead-back departures/arrivals (e.g. Tennessee's Derrick
Henry departing to Baltimore, Philadelphia's D'Andre Swift departing as
Saquon Barkley arrived). Like Draft Board, the season selector falls
back one year automatically (with a visible warning) if nflverse hasn't
published full weekly stats for the selected season yet.

### Free Agent Suggestions & Trade Finder

These two tabs turn every other tab's analysis into concrete actions —
"add this player, drop that one" / "propose this trade" — instead of
just rankings you'd still have to act on yourself manually.

Both need every team's roster, not just the waiver wire, to do anything —
something only live Yahoo access can give (`api/player_mapper.py`'s
`build_league_rosters_dataframe()`, via `YahooAuthManager.get_rosters()`
with no `team_id` — fetches every team at once). **This has not yet been
smoke-tested against a live league** — Yahoo API access was still pending
approval as of writing this — though every yfpy attribute it reads
(`Roster.players`, `Team.team_id`, `Team.is_owned_by_current_login`) was
confirmed directly against yfpy's own installed source, not guessed at.
Until then (and as an automatic fallback if a live fetch ever fails),
both tabs run against a procedural 10-team mock league
(`build_mock_league_rosters()` in `ui/dashboard.py`) — deliberately
imbalanced (your mock team is RB-stacked/WR-thin; one other team is the
mirror image) so there's always a real scenario to demonstrate, rather
than hand-authoring ~10 full rosters by hand.

Both are built on the standard fantasy-analysis idea of **replacement
level** (`compute_replacement_level()` in `data/calculators.py`): a
player's value isn't their raw score, it's how far above the point where
you could just plug in whoever's next-available at that position
league-wide. A team has real tradeable **surplus** at a position when it
has more startable (at-or-above-replacement-level) players there than
starting slots to fill; it has a real **need** when the opposite is true
(`assess_team_needs()`).

- **Free Agent Suggestions** (`find_free_agent_upgrades()`) compares your
  weakest rostered player at each position against the best available
  free agent, using Priority Board's blended `priority_score` for both —
  a concrete drop/add pair, not free agents ranked with no connection to
  your actual roster.
- **Trade Finder** (`find_trade_candidates()`) proactively scans every
  other team for a genuine two-way fit: a position where you have
  surplus and they have a need, paired with a position where they have
  surplus and you have a need — filtered to trades within a fairness
  tolerance (both traded players' `priority_score`s within ~35% of each
  other by default). **Read this caveat before trusting a suggestion**:
  it's a value-and-need heuristic, not a negotiation — it has no idea
  whether a manager actually wants to trade, their own roster philosophy,
  keeper/dynasty considerations, or plain stubbornness. Treat every
  suggestion as a conversation starter to evaluate yourself, never a
  trade either side is guaranteed to accept.

### Draft Board

Built specifically for pre-draft research while Yahoo API access is
pending approval: unlike every other player-level tab, **Draft Board**
needs zero Yahoo access at all — pure `nfl_data_py`, same as O-Line Power
Rankings/Team Change Impact — because a draft doesn't need *your* roster
or waiver wire, it needs every real player ranked.

`build_draft_board_pool()` (`api/nfl_enrichment.py`) pulls every real
skill-position (QB/RB/WR/TE) player rostered that season, their actual
season stat totals (via a new `load_season_stat_totals()` loader — real
nfl_data_py column names confirmed directly, not guessed), and every
enrichment signal the rest of this app already computes — then runs the
result through the exact same `build_priority_board()` blend as the
Priority Board tab. This required generalizing `build_enrichment_lookup()`/
`enrich_players_dataframe()` with a `key_by` argument: everywhere else in
this project keys onto Yahoo's own player IDs (covering only ~half of
rostered players), but a draft board needs *every* player, so this keys
by nflverse's own gsis `player_id` instead — zero Yahoo dependency, by
construction, not just by accident.

**Read this before trusting the rankings**: this is last season's real,
actual performance under your league's own scoring — not a synthetic
projection for the upcoming season. No real projections feed is
connected (see "Additional data sources" → paid services notes on
FantasyPros below); this is the most defensible free proxy available,
not a finished cheat sheet that already knows about every offseason
move (a free-agent signing that changes a target competition, for
instance, isn't reflected here).

The season selector defaults to `default_stats_season()` (the last
*completed* season) and automatically falls back one year if nflverse
hasn't published full weekly stats for it yet — a real, currently-live
gap as of this writing (`import_weekly_data([2025])` 404s; 2024 and
earlier work). The fallback shows a clear warning rather than silently
serving stale-looking data or crashing the tab.

#### Yahoo ADP reference (optional, manual)

If `data/yahoo_draft_reference.csv` exists, Draft Board also shows this
league's real Yahoo Fantasy Plus ADP (average draft position), tier, and
a **"value vs. Yahoo ADP"** column (this board's own position rank minus
Yahoo's — positive means this board likes the player more than Yahoo's
drafters do; negative means Yahoo's ADP has them going earlier than this
board would).

This file has no API behind it — it's a one-time, manually transcribed
snapshot of a real Yahoo Fantasy Plus premium "Cheat Sheet" PDF export
for this exact league (columns: `position,tier,overall_rank,
first_initial,last_name,team,adp`). Unlike every other join in this
project, there's no stable ID to cross-reference a rendered PDF against,
so `attach_yahoo_adp()` (`api/nfl_enrichment.py`) name-matches instead —
a deliberate, documented exception to this project's usual "never guess
by name" rule, made only because there's no alternative. It matches on
`(position, first initial, normalized last name, team)` first, falling
back to `(position, first initial, last name)` alone when that's
unambiguous — this fallback matters because the reference file's team
reflects *today's* roster while the stats pool's team can be a season or
more stale, and real trades happen in between.

**To refresh**: export a fresh Cheat Sheet PDF from your Yahoo Fantasy
Plus account, re-transcribe it into the same CSV format, and replace
`data/yahoo_draft_reference.csv`. Just ask Claude to do this and hand it
the new PDF — no code changes needed. Expect real, harmless misses for
players who weren't on any roster in whatever season the stats pool
covers (this year's incoming rookie class, by definition, since the pool
is last *completed* season's stats).

Also worth knowing: this same PDF is what confirmed this league's real
roster construction (`DEFAULT_ROSTER_REQUIREMENTS` in
`data/calculators.py`) — 1 QB / 2 RB / 3 WR / 1 TE / 1 FLEX / 1 K / 1 DEF
/ 6 BN / 2 IR, with the FLEX slot being **"W/R" only** (TE is not
FLEX-eligible in this league), which Free Agent Suggestions/Trade
Finder's need calculations already assume.

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

### Weather forecasts (optional, `openweathermap_api_key`)

Forecasted wind/precipitation (see `api/weather.py`) needs a free
OpenWeatherMap API key — get one at https://openweathermap.org/price (the
free tier's 5-day forecast is all this project uses). Either set it as an
environment variable:

```bash
export OPENWEATHERMAP_API_KEY=your-key-here
```

or add it to `private.json` alongside your Yahoo credentials:

```json
{
  "consumer_key": "...",
  "openweathermap_api_key": "your-key-here"
}
```

This is entirely optional — without a key, every weather field simply
stays empty (logged, never an error), same as any other missing enrichment
source in this project.

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

## Deploying (Streamlit Community Cloud)

Running `streamlit run app.py` locally works, but only while that terminal
stays open on your machine. To get a real URL you (or anyone) can open
anytime, deploy to [Streamlit Community Cloud](https://share.streamlit.io)
— free, and built for exactly this kind of project:

1. Go to https://share.streamlit.io and sign in with GitHub (the same
   account this repo is under).
2. Click **"New app"**, then pick:
   - Repository: `troytekllc-commits/CoachPops`
   - Branch: `claude/fantasy-football-dashboard-4buwpp`
   - Main file path: `app.py`
3. Click **Deploy**. Streamlit installs `requirements.txt` and starts the
   app automatically — this part needs no code changes, it's already set
   up to run this way.

That gets you a working URL immediately, showing **mock data** (there's no
`private.json` on the deployed server, and there never will be — it's
gitignored on purpose, see the Yahoo credentials section above). Two tabs
(**O-Line Power Rankings**, **Team Change Impact**) and most of
**Priority Board**'s context work fully live at this point, with zero
extra setup, since they don't need Yahoo at all.

### Adding your Yahoo/weather credentials to the deployed app

A hosted server has no local filesystem to put a `private.json` file on,
so credentials go through Streamlit's own **Secrets** manager instead
(Settings → Secrets, in your deployed app's dashboard) — `api/yahoo_auth.py`
and `api/weather.py` both check for this automatically (via `st.secrets`)
whenever there's no local `private.json` file, no code changes needed.

Paste in:

```toml
openweathermap_api_key = "your-openweathermap-key"

private_json = """
{"consumer_key": "...", "consumer_secret": "...", "league_id": "...", "game_code": "nfl", "game_id": null, "redirect_uri": "https://localhost:8080", "access_token": {...}}
"""
```

The `private_json` value is a raw JSON string (in a TOML triple-quoted
block) — literally the exact same content you'd put in a local
`private.json` file, `access_token` and all. That's deliberate: the Yahoo
OAuth browser handshake (`scripts/yahoo_login.py`) needs a real terminal
and browser to complete, which a hosted server doesn't have — so run it
**locally first** to get a working `access_token`, then copy your whole
local `private.json`'s contents into this one secret. Once both secrets
are set, redeploy (or just wait for the app to pick up the change) and
the "Live Yahoo data" option becomes available on the hosted app too.

### Additional data sources

Beyond Yahoo and nflverse's core stats, four more real (non-mock) data
sources feed the calculators above:

- **Vegas game script** (`build_game_script_lookup()` in
  `api/nfl_enrichment.py`): each team's point spread and implied point total
  for its next scheduled game, from `import_schedules()`'s own
  `spread_line`/`total_line` columns (the older `import_sc_lines()` stops
  around 2020 and isn't usable for a current season). The spread's sign
  convention (positive = that team favored) was verified empirically against
  actual game margins, not assumed. A team projected for a high implied
  total is playing in a game expected to feature more scoring — a real,
  pre-box-score tailwind `find_breakout_signals()` now credits. During the
  off-season, with no upcoming game on the schedule, this falls back to the
  final week of the completed season rather than showing nothing.
- **Next Gen Stats** (`load_ngs_receiving()`/`load_ngs_passing()` and their
  `build_*_lookup()` counterparts in `api/nfl_enrichment.py`): route-running
  separation and YAC-over-expectation for receivers/TEs, and time-to-throw
  and completion-%-over-expectation (CPOE) for QBs — all joined via the real
  `player_gsis_id` NGS already carries, no name matching needed. Separation
  feeds `find_te_difference_makers()` directly; CPOE is shown for context in
  QB Konami Code; time-to-throw is shown for context in O-Line Power
  Rankings (see above).
- **Sleeper trending adds** (`api/external_sources.py`): how many times each
  player was added across Sleeper's (a different, no-auth, free-API fantasy
  platform) leagues in the last 24 hours, joined via the `yahoo_id` field
  Sleeper's own player database carries. A spike here is the wider fantasy
  market catching on, often before this one Yahoo league's own
  `percent_owned` moves — `find_breakout_signals()` credits a large spike
  (5,000+ recent adds). Coverage is partial (verified live: roughly 9 of the
  top 50 trending players matched a `yahoo_id`) and a failed request is
  skipped, not raised.
- **Forecasted game-day weather** (`api/weather.py`, optional — needs a
  free OpenWeatherMap key, see Setup above): wind and precipitation for a
  team's next game. Not sourced from `import_schedules()`'s own `temp`/
  `wind` columns -- confirmed those are the *actual* recorded conditions,
  filled in only after a game is played (0 of 272 games populated for the
  fully-future 2026 schedule) -- so predicting an upcoming game's weather
  needs a real forecast API instead. Skips permanent domes and
  neutral-site/international games outright. Sustained wind above 15 mph
  raises (without penalizing the score) a caution in `find_breakout_signals()`
  for QB/WR/TE, since it's a well-documented suppressor of passing volume
  and efficiency.

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
