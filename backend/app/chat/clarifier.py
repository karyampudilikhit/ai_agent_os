"""Clarifier — asks the founder sharp questions BEFORE running a task.

The problem: the founder asks "design a hackathon sponsor deck" — a
seven-word prompt. Without clarification the CEO/Unit ships a generic
deck with placeholders. With 3-5 targeted questions up front, the same
run ships something actually usable.

CRITICAL: the clarifier is CONTEXT-AWARE. It reads:
  - the active Company's purpose (so it knows what the founder builds)
  - a snapshot of the most recent deliverable (so "this video idea"
    resolves to whatever the CEO just produced instead of getting
    misread as a literal Finance-Free-Friday post for finance pros)
  - prior Q&A already gathered in this session (so audience answered
    two turns ago is still "answered")

Every context-loss bug the founder has ever reported traces back to
one of these three feeds not being wired.

The rules the LLM must follow:
  - MAX 5 questions on the first pass.
  - MAX 7 questions total across the whole conversation.
  - Skip clarification entirely if the context already answers the
    question — default HARD to ready=true.
  - Never re-ask something the context already contains.
  - Questions must be atomic — one thing per question, not "audience
    AND objectives" combined.

Output on every call:
  {
    "ready": bool,               # true when we have enough to run
    "questions": [str, ...]      # empty when ready=true
  }
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

INITIAL_MAX = 5
TOTAL_MAX = 7   # Hard cap — even the LLM can't go past this


INITIAL_PROMPT = """You are a senior consultant about to work on a task for a founder.
Before you touch it, decide whether you need clarification. Great work
starts by asking the FEWEST possible high-leverage questions.

FOUNDER'S TASK (just came in):
"{task}"

CONTEXT YOU ALREADY KNOW (do NOT ask about anything already answered here):
{context_block}

Rules:
- Ask AT MOST {initial_max} questions. Aim for 0-2, not 5.
- STRONGLY prefer ready=true. Silence is better than a survey.
- Do NOT ask about:
  * anything already present in the context block above
  * the founder's product, audience, or company if the Company Context
    section makes it obvious
  * tone/style/format — pick a sensible default and note it in the output
- REFERRING EXPRESSIONS — this is the #1 mistake. When the founder says
  "each persona", "that video idea", "the plan", "this", "it", "them",
  "the ideas you gave" — they are pointing at the Recent Deliverable
  above. READ IT and resolve the reference yourself. NEVER ask "which
  personas?" / "who are the personas?" when a deliverable containing
  personas is shown. NEVER ask "which idea?" when the ideas are listed.
  Asking the founder to re-type something you were just shown is the
  single most damaging thing you can do here.
- Only ask what genuinely changes the deliverable AND is missing from
  the context. Bad question: "What tone?". Good question: a specific
  constraint (deadline, must-include, must-avoid) not already covered.
- Each question is ONE atomic thing — never combine two topics with
  "and". Wrong: "Who's the audience AND what's the objective?".
  Right: two separate questions if both are truly needed.

Return JSON only:
{{"ready": <bool>, "questions": [<string>, ...]}}"""


FOLLOWUP_PROMPT = """You already asked the founder some questions and they've answered.
Decide: do you have enough to do great work, or is there a genuine
information gap that will make the deliverable clearly worse?

ORIGINAL TASK:
"{task}"

CONTEXT YOU ALREADY KNOW (treat as answered — never re-ask):
{context_block}

Q&A THIS CYCLE (in order):
{qa_block}

STRICT RULES — read carefully:
- STRONGLY prefer ready=true. The founder wants work done, not a
  survey. Only ask more if a specific answer is MISSING and its
  absence will visibly hurt the output.
- Do NOT re-ask anything the founder has already addressed, even
  partially or in a prior turn. "No branding guidelines yet" is a
  complete answer to a branding question — do not follow up.
- Do NOT re-ask anything present in the CONTEXT block. If the Company
  Context says "audience: solo founders" and you were about to ask
  audience — don't.
