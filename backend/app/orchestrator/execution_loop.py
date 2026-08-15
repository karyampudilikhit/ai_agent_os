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
from typing import Any, Dict, List, Optional, Tuple

from backend.app.actions.action_registry import get_registry as get_action_registry
from backend.app.tools.http_tool_runner import HTTPToolRunner
from backend.app.tools.tool_registry import get_registry as get_tool_registry

logger = logging.getLogger(__name__)

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
MAX_TRANSCRIPT_CHARS = _env_int("AGENT_MAX_TRANSCRIPT_CHARS", 9000)
STEP_MAX_TOKENS = _env_int("AGENT_STEP_MAX_TOKENS", 700)
# How many times to re-attempt a step's model call before giving up on
# tool use. Ollama cloud 500s at random; without this, one unlucky
# failure on the step that decides to run the backtest ends tool use for
# the entire task and the run is then reported as "no computation was
# ever run" -- blaming the model for an upstream outage.
STEP_CALL_RETRIES = _env_int("AGENT_STEP_CALL_RETRIES", 3)
STEP_CALL_RETRY_BACKOFF = float(os.environ.get("AGENT_STEP_CALL_RETRY_BACKOFF", "1.5"))


def _loop_adapter(default: Any) -> Any:
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
    """
    name = os.environ.get("AGENT_LOOP_MODEL", "").strip()
    if not name:
        return default
    try:
        from backend.app.models.provider_adapters.ollama_adapter import OllamaAdapter
        adapter = OllamaAdapter(
            base_url=os.environ.get("OLLAMA_HOST", "http://localhost:11434"),
            model=name,
            api_key=os.environ.get("OLLAMA_API_KEY") or None,
        )
        logger.info("agentic loop using override model: %s", name)
        return adapter
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "AGENT_LOOP_MODEL=%s could not be built (%s) — falling back to the "
            "pipeline adapter", name, exc,
        )
        return default


STEP_PROMPT = """You are {role}, working on a task. You can call tools one at a time.
After each call you SEE the result, then decide the next action. Work
step by step until the task is genuinely done.

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

Return JSON only, one of these two shapes:
{{"thought": "<one sentence: why this action next>", "action": "<connection.tool_name>", "arguments": {{...}}}}
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
    ):
        self.adapter = _loop_adapter(model_adapter)
        self.max_steps = max_steps
        self.deadline_seconds = deadline_seconds

    # ---- tool surface -------------------------------------------------

    def _available_tools(self) -> List[Dict[str, Any]]:
        """Same unified surface the old planner used: MCP servers the
        founder connected + their custom HTTP tools + built-in actions.
        for_planner=True hides tools whose arguments are a whole
        deliverable (create_pptx/docx/xlsx — those fire post-synthesis
        from finished text) and tools that block on a human. Delegated
        to ToolRegistry (tool_registry.py) so this three-way union lives
        in exactly one place instead of being reimplemented per caller."""
        try:
            return _tool_registry.list_tools(for_planner=True)
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

    def run(self, task: str, role: str = "Specialist") -> Optional[str]:
        task = (task or "").strip()
        if not task:
            return None
        tools = self._available_tools()
        if not tools:
            return None

        tools_block = self._render_tools(tools, task)
        known_names = {t["qualified_name"] for t in tools}
        # Tools marked pollable (browser_task_status) are MEANT to be
        # called again with identical arguments while something else
        # finishes in the background — exempt them from the repeat-call
        # guard below, which exists to stop a model stuck calling the
        # same thing hoping for a different answer, not to stop a poll.
        pollable_names = {t["qualified_name"] for t in tools if t.get("pollable")}
        deadline = time.monotonic() + self.deadline_seconds

        steps: List[Dict[str, str]] = []   # rendered transcript entries
        seen_calls: set = set()            # (qname, args-json) repeat guard
        repeat_counts: Dict[Any, int] = {}  # how often each call has been repeated
        consecutive_failures: Dict[str, int] = {}  # per-tool failure streak
        successful_calls = 0   # real work done, used to challenge an early DONE
        done_challenged = False
        acted = False

        for step_i in range(self.max_steps):
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
                            max_steps=self.max_steps,
                        ),
                        temperature=0.1,
                        max_tokens=STEP_MAX_TOKENS,
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
                steps.append({"note": (
                    f"(stopped: the model backend failed {STEP_CALL_RETRIES + 1} "
                    f"times in a row -- this is an infrastructure failure, not a "
                    f"decision not to use tools)"
                )})
                break

            decision = _extract_json(raw) or {}
            action = str(decision.get("action") or "").strip()
            thought = str(decision.get("thought") or "").strip()

            if not action or action.upper() == "DONE":
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
                if not done_challenged and successful_calls <= 1 and step_i + 1 < self.max_steps:
                    done_challenged = True
                    logger.info(
                        "[%s] DONE at step %d with %d successful call(s) — "
                        "challenging once", role, step_i + 1, successful_calls,
                    )
                    steps.append({"note": (
                        f"(you returned DONE after {successful_calls} successful "
                        f"tool call(s), with {self.max_steps - step_i - 1} steps "
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
            if action not in pollable_names:
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
                seen_calls.add(fingerprint)

            logger.info("[%s] agentic step %d: %s(%s)", role, step_i + 1, action, json.dumps(args)[:120])
            result = self._execute(action, args)
            acted = True
            steps.append({
                "thought": thought,
                "call": f"{action}({json.dumps(args, ensure_ascii=False)[:300]})",
                "result": _truncate(result, MAX_OBSERVATION_CHARS),
            })

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

        _release_browser_sessions(role)

        if not acted:
            return None
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
        from backend.app.tools.browser_session_manager import get_manager
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
    ))


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
