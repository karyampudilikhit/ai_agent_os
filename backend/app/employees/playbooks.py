"""Playbook library — the "how to do this well" wisdom that closes
the quality gap between weaker local models (gpt-oss:120b) and
premium hosted models (Sonnet, GPT-4).

Behind the architecture: premium models produce better output on
research/validation tasks partly because they've internalized rules
like "mark unknown when there's no primary source," "triangulate
stats across two sources," "quote pricing pages verbatim." Weaker
models CAN follow those rules mechanically — they just need to be
told explicitly.

The Supervisor picks the right playbook per task and composes a
task-specific brief for each specialist that injects the relevant
rules. This is what turns "an AI team demo" into "the reason lower-
model users get premium-model output."

Task types (v1 set — expand as we see new failure patterns):
  - research        : any task that needs external facts/sources
  - validation      : research + a decision/verdict (Go/No-Go)
  - writing         : creative production (copy, emails, posts)
  - analysis        : data-driven analysis without external sources
  - strategy        : planning, prioritization, roadmap
  - general         : fallback when nothing matches cleanly

The eventual self-improvement loop (deferred to v1) will mutate these
playbooks in place — every recurring critique failure becomes a new
rule appended to the relevant playbook.
"""

from __future__ import annotations

import re
from typing import Dict, List


# ---- Playbook definitions ------------------------------------------

# Anti-hallucination / verification rules that apply to any task
# needing external facts. This is where the "mark unknown, cite URLs,
# triangulate" wisdom lives.
_RESEARCH_RULES: List[str] = [
    "For every specific number you cite: quote the URL that contains "
    "that exact number. If you cannot find such a URL, write 'unknown' "
    "instead of the number. NEVER estimate a number that is not in a "
    "source you are citing.",

    "If a statistic appears in only one source, mark it 'single-source, "
    "directional'. Real research triangulates — one obscure blog is not "
    "the same as two independent primary sources.",

    "If you searched for a fact and found no primary source, explicitly "
    "write 'no primary source found — unknown'. Proof of absence is more "
    "valuable than a plausible-sounding estimate.",

    "Prefer primary sources: research firms (Gartner, Statista, IDC, "
    "Grand View, Forrester), official pricing pages, company press "
    "releases, government data. Marketing blogs and content-farm "
    "aggregators are LOW-confidence — flag them as such if you must cite "
    "them.",

    "For competitor pricing: fetch the actual pricing page URL and quote "
    "the tier structure VERBATIM. Do not paraphrase pricing — small "
    "wording changes drift into fabrication. If the exact price is not "
    "visible on the URL you cite, write 'pricing not publicly listed'.",

    "For every URL you cite, prefer specific pages over homepages. "
    "'productboard.com/pricing' is a real citation. Bare 'productboard.com' "
    "is a guess that the pricing page exists.",
]

# Validation adds: a decision, adversarial thinking, and the "one reason
# it fails" pass that stops overconfident Go verdicts.
_VALIDATION_RULES: List[str] = _RESEARCH_RULES + [
    "Before your final verdict, write ONE paragraph attacking the idea. "
    "What is the strongest argument for NO? How could an incumbent kill "
    "this in a week if they wanted to? What's the actual moat? A "
    "validator that only ever says Go is a validator you cannot trust.",

    "Your verdict must be one of: Go, No-Go, or Conditional Go. Name the "
    "single specific reason for that verdict — not three generic ones. "
    "'Go — the market is large' is filler. 'Conditional Go — the gap "
    "is real but Savio could ship this feature in 2 weeks' is real.",

    "If no primary source exists for the specific niche (very common "
    "for new sub-niches): say so explicitly, and recommend cheap "
    "validation signal (landing page, 10-15 customer interviews) rather "
    "than an estimated TAM slide.",
]

_WRITING_RULES: List[str] = [
    "Every claim about the audience: back it with a quote from that "
    "audience, sourced (a real review, tweet, forum post). If you cannot "
    "quote them, say so — do not invent voices.",

    "Quote successful competitor examples VERBATIM with the source URL. "
    "Paraphrased 'inspiration' loses the exact wording that made the "
    "original work.",

    "Active voice. No weasel words (may, might, could, potentially, "
    "sometimes) unless you're describing genuine uncertainty.",

    "Concrete over abstract: 'reduces churn by 12% within 30 days' beats "
    "'improves retention significantly'. If you don't have the specific "
    "number, do not fabricate one — pick a different framing.",
]

_ANALYSIS_RULES: List[str] = [
    "Define every term you use non-trivially. 'MRR' means one thing; "
    "'MRR excluding annual contracts' means another — be specific.",

    "Show your work: if you claim a trend, quote the data points behind "
    "it. Do not summarize a chart you cannot describe row-by-row.",

    "Name the counterfactual: for any conclusion, state what evidence "
    "would change your mind. If nothing would, you are asserting a "
    "belief, not analyzing data.",

    "Distinguish correlation from causation explicitly when both are "
    "plausible.",
]

