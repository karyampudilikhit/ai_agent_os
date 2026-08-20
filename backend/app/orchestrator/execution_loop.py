"""AgenticExecutor — the real multi-step tool-use loop.

This is the Phase-2 upgrade MCPPlanner's docstring promised. The
difference is the whole ballgame for "Vision AI does the actual work":

  MCPPlanner (old):  ONE planning call picks up to 4 tools BLIND —
                     before seeing a single result — fires them flat,
                     then the specialist writes. It cannot react to
                     what a tool returned, cannot chain work whose
                     shape is only discovered mid-task, and cannot
                     retry a failed call differently.

  AgenticExecutor:   THINK -> ACT -> OBSERVE -> repeat. The model sees
                     each result before choosing the next action, so
                     dependent multi-step work becomes possible:
                     "find 10 investors, then draft an email to each",
                     "check their pricing page, and IF there's an
                     enterprise tier, write the comparison".

What this deliberately does NOT change:
  - The loop GATHERS and ACTS; it does not write the deliverable. Its
    transcript is injected as context and the specialist writes from
    it, so the critique/verification pass (the no-fabrication USP)
    still runs over the shipped text exactly as before.
  - Approval gating is untouched. A mutating action still enqueues and
    returns a "[queued for founder approval]" receipt; the loop treats
    that receipt as an observation and moves on. It never blocks
    waiting for a tap, and never fires an unapproved action.
  - browser_task (the SYNCHRONOUS, founder-present entry point) stays
    out of the loop (planner_excluded) — it can block for minutes
    waiting on a human login, which would stall every other step; it
    keeps its own explicit-dispatch path in dynamic_employee. The loop
    CAN reach browser automation, just through the non-blocking pair
    browser_task_async (returns at once, runs the real flow on a
    background thread) + browser_task_status (poll for progress) — see
    browser_task.py's module docstring. browser_task_status is marked
    `pollable` on its ActionSpec, which exempts it from the repeat-call
    guard below: calling it again with the SAME session_token while
    waiting is the correct next step, not a stuck loop.

Bounds, because an unbounded agent loop is how you burn an LLM quota
in one task: MAX_STEPS, a wall-clock deadline, a per-observation
character cap, and repeat-call detection that breaks a model stuck
calling the same thing forever.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

from backend.app.actions.action_registry import get_registry as get_action_registry
from backend.app.critique.compute_gate import (
    DATASET_COMPUTE_TOOLS,
    task_requires_computation,
)
from backend.app.orchestrator.goal_spec import from_task as goal_spec_from_task
from backend.app.orchestrator.goal_state import check as goal_check
from backend.app.orchestrator.task_graph import REFUSE, build as build_plan, describe as describe_plan
from backend.app.orchestrator.item_state import (
    derive as derive_items,
    progress_note as item_progress_note,
    shortfall_note as item_shortfall_note,
)
from backend.app.orchestrator.instrument_memory import (
    get_memory as get_instrument_memory,
    reset as reset_instrument_memory,
)
from backend.app.orchestrator.output_contract import (
    EXECUTED_CODE,
    RANKED_RESULT,
    run_saw_a_data_table,
    REFUSAL_TEXT,
    is_browser_tool,
    is_interaction_tool,
    is_page_view_tool,
    page_view_changed,
    task_wants_ranking,
    unsatisfied_kinds,
)
from backend.app.orchestrator.step_outcome import (
    ACHIEVED,
    MAX_STEP_RETRIES,
    MISSED,
    judge_step,
    retry_note,
)
from backend.app.tools.http_tool_runner import HTTPToolRunner
from backend.app.tools.tool_registry import get_registry as get_tool_registry

logger = logging.getLogger(__name__)

# The loop sees NAMESPACED tool names ("action.run_python"); compute_gate
# names the bare tools. Derived rather than written out twice so adding a
# compute tool in one place cannot silently fail to register in the other
# -- this codebase has been bitten repeatedly by one list living in two
# files and drifting apart.
DATASET_COMPUTE_TOOLS_QUALIFIED = tuple(
    f"action.{name}" for name in DATASET_COMPUTE_TOOLS
)

# How many times DONE may be refused for missing computation before the
# loop lets the specialist stop anyway. Refusing forever would trap a
# specialist whose data is genuinely unusable in an argument until the
# step budget is gone, burning quota to reach the same failure -- and
# _gate_deliverable still fails the run truthfully either way.
MAX_DONE_REFUSALS = 3

# _http_runner / _action_registry are kept as module-level singletons
# (not just imported inline) because a few things still reach into them
# directly: test_execution_loop.py's test_failed_call_becomes_observation
# monkeypatches `_action_registry.call` to force a failure, and it works
# because this IS the same singleton object tool_registry.py's ToolRegistry
# holds internally — mutating it here mutates it everywhere. _available_tools
# and _execute below go through _tool_registry now (see tool_registry.py's
# module docstring for why); these two names stay only for that shared-object
# back-compat, not because the loop calls them directly anymore.
_http_runner = HTTPToolRunner()
_action_registry = get_action_registry()
_tool_registry = get_tool_registry()

def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default


# All four are env-overridable. The defaults are tuned for the ordinary
# business task — research something, fill a form, draft a document —
# where a handful of steps and short observations are right, and a
# runaway loop is the failure to guard against.
#
# They are the WRONG defaults for an interactive environment, and it is
# worth being precise about why, because the failure is silent. Driving
# a stateful API (a game, a booking flow, a multi-screen app) means
# dozens to hundreds of actions, and each observation is a full state
# snapshot rather than a paragraph. Measured against ARC-AGI-3: human
# baselines for a single level run 7-578 actions against MAX_STEPS=10,
# and one 64x64 frame renders to ~4300 characters against
# MAX_OBSERVATION_CHARS=1500 — so the agent would silently see the top
# third of the board and be judged on decisions made from it.
#
# Making these configurable rather than raising the defaults is
# deliberate: a bigger budget on ordinary tasks buys nothing and costs
# tokens on every run.
# How many times one tool may fail IN A ROW (with any arguments) before
# the loop gives up on it. Three is enough to survive a transient blip
# and a single bad guess, and short of the eight-guess sweep that
# motivated it.
MAX_CONSECUTIVE_FAILURES = _env_int("AGENT_MAX_CONSECUTIVE_FAILURES", 3)

# How many tools get promoted to the top of the list with their full
# description. Small on purpose: promoting half the registry would
# reproduce the flat dump this exists to fix.
RELEVANT_TOOLS_SHOWN = _env_int("AGENT_RELEVANT_TOOLS_SHOWN", 5)

MAX_STEPS = _env_int("AGENT_MAX_STEPS", 10)               # THINK->ACT ceiling
DEADLINE_SECONDS = _env_float("AGENT_DEADLINE_SECONDS", 240.0)  # wall-clock
MAX_OBSERVATION_CHARS = _env_int("AGENT_MAX_OBSERVATION_CHARS", 1500)
# DATA results get far more room than a page description.
#
# 1500 characters is a sensible budget for "what is on this page" and a
# catastrophic one for "here are the rows". A live run extracted 100 rows
# and THREE survived into what the model could see -- it then reported
# three gainers and wrote "unknown" for the rest, blaming the site. The
# rows were there; the transcript cut them off.
MAX_DATA_OBSERVATION_CHARS = _env_int("AGENT_MAX_DATA_OBSERVATION_CHARS", 9000)
_DATA_TOOLS = ("browser_extract_table", "browser_extract_records",
               "browser_extract", "run_python")
MAX_TRANSCRIPT_CHARS = _env_int("AGENT_MAX_TRANSCRIPT_CHARS", 9000)
STEP_MAX_TOKENS = _env_int("AGENT_STEP_MAX_TOKENS", 700)
# How many times to re-attempt a step's model call before giving up on
# tool use. Ollama cloud 500s at random; without this, one unlucky
# failure on the step that decides to run the backtest ends tool use for
# the entire task and the run is then reported as "no computation was
# ever run" -- blaming the model for an upstream outage.
STEP_CALL_RETRIES = _env_int("AGENT_STEP_CALL_RETRIES", 3)
STEP_CALL_RETRY_BACKOFF = float(os.environ.get("AGENT_STEP_CALL_RETRY_BACKOFF", "1.5"))

# Budgets for a run that turns out to be driving a BROWSER.
#
# Operating a web UI costs three steps per attempt -- find the control,
# use it, check that the page moved -- so the ordinary budget of 10 buys
# barely three attempts, and a slow app spends much of the 240s on page
# loads rather than thinking. Four TradingView runs each ran out with
# the screener still unsorted.
#
# The token budget is the one with recorded evidence behind it.
# run_test_model.py measured the SAME model failing at 700 tokens per
# step and succeeding at 3000 -- the step budget flipped the outcome
# where the model choice did not (README.md, "the model is not the
# variable"). A reasoning model makes this sharper still: thinking
# tokens are spent from the same budget BEFORE any response, so a
# thinking model at 700 returns an empty string and looks like a model
# that refused to act.
#
# Applied only when a browser tool actually succeeds, and only when the
# founder has not set a budget of their own -- see _maybe_widen_budget.
BROWSER_MAX_STEPS = _env_int("AGENT_BROWSER_MAX_STEPS", 22)
BROWSER_DEADLINE_SECONDS = _env_float("AGENT_BROWSER_DEADLINE_SECONDS", 600.0)
BROWSER_STEP_MAX_TOKENS = _env_int("AGENT_BROWSER_STEP_MAX_TOKENS", 3000)

# How many times an interaction may leave the page unchanged before the
# loop stops nudging and tells the model to change tactics entirely.
# Two is enough to rule out a mis-click without spending the budget
# proving the same control does nothing.
MAX_INEFFECTIVE_INTERACTIONS = _env_int("AGENT_MAX_INEFFECTIVE_INTERACTIONS", 2)

# Guidance that only makes sense once a browser is involved, and only
# added to the prompt then -- an ordinary research task should not pay
# tokens for advice about sort controls.
#
# The first rule is the one that would have saved all four TradingView
# runs. Every one of them tried to operate the screener's UI; none tried
# the URL, which encodes the sort directly and cannot mis-click.
BROWSER_RULES = """
WORKING IN A BROWSER — read these before your next action:
- A BLOCKED PAGE IS A REASON TO CHANGE INSTRUMENT, NOT SITE. If a page
  comes back blocked, empty, or as a bot check, do not keep trying that
  site in the browser and do not conclude it has no content: read the
  SAME URL with action.web_read, which reaches many sites a browser
  cannot. Only move on to a different source once that has failed too.
