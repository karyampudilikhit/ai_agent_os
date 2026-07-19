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
from backend.app.tools.mcp_client import get_registry as get_mcp_registry
from backend.app.tools.mcp_planner import MCPPlanner
from backend.app.tools.web_fetch import WebFetchTool, extract_urls
from backend.app.tools.web_search import TavilySearchTool, should_search

logger = logging.getLogger(__name__)

# Module-level singletons — one of each shared across every employee,
# so we don't pay per-instance init cost. Reads TAVILY_API_KEY from env.
_web_search = TavilySearchTool()
_web_fetch = WebFetchTool()


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
    ) -> str:
        """Prepend the employee's generated role/mandate so the pipeline
        answers *as* this specific employee. Accepts optional
        `teammates_context` (Plan B collaboration) and `web_context`
        (real Tavily search results injected as reading material)."""
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
        # Real-web-data block. When present, tell the employee to prefer
        # THESE facts over training-data guesses — that's the whole point
        # of having the connector.
        web_block = (
            f"\n\nReal web search results you can rely on for facts (use "
            f"these over your training memory when they conflict; cite by "
            f"URL if you quote specific numbers):\n{web_context}"
            if web_context
            else ""
        )
        return f"""You are the {self.role} on this project team.

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

    def run_task(
        self,
        task: str,
        max_refinements: Optional[int] = None,
        teammates_context: Optional[str] = None,
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

        # (2) Tavily search when the task sounds research-shaped
        search_results = []
        if _web_search.enabled and should_search(task):
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

        # (4) MCP pre-flight: any external tool servers the user has
        #     connected (Notion, Slack, filesystem, etc.) get their tools
        #     considered — the planner does one cheap LLM classification
        #     call to pick which are useful for THIS task, then invokes
        #     them and hands the outputs back as source data.
        if get_mcp_registry().list_all_tools():
            try:
                mcp_context = MCPPlanner(self.pipeline.adapter).plan_and_execute(task)
                if mcp_context:
                    web_context_parts.append(mcp_context)
                    used_web = True
                    logger.info("[%s] MCP tool results injected", self.role)
            except Exception as exc:  # noqa: BLE001
                logger.warning("MCP planning failed: %s", exc)

        web_context = "\n\n".join(web_context_parts) if web_context_parts else None

        objective = self.build_objective(
            task, teammates_context=teammates_context, web_context=web_context
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
