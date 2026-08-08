"""arc_game — play an ARC-AGI-3 interactive environment.

Added to answer a specific question, not to chase a benchmark: CAN THIS
AGENT LOOP DRIVE A STATEFUL, INTERACTIVE API AT ALL? Every business task
worth doing has that shape — a booking flow, a web app, a multi-step
form. ARC-AGI-3 is simply the cleanest available instrument, because it
scores the answer objectively instead of leaving it to opinion.

Why a built-in rather than a config connector (the default for anything
new, see http_tool_store.py): the arcade returns a 64x64 integer grid as
raw JSON, ~12.4KB per frame. HTTPToolRunner truncates every response at
MAX_RESPONSE_CHARS = 6000, so a pure-config connector would hand the
model a frame cut off mid-row on EVERY call — unplayable, and worse,
unplayable in a way that looks like the model being stupid rather than
the plumbing being wrong. The grid needs rendering before it reaches a
prompt, and rendering is code.

The rendering is deliberately lossless: 64 rows of 64 characters, one
character per cell (0-9 then a-f), plus a column ruler. Downsampling
would be smaller but would destroy the coordinate precision the whole
game depends on — a click is (x, y) on the true grid.

Honest limitation, stated here so nobody reads a low score as a verdict
on reasoning: the agentic loop's MAX_STEPS is 10, while human baselines
for these levels run 7-578 actions. The loop budget, not the model, is
the binding constraint for anything past the first level or two.
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional

import httpx

from backend.app.actions.action_registry import ActionSpec

ARC_BASE = os.environ.get("ARC_ARCADE_URL", "http://127.0.0.1:8001")
TIMEOUT = 60.0

# One character per cell. ARC frames use small non-negative ints.
_SYMBOLS = "0123456789abcdef"

# Session state for the current game. Single-user MVP, same scope as
# BrowserSessionManager and RunStore.
# card_id and api_key come from the environment so the scorecard can be
# opened by the harness BEFORE this process starts — the score has to be
# ARC's own, recorded server-side, not anything this code computes.
_STATE: Dict[str, Any] = {
    "game_id": None,
    "guid": None,
    "card_id": os.environ.get("ARC_CARD_ID") or None,
    "api_key": os.environ.get("ARC_API_KEY") or None,
    "actions": 0,
}


class ArcGameError(Exception):
    pass


def _client() -> httpx.Client:
    key = _STATE.get("api_key") or os.environ.get("ARC_API_KEY") or "1234"
    return httpx.Client(timeout=TIMEOUT, headers={"X-API-Key": key})


def _render_grid(frame: Any) -> str:
    """64 rows of single characters, with a column ruler so the model can
    count coordinates without miscounting a 64-wide run of identical
    digits — which is most of what these frames contain."""
    if not frame:
        return "(no frame)"
    grid: List[List[int]] = frame[0] if isinstance(frame[0][0], list) else frame
    width = len(grid[0])
    tens = "    " + "".join(str((c // 10) % 10) for c in range(width))
    ones = "    " + "".join(str(c % 10) for c in range(width))
    lines = [tens, ones]
    for y, row in enumerate(grid):
        cells = "".join(
            _SYMBOLS[v] if isinstance(v, int) and 0 <= v < len(_SYMBOLS) else "?"
            for v in row
        )
        lines.append(f"{y:3d} {cells}")
    return "\n".join(lines)


def _describe(d: Dict[str, Any], header: str) -> str:
    state = d.get("state")
    lvls = d.get("levels_completed")
    win = d.get("win_levels")
    avail = d.get("available_actions") or []
    out = [
        header,
        f"state={state}  levels_completed={lvls}/{win}  "
        f"actions_used={_STATE['actions']}",
        f"available_actions={avail}  (6 = click at x,y)",
        "",
        "GRID (row index on the left, column ruler on top; each character "
        "is one cell's colour value):",
        _render_grid(d.get("frame")),
    ]
    return "\n".join(out)


def _known_game_ids(limit: int = 8) -> List[str]:
    """Ask the arcade what actually exists. Used to make a failure
    actionable instead of a prompt to guess again."""
    try:
        with _client() as c:
            r = c.get(f"{ARC_BASE}/api/games")
        games = r.json() if r.status_code == 200 else []
        out = []
        for g in games if isinstance(games, list) else []:
            gid = g.get("game_id") if isinstance(g, dict) else None
            if gid:
                out.append(str(gid))
        return out[:limit]
    except Exception:  # noqa: BLE001
        return []


def _post(path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    with _client() as c:
        r = c.post(f"{ARC_BASE}{path}", json=payload)
    if r.status_code != 200:
        # Name the valid values. "game 1 not found" told a live run
        # nothing about what a valid id looks like, so it guessed seven
        # more times — 0, 1, 2, 3, "default", "arcade", "test",
        # "arcade3" — and never sent the id printed in its own task.
        # An error that carries the recovery path is the difference
        # between one wrong call and eight.
        hint = ""
        if "not found" in r.text.lower():
            valid = _known_game_ids()
            if valid:
                hint = f" Valid game_id values are: {', '.join(valid)}."
        raise ArcGameError(
            f"arcade returned HTTP {r.status_code}: {r.text[:300]}{hint}"
        )
    return r.json()


def _reset_handler(args: Dict[str, Any]) -> str:
    game_id = str(args.get("game_id") or "").strip()
    if not game_id:
        raise ArcGameError("game_id is required")

    payload: Dict[str, Any] = {"game_id": game_id}
    if _STATE.get("card_id"):
        payload["card_id"] = _STATE["card_id"]
    if _STATE.get("guid") and _STATE.get("game_id") == game_id:
        payload["guid"] = _STATE["guid"]

    d = _post("/api/cmd/RESET", payload)
    _STATE.update(game_id=game_id, guid=d.get("guid"), actions=0)
    return _describe(d, f"RESET {game_id} — game started.")


def _click_handler(args: Dict[str, Any]) -> str:
    if not _STATE.get("guid"):
        raise ArcGameError("no active game — call arc_reset first")
    try:
        x = int(args.get("x"))
        y = int(args.get("y"))
    except (TypeError, ValueError):
        raise ArcGameError("x and y must be integers")
    if not (0 <= x <= 63 and 0 <= y <= 63):
        raise ArcGameError(f"x and y must each be 0-63; got x={x} y={y}")

    payload = {
        "game_id": _STATE["game_id"],
        "guid": _STATE["guid"],
        "x": x,
        "y": y,
        "reasoning": str(args.get("reasoning") or "")[:500] or None,
    }
    d = _post("/api/cmd/ACTION6", payload)
    _STATE["actions"] += 1
    _STATE["guid"] = d.get("guid", _STATE["guid"])
    return _describe(d, f"CLICK at (x={x}, y={y}).")


def status() -> Dict[str, Any]:
    """For the test harness — not exposed to the planner."""
    return dict(_STATE)


def configure(card_id: Optional[str] = None, api_key: Optional[str] = None) -> None:
    if card_id:
        _STATE["card_id"] = card_id
    if api_key:
        _STATE["api_key"] = api_key


ARC_RESET_SPEC = ActionSpec(
    name="arc_reset",
    description=(
        "Start (or restart) an ARC-AGI-3 game and get the opening frame. "
        "Returns the 64x64 grid, the game state, how many levels are "
        "completed, and which actions are available. Call this ONCE at "
        "the start, then use arc_click to play."
    ),
    parameters=[
        {
            "name": "game_id",
            "type": "string",
            "description": "The game to play, e.g. 'vc33-5430563c'.",
            "required": True,
        },
    ],
    handler=_reset_handler,
    preview=lambda a: f"Reset ARC game {a.get('game_id')}",
    mutating=False,
    planner_excluded=False,
    capability="game.reset",
)

ARC_CLICK_SPEC = ActionSpec(
    name="arc_click",
    description=(
        "Click one cell of the ARC-AGI-3 grid and get the resulting frame. "
        "x is the COLUMN (0-63, left to right), y is the ROW (0-63, top to "
        "bottom) — both read off the ruler in the grid you were shown. The "
        "response is the new grid plus state, so compare it against the "
        "previous one to work out what your click did. You must experiment: "
        "the rules are not given to you and can only be learned by acting."
    ),
    parameters=[
        {"name": "x", "type": "integer",
         "description": "Column index, 0-63.", "required": True},
        {"name": "y", "type": "integer",
         "description": "Row index, 0-63.", "required": True},
        {"name": "reasoning", "type": "string",
         "description": "One line on why you chose this cell.", "required": False},
    ],
    handler=_click_handler,
    preview=lambda a: f"Click ARC grid at ({a.get('x')},{a.get('y')})",
    mutating=False,
    planner_excluded=False,
    pollable=True,  # repeated identical clicks can be legitimate here
    capability="game.act",
)