- PREFER A URL OVER CLICKING. If a sort, filter, search, date range or
  page number can be written in the address, navigate straight to it
  instead of operating controls. Look at the current URL for the
  parameters the site already uses and change them. One navigation
  cannot mis-click; six clicks can, and a click that lands wrong still
  reports success.
- Use browser_find to locate a control by description. browser_observe
  lists the first elements it finds and stops, so on a busy page the
  control you need may not be in it at all — that is not the same as it
  being absent.
- After any click or select, CHECK the page actually changed before you
  believe it. If you are told the page is unchanged, that control did
  nothing: do not click it again with different hopes.
- Read the rows LAST. Anything you extract before sorting is the site's
  default view, not the answer to the question you were asked.
- PICK THE READER THAT MATCHES WHAT WAS ASKED FOR. Calling the same tool
  again cannot turn it into a different tool:
    whole page, prose            -> action.browser_extract
    ONE named section            -> action.browser_extract_section
    the contents / what it covers-> action.browser_extract_toc
    every heading                -> action.browser_outline
    rows and figures             -> action.browser_extract_table
    repeated cards or listings   -> action.browser_extract_records
    "what shape is this page?"   -> action.browser_page_structure
  If a reader returned the wrong thing twice, the answer is a DIFFERENT
  reader, not the same one again.
"""


def _obs_cap(action: str) -> int:
    """How much of a tool result the model gets to see.

    A page description is summarised fine at 1500 characters. A table is
    not: the answer IS the rows, and cutting them off makes an agent
    report three of ten and call the rest unavailable.
    """
    name = (action or "").lower()
    return (MAX_DATA_OBSERVATION_CHARS
            if any(t in name for t in _DATA_TOOLS) else MAX_OBSERVATION_CHARS)


def _loop_adapter(default: Any, configured: Optional[str] = None) -> Any:
    """Optionally run the AGENTIC LOOP on a different model than the rest
    of the pipeline.

    Deciding a tool call and writing a deliverable are different jobs.
    The default model (gpt-oss:120b-cloud) is decent at prose and poor at
    the first one: across four quant runs it called run_python ZERO
    times, while the tool sat first in its list with a full description,
    and then wrote that metrics "could not be retrieved because
    action.calculate returned no values" — a tool it had never invoked.
    Four separate scaffolding fixes (build the tool, lengthen the
    description, rank it first, add prompt rules) moved that count from
    0 to 0.

    So this exists to answer the question those fixes could not: is the
    planner failing because of the scaffolding, or because of the model?
    Set AGENT_LOOP_MODEL and only the loop changes; synthesis, critique
    and evidence all stay on the main adapter, so any difference in
    behaviour is attributable.

    If a stronger model does pick the right tool, this stops being a
    diagnostic and becomes the fix — route tool-selection to a
    tool-capable model, keep the cheaper one for prose.

    `configured` is the per-employee `model.loop_model` setting, so one
    employee can drive a browser on a stronger model while the rest of
    the company stays on the cheap one. AGENT_LOOP_MODEL still outranks
    it: a process-wide diagnostic knob has to beat configuration, or
    changing one variable to answer a question means auditing every
    employee first.

    Adapters come from a pool rather than being built here, because this
    now runs per employee per run rather than once per process, and
    every construction used to cost a health probe.
    """
    name = os.environ.get("AGENT_LOOP_MODEL", "").strip() or (configured or "").strip()
    if not name:
        return default
    from backend.app.models.adapter_pool import get_adapter
    return get_adapter(name, default)


STEP_PROMPT = """You are {role}, working on a task. You can call tools one at a time.
After each call you SEE the result, then decide the next action. Work
step by step until the task is genuinely done.
{standing_rules}{browser_rules}{playbook}
YOUR TASK:
"{task}"

TOOLS YOU CAN CALL:
{tools_block}

WORK SO FAR (your previous actions and their real results):
{transcript}

Decide the SINGLE next action.

Rules:
- Call ONE tool per step. Base it on what the results above actually
  say — not on what you assumed before calling.
- If a call failed or returned nothing useful, try a DIFFERENT approach
  or different arguments. Do not repeat an identical call.
- If a call fails because of an ARGUMENT you supplied, RE-READ THE TASK
  for the correct value before trying again. Do not invent plausible
  substitutes. Guessing an id or a name three times in a row is never
  the right move — the correct value is usually written in the task.
