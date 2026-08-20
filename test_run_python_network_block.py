"""Blocking the network must not break importing the standard library.

run_python disables network access on purpose, and the message it raises
is a good one: "network access is disabled inside run_python. Fetch data
with a data connector tool first, then pass it in and compute here."

That message was unreachable for exactly the code it was written for.
The block replaced `socket.socket` -- a CLASS -- with a function, and
ssl.py line 943 is `class SSLSocket(socket)`. Subclassing a function
raises at IMPORT time:

    TypeError: function() argument 'code' must be code, not str

So `import ssl`, `import urllib.request`, `import requests` and
`import httpx` all failed before reaching any network call, with a
message about code objects.

Watched live on a Wikipedia run: the agent could not tell a policy from
a bug and spent three of its twenty calls retrying variations of the same
blocked import.

The block now subclasses the real socket and refuses at CONSTRUCTION, so
the module imports, the subclass builds, and the refusal arrives where it
belongs -- carrying the sentence that says what to do instead.
"""
from __future__ import annotations

from backend.app.actions.builtin.run_python import SPEC

NET_MESSAGE = "network access is disabled inside run_python"


def _run(code: str) -> str:
    return SPEC.handler({"code": code})


# ------------------------------------------- the library still imports

def test_ssl_imports():
    """The exact import that raised TypeError at class-creation time."""
    out = _run("import ssl; print('ok')")
    assert "ok" in out
    assert "must be code, not str" not in out


def test_urllib_imports():
    out = _run("import urllib.request; print('ok')")
    assert "ok" in out


def test_http_client_imports():
    out = _run("import http.client; print('ok')")
    assert "ok" in out


# ------------------------------- and the refusal says what it means

def test_an_actual_fetch_is_refused_with_the_useful_message():
    out = _run("import urllib.request as u; u.urlopen('https://example.com', timeout=5)")
    assert NET_MESSAGE in out
    assert "data connector tool first" in out, "say what to do instead"


def test_a_raw_socket_is_refused_too():
    out = _run("import socket; socket.socket()")
    assert NET_MESSAGE in out


def test_create_connection_is_refused():
    out = _run("import socket; socket.create_connection(('example.com', 80))")
    assert NET_MESSAGE in out


# ----------------------------------------- ordinary compute is intact

def test_offline_computation_still_works():
    """The tool's actual job. Blocking the network must cost nothing
    here."""
    out = _run("import statistics; print(statistics.mean([1, 2, 3, 4]))")
    assert "2.5" in out


def test_the_scientific_stack_is_still_importable():
    """-E rather than -I, so the user site directory holding numpy and
    pandas stays on the path -- the reason this tool exists."""
    out = _run("import json, math, statistics; print(math.sqrt(16))")
    assert "4.0" in out


# ------------------------------------------------ UTF-8 out of the child

def test_non_ascii_output_survives():
    """The Windows failure. _child_env sets PYTHONIOENCODING=utf-8 and -E
    ignores every PYTHON* variable, so the child's stdout fell back to the
    console codepage (cp1252) and a non-ASCII print died on the way out.
    Measured: this exact print returned an EMPTY string, and the agent saw
    an unexplained crash rather than an encoding problem."""
    out = _run("print('café — naïve ✓ 日本語 Ω')")
    assert "café — naïve ✓ 日本語 Ω" in out


def test_the_child_reports_utf8_encoding():
    out = _run("import sys; print('enc=', sys.stdout.encoding)")
    assert "utf-8" in out.lower()
    assert "cp1252" not in out.lower()


def test_non_ascii_on_stderr_survives():
    """Both streams are decoded, so a warning carrying non-ASCII arrives
    intact rather than as a second crash.

    Written with a print as well, because a script that produces NO
    stdout takes a different path by design -- it is told that results
    are only visible if printed -- and that path is not what this is
    about."""
    out = _run(
        "import sys\n"
        "print('out: ok')\n"
        "sys.stderr.write('warn: café ✓')"
    )
    assert "café ✓" in out
    assert "charmap" not in out


def test_a_non_ascii_traceback_is_readable():
    """A crash carrying non-ASCII must not become a second, confusing
    crash about encodings."""
    out = _run("raise ValueError('naïve café ✓')")
    assert "naïve café ✓" in out
    assert "charmap" not in out


def test_non_ascii_survives_a_round_trip_through_data():
    out = _run("import json; d = json.loads('{\"k\": \"café ✓\"}'); print(d['k'])")
    assert "café ✓" in out


def test_utf8_mode_is_a_flag_not_an_env_var():
    """-E strips PYTHON* variables, so PYTHONIOENCODING cannot be the
    mechanism. -X utf8 is a command-line flag and survives it."""
    import inspect
    from backend.app.actions.builtin import run_python
    src = inspect.getsource(run_python)
    assert '"-X", "utf8"' in src
    assert '"-E"' in src, "the isolation flag is still wanted"


def test_errors_are_not_silently_swallowed():
    """errors='ignore' would hide exactly the mangling this fixes."""
    import inspect
    from backend.app.actions.builtin import run_python
    src = inspect.getsource(run_python)
    assert 'errors="ignore"' not in src