_STRATEGY_RULES: List[str] = [
    "For every recommendation: name the one specific reason it's better "
    "than the obvious alternative. If you can't name the alternative, "
    "you haven't thought about it hard enough.",

    "Every recommendation must have a kill criterion: 'we stop doing "
    "this if X happens by Y date'. A strategy without a kill criterion "
    "is optimism.",

    "Sequence explicitly: what happens first, what waits. 'In parallel' "
    "is often code for 'I haven't decided the order'.",

    "Name what you're deliberately NOT doing. Non-goals matter as much "
    "as goals — otherwise scope creeps every review.",
]

_GENERAL_RULES: List[str] = [
    "Be specific over general. Numbers, names, dates, URLs beat "
    "adjectives every time.",

    "If you don't know something, say 'unknown' rather than guessing. "
    "Confident wrong answers are the failure mode we prevent.",

    "Cite sources for any factual claim about the outside world.",
]


# Prepended to EVERY playbook, whatever the task type. These encode the
# product's core promise — the AI does the work — and exist because it
# was violated in real use: asked to create a GitHub repo, the system
# failed to do it and then produced a tidy numbered guide telling the
# founder to "Open github.com/new, enter the name, click Create
# repository", complete with links to GitHub's docs. A how-to guide is
# the single worst possible output here: if the founder has to do the
# steps, the product has no reason to exist. Failing honestly is fine.
# Handing the work back is not.
#
# The last three rules were added after a SECOND, subtler version of the
# same violation. Asked for "a report of stocks up more than 30%", a
# nine-employee run spent twelve minutes reading files that never
# existed (staged_raw_data.csv, transformed_dataset.csv), emailed
# invented addresses (founder@example.com) asking the founder to send
# the data, wrote out an empty CSV with headers and no rows, and
# delivered a final answer of "we cannot generate the report until
# clean_prices.csv is uploaded to the shared workspace." No rule above
# forbade any of that — they ban how-to guides and credential requests,
# not "please upload the data." Meanwhile a working browser tool sat
# unused in the same tool list. Asking the founder for data is the same
# failure as a tutorial, just better disguised.
_UNIVERSAL_RULES: List[str] = [
    "NEVER write instructions telling the founder how to do the task "
    "themselves. No numbered how-to steps, no 'go to X and click Y', no "
    "links to a provider's documentation as a substitute for doing the "
    "work. You are the one doing it — not a manual.",

    "If you genuinely could not complete the work, say so in one line "
    "and name the SINGLE specific thing that would unblock you (e.g. "
    "'GitHub isn't connected yet'). Then stop. Do not pad a failure "
    "with a tutorial, a checklist, or background research.",

    "Never ask the founder for a password, API key, or access token. "
    "Logins happen in a real browser they control, and integrations are "
    "connected via an approval flow — credentials never come through "
    "chat. If auth is missing, name the connection that's needed.",

    "Report state accurately. An action that is queued for approval has "
    "NOT happened yet — say that plainly. Never imply completed work "
    "that is still pending, and never claim a result you did not get "
    "back from a real tool call.",

    "NEVER ask the founder to supply, upload, send, or paste DATA — no "
    "'upload prices.csv to the shared workspace', no 'provide the "
    "dataset', no 'notify us once the file is ready'. If you need data, "
    "GO GET IT: browse to a real source and read it, call an API, run a "
    "search. 'Please send me the data' is the same failure as a how-to "
    "guide — it makes the founder do the work.",

    "Never email, Slack, or otherwise message the founder to request "
    "information or a file. You are already talking to them — a message "
    "asking them to do something is not work, it is the absence of work. "
    "Addresses like founder@example.com are invented and go nowhere.",

    "Do not assume a file or dataset exists because some other employee "
    "was supposed to produce it. If a read fails, that is your cue to "
    "fetch the underlying data yourself from a real source — not to "
    "escalate, wait, or hand the gap back to the founder. Never emit an "
    "empty template or placeholder file as if it were a deliverable.",

    "Only report values you actually READ. Never reconstruct, normalise "
    "or guess an identifier — a ticker symbol, product code, ID, exact "
    "date — from a name or from context. If the source shows a company "
    "name but no symbol, give the name and leave the symbol out. "
    "Inventing a plausible-looking identifier while calling your method "
    "'verbatim' is fabrication, even when the surrounding numbers are "
    "right.",

    "Answer the question you were ACTUALLY asked. Do not silently narrow "
    "the scope — to one country, one exchange, one time window — because "
    "that is what the first source you found happened to cover. If you "
    "had to narrow it, say so in one line at the top, in the founder's "
    "terms, and say what is missing.",

    "Never claim something does not exist because you failed to find it. "
    "'No such data/list/source is available' is a strong factual claim "
    "and it is usually wrong — say 'I could not retrieve X from Y' and "
    "name what you actually tried. A page that was blocked, slow, or "
    "unparseable is a tool failure, not proof of absence.",

    "NEVER cite a URL you did not actually open in this task. Do not "
    "attach a source to something you already knew, and do not reach for "
    "a plausible-looking link (a Wikipedia page, a company's About page) "
    "to dress up an answer from memory — that is worse than no citation, "
    "because a citation buys trust. Every URL you print is now checked "
    "against what this system really retrieved, and invented ones are "
    "flagged to the founder. If a fact came from your own knowledge, say "
    "so plainly and leave it uncited.",

    "Deliver the whole result, not a sample. If the source has 58 rows "
    "and the founder asked for the list, give 58 — or state the real "
    "reason you have fewer (a page limit you hit, a truncated read), "
    "never 'for brevity'. And if producing a file would serve them "
    "better, WRITE it with your tools; do not offer to produce it later.",
]


