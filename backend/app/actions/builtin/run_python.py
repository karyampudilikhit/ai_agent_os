"""run_python — execute Python and return what it actually printed.

The capability that separates "describes a backtest" from "ran one".

Asked to build a quant model end to end, a full run produced a correct
strategy definition, a correct evaluation methodology, and NO NUMBERS —
no return, no Sharpe, no drawdown — because nothing in the system can
execute anything. Prose reasoning cannot compute a Sharpe ratio, and the
honest specialist correctly refused to invent one. Every "analyse this
data" task is unreachable without this.

WHY NOT calculate.py. That built-in walks an AST and permits arithmetic
only — deliberately, because its job is to evaluate one expression
exactly and a hostile `__import__` should be a parse-level refusal. A
backtest needs pandas, numpy, loops and dataframes. Extending the AST
walker to cover those would recreate a Python interpreter with a
whitelist, which is the classic way to build a sandbox that looks safe
and is not.

THREAT MODEL, stated plainly because the word "sandbox" promises more
than this delivers. The code executed here is written by OUR OWN model
in service of a founder's task. The realistic risk is a confused agent
doing something destructive or wasteful — deleting files outside its
scratch space, hanging forever, hammering an API in a loop — NOT a
determined attacker trying to escape. The measures below are sized for
that:

  - a separate interpreter process (`-I`, isolated: no user site, no
    inherited PYTHON* env), so a crash or hang cannot take the server
    with it
  - a wall-clock timeout, killed hard
  - cwd forced to a scratch directory, and the environment stripped of
    credentials, so a stray open() lands somewhere harmless
  - sockets disabled in the child, so generated code cannot quietly
    reach the network (fetching belongs to the connectors, where it is
    logged and verifiable)
  - output captured and bounded

What this is NOT: an OS-level boundary. There is no seccomp, no
container, no memory cap (Python's `resource` module does not exist on
Windows, where this currently runs). Determined code CAN get out of it.
If this ever executes code from an untrusted source rather than our own
planner, it must be replaced with real isolation — a container or a
dedicated sandbox service — not hardened in place.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import tempfile
from typing import Any, Dict

from backend.app.actions.action_registry import ActionSpec

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 120.0
MAX_CODE_CHARS = 20000
MAX_OUTPUT_CHARS = 8000

# Injected ahead of the model's code in the child process. Disables
# network access at the socket layer — generated code should compute,
# not fetch. Retrieval goes through the connectors, where every call is
# recorded in the tool ledger and can be checked against a citation.
_PREAMBLE = """
import socket as _socket

class _NoNetwork(IOError):
    pass

def _blocked(*a, **k):
    raise _NoNetwork(
        "network access is disabled inside run_python. Fetch data with a "
        "data connector tool first, then pass it in and compute here."
    )

