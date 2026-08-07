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
from backend.app.orchestrator.execution_loop import AgenticExecutor
from backend.app.orchestrator.pipeline_controller import Pipeline
from backend.app.tools.browser_automation import DeepResearchTool, distinct_domain_urls
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
_deep_research = DeepResearchTool()

# Competitor/comparison-shaped tasks get a deep multi-page site crawl
# instead of a flat single-page fetch — a subset of web_search's
# broader _TRIGGER_WORDS, narrowed to the cases where reading just the
# homepage (or a search snippet) genuinely isn't enough: pricing pages,
# product pages, and "who are they" all live on different URLs. Same
# over-trigger philosophy as the rest of this file — a wasted deep
# crawl just costs some wall-clock time, a missed one means a shallow
# competitor report.
_DEEP_RESEARCH_TRIGGER = (
    "competitor", "competitors", "competitive analysis",
    "compare", "vs.", " vs ", "versus", "rival", "rivals",
)


def should_deep_research(text: str) -> bool:
    if not text:
        return False
    lowered = text.lower()
    return any(t in lowered for t in _DEEP_RESEARCH_TRIGGER)


# Interactive browser automation (Phase 2) — login/fill/submit — is
# explicitly dispatched rather than folded into the generic MCPPlanner
# pre-flight (step 4 below). Reason: unlike every other pre-flight tool,
# action.browser_task can BLOCK for up to 10 minutes waiting on the
# founder to log in in a real browser window. Letting the generic
# planner auto-pick this tool the way it picks send_email or read_file
# would risk it firing — and blocking the whole run — on a task that
# only loosely resembles form-filling. Requiring BOTH an explicit
# action verb AND a URL mentioned in the task (checked via extract_urls,
# not just a trigger word) keeps this deliberate: there has to be
# somewhere concrete to go, not just words that sound action-y.
_BROWSER_AUTOMATE_TRIGGER = (
    "apply to", "apply for", "submit the application", "submit an application",
    "sign up for", "sign up at", "register on", "register at",
    "fill out the form", "fill in the form", "submit this form", "submit the form",
)