- Only use a tool that genuinely does the job. If NO available tool can
  do what this task needs, say so plainly and return DONE — do not
  substitute a loosely related tool (do not send email, read the inbox,
  or read unrelated files just because those tools exist).
- READING a page and OPERATING one are different jobs, so pick the
  instrument before the first call. To find pages or read what is on
  one, action.web_search and action.web_read do it in a single call and
  reach sites that turn a browser away. Open a browser when you must ACT
  on the page — click, sort by a control, filter, fill a form, sign in —
  or when you need a table's cells rather than its flowed text.
- Do not stop early while useful work remains. If you return DONE before
  the task is actually complete, your thought MUST state what stopped
  you.
- If a tool result says something is "queued for founder approval",
  that action has NOT happened yet. That is expected and correct —
  treat it as done-for-now and move on. Never re-queue the same action.
- If a tool result tells you to poll it again (e.g. browser_task_status),
  calling it again with the same arguments is correct and expected —
  that is not a repeated mistake, it's checking on background progress.
- When you have everything needed to write the deliverable, or no
  remaining tool would help, return the DONE action. Do not keep
  calling tools just to look busy.
- You have used {steps_used} of {max_steps} steps.
- SAY WHAT SHOULD BE TRUE AFTERWARDS. Every action carries an "expect"
  describing the state you are trying to reach, in plain words — "the
  table is sorted by weekly change", "the login form is filled in", "the
  search results show Product Manager roles". This is checked against the
  page. If the state does not arrive you are told so and you retry THAT
  step, rather than carrying on as though it worked. Guessing at the
  expectation to satisfy the format is worse than useless: it is the
  thing that decides whether a step is repeated or accepted.

Return JSON only, one of these two shapes:
{{"thought": "<one sentence: why this action next>", "action": "<connection.tool_name>", "arguments": {{...}}, "expect": "<what should be true after this>"}}
{{"thought": "<one sentence: why you're finished>", "action": "DONE"}}

