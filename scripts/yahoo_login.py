"""One-off CLI script to perform the initial Yahoo OAuth browser handshake.

Run this BEFORE launching the Streamlit dashboard with live data. Streamlit
reruns its script on every interaction, which is a bad place to run a
one-time interactive "open a browser and paste a code back" flow -- do it
here once instead. After a successful run, your access/refresh token is
cached in `private.json`, and the dashboard (or any other script) can reuse
it silently until it's revoked or expires.

Usage (from the project root, with the venv activated):
    python scripts/yahoo_login.py

See the module docstring in api/yahoo_auth.py for the full walkthrough of
registering a Yahoo Developer Network app and completing the OAuth consent
screen.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent.parent))

from api.yahoo_auth import YahooAuthManager  # noqa: E402


def main() -> None:
    print("Authenticating with Yahoo... (a browser window may open)")
    auth = YahooAuthManager()

    # Touching `.query` triggers authentication (see YahooAuthManager.query).
    _ = auth.query
    print(f"Authenticated. Token cached in {auth.private_json_path}")

    settings = auth.get_league_settings()
    print(f"Connected to league: {getattr(settings, 'name', '(name unavailable)')}")

    free_agents = auth.get_waiver_wire_players(count_limit=5)
    print(f"Fetched {len(free_agents)} sample free agent(s):")
    for player in free_agents:
        full_name = getattr(getattr(player, "name", None), "full", "?")
        print(f"  - {full_name} ({getattr(player, 'display_position', '?')}, "
              f"{getattr(player, 'editorial_team_abbr', '?')})")


if __name__ == "__main__":
    main()
