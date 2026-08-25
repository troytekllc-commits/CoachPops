"""Identifies QBs whose real value under THIS league's own scoring ranks
much better than where the market actually drafts them -- built for a
"don't draft a QB early" strategy: which QBs are safe to wait on because
their real production doesn't match their draft cost.

Reuses Draft Board's exact pipeline (`build_draft_board_pool()` ->
`build_priority_board()` -> `attach_yahoo_adp()`) rather than
recomputing anything -- this module's only job is to filter that board
to QBs and score the gap between real rank and market rank.

Two independent "market cost" signals, either optional
----------------------------------------------------------
- `yahoo_position_rank` (from `attach_yahoo_adp()`, this league's real
  Yahoo Fantasy Plus ADP export) -- literal ADP, the most direct answer
  to "where does the market actually draft this QB."
- `fantasypros_pos_rank` (from `api/fantasypros.py`'s
  `join_fantasypros_data()`) -- a real, live expert-consensus rank, not
  literally ADP but a strongly correlated proxy, and useful as a second
  opinion when it agrees (or a flag to double-check when it doesn't).

Either can be missing (no Yahoo ADP CSV uploaded, or FantasyPros fetch
skipped/failed) -- `compute_qb_adp_value()` degrades to whichever signal
is actually present rather than requiring both.
"""

from __future__ import annotations

import pandas as pd

QB_POSITION = "QB"

# A value gap at/above this many rank spots is worth calling out by name
# in the plain-language notes -- smaller gaps are just normal draft-day
# noise, not a real edge.
NOTABLE_VALUE_GAP = 3


def compute_qb_adp_value(board: pd.DataFrame) -> pd.DataFrame:
    """`board` is Draft Board's own pool -- must have `display_position`
    and `priority_score`, plus optionally `yahoo_position_rank` (from
    `attach_yahoo_adp()`) and/or `fantasypros_pos_rank` (from
    `join_fantasypros_data()`). Filters to QBs, ranks them by real
    league-scoring value, and adds a `value_score` (market rank minus our
    rank -- positive means we rank them better than the market drafts
    them, i.e. real value) per available signal, sorted by the strongest
    available value signal descending.
    """
    qbs = board[board["display_position"] == QB_POSITION].copy()
    if qbs.empty:
        return qbs

    qbs["our_qb_rank"] = qbs["priority_score"].rank(ascending=False, method="min").astype(int)

    has_yahoo = "yahoo_position_rank" in qbs.columns and qbs["yahoo_position_rank"].notna().any()
    qbs["yahoo_value_score"] = (
        qbs["yahoo_position_rank"] - qbs["our_qb_rank"] if has_yahoo else None
    )

    has_fantasypros = "fantasypros_pos_rank" in qbs.columns and qbs["fantasypros_pos_rank"].notna().any()
    qbs["fantasypros_value_score"] = (
        qbs["fantasypros_pos_rank"] - qbs["our_qb_rank"] if has_fantasypros else None
    )

    qbs["value_notes"] = qbs.apply(_value_notes, axis=1)

    sort_col = "yahoo_value_score" if has_yahoo else ("fantasypros_value_score" if has_fantasypros else "our_qb_rank")
    ascending = sort_col == "our_qb_rank"
    return qbs.sort_values(sort_col, ascending=ascending, na_position="last").reset_index(drop=True)


def _value_notes(row: pd.Series) -> str:
    notes = []
    yahoo_gap = row.get("yahoo_value_score")
    if pd.notna(yahoo_gap):
        if yahoo_gap >= NOTABLE_VALUE_GAP:
            notes.append(
                f"our QB{row['our_qb_rank']}, but Yahoo ADP has him as QB{int(row['yahoo_position_rank'])} "
                f"-- real value, safe to wait"
            )
        elif yahoo_gap <= -NOTABLE_VALUE_GAP:
            notes.append(
                f"Yahoo ADP drafts him as QB{int(row['yahoo_position_rank'])}, earlier than our real "
                f"QB{row['our_qb_rank']} value supports"
            )

    fp_gap = row.get("fantasypros_value_score")
    if pd.notna(fp_gap) and abs(fp_gap) >= NOTABLE_VALUE_GAP:
        agrees = pd.notna(yahoo_gap) and (yahoo_gap > 0) == (fp_gap > 0)
        direction = "also a value" if fp_gap > 0 else "also ranked earlier than our value supports"
        notes.append(
            f"FantasyPros consensus has him as QB{int(row['fantasypros_pos_rank'])}"
            + (f" -- agrees, {direction}" if agrees else f" -- {direction}")
        )

    return "; ".join(notes) if notes else "Roughly in line with market cost"