JSON only."""


class AgenticExecutor:
    """Runs a bounded THINK -> ACT -> OBSERVE loop for one specialist.

    `run()` returns a prompt-injectable transcript of what ACTUALLY
    happened (real calls, real results), or None when no tools are
    available / nothing was done — matching MCPPlanner's contract so
    callers can swap between them.
    """

    def __init__(
        self,
        model_adapter: Any,
        max_steps: int = MAX_STEPS,
        deadline_seconds: float = DEADLINE_SECONDS,
        step_max_tokens: int = STEP_MAX_TOKENS,
        required_outputs: Sequence[str] = (),
        standing_rules: Sequence[str] = (),
        loop_model: Optional[str] = None,
    ):
        # Every one of these is defaulted, so `AgenticExecutor(adapter)`
        # anywhere else in the repo -- and in every test -- behaves
        # exactly as it did before per-employee config existed.
        self.adapter = _loop_adapter(model_adapter, loop_model)
        self.max_steps = max_steps
        self.deadline_seconds = deadline_seconds
        self.step_max_tokens = step_max_tokens
        self.required_outputs = tuple(required_outputs or ())
        self.standing_rules = tuple(standing_rules or ())

        # Whether the CALLER chose these, or they are simply the module
        # defaults. Only defaults get widened for browser work: a founder
        # who set max_steps=4 meant 4, and silently spending 22 would
        # make the settings page a suggestion box.
        self._budget_is_default = (
            max_steps == MAX_STEPS
            and deadline_seconds == DEADLINE_SECONDS
            and step_max_tokens == STEP_MAX_TOKENS
        )
        self._budget_widened = False

    # ---- tool surface -------------------------------------------------

    def _available_tools(self) -> List[Dict[str, Any]]:
        """Same unified surface the old planner used: MCP servers the
        founder connected + their custom HTTP tools + built-in actions.
        for_planner=True hides tools whose arguments are a whole
        deliverable (create_pptx/docx/xlsx — those fire post-synthesis
        from finished text) and tools that block on a human. Delegated
        to ToolRegistry (tool_registry.py) so this three-way union lives
        in exactly one place instead of being reimplemented per caller.

        The task hint travels on `self._task_hint` rather than as a
        parameter, deliberately: this method is overridden by every test
        that needs a deterministic tool surface, and widening its
        signature broke all of them at once. A method others substitute
        should keep the shape they substituted.

        The hint drops niche tools the task never mentions, so they
        cannot crowd the ranking -- `arc_click` beat every browser tool
        on a TradingView task purely by matching the word "click"."""
        try:
            return _tool_registry.list_tools(
                for_planner=True, task_hint=getattr(self, "_task_hint", ""))
        except Exception:  # noqa: BLE001
            return []

    def _render_tools(self, tools: List[Dict[str, Any]], task: str = "") -> str:
        """Render the tool list with the ones that fit THIS task first.

        Order and length both turned out to matter more than expected.
        Two live runs had the right tool registered, visible, and unused:
        a quant run reached for read_inbox and post_slack while
        fetch_market_data sat in the list, and never called run_python at
        all despite being asked for backtest metrics. The list was a flat
        dump of 26 tools in registration order, each truncated at 180
        characters — so the relevant one was buried among irrelevant ones
        and its usage guidance ("Use this for EVERY computed result…")
        was cut off mid-sentence.

        So the most relevant few are promoted to the top with their FULL
        description, and everything else follows. Ranking is the same
        BM25 used for memory recall (memory/retrieval.py) — no extra LLM
        call, no new dependency, and it degrades to the old flat listing
        when nothing scores.

        This is a nudge, not a restriction: every tool is still listed
        and callable. Hiding the rest would trade one failure (wrong tool
        chosen) for a worse one (right tool unavailable).
        """
        def _entry(t: Dict[str, Any], desc_chars: int) -> str:
            schema = t.get("input_schema") or {}
            props = list((schema.get("properties") or {}).keys())[:8]
            params = f"  params: {', '.join(props)}" if props else ""
            desc = str(t.get("description") or "")[:desc_chars]
            return f"- {t['qualified_name']}: {desc}" + (f"\n{params}" if params else "")

        ranked_names: List[str] = []
        if task:
            try:
                from backend.app.memory import retrieval
                docs = [
                    (
                        t["qualified_name"],
                        f"{t['qualified_name']} {t.get('capability') or ''} "
                        f"{t.get('description') or ''}",
                    )
                    for t in tools
                ]
                ranked_names = [
                    name for name, _ in retrieval.rank(
                        task, docs, limit=RELEVANT_TOOLS_SHOWN, min_relevance=0.0
                    )
                ]
            except Exception:  # noqa: BLE001
                ranked_names = []

        if not ranked_names:
            return "\n".join(_entry(t, 180) for t in tools)

        by_name = {t["qualified_name"]: t for t in tools}
        top = [by_name[n] for n in ranked_names if n in by_name]
        rest = [t for t in tools if t["qualified_name"] not in set(ranked_names)]
        logger.info("tool ranking for task: %s", ", ".join(ranked_names))

        lines = ["MOST LIKELY RELEVANT TO THIS TASK (read these first):"]
        lines += [_entry(t, 600) for t in top]
        lines.append("")
        lines.append("ALL OTHER TOOLS:")
        lines += [_entry(t, 180) for t in rest]
        return "\n".join(lines)

    # ---- execution ----------------------------------------------------

    def _execute(self, qname: str, args: Dict[str, Any]) -> str:
        """Route one call by namespace. Never raises — a failure is an
        observation the model can react to, not a crashed run. Delegated
        to ToolRegistry.call(), which does this same namespace routing
        (and normalizes MCP's raise-on-error to the same text-result
        shape action/HTTP already use) in one place."""
        try:
            return _tool_registry.call(qname, args)
        except Exception as exc:  # noqa: BLE001
            return f"(call failed: {exc})"

    def run(self, task: str, role: str = "Specialist",
            original_task: Optional[str] = None) -> Optional[str]:
        task = (task or "").strip()
        if not task:
            return None
        # Set before listing so niche tools the task never mentions are
        # dropped BEFORE ranking rather than competing in it.
        self._task_hint = task if not original_task else task + chr(10) + original_task
        tools = self._available_tools()
        if not tools:
            return None

        # Rank against the founder's ORIGINAL wording as well as this
        # specialist's sub-task. A Supervisor's decomposition strips the
        # words that make a tool relevant: a sub-task reading "retrieve
        # the 10-year daily price history" ranked ONLY fetch_market_data
        # and buried run_python, on a run whose founder-level ask was a
        # backtest with CAGR/Sharpe/drawdown. Ranking on both keeps the
        # compute tool visible to whoever ends up needing it.
        ranking_text = task if not original_task else task + chr(10) + original_task
        tools_block = self._render_tools(tools, ranking_text)
        known_names = {t["qualified_name"] for t in tools}
        # Tools marked pollable (browser_task_status) are MEANT to be
        # called again with identical arguments while something else
        # finishes in the background — exempt them from the repeat-call
        # guard below, which exists to stop a model stuck calling the
        # same thing hoping for a different answer, not to stop a poll.
        pollable_names = {t["qualified_name"] for t in tools if t.get("pollable")}

        # Budgets are LOCAL to this run, not attributes, so widening them
        # for a browser task cannot leak into the next run of a reused
        # executor.
        max_steps = self.max_steps
        step_tokens = self.step_max_tokens
        started = time.monotonic()
        deadline = started + self.deadline_seconds
        # Reset per run, so a reused executor widens again rather than
        # remembering that a previous task already did.
        self._budget_widened = False

        steps: List[Dict[str, str]] = []   # rendered transcript entries
        # (qname, args-json) -> the page as it looked when that call was
        # last made. A dict rather than a set because for browser work
        # "the same call" is not the same question: see below.
        # A wall is a fact about right now, not one worth carrying into
        # the next run an hour later -- a bot check expires, a login
        # completes. Cleared per run for the same reason element ids are.
        reset_instrument_memory()

        seen_calls: Dict[Any, str] = {}
        repeat_counts: Dict[Any, int] = {}  # how often each call has been repeated
        consecutive_failures: Dict[str, int] = {}  # per-tool failure streak
        successful_calls = 0   # real work done, used to challenge an early DONE
        succeeded_tools: set = set()  # qualified names that returned without error
        computed = False       # a compute tool ran AND printed something
        done_challenged = False
        # Set once the ledger shows every state the task named has been
        # reached; the loop stops on the same step it becomes true.
        goal_done, goal_why = False, ""

        # WHAT THIS GOAL COSTS, DECIDED BEFORE THE BUDGET IS SPENT.
        #
        # A live run was asked for ten items, each verified on its own
        # page -- about twenty-five steps against a budget of twenty-two.
        # It was unwinnable before its first call, spent all twenty on
        # results pages, opened none of the ten and produced nothing.
        # Twenty steps of silence is the worst available answer.
        #
        # The plan is rebuilt once if the browser budget widens, because
        # the same goal is affordable at 22 steps and not at 10.
        goal_spec = goal_spec_from_task(task)
        plan = build_plan(task, max_steps, spec=goal_spec)
        if plan.spec.confident:
            logger.info("[%s] %s", role, describe_plan(plan).replace(chr(10), " | "))
        plan_note = plan.note()

        # A goal that cannot be done even once is refused BEFORE the
        # first call. Beginning it would spend the whole budget to
        # produce something that looks like an answer and is not one.
        # ONLY WHEN THE BUDGET IS OURS TO JUDGE.
        #
        # A founder who sets a small budget on purpose has made a
        # decision, and refusing their task because OUR estimate says it
        # is tight would override it. Caught by
        # test_a_founder_who_set_a_budget_keeps_it, which is exactly the
        # intent it was written to protect.
        if plan.verdict == REFUSE and self._budget_is_default:
            logger.warning("[%s] refusing before starting — %s", role, plan.reason)
            return (
                f"TASK NOT ATTEMPTED — {plan.reason}\n\n"
                f"Nothing was run, so there is nothing to report. Raise the "
                f"step budget or narrow the request."
            )
        done_refusals = 0      # times DONE was refused for missing computation
        acted = False

        # What must this turn actually CONTAIN before DONE is accepted?
        #
        # Two independent sources, ORed. The employee's own contract says
        # what this role always owes; the task text says what this
        # particular ask needs. Union, never intersection -- configuring
        # an employee must not be able to WEAKEN a requirement the task
        # itself established, or a settings page becomes a way to switch
        # the guards off.
        required = set(self.required_outputs)
        if task_requires_computation(ranking_text):
            # Computed from the same text the tools were ranked on, so the
            # founder's original wording counts even when the Supervisor's
            # paraphrase dropped the metric names.
            required.add(EXECUTED_CODE)
        if task_wants_ranking(ranking_text):
            # "top 5 gainers" is a claim about ORDER, and the default view
            # of a table is not ordered the way anyone asked. A live run
            # read a screener's default market-cap list and reported it as
            # top weekly gainers: every price real, every row wrong.
            required.add(RANKED_RESULT)
        needs_compute = EXECUTED_CODE in required

        # Standing rules reach the LOOP, not just the prose writer.
        #
        # This is the half that matters for correctness. "Always use
        # adjusted close", "execute on T+1, never same-bar", "252 trading
        # days" are rules about the CODE the loop writes -- and until now
        # the loop saw only `role` and `task`, never the employee's
        # mandate. A domain rule that reaches build_objective alone
        # arrives after the arithmetic is already wrong.
        standing_block = ""
        if self.standing_rules:
            standing_block = (
                "\nSTANDING RULES FOR YOUR WORK — they apply to every task and\n"
                "override generic guidance. Follow them in the code you write:\n"
                + "\n".join(f"- {r}" for r in self.standing_rules)
                + "\n"
            )

        # Scoped to THIS loop, so a file written by an earlier run cannot
        # satisfy this turn's file_written contract. Same lesson as the
        # compute gate's `since=` parameter: the ledger is a process-wide
        # singleton, and an unscoped query answers the wrong question.
        loop_started = time.time()

        def _ledger_calls() -> List[Dict[str, Any]]:
            """Never raises: a missing receipt must not break the loop, and
            an empty list simply means a kind stays unsatisfied."""
            try:
                from backend.app.tools.tool_call_ledger import get_call_ledger
                return get_call_ledger().calls(since=loop_started)
            except Exception:  # noqa: BLE001
                return []

        # State for the "that click did nothing" check.
        #
        # Every browser primitive re-observes after acting and returns the
        # page it produced, so comparing consecutive results answers the
        # one question the model cannot answer for itself: did what I just
        # did have any effect? A click that lands on empty space succeeds,
        # returns a full page, and looks exactly like a click that sorted
        # the table.
        last_page_view = ""
        ineffective_interactions = 0
        browser_rules_block = ""

        # What worked last time on a task like this. Looked up ONCE, at
        # the start, because it changes where the agent goes first --
        # by the time a browser call has succeeded it has already chosen
        # a site, and the most valuable thing a playbook carries is which
        # site to open.
        playbook_block = ""
        try:
            from backend.app.browser.playbook import hint_for
            playbook_block = hint_for(ranking_text)
            if playbook_block:
                logger.info("[%s] recalled a playbook for this task", role)
        except Exception:  # noqa: BLE001
            playbook_block = ""

        # Per-step recovery. `step_retries` counts how many times the
        # CURRENT step has been sent back; it resets the moment a step
        # reaches the state it declared. Without this the loop could only
        # multiply its failures -- one wrong step at minute fifteen wasted
        # everything after it, because nothing sent the agent back to the
        # step rather than onward.
        step_retries = 0
        plausibility_warned = False

        step_i = -1
        while True:
            step_i += 1
            if step_i >= max_steps:
                break
            if time.monotonic() > deadline:
                logger.info("[%s] agentic loop hit wall-clock deadline at step %d", role, step_i)
                steps.append({"note": "(stopped: time budget for tool use reached)"})
                break

            transcript = self._render_transcript(steps)
            # Retry the step call on a TRANSIENT backend failure instead of
            # ending the loop.
            #
            # Ollama cloud 500s intermittently and at random -- not
            # correlated with prompt size (verified: 1KB through 64KB all
            # return 200), not with general availability (five consecutive
            # short probes returned 200 while a real run was failing). A
            # single break here meant one unlucky 500 on step 2 -- the call
            # that decides whether to run the backtest -- silently ended
            # tool use for the whole task. The run then reported "the task
            # asked for computed figures but no computation was ever run",
            # blaming the model for what was an upstream outage. That is the
            # same failure shape this project keeps hitting: infrastructure
            # presenting as a behavioural result.
            #
            # Bounded and backed off so a genuinely dead backend still ends
            # the loop quickly rather than burning the time budget.
            raw = None
            last_exc = None
            for attempt in range(STEP_CALL_RETRIES + 1):
                try:
                    raw = self.adapter.chat_completion(
                        STEP_PROMPT.format(
                            role=role,
                            task=task[:1200],
                            tools_block=tools_block,
                            transcript=transcript,
                            steps_used=step_i,
                            max_steps=max_steps,
                            # The budget verdict rides with the standing
                            # rules because it IS one: "do 8 properly and
                            # say so" is a constraint on the work, not a
                            # hint about the page.
                            standing_rules=(
                                (plan_note + chr(10) + chr(10) + standing_block)
                                if plan_note else standing_block),
                            browser_rules=browser_rules_block,
                            playbook=playbook_block,
                        ),
                        temperature=0.1,
                        max_tokens=step_tokens,
                    )
                    break
                except Exception as exc:  # noqa: BLE001
                    last_exc = exc
                    if attempt < STEP_CALL_RETRIES:
                        delay = STEP_CALL_RETRY_BACKOFF * (2 ** attempt)
                        logger.warning(
                            "[%s] agentic step call failed (attempt %d/%d), "
                            "retrying in %.1fs: %s",
                            role, attempt + 1, STEP_CALL_RETRIES + 1, delay, exc,
                        )
                        time.sleep(delay)
            if raw is None:
                logger.warning(
                    "[%s] agentic step call failed after %d attempts, ending "
                    "tool use: %s", role, STEP_CALL_RETRIES + 1, last_exc,
                )
                # Record it as a fact this run can be judged against.
                # Without this the run proceeds to synthesis, reports
                # 'unknown', and the founder is told their AI handed the
                # work back -- when the real cause was the model backend
                # being unreachable. Same lesson as everywhere else here:
                # an infrastructure failure must not present as a
                # behavioural one.
                try:
                    from backend.app.tools.tool_call_ledger import get_call_ledger
                    get_call_ledger().record(
                        "backend.unavailable",
                        {"role": role, "attempts": STEP_CALL_RETRIES + 1},
                        ok=False,
                        result_preview=str(last_exc)[:400],
                        role=role,
                    )
                except Exception:  # noqa: BLE001
                    pass
                steps.append({"note": (
                    f"(stopped: the model backend failed {STEP_CALL_RETRIES + 1} "
                    f"times in a row -- this is an infrastructure failure, not a "
                    f"decision not to use tools)"
                )})
                break

            decision = _extract_json(raw) or {}
            action = str(decision.get("action") or "").strip()
            thought = str(decision.get("thought") or "").strip()
            expect = str(decision.get("expect") or "").strip()

            if not action or action.upper() == "DONE":
                # REFUSE a DONE that leaves a required output unproduced.
                #
                # Checked BEFORE the advisory challenge below, and it is a
                # different kind of thing: the challenge asks the model to
                # reconsider and accepts whatever it says next; this one
                # queries a RECORDED FACT -- did a compute tool really
                # succeed, was a page really fetched, does the file really
                # exist -- and will not accept DONE while one is missing.
                #
                # THE FAILURE THIS FIXES, seen live on 2026-08-15. Asked to
                # backtest SPY and report CAGR/Sharpe/max drawdown, the
                # Quant Analyst called fetch_market_data at step 1, then
                # returned DONE at step 2. The challenge fired, the model
                # said DONE again, and the loop conceded -- having computed
                # nothing, with 8 steps still unused and run_python ranked
                # FIRST in its tool list. The deliverable then reported no
                # figures at all.
                #
                # This is the shape this codebase keeps relearning: a
                # single advisory challenge is a prompt rule wearing a
                # guard's clothes. Guards that query a record hold; guards
                # that ask the model to think again do not.
                #
                # Bounded so a specialist that genuinely cannot compute
                # (bad data, tool broken) is not trapped arguing until the
                # step budget is gone -- after MAX_DONE_REFUSALS it is let
                # go, and _gate_deliverable still fails the run honestly.
                missing_kinds = unsatisfied_kinds(
                    required, succeeded_tools, computed, _ledger_calls(),
                ) if required else []
                # A RANKING NEEDS ROWS TO RANK.
                #
                # _gate_deliverable already only demands this of a run
                # that actually saw a data table; the loop demanded it of
                # every ranking-shaped task, and the two disagreed.
                #
                # Measured on a live jobs run: the task said "sort or
                # filter to the most recently posted IF THE SITE OFFERS
                # IT", the words "most " tripped the ranking contract,
                # and DONE was refused over a sort that (a) was optional
                # and (b) had no table behind it -- Naukri renders job
                # cards, not rows. The agent then burned seven of its
                # twenty-two steps bouncing between three job boards
                # hunting for a sort control to satisfy a requirement
                # nothing on any of those pages could satisfy.
                #
                # So the loop now asks the same question the gate asks.
                # Refusing DONE for an unsatisfiable reason does not make
                # a run more honest, it just spends the budget before the
                # run can finish -- and the gate still fails any run that
                # really did report a default view as a ranking.
                if RANKED_RESULT in missing_kinds and not run_saw_a_data_table(
                    _ledger_calls()
                ):
                    missing_kinds = [k for k in missing_kinds if k != RANKED_RESULT]
                if (
                    missing_kinds
                    and done_refusals < MAX_DONE_REFUSALS
                    and step_i + 1 < max_steps
                ):
                    done_refusals += 1
                    logger.warning(
                        "[%s] refused DONE at step %d - required output(s) not "
                        "produced yet: %s (refusal %d/%d)",
                        role, step_i + 1, ", ".join(missing_kinds),
                        done_refusals, MAX_DONE_REFUSALS,
                    )
                    for kind in missing_kinds:
                        steps.append({"note": REFUSAL_TEXT[kind]})
                    continue

                # Challenge a DONE that arrives with nothing achieved.
                #
                # A prompt rule was tried first and did not hold: a Game
                # Runner opened an ARC game successfully — it had the
                # grid in hand — then returned DONE at step 2 of 40
                # without a single click, and the run reported "no game
                # data was returned". One successful call is not the
                # same as a finished task, and the model is a poor judge
                # of the difference when it is eager to stop.
                #
                # Challenged ONCE only. A second DONE is accepted: a
                # specialist that genuinely has nothing left to do must
                # be able to stop, and nagging it into make-work is the
                # failure mode on the other side of this.
                if not done_challenged and successful_calls <= 1 and step_i + 1 < max_steps:
                    done_challenged = True
                    logger.info(
                        "[%s] DONE at step %d with %d successful call(s) — "
                        "challenging once", role, step_i + 1, successful_calls,
                    )
                    steps.append({"note": (
                        f"(you returned DONE after {successful_calls} successful "
                        f"tool call(s), with {max_steps - step_i - 1} steps "
                        f"still available. If the task is genuinely finished, "
                        f"return DONE again and say in your thought what you "
                        f"completed. If it is NOT finished — you opened something "
                        f"but never acted on it, or you have data you have not "
                        f"used yet — continue working now.)"
                    )})
                    continue
                logger.info("[%s] agentic loop finished after %d step(s)", role, step_i)
                break

            if action not in known_names:
                # Unknown tool: tell the model, let it correct itself.
                steps.append({
                    "thought": thought,
                    "call": f"{action}(...)",
                    "result": f"(no such tool: {action!r}. Pick one from the tool list exactly as written.)",
                })
                continue

            args = decision.get("arguments") or {}
            if not isinstance(args, dict):
                args = {}

            fingerprint = (action, json.dumps(args, sort_keys=True)[:400])

            # LOOKING AGAIN AT A PAGE THAT MOVED IS NOT A REPEAT.
            #
            # browser_observe takes only a session token, so observing a
            # page after changing it produces a byte-identical CALL --
            # and the guard below killed the run for it. That made the
            # observe -> act -> observe-to-verify cycle structurally
            # impossible, which is the very cycle the browser rules
            # instruct and the ranked_result contract requires. A live
            # trace ended exactly there: "stopped: repeated the same
            # action.browser_observe call - no new information", on a
            # page that had just been navigated somewhere new.
            #
            # Allowed only on EVIDENCE that the page really moved, so a
            # model observing the same unchanged page twice is still
            # stopped. Non-browser tools keep the old behaviour exactly:
            # last_page_view stays empty for them and an empty view
            # never counts as changed.
            prior_view = seen_calls.get(fingerprint)
            page_moved = (
                prior_view is not None
                and is_browser_tool(action)
                and page_view_changed(prior_view, last_page_view)
            )
            if page_moved:
                logger.info(
                    "[%s] %s repeated, but the page moved since — allowing",
                    role, action,
                )

            if action not in pollable_names and not page_moved:
                if fingerprint in seen_calls:
                    # A repeat used to abort the entire run on the FIRST
                    # occurrence. That was too blunt: on a real
                    # stock-gainers task the model clicked a search
                    # result, repeated the click, and the whole loop
                    # broke at step 3 — one browser_extract away from
                    # having the data on screen. The founder got a
                    # delegation plan instead of a report.
                    #
                    # A duplicate call is usually a recoverable fumble,
                    # not a wedged model, so tell it what happened and
                    # let it correct course. Only a SECOND repeat of the
                    # same call is treated as genuinely stuck.
                    repeat_counts[fingerprint] = repeat_counts.get(fingerprint, 0) + 1
                    if repeat_counts[fingerprint] >= 2:
                        logger.info("[%s] agentic loop repeated the same call twice, stopping", role)
                        steps.append({"note": f"(stopped: repeated the same {action} call — no new information)"})
                        break
                    logger.info("[%s] agentic loop repeated a call; nudging instead of stopping", role)
                    steps.append({
                        "thought": thought,
                        "call": f"{action}({json.dumps(args, ensure_ascii=False)[:300]})",
                        "result": (
                            f"(you already made this exact {action} call — its result is "
                            f"above. Do NOT repeat it. Either use what it already returned, "
                            f"call a DIFFERENT tool, change the arguments, or return DONE.)"
                        ),
                    })
                    continue
            seen_calls[fingerprint] = last_page_view

            logger.info("[%s] agentic step %d: %s(%s)", role, step_i + 1, action, json.dumps(args)[:120])
            # The page as it was BEFORE this action, captured before any
            # bookkeeping below moves it on. This is what the step's
            # declared expectation gets judged against.
            view_before = last_page_view

            # WHAT ALREADY FAILED ON THIS HOST, THIS RUN.
            #
            # With two ways to reach a page, a waste appeared that
            # neither tool could see alone: a live Reddit run learned at
            # step 1 that web_read is blocked there, switched correctly
            # to a headed browser, and then went BACK to web_read at
            # steps 11, 12 and 19 -- five of nineteen calls spent
            # re-learning a fact established in the first thirty seconds.
            # The repeat guard could not catch it: every URL differed.
            #
            # The note is attached to the RESULT rather than refusing the
            # call, because a wall can come down mid-run and a guard that
            # cannot recover is worse than the waste it prevents.
            _url = str(args.get("url") or "")
            _instrument_note = get_instrument_memory().note_for(action, _url)

            # HOW MANY OF THE ASKED-FOR ITEMS ARE ACTUALLY DONE.
            #
            # Phase 2 handed the model a verdict — "do 8 properly" — and
            # the live re-run ignored it, spending 17 of 22 steps on
            # results pages before opening one item. A note in a prompt is
            # not enforcement. This reads the LEDGER instead: candidates
            # found, candidates opened, candidates read. A run that has
            # found plenty and opened none is told so at the moment it
            # reaches for another list, rather than fifteen steps later.
            _item_note = None
            if plan.spec.per_item_work and plan.feasible_items > 1:
                _item_note = item_progress_note(
                    derive_items(_ledger_calls(), plan.feasible_items),
                    action, max(0, max_steps - step_i))

            result = self._execute(action, args)
            acted = True
            get_instrument_memory().record(action, _url, result)

            steps.append({
                "thought": thought,
                "call": f"{action}({json.dumps(args, ensure_ascii=False)[:300]})",
                "result": _truncate(
                    "\n".join(x for x in (_instrument_note, _item_note, result) if x),
                    _obs_cap(action),
                ),
            })

            # HAS THE TASK FINISHED, OR ONLY THE LAST ACTION?
            #
            # Asked to open a page, follow a link and go back, a live run
            # did exactly that in three calls and then made TWELVE MORE --
            # See also, scrolls, finds, clicks — ending where it already
            # was at call three. Every one succeeded; none advanced the
            # task. The loop could tell an ACTION had worked and had no
            # way to tell the GOAL was reached, so success kept reading
            # as "carry on".
            #
            # The stop is here, in the code, and rests on the LEDGER: the
            # pages the task named have actually been landed on, and a
            # required back-navigation was actually performed by the
            # browser. Not on the model announcing it is finished.
            #
            # It fires only when the task states a finish line a page
            # view can check, so open-ended work is untouched and still
            # governed by the DONE handling above. Ending a run early is
            # a worse failure than ending it late, so silence is the
            # default.
            if not goal_done:
                goal_done, goal_why = goal_check(task, _ledger_calls())
                if goal_done:
                    logger.info("[%s] task complete at step %d — %s",
                                role, step_i + 1, goal_why)
                    steps.append({"note": (
                        f"(TASK COMPLETE — verified from what this run "
                        f"actually observed: {goal_why}. Every state the task "
                        f"asked for has been reached, so the run stops here. "
                        f"Report what you found.)"
                    )})
                    break

            # Degenerate-sweep guard.
            #
            # The repeat guard above keys on (tool, arguments), so it only
            # catches a model calling the SAME thing twice. A live run
            # walked arc_reset through game_id 0, 1, 2, 3, "default",
            # "arcade", "test", "arcade3" — eight consecutive failures,
            # every fingerprint different, guard never fired. That is the
            # same stuck-loop pathology wearing incrementing arguments.
            #
            # Keyed on the tool alone and reset by any success, so a tool
            # that fails once and then works is unaffected.
            if _call_failed(result):
                consecutive_failures[action] = consecutive_failures.get(action, 0) + 1
                if consecutive_failures[action] >= MAX_CONSECUTIVE_FAILURES:
                    logger.warning(
                        "[%s] %s failed %d times in a row — stopping the loop",
                        role, action, consecutive_failures[action],
                    )
                    steps.append({"note": (
                        f"(stopped: {action} failed "
                        f"{consecutive_failures[action]} times in a row with "
                        f"different arguments. The arguments are not the "
                        f"problem to guess at — re-read the task for the "
                        f"correct value, or report honestly that this tool "
                        f"could not be used and why.)"
                    )})
                    break
            else:
                consecutive_failures[action] = 0
                successful_calls += 1
                succeeded_tools.add(action)
                if (
                    action in DATASET_COMPUTE_TOOLS_QUALIFIED
                    and _computation_produced_output(result)
                ):
                    computed = True

                if is_browser_tool(action):
                    # This run is driving a browser. Two things follow,
                    # both triggered by EVIDENCE that a browser tool
                    # really worked rather than by guessing from the
                    # task's wording -- the same reason every other check
                    # here reads a record instead of reading text.
                    if not browser_rules_block:
                        browser_rules_block = BROWSER_RULES
                        logger.info("[%s] browser detected — browser rules added", role)
                    if self._budget_is_default and not self._budget_widened:
                        self._budget_widened = True
                        max_steps = max(max_steps, BROWSER_MAX_STEPS)
                        step_tokens = max(step_tokens, BROWSER_STEP_MAX_TOKENS)
                        deadline = max(deadline, started + BROWSER_DEADLINE_SECONDS)
                        logger.info(
                            "[%s] browser budget: %d steps, %.0fs, %d tokens/step",
                            role, max_steps, BROWSER_DEADLINE_SECONDS, step_tokens,
                        )
                        # The same goal is affordable at 22 steps and not
                        # at 10, so the verdict is recomputed rather than
                        # inherited from the pre-browser budget.
                        plan = build_plan(task, max_steps, spec=goal_spec)
                        plan_note = plan.note()
                        if plan.spec.confident:
                            logger.info("[%s] %s", role,
                                        describe_plan(plan).replace(chr(10), " | "))

                    comparable = is_page_view_tool(action)
                    page_moved = (comparable and bool(last_page_view)
                                  and page_view_changed(last_page_view, result))

                    # DID THE DATA MOVE? A different question from "did the
                    # page change", and the only one that matters when the
                    # task is about rows.
                    #
                    # Three-valued. None means this page carries no data
                    # table, so the question does not apply and the page
                    # comparison stands on its own. It must never collapse
                    # into False -- "there were no rows to move" and "the
                    # rows did not move" are different facts, and treating
                    # missing evidence as failing evidence would nag every
                    # honest run on a form or a dashboard.
                    data_moved = None
                    if comparable and last_page_view:
                        try:
                            from backend.app.browser.observation import rows_changed
                            data_moved = rows_changed(last_page_view, result)
                        except Exception:  # noqa: BLE001
                            data_moved = None

                    # ONE resolution, so the model never gets two notes
                    # that contradict each other. Where there are rows,
                    # the rows are the truth; elsewhere, the page is.
                    changed = data_moved if data_moved is not None else page_moved

                    # THE FEEDBACK THAT WAS MISSING.
                    #
                    # A click that hits nothing succeeds, returns a full
                    # page, and is indistinguishable in the call log from
                    # a click that sorted the table. The end-of-run
                    # contract could already tell them apart -- it
                    # compares the page before and after -- but it ran
                    # after the turn was over. So a model clicked
                    # ineffectively, was told nothing, and clicked again;
                    # four TradingView runs never once learned that the
                    # screener had not moved. Same comparison, run at the
                    # moment the model can still act on it.
                    # Navigations count as acting on the page, not just
                    # clicks. The single most misleading result seen live
                    # was a navigate to an invented sort parameter: the
                    # page loaded, the address bar read as sorted, the
                    # rows were the defaults. Judged as an interaction it
                    # was invisible; judged on its data it is obvious.
                    acted_on_page = (is_interaction_tool(action)
                                     or "browser_navigate" in action)
                    if acted_on_page and last_page_view:
                        if changed:
                            ineffective_interactions = 0
                        else:
                            ineffective_interactions += 1
                            logger.info(
                                "[%s] %s changed nothing that matters "
                                "(page_moved=%s data_moved=%s, %d in a row)",
                                role, action, page_moved, data_moved,
                                ineffective_interactions,
                            )
                            if ineffective_interactions >= MAX_INEFFECTIVE_INTERACTIONS:
                                steps.append({"note": (
                                    f"(THE ANSWER HAS STILL NOT CHANGED. That is "
                                    f"{ineffective_interactions} actions in a row that "
                                    f"did nothing to the data — the elements you are "
                                    f"choosing are not the control you need, and trying "
                                    f"a third is unlikely to differ. CHANGE TACTICS NOW: "
                                    f"call action.browser_find to search the whole page "
                                    f"for the control by description, or switch the view "
                                    f"— a column you cannot see often lives under a "
                                    f"different tab or preset. Do not report anything "
                                    f"you have read so far as the answer: it is the "
                                    f"page's default view, not the result you were "
                                    f"asked for.)"
                                )})
                            elif data_moved is False and page_moved:
                                # The case that used to read as success.
                                steps.append({"note": (
                                    "(NOTE: the page changed but THE ROWS DID NOT. The "
                                    "table is showing exactly the same records in "
                                    "exactly the same order as before. You opened or "
                                    "closed something — a filter panel, a menu, a page "
                                    "that ignored the parameter you put in the URL — "
                                    "but you did not change the answer. Reading these "
                                    "rows now would report the default view. Find the "
                                    "control that actually reorders the table.)"
                                )})
                            else:
                                steps.append({"note": (
                                    "(NOTE: the page is byte-for-byte the same as "
                                    "before that action. It succeeded, but the element "
                                    "you picked was not the control — nothing sorted, "
                                    "filtered or opened. Do not treat this as done. "
                                    "Pick a different element, or use action.browser_find "
                                    "to locate the right one by description.)"
                                )})
                    elif changed:
                        # A navigation or scroll moved the page; whatever
                        # was not working before is no longer the state.
                        ineffective_interactions = 0

                    if comparable and result:
                        last_page_view = result

                    # A ranking of impossible figures, caught while the
                    # agent can still filter the page. At the gate this
                    # only fails the run; here it can be fixed.
                    if "extract" in action and not plausibility_warned:
                        try:
                            from backend.app.orchestrator.plausibility import (
                                RETRY_NOTE, check as _plausible,
                            )
                            if _plausible(ranking_text, result):
                                plausibility_warned = True
                                logger.info(
                                    "[%s] extracted ranking is implausible — "
                                    "telling it to filter", role)
                                steps.append({"note": RETRY_NOTE})
                        except Exception:  # noqa: BLE001
                            pass

            # DID THE STEP REACH THE STATE IT DECLARED?
            #
            # Last, so the bookkeeping above has already run: a step that
            # missed still counts as a browser call, still widens the
            # budget, still moves the recorded view. Only the loop's
            # POSITION is rolled back.
            #
            # Judged from the page, never from the model's account of its
            # own action -- a model that has just acted is the least
            # reliable witness to whether the action landed, and every
            # guard here that asked the model something has had to be
            # rewritten as one that reads a record.
            #
            # UNKNOWN is the common case and never retries: "the results
            # show Product Manager roles" is a claim about content that no
            # mechanical check settles. A check that guessed there would
            # stall honest runs on every page it could not read, which is
            # a worse failure than the one it set out to fix.
            if expect and is_browser_tool(action):
                verdict, miss_reason = judge_step(
                    expect=expect,
                    result=result,
                    before_view=view_before,
                    after_view=result,
                    call_failed=_call_failed(result),
                    tool=action,
                )
                if verdict == MISSED and step_retries < MAX_STEP_RETRIES:
                    step_retries += 1
                    logger.info(
                        "[%s] step missed its expectation (%s) — retry %d/%d",
                        role, miss_reason, step_retries, MAX_STEP_RETRIES,
                    )
                    steps.append({"note": retry_note(
                        expect, miss_reason or "the state did not arrive",
                        step_retries)})
                    continue
                if verdict == ACHIEVED:
                    if step_retries:
                        logger.info("[%s] step recovered after %d retry(ies)",
                                    role, step_retries)
                    step_retries = 0

        _release_browser_sessions(role)

        if not acted:
            return None

        # A RUN THAT FINISHED SHORT SAYS SO, WITH A NUMBER.
        #
        # The prose is written from this transcript, so the real count has
        # to reach it. Without this the writer sees a list of candidates
        # and a handful of opened pages with nothing distinguishing them —
        # which is the path by which a run that opened three items ends up
        # presenting ten.
        #
        # Derived from the ledger at the end rather than counted as the
        # run goes, for the same reason every other check here reads a
        # record: a counter the loop maintains is a counter the loop can
        # get wrong.
        if plan.spec.per_item_work and plan.feasible_items > 1:
            short = item_shortfall_note(
                derive_items(_ledger_calls(), plan.feasible_items))
            if short:
                logger.info("[%s] finished short — %s", role, short[:110])
                steps.append({"note": short})

        return self._wrap(steps)

    # ---- rendering ----------------------------------------------------

    def _render_transcript(self, steps: List[Dict[str, str]]) -> str:
        if not steps:
            return "(nothing yet — this is your first action)"
        chunks = []
        for i, s in enumerate(steps, 1):
            if s.get("note"):
                chunks.append(s["note"])
                continue
            chunks.append(
                f"Step {i}:\n"
                f"  thought: {s.get('thought', '')}\n"
                f"  called:  {s.get('call', '')}\n"
                f"  result:  {s.get('result', '')}"
            )
        text = "\n\n".join(chunks)
        # Keep the most RECENT context when trimming — the latest results
        # are what the next decision depends on.
        if len(text) > MAX_TRANSCRIPT_CHARS:
            text = "[…earlier steps trimmed…]\n\n" + text[-MAX_TRANSCRIPT_CHARS:]
        return text

    def _wrap(self, steps: List[Dict[str, str]]) -> str:
        return (
            "REAL WORK YOU ALREADY DID (multi-step tool use)\n"
            "(You called these tools one at a time and saw each result before "
            "the next. Use these REAL results as your source data — do not "
            "invent facts they didn't return. If a result says something is "
            "queued for founder approval, it has NOT happened yet: say so "
            "plainly, don't imply it's done.)\n\n"
            + self._render_transcript(steps)
            + "\n"
        )


def _release_browser_sessions(role: str) -> None:
    """Close browser sessions this loop opened, once it's finished.

    browser_navigate deliberately leaves its session alive so the model
    can extract and click across several steps. Nothing closed it
    afterwards, so each run left a real Chrome process holding the shared
    on-disk profile until the 15-minute idle sweep — and because one
    profile can only be held by one context at a time, the next launch
    failed with "profile is already in use". That is what made the
    browser tests flaky whenever a dev server had recently run a task.

    Sessions parked in a founder-approval state are left alone: closing
    one would destroy the live page a pending browser_submit still needs.
    Never raises — cleanup must not fail a run that otherwise succeeded.
    """
    try:
        from backend.app.browser.session_manager import get_manager
        mgr = get_manager()
        for token in list(mgr._live._resources.keys()):
            session = mgr.get(token)
            if session is None:
                continue
            status, _ = session.get_status()
            if status in ("awaiting_login", "awaiting_submit"):
                continue  # a human still has to act on this one
            mgr.close(token)
            logger.info("[%s] released browser session %s", role, token)
    except Exception as exc:  # noqa: BLE001
        logger.info("browser session cleanup skipped: %s", exc)


def _call_failed(result: Any) -> bool:
    """True when a tool result is one of the system's failure strings.

    Failure is signalled as TEXT here, not exceptions — every subsystem
    normalizes to a parenthesised marker so a failure is an observation
    the model can react to rather than a crashed run (see
    AgenticExecutor._execute and ToolRegistry.call). The cost of that
    choice is that callers cannot use try/except to notice a failure, so
    the markers have to be recognised explicitly.
    """
    text = str(result or "").lstrip().lower()
    return text.startswith((
        "(call failed",
        "(action failed",
        "(invalid argument",
        "(missing required argument",
        "(no action named",
        "(invalid tool name",
        "(not an action tool",
        "(could not enqueue",
        # run_python does NOT use the parenthesised marker -- it hands
        # back the traceback whole, on purpose, so the model can fix its
        # own code. That made a crashed script read as a successful call
        # everywhere this function is consulted: the degenerate-sweep
        # guard never fired on broken Python, and it counted toward
        # `successful_calls`, which is what the DONE-challenge measures.
        "python exited with code",
    ))


def _computation_produced_output(result: Any) -> bool:
    """True when a compute call actually returned a computed figure.

    Stricter than "the call did not fail", because two run_python
    outcomes are technically successful and have still computed nothing
    the deliverable can quote:

      - the script crashed (caught by `_call_failed` above);
      - the script ran and printed nothing, which run_python reports
        explicitly. Exit code 0, no number anywhere.

    Letting either satisfy the required-outputs gate would rebuild D6 in
    a new place -- a run passing "yes, it computed" on a call that
    produced no number.
    """
    text = str(result or "").strip().lower()
    if not text or _call_failed(result):
        return False
    return not text.startswith("python ran successfully but printed nothing")


def _truncate(text: str, limit: int) -> str:
    text = str(text or "")
    if len(text) > limit:
        return text[:limit].rstrip() + "\n[…result truncated…]"
    return text


_JSON_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


def _extract_json(text: str) -> Optional[Dict[str, Any]]:
    if not text:
        return None
    stripped = _JSON_FENCE_RE.sub("", text.strip()).strip()
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        pass
    s, e = stripped.find("{"), stripped.rfind("}")
    if s >= 0 and e > s:
        try:
            return json.loads(stripped[s : e + 1])
        except json.JSONDecodeError:
            return None
    return None