PLAYBOOKS: Dict[str, Dict[str, List[str]]] = {
    "research":   {"rules": _RESEARCH_RULES},
    "validation": {"rules": _VALIDATION_RULES},
    "writing":    {"rules": _WRITING_RULES},
    "analysis":   {"rules": _ANALYSIS_RULES},
    "strategy":   {"rules": _STRATEGY_RULES},
    "general":    {"rules": _GENERAL_RULES},
}


# ---- Task-type classification (heuristic + LLM-optional) -----------

# Heuristic keyword sets. Intentionally over-triggering — the cost of a
# false positive is a slightly-off playbook, the cost of a false
# negative is a bare `general` playbook and worse output.
_VALIDATION_WORDS = (
    "validate", "validation", "should i build", "should we build",
    "go / no go", "go / no-go", "go/no-go", "go or no",
    "worth building", "is this idea", "verdict",
    "should i pursue", "should i launch",
)
_RESEARCH_WORDS = (
    "research", "market size", "competitor", "competitors",
    "pricing", "landscape", "who is doing", "what tools",
    "analyze the market", "find sources", "sources for",
    "who are the players", "what's out there",
)
_WRITING_WORDS = (
    "write", "draft", "copy", "email", "post", "blog", "tweet",
    "landing page", "headline", "subject line", "outreach",
    "cold email", "product hunt", "linkedin post",
)
_ANALYSIS_WORDS = (
    "analyze", "analysis of", "trend in", "pattern in",
    "cohort", "funnel", "retention", "churn analysis",
    "look at these numbers", "diagnose", "root cause",
)
_STRATEGY_WORDS = (
    "strategy", "roadmap", "plan for", "prioritize",
    "which should we", "next quarter", "go-to-market",
    "gtm", "launch plan", "positioning",
)


def classify_task_type(task: str) -> str:
    """Heuristic classifier — deterministic, keyword-based, fast.
    Returns one of the PLAYBOOKS keys.

    Order matters: validation is checked before research (a validation
    task contains research vocabulary but wants stronger rules than
    plain research). Same for analysis vs strategy.

    Falls back to 'general' when nothing matches — better a plain
    playbook than a wrongly-loaded specialized one.
    """
    if not task:
        return "general"
    lowered = task.lower()
    if any(w in lowered for w in _VALIDATION_WORDS):
        return "validation"
    if any(w in lowered for w in _RESEARCH_WORDS):
        return "research"
    if any(w in lowered for w in _WRITING_WORDS):
        return "writing"
    if any(w in lowered for w in _ANALYSIS_WORDS):
        return "analysis"
    if any(w in lowered for w in _STRATEGY_WORDS):
        return "strategy"
    return "general"


# ---- Playbook access -----------------------------------------------

def get_playbook(task_type: str) -> Dict[str, List[str]]:
    """Return the playbook for a task type, falling back to `general`."""
    return PLAYBOOKS.get(task_type) or PLAYBOOKS["general"]


def format_rules_for_prompt(task_type: str) -> str:
    """Render a playbook's rules as a numbered list ready to inject into
    a Supervisor prompt or a specialist's task brief.

    _UNIVERSAL_RULES come FIRST, ahead of the task-type rules, and apply
    to every task type — they're the "actually do the work, report
    honestly" floor that must hold whether this is research, writing, or
    anything else. Putting them first also means they survive any
    downstream truncation of a long brief.
    """
    playbook = get_playbook(task_type)
    rules = _UNIVERSAL_RULES + list(playbook.get("rules") or [])
    if not rules:
        return ""
    return "\n".join(f"{i}. {r}" for i, r in enumerate(rules, 1))