- REFERRING EXPRESSIONS: "each persona", "that idea", "the plan", "it",
  "them" all point at the Recent Deliverable in the context block.
  Resolve them by READING it. Never ask the founder to re-state
  something the deliverable already contains.
- Do NOT ask for "clarifications" of answers that are perfectly
  usable. "$10k Platinum" doesn't need "can you elaborate on the
  Platinum pricing?".
- Do NOT ask for exact dates/formats/style specifics unless the
  original task specifically required them — pick sensible defaults
  instead and note them in the deliverable.
- Each question is ONE atomic thing. Never combine "audience AND
  objective" — if you need both, ask the missing one only.
- Total questions asked SO FAR (before this pass): {asked_so_far}.
  Hard cap: {total_max} total. You have {remaining} left.
- If you do ask more, ask ONE at most. Not two.

Default when in doubt: ready=true, questions=[].

Return JSON only:
{{"ready": <bool>, "questions": [<string>, ...]}}"""


def _build_context_block(context: Optional[Dict[str, Any]]) -> str:
    """Render the founder's known-context into the prompt.

    Format is deliberately dense — the LLM needs to scan it fast and
    treat every line as "already answered, do not ask about this".
    """
    if not context:
        return "(none)"
    lines: List[str] = []
    company = (context.get("company_purpose") or "").strip()
    if company:
        lines.append(f"Company Context: {company[:600]}")
    prior = context.get("prior_qa") or []
    if prior:
        lines.append("")
        lines.append("Prior answers from earlier turns (still valid, do NOT re-ask):")
        for i, pair in enumerate(prior[-8:], 1):
            q = str(pair.get("q") or "").strip()[:200]
            a = str(pair.get("a") or "").strip()[:300]
            if q and a:
                lines.append(f"- Q: {q}")
                lines.append(f"  A: {a}")
    recent = context.get("recent_deliverables") or []
    if recent:
        lines.append("")
        lines.append("Recent deliverables in this session (task + snippet):")
        for r in recent[:2]:
            task = str(r.get("task") or "").strip()[:200]
            snippet = str(r.get("snippet") or "").strip()[:800]
            if task or snippet:
                lines.append(f"- Prior task: {task}")
                if snippet:
                    lines.append(f"  Output snippet: {snippet}")
    last_user = str(context.get("last_user_prompt") or "").strip()[:300]
    if last_user:
        lines.append("")
        lines.append(f"Previous founder message: {last_user}")
    if not lines:
        return "(none)"
    return "\n".join(lines)


class Clarifier:
    def __init__(self, model_adapter: Any):
        self.adapter = model_adapter

    def initial_questions(
        self,
        task: str,
        context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """First pass — see if the task needs clarification at all.
        On any parse failure, default to ready=true so we don't
        dead-end the founder."""
        task = (task or "").strip()
        if not task:
            return {"ready": True, "questions": []}
        context_block = _build_context_block(context)
        try:
            raw = self.adapter.chat_completion(
                INITIAL_PROMPT.format(
                    task=task[:2000],
                    initial_max=INITIAL_MAX,
                    context_block=context_block,
                ),
                temperature=0.2,
                max_tokens=500,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Clarifier initial call failed: %s", exc)
            return {"ready": True, "questions": []}
        parsed = self._parse(raw, cap=INITIAL_MAX)
        # Two-stage scrub: entities the deliverable already resolves,
        # then anything redundant with the wider context.
        parsed["questions"] = _drop_resolved_reference(
            parsed["questions"], task, context,
        )
        parsed["questions"] = _drop_redundant(parsed["questions"], context)
        if not parsed["questions"]:
            parsed["ready"] = True
        return parsed

    def next_step(
        self,
        original_task: str,
        questions_asked: List[str],
        answers: List[str],
        context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Given the running Q&A, decide: more questions, or ready?
        Hard-caps at TOTAL_MAX so we can't spiral."""
        asked = len(questions_asked)
        if asked >= TOTAL_MAX:
            return {"ready": True, "questions": []}
        remaining = TOTAL_MAX - asked
        qa_lines = []
        for i, q in enumerate(questions_asked):
            a = answers[i] if i < len(answers) else "(no answer yet)"
            qa_lines.append(f"Q{i + 1}: {q}\nA{i + 1}: {a}")
        qa_block = "\n\n".join(qa_lines) if qa_lines else "(none)"
        context_block = _build_context_block(context)
        try:
            raw = self.adapter.chat_completion(
                FOLLOWUP_PROMPT.format(
                    task=(original_task or "")[:2000],
                    qa_block=qa_block[:6000],
                    context_block=context_block,
                    asked_so_far=asked,
                    total_max=TOTAL_MAX,
                    remaining=remaining,
                ),
                temperature=0.2,
                max_tokens=400,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Clarifier follow-up call failed: %s", exc)
            return {"ready": True, "questions": []}
        parsed = self._parse(raw, cap=min(1, remaining))
        # Strip questions that duplicate prior Q&A this cycle OR the
        # context block. This is where the LLM most often stumbles.
        prior_texts = list(questions_asked) + list(answers)
        parsed["questions"] = _drop_resolved_reference(
            parsed["questions"], original_task, context,
        )
        parsed["questions"] = _drop_redundant(
            parsed["questions"], context, extra_texts=prior_texts,
        )
        # Hard-enforce the total cap regardless of what the LLM returned
        if asked + len(parsed["questions"]) > TOTAL_MAX:
            parsed["questions"] = parsed["questions"][: TOTAL_MAX - asked]
        if not parsed["questions"]:
            parsed["ready"] = True
        return parsed

    # ----------------------------------------------------------------

    def _parse(self, raw: str, cap: int) -> Dict[str, Any]:
        data = _extract_json(raw) or {}
        ready = bool(data.get("ready"))
        qs_raw = data.get("questions") or []
        qs: List[str] = []
        if isinstance(qs_raw, list):
            for q in qs_raw:
                s = str(q or "").strip()
                if s:
                    qs.extend(_split_multi_part(s))
        # Dedupe while preserving order
        seen = set()
        deduped: List[str] = []
        for q in qs:
            key = _normalize(q)
            if key and key not in seen:
                seen.add(key)
                deduped.append(q)
        qs = deduped[:cap]
        # If ready was not set explicitly and there are no questions,
        # treat as ready. If questions came back, ready must be false.
        if qs:
            ready = False
        elif not ready:
            ready = True
        return {"ready": ready, "questions": qs}


def format_questions_for_chat(questions: List[str], first_pass: bool = True) -> str:
    """The reply the founder sees in the chat when we're gathering
    context. Numbered list; brief lead-in so it reads like a person,
    not a form."""
    if not questions:
        return ""
    lead = (
        "A few quick things before I get to work — the tighter your "
        "answers, the sharper the output:"
        if first_pass
        else "Quick follow-up so I nail this:"
    )
    body = "\n".join(f"{i + 1}. {q}" for i, q in enumerate(questions))
    tail = (
        "\n\nJust type your answers in one message. If any question "
        "doesn't apply, say so and I'll skip it."
    )
    return f"{lead}\n\n{body}{tail}"


def enrich_task_with_qa(
    original_task: str,
    questions: List[str],
    answers: List[str],
) -> str:
    """Fold the Q&A back into the task so the run pipeline sees it as
    part of the brief instead of trying to invent the missing details."""
    if not questions:
        return original_task
    lines = ["FOUNDER'S BRIEF FROM CLARIFICATION:"]
    for i, q in enumerate(questions):
        a = answers[i] if i < len(answers) else ""
        if a:
            lines.append(f"- Q: {q}\n  A: {a}")
    if len(lines) == 1:  # nothing but the header
        return original_task
    context = "\n".join(lines)
    return f"{original_task}\n\n{context}"


# ----------------------------------------------------------------
# Question hygiene: split multi-part, drop redundant, normalize
# ----------------------------------------------------------------

# Split ONLY when 'and' is followed by a fresh interrogative
# (what/who/where/when/why/how/which/do/does/is/are). That way
# "roles, and industries" (industries is a noun) stays intact, but
# "industries, and what specific insights..." splits.
_INTERROGATIVES = "what|who|where|when|why|how|which|do|does|is|are"
_MULTI_CONNECTOR_RE = re.compile(
    rf",?\s+and\s+(?={_INTERROGATIVES})\b",
    re.IGNORECASE,
)
_COMBINED_TAIL_RE = re.compile(
    rf",?\s+and\s+(?:{_INTERROGATIVES})\b",
    re.IGNORECASE,
)


def _split_multi_part(q: str) -> List[str]:
    """If a question crams two topics with 'and' + a new interrogative
    (e.g. 'who is the audience AND what is the objective?'), split.
    We're conservative — only split when the second half starts with a
    new question word, so we don't break legitimate compound noun
    phrases like 'ai employees and their ability' or parenthetical
    lists like 'roles, and industries'."""
    q = q.strip()
    if not q:
        return []
    parts = _MULTI_CONNECTOR_RE.split(q)
    if len(parts) <= 1:
        return [q]
    out: List[str] = []
    for p in parts:
        p = p.strip().strip(",;")
        if not p:
            continue
        if not p.endswith("?"):
            p = p + "?"
        p = p[0].upper() + p[1:] if p else p
        out.append(p)
    return out or [q]


def _looks_combined(q: str) -> bool:
    """Best-effort detector for 'A and what B?'-style questions the
    splitter failed to break. Used as a last-ditch guard so the founder
    never sees a bundled multi-part question."""
    return bool(_COMBINED_TAIL_RE.search(q or ""))


_STOP_WORDS = {
    "the", "a", "an", "is", "are", "do", "does", "of", "for", "to", "in",
    "on", "with", "your", "you", "and", "or", "any", "as", "at", "be",
    "by", "it", "this", "that", "what", "who", "which", "how", "will",
    "would", "should", "could", "have", "has", "had", "specific", "must",
    "include", "primary", "main", "key", "want", "need", "please",
}


def _stem(w: str) -> str:
    """Crude plural stripper so 'persona'/'personas' and
    'audience'/'audiences' compare equal. Deliberately dumb — we only
    need consistency, not linguistic correctness."""
    if len(w) > 3 and w.endswith("ies"):
        return w[:-3] + "y"
    if len(w) > 4 and w.endswith("ses"):
        return w[:-2]
    if len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
        return w[:-1]
    return w


def _normalize(text: str) -> str:
    """Lowercase, strip punctuation, drop stop words, stem plurals —
    used for the redundancy check. We're aiming for 'audience' and
    'target audience' and 'primary linkedin audience' to normalize to
    overlapping token sets."""
    if not text:
        return ""
    t = re.sub(r"[^a-zA-Z0-9\s]", " ", text.lower())
    toks = [
        _stem(w)
        for w in t.split()
        if w and w not in _STOP_WORDS and len(w) > 2
    ]
    return " ".join(sorted(toks))


# Questions that ask the founder to IDENTIFY or ENUMERATE something.
# "Who are the personas?" / "Which idea?" / "List the segments".
_IDENTIFY_RE = re.compile(
    r"\b(who\s+(are|is)|what\s+(are|is)\s+the|which\b|specify\b|"
    r"list\s+the\b|name\s+the\b|identify\b)",
    re.IGNORECASE,
)


def _drop_resolved_reference(
    questions: List[str],
    task: str,
    context: Optional[Dict[str, Any]],
) -> List[str]:
    """Kill questions that ask the founder to re-specify an entity the
    prior deliverable already defines.

    This catches what token-overlap cannot. When the founder says
    "give me a message for each persona" right after a run that
    produced personas, the clarifier's "Who are the specific personas
    you want messages for?" is ~80% NEW words (job, titles,
    demographics) — overlap scoring rates it as a fresh question. But
    it is the single most infuriating thing the product can do.

    Rule: if a content word appears in BOTH the founder's task and the
    recent deliverable, that word is a RESOLVED REFERENCE. Any
    identification-shaped question about it gets dropped — the answer
    is in the deliverable, go read it.
    """
    if not questions or not context:
        return questions
    deliv_tokens: set = set()
    for r in context.get("recent_deliverables") or []:
        deliv_tokens |= _tokens(r.get("snippet") or "")
        deliv_tokens |= _tokens(r.get("task") or "")
    if not deliv_tokens:
        return questions
    resolved = _tokens(task) & deliv_tokens
    if not resolved:
        return questions
    kept: List[str] = []
    for q in questions:
        if (_tokens(q) & resolved) and _IDENTIFY_RE.search(q):
            logger.info(
                "Clarifier drop resolved-reference question %r (entities=%s)",
                q, sorted(_tokens(q) & resolved),
            )
            continue
        kept.append(q)
    return kept


def _tokens(text: str) -> set:
    return set(_normalize(text).split())


def _drop_redundant(
    questions: List[str],
    context: Optional[Dict[str, Any]],
    extra_texts: Optional[List[str]] = None,
) -> List[str]:
    """Drop any question whose informational content is already covered.

    Two checks, either one triggers a drop:
      (a) Pool check — >=60% of the question's content tokens appear
          somewhere in the assembled context+extras token bag. Catches
          broad "already-covered" cases.
      (b) Per-source check — >=50% overlap with any SINGLE prior question
          or founder answer. Catches literal rephrases that a big pool
          might dilute below the pool threshold.
    Combined bundled questions ('A and what B?') that slipped past the
    splitter are also dropped as a last-ditch UX guard.
    """
    if not questions:
        return []
    # Two source tiers:
    #   short_sources -> feed BOTH the pool check and the stricter
    #     per-source check. These are targeted texts (a prior question,
    #     a founder answer) where high overlap really does mean "already
    #     asked / already answered".
    #   long_sources -> feed the pool check ONLY. A 2500-char
    #     deliverable shares incidental vocabulary with almost any
    #     question; running the 0.5 per-source test against it would
    #     start eating legitimately-new questions.
    short_sources: List[str] = []
    long_sources: List[str] = []
    if context:
        for key in ("company_purpose", "last_user_prompt"):
            v = context.get(key)
            if v:
                short_sources.append(str(v))
        for pair in context.get("prior_qa") or []:
            if pair.get("q"):
                short_sources.append(str(pair["q"]))
            if pair.get("a"):
                short_sources.append(str(pair["a"]))
        for r in context.get("recent_deliverables") or []:
            if r.get("task"):
                short_sources.append(str(r["task"]))
            if r.get("snippet"):
                long_sources.append(str(r["snippet"]))
    if extra_texts:
        short_sources.extend(extra_texts)

    pool_tokens = set()
    per_source_tokens: List[set] = []
    for s in short_sources:
        toks = _tokens(s)
        if toks:
            pool_tokens |= toks
            per_source_tokens.append(toks)
    for s in long_sources:
        pool_tokens |= _tokens(s)

    kept: List[str] = []
    for q in questions:
        # Last-ditch: never let a combined multi-part question through.
        if _looks_combined(q):
            logger.info("Clarifier drop combined multi-part question: %r", q)
            continue
        q_toks = _tokens(q)
        if not q_toks:
            continue
        drop = False
        if pool_tokens:
            pool_overlap = len(q_toks & pool_tokens) / len(q_toks)
            if pool_overlap >= 0.6:
                logger.info(
                    "Clarifier drop (pool overlap %.2f): %r", pool_overlap, q,
                )
                drop = True
        if not drop:
            for src_toks in per_source_tokens:
                if not src_toks:
                    continue
                per_overlap = len(q_toks & src_toks) / len(q_toks)
                if per_overlap >= 0.5:
                    logger.info(
                        "Clarifier drop (source overlap %.2f): %r", per_overlap, q,
                    )
                    drop = True
                    break
        if not drop:
            kept.append(q)
    return kept


_JSON_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


def _extract_json(text: str) -> Optional[Dict[str, Any]]:
    if not text:
        return None
    stripped = text.strip()
    stripped = _JSON_FENCE_RE.sub("", stripped).strip()
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
