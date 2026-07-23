"""DynamicEmployee — an Employee whose role and mandate come from the
spawner at instance-creation time, instead of being a hand-coded class
like IdeaValidationEmployee.

This is the shape the real Vision AI vision needs: a user says "help me
launch a Substack" and the system spins up a Writer + a Marketer + a
Growth Employee that didn't exist before that prompt. The class itself
is generic; each *instance* carries its own generated identity.

Uses the exact same Employee base (persistent role, scoped memory,
task inbox, min_tier verification floor) — nothing about how downstream
code interacts with an Employee changes.
"""

from __future__ import annotations

import logging
from typing import Optional

from backend.app.employees.employee import Employee
from backend.app.employees.memory_store import EmployeeMemoryStore
from backend.app.orchestrator.pipeline_controller import Pipeline
from backend.app.tools.http_tool_store import get_store as get_http_tool_store
from backend.app.tools.mcp_client import get_registry as get_mcp_registry
from backend.app.tools.mcp_planner import MCPPlanner
from backend.app.tools.reddit_reader import RedditReader, should_read_reddit
from backend.app.tools.web_fetch import WebFetchTool, extract_urls
from backend.app.tools.web_search import TavilySearchTool, should_search

logger = logging.getLogger(__name__)

# Module-level singletons — one of each shared across every employee,
# so we don't pay per-instance init cost. Reads TAVILY_API_KEY from env.
_web_search = TavilySearchTool()
_web_fetch = WebFetchTool()
_reddit = RedditReader()


