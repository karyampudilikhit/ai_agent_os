"""RunArtifacts — the register that stops specialists guessing filenames.

The failure this closes, verbatim from the incident report: specialists
invented `data.csv`, `chosen_etf.txt`, `clean_data/SPY_5y_1d_clean.csv`
and `reports/SPY_SMA_Backtest_Report.pdf`. Two were reported to the
founder as finished work; neither had ever been written. One run emailed
`dataengineer@example.com` to ask a colleague who does not exist for a
file that did not exist either.

The property that matters most here is that a producer cannot register
an INTENTION — only something that is actually on disk.
"""
from __future__ import annotations

import os

from backend.app.state import run_artifacts as ra


def _reg(tmp_path, name="run_test"):
    ra.set_current_run(None)
    reg = ra.for_run(name + str(tmp_path).replace(os.sep, ""))
    return reg


# ------------------------------------------------- claims are checked

def test_a_file_that_does_not_exist_is_refused(tmp_path):
    """The whole point. `reports/SPY_SMA_Backtest_Report.pdf` was
    reported as finished work and had never been written."""
    reg = _reg(tmp_path)
    out = reg.register_file(str(tmp_path / "ghost.pdf"), producer="Report Writer")
    assert out is None
    assert reg.all() == []


def test_a_real_file_is_registered_with_its_true_size(tmp_path):
    reg = _reg(tmp_path)
    p = tmp_path / "index.html"
    p.write_text("<h1>hello</h1>", encoding="utf-8")

    art = reg.register_file(str(p), producer="write_file")
    assert art is not None
    assert art.size == 14
    assert art.verified is True
    assert art.producer == "write_file"


def test_a_directory_is_not_a_file(tmp_path):
    reg = _reg(tmp_path)
    assert reg.register_file(str(tmp_path), producer="x") is None


# ------------------------------------------------------------- urls

def test_a_deployment_url_starts_unverified(tmp_path):
    """Vercel reporting READY is Vercel's claim. It becomes verified when
    something actually opens it."""
    reg = _reg(tmp_path)
    art = reg.register_url("https://x.vercel.app", producer="deploy_vercel")
    assert art.verified is False

    assert reg.mark_verified(art.artifact_id, evidence="opened, HTTP 200")
    assert reg.by_id(art.artifact_id).verified is True
    assert reg.by_id(art.artifact_id).metadata["evidence"] == "opened, HTTP 200"


def test_a_non_http_url_is_refused(tmp_path):
    reg = _reg(tmp_path)
    assert reg.register_url("file:///etc/passwd", producer="x") is None


# --------------------------------------------------------- manifest

def test_the_manifest_names_real_paths_and_forbids_guessing(tmp_path):
    reg = _reg(tmp_path)
    p = tmp_path / "spy.csv"
    p.write_text("date,close\n", encoding="utf-8")
    reg.register_file(str(p), producer="Data Engineer", summary="10y SPY daily bars")
    reg.register_url("https://x.vercel.app", producer="deploy_vercel")

    m = reg.manifest()
    assert "spy.csv" in m
    assert "Data Engineer" in m
    assert "10y SPY daily bars" in m
    assert "UNVERIFIED" in m, "the un-opened deployment must be marked as such"
    # The two instructions that directly counter the observed failures.
    assert "Do NOT invent filenames" in m
    assert "do NOT ask anyone to send you a file" in m
    assert "it does not exist yet" in m


def test_an_empty_manifest_is_empty_not_noise(tmp_path):
    """Nothing produced yet should add nothing to the prompt, rather than
    a header announcing an empty list."""
    assert _reg(tmp_path).manifest() == ""


# ------------------------------------------------- run scoping

def test_artifacts_are_scoped_to_their_run(tmp_path):
    """Same lesson as the compute gate's `since=`: an earlier run
    vouching for this one is how a hollow deliverable passed."""
    a, b = ra.for_run("run_a"), ra.for_run("run_b")
    p = tmp_path / "x.txt"
    p.write_text("x", encoding="utf-8")
    a.register_file(str(p), producer="w")

    assert len(a.all()) == 1
    assert b.all() == []
    assert "x.txt" not in b.manifest()


def test_the_thread_binding_routes_to_the_right_run(tmp_path):
    p = tmp_path / "y.txt"
    p.write_text("y", encoding="utf-8")

    ra.set_current_run("run_bound")
    ra.register_file(str(p), producer="write_file")
    assert len(ra.for_run("run_bound").all()) == 1

    ra.set_current_run(None)
    assert ra.register_file(str(p), producer="write_file") is None, \
        "outside a run there is nowhere to register, and that must not raise"
    assert ra.manifest_for_current() == ""


def test_registration_never_raises_on_a_bad_path(tmp_path):
    """Bookkeeping must not break the action it records."""
    ra.set_current_run("run_safe")
    assert ra.register_file("\x00not/a/path", producer="w") is None
    assert ra.register_url("", producer="w") is None


# ------------------------------------------------- producers wired

def test_write_file_registers_what_it_wrote(tmp_path, monkeypatch):
    monkeypatch.setenv("VISION_WORKSPACE_DIR", str(tmp_path))
    ra.set_current_run("run_wf")
    from backend.app.actions.builtin import write_file

    write_file.SPEC.handler({"path": "site/index.html", "content": "<h1>hi</h1>"})

    arts = ra.for_run("run_wf").all()
    assert any(a.name == "index.html" and a.verified for a in arts)


def test_the_manifest_reaches_a_specialists_prompt(tmp_path, monkeypatch):
    """Registering only matters if the next specialist is handed it."""
    monkeypatch.setenv("VISION_WORKSPACE_DIR", str(tmp_path))
    ra.set_current_run("run_prompt")
    p = tmp_path / "data.csv"
    p.write_text("a,b\n", encoding="utf-8")
    ra.for_run("run_prompt").register_file(str(p), producer="Data Engineer")

    from backend.app.employees.dynamic_employee import DynamicEmployee
    from backend.app.employees.employee_config import resolve

    class _Mem:
        def relevant_context(self, task): return ""
        def record(self, task, result): pass

    emp = DynamicEmployee(employee_id="e", role="Quant Analyst", mandate="m",
                          pipeline=None, memory_store=_Mem(), config=resolve({}))
    prompt = emp.build_objective("do the thing")

    assert "data.csv" in prompt
    assert "Do NOT invent filenames" in prompt
    ra.set_current_run(None)