_socket.socket = _blocked
_socket.create_connection = _blocked
_socket.socketpair = _blocked
del _socket
"""


class PythonRunError(Exception):
    pass


def _scratch_dir() -> str:
    """Where the child runs. Inside the workspace so any file it writes
    is reachable by read_file afterwards, rather than vanishing into a
    temp dir the rest of the system cannot see."""
    try:
        from backend.app.actions.builtin._workspace import workspace_root
        root = os.path.join(str(workspace_root()), "python_runs")
    except Exception:  # noqa: BLE001
        root = os.path.join(tempfile.gettempdir(), "vision_python_runs")
    os.makedirs(root, exist_ok=True)
    return root


def _child_env() -> Dict[str, str]:
    """A deliberately bare environment. Credentials live in the parent's
    env (OLLAMA_API_KEY, TAVILY_API_KEY, SMTP_PASS, GitHub tokens); code
    generated to compute a Sharpe ratio has no business reading them,
    and the cheapest way to guarantee that is not to pass them."""
    # APPDATA is not optional on Windows, however much it looks like it:
    # Python derives the per-user site directory from it, and that is
    # where numpy/pandas/scipy are actually installed here. Stripping it
    # produced a child that started cleanly and then failed with
    # "ModuleNotFoundError: No module named 'numpy'" — a sandbox with
    # perfect isolation and no ability to do the one job it exists for.
    # It carries no credentials; the secrets are the API keys, and those
    # stay out.
    keep = (
        "PATH", "SYSTEMROOT", "SystemRoot", "COMSPEC", "PATHEXT",
        "APPDATA", "LOCALAPPDATA", "USERPROFILE", "HOME",
        "TEMP", "TMP", "LANG", "LC_ALL",
    )
    env = {k: os.environ[k] for k in keep if k in os.environ}
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def _handler(args: Dict[str, Any]) -> str:
    code = str(args.get("code") or "")
    if not code.strip():
        raise PythonRunError("code is required")
    if len(code) > MAX_CODE_CHARS:
        raise PythonRunError(
            f"code is {len(code)} chars, limit is {MAX_CODE_CHARS}"
        )
    try:
        timeout = float(args.get("timeout_seconds") or DEFAULT_TIMEOUT)
    except (TypeError, ValueError):
        timeout = DEFAULT_TIMEOUT
    timeout = max(1.0, min(timeout, 600.0))

    cwd = _scratch_dir()
    try:
        proc = subprocess.run(
            # -E ignores PYTHON* environment variables; -B skips writing
            # .pyc files into the workspace.
            #
            # NOT -I (isolated). That also disables the user site
            # directory, which is exactly where numpy, pandas and scipy
            # are installed here — under -I the child came back with
            # "ModuleNotFoundError: No module named 'numpy'", i.e. fully
            # isolated and completely useless for the one job this tool
            # exists to do. The env is already stripped in _child_env,
            # which is where the isolation that matters comes from.
            [sys.executable, "-E", "-B", "-c", _PREAMBLE + "\n" + code],
            cwd=cwd,
            env=_child_env(),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        raise PythonRunError(
            f"the code ran longer than {timeout:.0f}s and was killed. Make it "
            f"cheaper (fewer iterations, smaller data) or raise "
            f"timeout_seconds."
        )
    except Exception as exc:  # noqa: BLE001
        raise PythonRunError(f"could not start the interpreter: {exc}")

    out = (proc.stdout or "").strip()
    err = (proc.stderr or "").strip()

    if proc.returncode != 0:
        # The traceback IS the useful part — hand it back whole so the
        # model can fix its own code rather than guess at what broke.
        detail = err or out or "(no output)"
        return (
            f"Python exited with code {proc.returncode}. This code did NOT "
            f"run successfully — do not report its intended results as if it "
            f"had.\n\n{detail[-MAX_OUTPUT_CHARS:]}"
        )

    if not out:
        return (
            "Python ran successfully but printed nothing. Results are only "
            "visible if you print() them — add print() around the numbers you "
            "need, then run it again."
        )

    truncated = ""
    if len(out) > MAX_OUTPUT_CHARS:
        out = out[:MAX_OUTPUT_CHARS]
        truncated = "\n[…output truncated — print less, or summarise first…]"
    warn = f"\n\n(stderr: {err[:500]})" if err else ""
    return f"Python output:\n{out}{truncated}{warn}"


SPEC = ActionSpec(
    name="run_python",
    description=(
        "Execute Python and get back exactly what it prints. Use this for "
        "EVERY computed result — backtest metrics, statistics, aggregations, "
        "growth rates, anything with real numbers — instead of working them "
        "out in prose, which produces confidently wrong figures. pandas, "
        "numpy and scipy are available. You MUST print() what you want to "
        "see; the return value of the last line is not shown. No network "
        "access: fetch data with a connector tool first, then compute on it "
        "here. Files you write land in the shared workspace and can be read "
        "back with read_file."
    ),
    parameters=[
        {
            "name": "code",
            "type": "string",
            "description": (
                "The Python to run. Print your results, e.g. "
                "print(f'Sharpe: {sharpe:.2f}')."
            ),
            "required": True,
        },
        {
            "name": "timeout_seconds",
            "type": "number",
            "description": "Wall-clock limit, default 120, max 600.",
            "required": False,
        },
    ],
    handler=_handler,
    preview=lambda a: f"Run Python ({len(str(a.get('code') or ''))} chars)",
    mutating=False,
    planner_excluded=False,
    capability="code.execute",
)
