"""What actually happened, assembled from the record rather than written.

THE FAILURE THIS ANSWERS. Asked for ten items, a run that verified two
would say "here are the internships I found" and list whatever it had.
The count, the confidence and the framing were all the model's, written
from a transcript it had partly forgotten, and the founder's only way to
know that two of ten were real was to check by hand.

The system already KNEW. item_state had the count; task_graph had the
verdict; the ledger had the pages. None of it reached the page the
founder read, because the last step in every run was a language model
writing prose about its own work.

SO THIS IS NOT A PROMPT. Nothing here asks a model for anything. Every
number below is counted from the ledger-derived state, and the sentences
around them are fixed text chosen by which numbers came back. A model
cannot make this say ten by being confident, and cannot make it say two
by being modest.

WHAT IT IS FOR. Two callers: the loop appends it to the transcript so
the writing step is looking at the true counts while it writes, and the
gate can attach it to a refusal so a blocked run still tells the founder
what it DID get. Both are the same text, because the founder and the
model should not be reading different accounts of the same run.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Iterable, List, Optional, Sequence

logger = logging.getLogger(__name__)


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" + ("" if n == 1 else "s")


def build(task: str, spec: Any = None, plan: Any = None,
          prog: Any = None, verdicts: Optional[Sequence[Any]] = None,
          errors: Optional[Sequence[str]] = None) -> str:
    """The honest account of one run. Empty when there is nothing
    countable to report -- an open-ended task has no shortfall to state,
    and a report that says "0 of 0" is noise in a prompt.
    """
    try:
        from backend.app.orchestrator.item_verification import tally
    except Exception:  # noqa: BLE001
        return ""

    wanted = 0
    try:
        if spec is not None and getattr(spec, "confident", False):
            wanted = int(getattr(spec, "item_count", 0) or 0)
    except (TypeError, ValueError):
        wanted = 0

    lines: List[str] = []

    if wanted >= 2 and verdicts is not None:
        t = tally(verdicts, wanted=wanted)
        lines.append("WHAT THIS RUN ACTUALLY VERIFIED")
        lines.append(f"  asked for : {_plural(wanted, 'item')}")
        lines.append(f"  verified  : {t.verified}  "
                     f"(opened on its own page and read)")
        if t.rejected:
            lines.append(f"  rejected  : {t.rejected}  "
                         f"(read, but not what the task asked for)")
        if t.blocked:
            lines.append(f"  blocked   : {t.blocked}  "
                         f"(a call tried to open it and failed)")
        never = t.discovered - (t.verified + t.rejected + t.blocked)
        if never > 0:
            lines.append(f"  not opened: {never}  "
                         f"(found in a list, never visited)")
        if t.shortfall():
            lines.append("")
            lines.append(
                f"  SHORTFALL: {t.shortfall()} of {wanted} were not verified. "
                f"Report the {t.verified} that were and say so plainly. Do NOT "
                f"make up the difference from a results list — an item that "
                f"was not opened is not one of the {wanted} that were asked "
                f"for."
            )

        # Why each rejected item was rejected. Named, because "3
        # rejected" with no reasons is a number the founder cannot check
        # and cannot argue with.
        rejected = [v for v in verdicts
                    if getattr(v, "state", "") == "REJECTED"][:5]
        if rejected:
            lines.append("")
            lines.append("  REJECTED, and why:")
            for v in rejected:
                lines.append(f"    - {getattr(v, 'url', '?')}: "
                             f"{getattr(v, 'reason', '')}")

    # FIELDS ASKED FOR THAT NOTHING ON THE PAGE WAS LABELLED AS.
    #
    # A run reported ten AI startups with funding and stopped. It had
    # been asked for company, funding, product AND website, and the
    # source carried the first two. The count was right and the answer
    # was half of one, and only the founder could see that.
    fields = list(getattr(spec, "per_item_fields", ()) or ())
    if fields and prog is not None:
        try:
            from backend.app.orchestrator.goal_state import fields_evidenced
            captured = "\n".join(getattr(prog, "evidence", {}).values())
            if captured.strip():
                _, missing = fields_evidenced(fields, captured)
                if missing:
                    lines.append("")
                    lines.append("FIELDS WITH NO EVIDENCE")
                    lines.append("  " + ", ".join(missing)
                                 + " — nothing on the pages this run read was "
                                   "labelled as these. Do not report a value "
                                   "for them.")
        except Exception:  # noqa: BLE001
            pass

    if plan is not None and getattr(plan, "reason", ""):
        lines.append("")
        lines.append("BUDGET")
        lines.append(f"  {plan.reason}")

    if errors:
        lines.append("")
        lines.append("ERRORS ENCOUNTERED")
        for e in list(errors)[:6]:
            lines.append(f"  - {str(e)[:200]}")

    if not lines:
        return ""
    return "\n".join(["(RUN RECORD — counted from what this run did, not "
                      "written from memory. These numbers are the ones to "
                      "report.)", ""] + lines)


def for_run(task: str, ledger_calls: Iterable[Dict[str, Any]],
            budget: int = 22) -> str:
    """The same report, built from nothing but the task and the ledger.

    For callers that never held the loop's plan object -- the deliverable
    gate is the one that matters. It has the founder's task and the run's
    calls, and that is enough to recompute every number here.
    """
    try:
        from backend.app.orchestrator.goal_spec import from_task
        from backend.app.orchestrator.item_state import derive
        from backend.app.orchestrator.item_verification import verify
        from backend.app.orchestrator.relevance import subject_of
        from backend.app.orchestrator.task_graph import build as build_plan

        calls = list(ledger_calls or ())
        spec = from_task(task)
        if not spec.confident:
            return ""
        plan = build_plan(task, budget, spec=spec)
        prog = derive(calls, plan.feasible_items)
        verdicts = verify(prog, subject_of(task, spec))
        return build(task, spec=spec, plan=plan, prog=prog, verdicts=verdicts)
    except Exception as exc:  # noqa: BLE001
        logger.info("run report unavailable (%s)", exc)
        return ""