def should_browser_automate(text: str) -> bool:
    if not text:
        return False
    lowered = text.lower()
    return any(t in lowered for t in _BROWSER_AUTOMATE_TRIGGER)


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

    _SEARCH_QUERY_PROMPT = (
        "Turn this work assignment into ONE web search query.\n\n"
        "ASSIGNMENT:\n\"{task}\"\n\n"
        "Rules:\n"
        "- Output ONLY the query text. No quotes, no explanation, no label.\n"
        "- Write what you would actually type into a search box: the "
        "subject and its key terms.\n"
        "- Strip instructions to yourself. \"Extract the full details for "
        "each paper in the raw list: title, authors...\" is not a query; "
        "the query is the SUBJECT those papers are about.\n"
        "- Keep it under 12 words."
    )

    def _search_query_for(self, task: str) -> str:
        """Derive a real search query from a work assignment.

        Passing the raw assignment text to the search engine was a real
        bug: a research-papers sub-task worded "Extract the full details
        for each paper in the raw list: tit..." went to Tavily verbatim
        and came back with slidedownloader.com, brainly.com, a YouTube
        video and a LinkedIn post about how to write a research paper.
        Not one scholarly source — so the employee wrote its citations
        from memory instead, which is where the fabricated DOIs came
        from.

        Falls back to the original text on any failure: a bad query is
        strictly better than no search.
        """
        try:
            q = (self.pipeline.adapter.chat_completion(
                self._SEARCH_QUERY_PROMPT.format(task=task[:600]),
                temperature=0.0,
                # Same reason _needs_external_lookup uses a large budget:
                # this is a reasoning model and spends tokens thinking
                # before it emits anything. At 120 it returned an empty
                # string every time and silently fell back to the raw
                # task text — the exact bug this method exists to fix.
                max_tokens=600,
                format=None,
            ) or "").strip()
        except Exception as exc:  # noqa: BLE001
            logger.warning("[%s] search-query rewrite failed: %s", self.role, exc)
            return task
        # Models like to answer with a label or wrap the query in quotes.
        q = q.splitlines()[-1].strip() if q else ""
        q = q.strip().strip('"').strip("'")
        for prefix in ("query:", "search query:", "search:"):
            if q.lower().startswith(prefix):
                q = q[len(prefix):].strip()
        if not q or len(q) < 3:
            return task
        logger.info("[%s] search query: %r", self.role, q[:80])
        return q

    def run_task(
        self,
        task: str,
        max_refinements: Optional[int] = None,
        teammates_context: Optional[str] = None,
        task_brief: Optional[str] = None,
        original_task: Optional[str] = None,
    ):
        """Same as Employee.run_task, but threads the (optional)
        teammates_context through build_objective. Overriding here (not
        on the base) keeps the base class truly role-agnostic — a v1
        collaboration coordinator can pass richer context without
        touching Employee.

        `original_task`: the founder's UNREWRITTEN top-level prompt, if
        this specialist is running a Supervisor- or CEO-composed
        sub_task rather than the founder's raw wording. Delegation
        rewriting is lossy by design (a Supervisor paraphrases "do a
        competitor analysis on linear.app" into "Research Linear's
        pricing tiers…") — but URLs, company names, and trigger words
        the pre-flight heuristics below key off often live only in the
        founder's original phrasing. Every pre-flight check below scans
        BOTH `task` and `original_task` (when provided) so a Supervisor's
        paraphrase can never silently disable web-fetch, Tavily search,
        or deep browser research that the founder's own wording would
        have triggered.
        """
        # The combined text pre-flight heuristics scan — task first (the
        # actual sub-task, most relevant), original_task appended so
        # anything only present in the founder's raw wording still
        # counts. Deliberately NOT deduped/normalized — heuristics here
        # are substring checks, redundancy is harmless.
        heuristic_text = task if not original_task else f"{task}\n{original_task}"
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

        # (1) URLs in the task itself. If this looks like a competitor/
        #     comparison task, deep-crawl each mentioned URL (multiple
        #     JS-rendered pages: pricing, product, about) instead of a
        #     flat single-page fetch — skips the flat fetch entirely so
        #     the same homepage text doesn't get injected twice.
        #
        #     `deep_wanted` (URL case) scans heuristic_text — an explicit
        #     URL is unambiguous evidence worth deep-reading regardless
        #     of which delegation layer mentioned it.
        #
        #     `deep_wanted_for_search` (used below in step 3) scans ONLY
        #     `task` — the specialist's OWN sub-task, not the inherited
        #     original_task. Found via a real bug: in a 4-Unit CEO run
        #     where the founder's top-level prompt said "competitor
        #     analysis", a downstream "Data Curator" specialist whose
        #     actual job was "clean, normalize, and verify the combined
        #     data" (zero web-research need) inherited the trigger word
        #     from the founder's prompt via heuristic_text, ran a Tavily
        #     search on ITS OWN unrelated sub-task text, and deep-crawled
        #     3 completely irrelevant sites (generic data-cleaning
        #     methodology blogs) — wasted ~15s and polluted its context.
        #     A URL is a concrete, unambiguous target regardless of
        #     which layer names it; a bare trigger WORD inherited from
        #     an ancestor prompt is not — it says nothing about whether
        #     THIS specialist's own task is actually competitor-shaped.
        mentioned = extract_urls(heuristic_text)
        deep_wanted = _deep_research.enabled and should_deep_research(heuristic_text)
        deep_wanted_for_search = _deep_research.enabled and should_deep_research(task)
        deep_site_pages: dict = {}

        if mentioned and deep_wanted:
            deep_site_pages = _deep_research.research_multiple(mentioned)
            block = _deep_research.format_for_prompt(deep_site_pages)
            if block:
                web_context_parts.append(block)
                used_web = True
                logger.info("[%s] deep-crawled %d mentioned site(s)", self.role, len(mentioned))
        elif mentioned:
            pages = _web_fetch.fetch_multiple(mentioned)
            if pages:
                web_context_parts.append(_web_fetch.format_for_prompt(pages))
                used_web = True
                logger.info("[%s] fetched %d URL(s) mentioned in task", self.role, len(pages))

        # (1.5) Interactive browser automation (Phase 2 — login/fill/
        #       submit). Explicit dispatch, not the generic MCPPlanner
        #       pre-flight — see should_browser_automate's docstring for
        #       why. Requires BOTH an explicit action verb ("apply to",
        #       "sign up for", ...) AND a URL actually mentioned in the
        #       task — there has to be somewhere concrete to go. Runs
        #       the FULL flow inline (login-wait can block for minutes;
        #       that's expected here, not a bug — the founder is meant
        #       to be present) and its receipt (queued for approval, or
        #       a failure reason) gets folded into web_context so the
        #       specialist's deliverable accurately reflects that the
        #       submission is PENDING, not already done.
        #
        #       goal=heuristic_text, NOT bare `task` — found via a real
        #       failure: the Supervisor's delegated sub_task said "fill
        #       in the fields with the exact values provided" without
        #       actually repeating any values (it assumed the specialist
        #       could see the founder's original prompt, which
        #       browser_task's single field-mapping LLM call never
        #       does). heuristic_text carries the founder's original
        #       value-bearing wording alongside the paraphrase, so the
        #       actual name/email/company/etc. survive the rewrite.
        if mentioned and should_browser_automate(heuristic_text):
            try:
                from backend.app.actions.action_registry import get_registry as _get_actions_for_browser
                browser_result = _get_actions_for_browser().call(
                    "action.browser_task", {"url": mentioned[0], "goal": heuristic_text},
                )
                # The hard rule below exists because of a real failure: a
                # specialist, given a terse timeout message, CONFABULATED
                # a story that credentials were needed and asked the
                # founder to hand over a GitHub password to be "stored"
                # — a fabrication that also happened to be dangerous
                # advice. This tool NEVER touches or wants a password/
                # token; it works by opening a real browser the founder
                # logs into themselves. Stated explicitly, twice
                # (browser_task's own return text is equally explicit)
                # because one layer wasn't enough to stop it happening once.
                web_context_parts.append(
                    "REAL BROWSER AUTOMATION RESULT\n"
                    "(You started this action before writing anything. It reflects "
                    "the ACTUAL current state — if it says something is queued for "
                    "approval, it has NOT happened yet. Say so plainly, don't imply "
                    "it's done.)\n\n"
                    "HARD RULE: this tool never uses, needs, or wants a password, "
                    "API key, or access token — it works by opening a real browser "
                    "window that the FOUNDER logs into themselves. If the result "
                    "below mentions a timeout or a login wait, that means the "
                    "browser window opened correctly and was waiting for the founder "
                    "to log in and approve it — NOT that credentials are missing. "
                    "Never ask the founder to provide or store a password/token for "
                    "this. Report only what the result below actually says.\n\n"
                    f"=== action.browser_task(url={mentioned[0]!r}) ===\n{browser_result}\n"
                )
                used_web = True
                logger.info("[%s] browser_task dispatched for %s", self.role, mentioned[0])
            except Exception as exc:  # noqa: BLE001
                logger.warning("[%s] browser_task dispatch failed: %s", self.role, exc)

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
        if _web_search.enabled and should_search(heuristic_text) and self._needs_external_lookup(heuristic_text):
            try:
                # Search the SUBJECT, not the assignment prose — see
                # _search_query_for for what feeding the raw task text
                # to a search engine actually returned.
                search_query = self._search_query_for(task)
                search_results = _web_search.search(search_query, max_results=5)
                if search_results:
                    web_context_parts.append(_web_search.format_for_prompt(search_query, search_results))
                    used_web = True
                    logger.info("[%s] Tavily returned %d result(s) for: %s",
                                self.role, len(search_results), task[:60])
            except Exception as exc:  # noqa: BLE001
                logger.warning("Tavily search failed: %s", exc)

        # (3) Deep-read Tavily results. Competitor/comparison tasks that
        #     didn't already deep-crawl mentioned URLs (no URLs were
        #     given — just company names, e.g. "compare us to Notion
        #     and Linear") get up to 3 distinct-domain search results
        #     deep-crawled instead of a flat 2-page fetch. Everything
        #     else keeps the original flat behavior.
        #
        #     Gated on deep_wanted_for_search (task-only), NOT the wider
        #     deep_wanted — see the comment on deep_wanted_for_search
        #     above. This step launches a NEW search off `task` and
        #     crawls whatever comes back; if `task` alone doesn't look
        #     competitor-shaped, inheriting the trigger from an ancestor
        #     prompt would search+crawl on THIS specialist's unrelated
        #     wording and pull in irrelevant sites.
        # "Handled already" means step 1 actually read pages, not just
        # attempted them — a dead mentioned-URL crawl leaves deep_site_pages
        # as {url: []}, which is falsy-content but truthy-dict, so check
        # the values, not just dict presence.
        deep_already_handled = any(deep_site_pages.values())

        if search_results and deep_wanted_for_search and not deep_already_handled:
            candidate_urls = distinct_domain_urls(
                [r["url"] for r in search_results if r.get("url")]
            )
            if candidate_urls:
                deep_site_pages = _deep_research.research_multiple(candidate_urls)
                block = _deep_research.format_for_prompt(deep_site_pages)
                if block:
                    web_context_parts.append(block)
                    used_web = True
                    logger.info(
                        "[%s] deep-crawled %d competitor site(s) from search",
                        self.role, len(candidate_urls),
                    )
                deep_already_handled = any(deep_site_pages.values())
        elif search_results and not deep_already_handled:
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
        if _reddit.enabled and should_read_reddit(heuristic_text):
            try:
                reddit_block = _reddit.read_for_task(heuristic_text)
                if reddit_block:
                    web_context_parts.append(reddit_block)
                    used_web = True
                    logger.info("[%s] Reddit context injected", self.role)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Reddit read failed: %s", exc)

        # (4) Multi-step tool use — the specialist ACTUALLY DOING WORK.
        #     Every external tool the founder has available (MCP servers,
        #     their custom HTTP tools, built-in actions like send_email /
        #     create_github_repo / write_file) goes into a real
        #     THINK -> ACT -> OBSERVE loop: call one tool, SEE the result,
        #     decide the next call based on it, repeat until done.
        #
        #     This replaced MCPPlanner's single-shot pre-flight, which
        #     picked up to 4 tools BLIND (before seeing any result) and
        #     fired them flat. That could never handle work whose shape
        #     is discovered mid-task — "find 10 investors then draft an
        #     email to each", "check their pricing page and IF there's an
        #     enterprise tier, write the comparison" — or retry a failed
        #     call differently. MCPPlanner is kept in the tree for now
        #     as the documented fallback if the loop ever needs to be
        #     switched off; nothing calls it from here anymore.
        #
        #     The loop only GATHERS and ACTS. The specialist still writes
        #     the deliverable below, and the critique/verification pass
        #     still runs over that shipped text — so the no-fabrication
        #     guarantee is unchanged. Approval gating is unchanged too:
        #     a mutating action enqueues and returns a receipt, which the
        #     loop reads as an observation and moves past. It never waits
        #     for a tap and never fires an unapproved action.
        has_mcp = bool(get_mcp_registry().list_all_tools())
        has_http = bool(get_http_tool_store().enabled())
        # Built-in action tools are always present — so the loop runs
        # even for founders who haven't connected anything themselves.
        from backend.app.actions.action_registry import get_registry as _get_actions
        has_actions = bool(_get_actions().known_names())
        if has_mcp or has_http or has_actions:
            try:
                loop_context = AgenticExecutor(self.pipeline.adapter).run(
                    task=task, role=self.role,
                )
                if loop_context:
                    web_context_parts.append(loop_context)
                    used_web = True
                    logger.info("[%s] multi-step tool work injected", self.role)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Agentic tool loop failed: %s", exc)

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
            # The RAW material this employee retrieved — search results,
            # fetched pages, and the agentic loop's real tool results.
            # Returned so the next specialist can be handed the FACTS,
            # not just this one's prose summary of them.
            #
            # Discarding it was the cause of the worst failure this
            # system has produced: a Literature Reviewer made 18 real
            # Crossref calls and got genuine DOIs, but only its written
            # paragraphs went downstream. The next specialist, with no
            # access to those DOIs, searched for a "papers.txt" that
            # never existed and then emailed a colleague who doesn't
            # exist; the one after that quietly substituted a different
            # topic and wrote five citations from memory.
            "gathered_context": web_context,
        }
        self.memory.record(task, result)
        return result