class DynamicEmployee(Employee):
    """An Employee whose role/mandate are provided at spawn time."""

    # Every dynamic employee gets verification by default — the whole
    # product promise is "AI you can trust."
    min_tier = "single_call_critique"
    # And a specialist NEVER spawns another team inside itself. Without
    # this cap, a team of N employees where each employee's task also
    # looked "team-shaped" to the router would spawn N nested sub-teams,
    # blowing wall-clock cost by another N× (a 3-employee team turning
    # into 15+ minutes of nested spawning). Employees stay lean.
    max_tier = "single_call_critique"

    def __init__(
        self,
        employee_id: str,
        role: str,
        mandate: str,
        pipeline: Pipeline,
        memory_store: Optional[EmployeeMemoryStore] = None,
        min_tier: Optional[str] = None,
    ):
        super().__init__(employee_id=employee_id, pipeline=pipeline, memory_store=memory_store)
        # Instance-level overrides of the class attributes:
        self.role = role
        self.mandate = mandate
        if min_tier:
            self.min_tier = min_tier

    def build_objective(
        self,
        task: str,
        teammates_context: Optional[str] = None,
        web_context: Optional[str] = None,
        task_brief: Optional[str] = None,
    ) -> str:
        """Prepend the employee's generated role/mandate so the pipeline
        answers *as* this specific employee. Accepts optional
        `teammates_context` (Plan B collaboration), `web_context` (real
        Tavily search results injected as reading material), and
        `task_brief` (Supervisor-composed task-specific briefing with
        playbook quality rules — the "how to be premium-quality"
        instructions for this specific task).

        Layering (Supervisor-composed briefing is the outermost,
        highest-priority layer — quality rules come first):
          1. Task brief from Supervisor (quality rules + task-specific
             format/failure-mode instructions)
          2. Employee identity (role + persistent mandate)
          3. The task itself
          4. Anti-fabrication baseline
          5. Web context, teammates, history
        """
        context = self.memory.relevant_context(task)
        history_block = (
            f"\n\nRelevant history from your prior work for this user:\n{context}"
            if context
            else ""
        )
        teammates_block = (
            f"\n\nWork your teammates have already contributed on this same task "
            f"— use it, do not duplicate it:\n{teammates_context}"
            if teammates_context
            else ""
        )
        web_block = (
            f"\n\nReal web search results, provided as SUPPLEMENTARY source "
            f"material (cite by URL if you quote specific numbers):\n{web_context}"
            f"\n\nHow to use these results: prefer them over your training "
            f"memory for current/changing facts. BUT if the results are "
            f"incomplete, conflicting, or don't clearly answer the question, "
            f"do NOT fabricate a precise-looking justification (a table of "
            f"dates, a specific figure) that the sources don't actually "
            f"support. In that case, give your best-supported answer and "
            f"state the uncertainty plainly, or say the specific detail is "
            f"unclear from the sources."
            if web_context
            else ""
        )
        # Supervisor's task-specific briefing goes FIRST — the quality
        # rules and failure modes should shape everything the specialist
        # writes below. Emphasized so weaker models don't skim past.
        brief_block = (
            f"BRIEF FROM YOUR SUPERVISOR (follow these instructions exactly — "
            f"they are what makes this output premium-quality vs. generic):\n"
            f"{task_brief}\n\n"
            if task_brief
            else ""
        )
        return f"""{brief_block}You are the {self.role} on this project team.

Your mandate — what only you are responsible for:
{self.mandate}

Task the whole team is working on:
{task}

Do the part of this task that belongs to your role, and only your part.
If a piece of the task belongs to a different role on this team, say so
briefly and skip it — do not do work outside your mandate.

Do not fabricate specific statistics, survey results, or claims that
work has already been completed. If you don't know a real number,
describe things qualitatively (unless the web results below give you a
real one).{web_block}{teammates_block}{history_block}"""

    # Cheap LLM gate deciding whether a task genuinely needs external /
    # current data before we fire Tavily. The benchmark exposed the bug
    # this fixes: the keyword-only should_search() heuristic fired
    # search on simple, stable factual questions the base model already
    # knew (e.g. "how many moons does Mars have") — and noisy/conflicting
    # search results then FLIPPED a correct recall into a confident
    # fabrication (it invented a wrong Node.js LTS table). Tools should
    # add live facts the model lacks, never override facts it has.
    _LOOKUP_GATE_PROMPT = (
        "Decide whether answering the following accurately REQUIRES looking up "
        "current, changing, or external information — e.g. current prices, "
        "latest software versions, recent events, live metrics, specific "
        "niche/company details, or anything that changes over time or that a "
        "general-purpose model would not reliably know.\n\n"
        "If it can be answered reliably from stable, well-established general "
        "knowledge (history, science, geography, definitions, famous facts), "
        "it does NOT need a lookup.\n\n"
        "Question/task:\n\"{task}\"\n\n"
        "Reply with exactly one word: LOOKUP or DIRECT."
    )

    def _needs_external_lookup(self, task: str) -> bool:
        """True when the task genuinely needs live/external data. Gates
        the Tavily search so we don't inject noise into questions the
        model can already answer. Fails OPEN (returns True) on any error
        — better to search unnecessarily than to miss a needed lookup."""
        try:
            verdict = (self.pipeline.adapter.chat_completion(
                self._LOOKUP_GATE_PROMPT.format(task=task[:600]),
                temperature=0.0,
                max_tokens=600,  # reasoning model needs room to emit the word
                format=None,
            ) or "").strip().upper()
        except Exception as exc:  # noqa: BLE001
            logger.warning("[%s] lookup gate failed, defaulting to search: %s", self.role, exc)
            return True
        if "DIRECT" in verdict and "LOOKUP" not in verdict:
            logger.info("[%s] lookup gate: DIRECT — skipping web search", self.role)
            return False
        return True

    def run_task(
        self,
        task: str,
        max_refinements: Optional[int] = None,
        teammates_context: Optional[str] = None,
        task_brief: Optional[str] = None,
    ):
        """Same as Employee.run_task, but threads the (optional)
        teammates_context through build_objective. Overriding here (not
        on the base) keeps the base class truly role-agnostic — a v1
        collaboration coordinator can pass richer context without
        touching Employee.
        """
        # Real-work step. Three ways an employee can now touch the real
        # world before it writes anything:
        #   1. URLs literally in the task text -> fetch each one, read
        #      its actual page body. Covers "look at competitor.com",
        #      "check reddit.com/r/marketing/top", etc.
        #   2. Task looks research-shaped (should_search heuristic) ->
        #      hit Tavily for a list of relevant links + snippets.
        #   3. If we searched, ALSO fetch the top 2 result URLs from
        #      Tavily for a real-depth read, not just snippets.
        # All three feed into web_context. All three fail quietly.
        web_context_parts: list[str] = []
        used_web = False

        # (1) URLs in the task itself
        mentioned = extract_urls(task)
        if mentioned:
            pages = _web_fetch.fetch_multiple(mentioned)
            if pages:
                web_context_parts.append(_web_fetch.format_for_prompt(pages))
                used_web = True
                logger.info("[%s] fetched %d URL(s) mentioned in task", self.role, len(pages))

        # (2) Tavily search — but ONLY when the task genuinely needs
        #     external/current data. Two-stage gate:
        #       a) cheap keyword pre-filter (should_search) — skips the
        #          LLM gate entirely for obviously-non-research tasks
        #          ("write a poem"), preserving the fast path.
        #       b) cheap LLM gate (_needs_external_lookup) — for tasks
        #          that pass the keyword filter, confirm they actually
        #          need a lookup vs. being stable general knowledge.
        #     This stops search noise from overriding facts the model
        #     already knows (the Node.js-LTS fabrication the benchmark
        #     caught).
        search_results = []
        if _web_search.enabled and should_search(task) and self._needs_external_lookup(task):
            try:
                search_results = _web_search.search(task, max_results=5)
                if search_results:
                    web_context_parts.append(_web_search.format_for_prompt(task, search_results))
                    used_web = True
                    logger.info("[%s] Tavily returned %d result(s) for: %s",
                                self.role, len(search_results), task[:60])
            except Exception as exc:  # noqa: BLE001
                logger.warning("Tavily search failed: %s", exc)

        # (3) Deep-read: fetch top 2 Tavily URLs as full pages
        if search_results:
            deep_urls = [r["url"] for r in search_results[:2] if r.get("url")]
            # Skip URLs we already fetched under (1)
            deep_urls = [u for u in deep_urls if u not in mentioned]
            if deep_urls:
                deep_pages = _web_fetch.fetch_multiple(deep_urls)
                if deep_pages:
                    web_context_parts.append(_web_fetch.format_for_prompt(deep_pages))
                    logger.info("[%s] deep-read %d Tavily result page(s)", self.role, len(deep_pages))

        # (3.5) Reddit read-only pull — fires on r/subreddit mentions,
        #       full reddit.com/r/*/comments/* URLs, or reddit-shaped
        #       task language ("what are people saying", "community
        #       sentiment"). Needs REDDIT_CLIENT_ID + REDDIT_CLIENT_SECRET;
        #       silently disabled without them. Read-only; quiet on failure.
        if _reddit.enabled and should_read_reddit(task):
            try:
                reddit_block = _reddit.read_for_task(task)
                if reddit_block:
                    web_context_parts.append(reddit_block)
                    used_web = True
                    logger.info("[%s] Reddit context injected", self.role)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Reddit read failed: %s", exc)

        # (4) Tool pre-flight: any external tools the user has connected
        #     — MCP servers (Notion, Slack, filesystem, etc.) AND custom
        #     HTTP tools (user's own APIs) — get their tools considered
        #     by one cheap LLM classification call. The planner picks
        #     which are useful for THIS task, executes them, and returns
        #     the outputs as source data.
        has_mcp = bool(get_mcp_registry().list_all_tools())
        has_http = bool(get_http_tool_store().enabled())
        # Built-in action tools (send_email, post_slack, write_file,
        # read_file) are always present — including them means the
        # planner runs even for founders who haven't connected any MCP
        # or custom HTTP tool yet.
        from backend.app.actions.action_registry import get_registry as _get_actions
        has_actions = bool(_get_actions().known_names())
        if has_mcp or has_http or has_actions:
            try:
                planner_context = MCPPlanner(self.pipeline.adapter).plan_and_execute(task)
                if planner_context:
                    web_context_parts.append(planner_context)
                    used_web = True
                    logger.info("[%s] external tool results injected", self.role)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Tool planning failed: %s", exc)

        web_context = "\n\n".join(web_context_parts) if web_context_parts else None
        if task_brief:
            logger.info("[%s] task brief from Supervisor injected", self.role)

        objective = self.build_objective(
            task,
            teammates_context=teammates_context,
            web_context=web_context,
            task_brief=task_brief,
        )
        manager = self.pipeline.run_objective(
            objective,
            max_refinements=max_refinements,
            min_tier=self.min_tier,
            max_tier=self.max_tier,
        )
        snap = manager.snapshot()
        result = {
            "task": task,
            "role": self.role,
            "output": snap.get("synthesized_output"),
            "critique": snap.get("critique"),
            "was_refined": snap.get("was_refined"),
            "counts": snap.get("counts"),
            "used_web_search": used_web,
        }
        self.memory.record(task, result)
        return result
