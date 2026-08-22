"""Yahoo Fantasy Sports API authentication and query bridge, built on yfpy.

Credential storage
-------------------
Per your instructions, credentials live in a ``private.json`` file at the
project root instead of a ``.env`` file. ``private.json`` is already listed
in ``.gitignore`` -- never commit your real one. Copy ``private.json.example``
to ``private.json`` and fill in your values before using this module.

A note on "yfpy's native private.json method"
-----------------------------------------------
Older releases of yfpy (roughly v14 and earlier) delegated OAuth entirely to
the ``yahoo_oauth`` library, which has its own built-in convention of reading
*and writing* a JSON credentials file (traditionally named ``private.json``)
containing ``consumer_key``/``consumer_secret`` plus the access/refresh token
it manages for you. Pointing yfpy at that file was a first-class, "native"
feature back then.

The version of yfpy pinned in ``requirements.txt`` (17.x) refactored this: it
now calls ``yahoo_oauth.OAuth2(..., store_file=False)`` internally, and
instead expects the consumer key/secret and (optionally) a saved access
token to be passed directly into ``YahooFantasySportsQuery(...)``, or pulled
from environment variables / a ``.env`` file. There is no longer a built-in
"just point me at a JSON file" switch inside yfpy itself.

To give you the single-file, no-``.env`` workflow you asked for anyway, this
class reads ``private.json`` itself, hands the values to
``YahooFantasySportsQuery`` as constructor arguments, and writes the
refreshed OAuth token back into the same file after every authentication.
Functionally that reproduces the old "native" behavior -- it's just
implemented in our own code instead of inside yfpy.

---------------------------------------------------------------------------
HOW TO TRIGGER THE INITIAL BROWSER AUTHENTICATION
---------------------------------------------------------------------------
1. Register an app at https://developer.yahoo.com/apps/create/
   - Redirect URI: use ``https://localhost:8080`` for a "Web Application",
     or select "Installed Application" / desktop app to get the simpler
     out-of-band (OOB) flow described in step 4 below.
   - API Permissions: check "Fantasy Sports" (Read, or Read/Write if you
     plan to submit waiver claims / lineup changes through the API).
   - Copy the generated "Client ID" into ``consumer_key`` and the
     "Client Secret" into ``consumer_secret`` in ``private.json``.
2. Fill in ``league_id`` (the numeric ID from your league's Yahoo URL) and
   ``game_code`` (``"nfl"``) in ``private.json``.
3. The FIRST time any method on this class touches ``self.query`` (i.e. the
   first time you call ``get_league_settings()``, ``get_waiver_wire_players()``,
   or ``get_rosters()``), yfpy has no access/refresh token yet, so it opens
   your default web browser to a Yahoo consent screen
   (``browser_callback=True``).
4. Log into the Yahoo account that owns/manages your fantasy league and
   click "Agree".
   - Web Application redirect: Yahoo redirects to your redirect URI with a
     ``?code=...`` query param; yahoo_oauth reads it automatically.
   - Installed Application (OOB) redirect: Yahoo instead displays the code
     directly on the page. Copy it and paste it into the terminal prompt
     that yfpy/yahoo_oauth prints (something like "Enter verifier: ").
5. Once authorized, yfpy holds the resulting ``access_token`` /
   ``refresh_token`` / ``token_time`` in memory; this class immediately
   writes them back into ``private.json`` under the ``"access_token"`` key
   so step 3 won't happen again until that refresh token itself expires or
   is revoked from your Yahoo account settings.
---------------------------------------------------------------------------
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from yfpy.query import YahooFantasySportsQuery

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PRIVATE_JSON_PATH = PROJECT_ROOT / "private.json"


class YahooAuthManager:
    """Owns the ``private.json`` credential file and a yfpy query client."""

    def __init__(self, private_json_path: Union[str, Path] = DEFAULT_PRIVATE_JSON_PATH):
        self.private_json_path = Path(private_json_path)
        self._credentials: Dict[str, Any] = self._load_private_json()
        self._query: Optional[YahooFantasySportsQuery] = None

    # ------------------------------------------------------------------
    # private.json handling
    # ------------------------------------------------------------------

    def _load_private_json(self) -> Dict[str, Any]:
        if not self.private_json_path.is_file():
            raise FileNotFoundError(
                f"{self.private_json_path} not found. Copy private.json.example to "
                "private.json at the project root and fill in your Yahoo consumer_key, "
                "consumer_secret, league_id, and game_code before running this."
            )
        with open(self.private_json_path, "r") as f:
            return json.load(f)

    def _save_private_json(self) -> None:
        with open(self.private_json_path, "w") as f:
            json.dump(self._credentials, f, indent=2)

    def _persist_refreshed_token(self) -> None:
        """Copy the (possibly newly-refreshed) OAuth token back into private.json."""
        token_dict = getattr(self._query, "_yahoo_access_token_dict", None)
        if token_dict:
            self._credentials["access_token"] = token_dict
            self._save_private_json()

    # ------------------------------------------------------------------
    # yfpy client
    # ------------------------------------------------------------------

    @property
    def query(self) -> YahooFantasySportsQuery:
        """Lazily authenticate (triggering the browser flow if needed) and
        return a ready-to-use yfpy query client."""
        if self._query is None:
            self._query = YahooFantasySportsQuery(
                league_id=self._credentials["league_id"],
                game_code=self._credentials.get("game_code", "nfl"),
                game_id=self._credentials.get("game_id"),
                yahoo_consumer_key=self._credentials.get("consumer_key"),
                yahoo_consumer_secret=self._credentials.get("consumer_secret"),
                yahoo_access_token_json=self._credentials.get("access_token"),
                env_var_fallback=False,  # we manage credentials ourselves via private.json
                browser_callback=True,   # opens a browser tab on first auth / once the refresh token dies
            )
            self._persist_refreshed_token()
        return self._query

    # ------------------------------------------------------------------
    # Requested data-fetching methods
    # ------------------------------------------------------------------

    def get_league_settings(self):
        """Fetch this league's settings: scoring rules, roster positions,
        waiver/trade rules, playoff structure, etc."""
        return self.query.get_league_settings()

    def get_waiver_wire_players(
        self,
        status: str = "FA",
        position: Optional[str] = None,
        count_limit: Optional[int] = None,
    ) -> List[Any]:
        """Fetch players currently available on the waiver wire / as free agents.

        yfpy's high-level ``get_league_players()`` doesn't expose Yahoo's
        ``status`` filter, so this hits the same ``league/.../players``
        endpoint directly with ``status`` appended to the query string.

        Args:
            status: Yahoo player status filter:
                - ``"FA"`` = free agents (unclaimed, no waiver process)
                - ``"W"``  = on waivers (claimed, still in the waiver period)
                - ``"A"``  = all available players (FA + W combined)
                Defaults to ``"FA"``.
            position: Optional Yahoo position code to filter by (e.g. ``"RB"``, ``"WR"``).
            count_limit: Optional cap on the number of players returned.

        Returns:
            list[yfpy.models.Player]: Matching players.
        """
        query = self.query
        league_key = query.get_league_key()
        players: List[Any] = []
        start = 0
        page_size = 25

        while count_limit is None or len(players) < count_limit:
            url = (
                f"https://fantasysports.yahooapis.com/fantasy/v2/league/{league_key}/players;"
                f"status={status}"
            )
            if position:
                url += f";position={position}"
            # `out=percent_owned` pulls each player's league-wide ownership %
            # (and week-over-week delta) in the same call, at no extra API
            # cost -- used by the "rising ownership" breakout signal in
            # data/calculators.py's find_breakout_signals(). NOTE: not
            # verified against a live league yet (no test credentials in
            # this environment) -- if `percent_owned`/`percent_owned_delta`
            # come back empty once you're live, double check this
            # sub-resource name against Yahoo's current API docs.
            url += f";start={start};count={page_size};out=percent_owned"

            try:
                page = query.query(url, ["league", "players"])
            except Exception as exc:
                # yfpy raises YahooFantasySportsDataNotFound once a page is empty
                logger.debug("Stopped paginating waiver wire players: %s", exc)
                break

            page_players = page if isinstance(page, list) else [page] if page else []
            if not page_players:
                break

            players.extend(page_players)
            start += page_size

        return players[:count_limit] if count_limit else players

    def get_rosters(
        self,
        team_id: Optional[Union[str, int]] = None,
        week: Optional[Union[int, str]] = None,
    ) -> Dict[Union[str, int], Any]:
        """Fetch current roster(s) for the league.

        Args:
            team_id: Optional single Yahoo ``team_id`` to fetch just that
                team's roster. If omitted, fetches every team's roster.
            week: Optional week number; defaults to yfpy's ``"current"`` week.

        Returns:
            dict[team_id, yfpy.models.Roster]: Roster(s) keyed by team_id.
        """
        query = self.query
        chosen_week = week if week is not None else "current"

        if team_id is not None:
            return {team_id: query.get_team_roster_by_week(team_id, chosen_week=chosen_week)}

        teams = query.get_league_teams()
        return {
            team.team_id: query.get_team_roster_by_week(team.team_id, chosen_week=chosen_week)
            for team in teams
        }
