"""Numbers in a deliverable must trace to what the code actually printed.

THE FABRICATION THIS EXISTS FOR, 2026-08-16. A run reported:

    "CAGR of 7.65%, an annualised Sharpe ratio of 0.76, and a maximum
     drawdown of -20.70%"

for a 50/200 SMA crossover on SPY. `run_python` genuinely ran, so the
provenance gate passed. Real figures appeared, so the substance gate
passed. Both existing gates were satisfied by a fabrication.

Re-deriving from the same CSV gave Sharpe 0.4457 and drawdown -34.10%
under all eight variants tried. The script the agent supplied when asked
for its code contained NO Sharpe calculation at all, used 20/50 windows
over 5 years instead of 50/200 over 10, and applied none of the costs it
quoted. The numbers were never computed.

Three gates, three different questions:
    provenance  -- did a compute tool run?
    substance   -- are numbers stated at all?
    correctness -- are they THE numbers it computed?   <- this file
"""
from __future__ import annotations

import time

from backend.app.critique.compute_gate import (
    missing_metric_values,
    untraceable_metric_values,
)
from backend.app.tools.tool_call_ledger import ToolCallLedger

TASK = ("backtest a 50/200 SMA crossover on SPY and report CAGR, "
        "Sharpe ratio and maximum drawdown")

# What run_python really printed, from the verified re-derivation.
REAL_STDOUT = """Python output:
=== SMA Crossover Backtest Results ===
Period      : 2016-08-15 - 2026-08-14
CAGR        : 9.62%
Sharpe      : 0.5538
Max drawdown: -33.72%
Trades      : 9
"""

# The deliverable the founder actually received.
FABRICATED = (
    "The 50-day vs 200-day SMA crossover on SPY delivers a CAGR of 7.65 %, "
    "an annualised Sharpe ratio of 0.7565, and a maximum drawdown of "
    "-20.70 % (including 0.10 % transaction cost and 0.05 % slippage)."
)

HONEST = (
    "The 50/200 SMA crossover on SPY returned a CAGR of 9.62%, a Sharpe "
    "ratio of 0.5538, and a maximum drawdown of -33.72%."
)


# ------------------------------------------------------- the regression

def test_the_live_fabrication_is_caught():
    bad = untraceable_metric_values(TASK, FABRICATED, REAL_STDOUT)

    joined = " | ".join(bad)
    assert any("sharpe" in b for b in bad), joined
    assert any("cagr" in b for b in bad), joined
    assert any("drawdown" in b for b in bad), joined
    assert "0.7565" in joined and "7.65" in joined and "20.70" in joined


def test_the_two_older_gates_both_pass_it():
    """Proof this is a THIRD failure class, not a duplicate. If either
    existing gate already caught it, this module would be redundant."""
    assert missing_metric_values(TASK, FABRICATED) == [], (
        "the substance gate should see numbers present -- they are")


def test_honest_numbers_are_not_flagged():
    assert untraceable_metric_values(TASK, HONEST, REAL_STDOUT) == []


# ------------------------------------------------- the three allowances

def test_rounding_is_allowed():
    """The code prints more precision than the write-up quotes."""
    stdout = "CAGR: 7.6523\nSharpe: 0.55381\nMax drawdown: -33.7245"
    text = "CAGR 7.65%, Sharpe 0.55, max drawdown -33.72%"
    assert untraceable_metric_values(TASK, text, stdout) == []


def test_percent_vs_fraction_is_allowed():
    """A percentage printed as a fraction is the same number."""
    stdout = "cagr=0.0962 sharpe=0.5538 max_drawdown=-0.3372"
    text = "CAGR of 9.62%, Sharpe 0.5538, drawdown of -33.72%"
    assert untraceable_metric_values(TASK, text, stdout) == []


def test_sign_flip_is_allowed():
    """Drawdown is printed positive about as often as negative."""
    stdout = "CAGR 9.62\nSharpe 0.5538\nMax drawdown 33.72"
    text = "CAGR 9.62%, Sharpe 0.5538, maximum drawdown -33.72%"
    assert untraceable_metric_values(TASK, text, stdout) == []


# ------------------------------------------------ never accuse blindly

def test_no_captured_output_means_no_accusation():
    """Absence of evidence is not evidence of fabrication -- and the
    provenance gate already covers 'nothing ever computed'."""
    assert untraceable_metric_values(TASK, FABRICATED, "") == []
    assert untraceable_metric_values(TASK, FABRICATED, "   ") == []


def test_output_with_no_numbers_means_no_accusation():
    assert untraceable_metric_values(TASK, FABRICATED, "done, no errors") == []


def test_unrequested_metrics_are_ignored():
    """Only figures the founder actually asked for are checked."""
    task = "backtest SPY and report the Sharpe ratio"
    text = "Sharpe 0.5538, and incidentally a CAGR of 99.99%"
    assert untraceable_metric_values(task, text, REAL_STDOUT) == []


# --------------------------------------------------- the ledger plumbing

def test_ledger_keeps_compute_output_whole_not_clipped():
    """The 200-char preview is why this was invisible: it cut off past
    the header and before the numbers."""
    led = ToolCallLedger()
    long_output = "header line\n" * 40 + "Sharpe: 0.5538\n"
    assert len(long_output) > 400

    led.record("action.run_python", {"code": "..."}, ok=True,
               result_preview=long_output[:200], full_output=long_output)

    captured = led.compute_output()
    assert "Sharpe: 0.5538" in captured, (
        "the number was past the preview cutoff and must survive in full")


def test_ledger_ignores_failed_and_non_compute_calls():
    led = ToolCallLedger()
    led.record("action.run_python", {}, ok=False,
               full_output="Sharpe: 9.99")            # crashed
    led.record("action.fetch_market_data", {}, ok=True,
               result_preview="Sharpe: 8.88")          # not compute
    assert led.compute_output() == "", led.compute_output()


def test_ledger_compute_output_is_run_scoped():
    """Same bug class as D4 -- an unscoped query would let a PREVIOUS
    run's printed numbers vouch for this run's claims."""
    led = ToolCallLedger()
    led.record("action.run_python", {}, ok=True, full_output="old: 1.11")
    cutoff = time.time() + 0.001
    time.sleep(0.005)
    led.record("action.run_python", {}, ok=True, full_output="new: 2.22")

    scoped = led.compute_output(since=cutoff)
    assert "new: 2.22" in scoped
    assert "old: 1.11" not in scoped
