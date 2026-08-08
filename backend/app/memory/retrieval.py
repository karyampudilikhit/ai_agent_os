"""Lexical relevance ranking — the difference between "the last two
runs" and "the run that was actually about this".

Recall in this codebase has been recency-only everywhere: RunStore.
list_recent_done takes the newest N, EmployeeMemoryStore.relevant_context
takes the last 3 entries and admits in its own docstring that there is no
similarity search. That is fine for "the report you just wrote" and
useless for "what did you find on Notion's pricing" once three unrelated
runs have happened since. The founder hitting that looks identical to the
amnesia bug RunStore persistence was meant to fix.

WHY NOT EMBEDDINGS. config.yaml anticipates a vector store (Chroma/
FAISS/Pinecone) and EmbeddingStoreConfig defaults to enabled: False. For
this corpus that would be the wrong trade:

  - It is ~200 documents, capped by MAX_PERSISTED_RUNS. BM25 and a
    vector index are indistinguishable in quality at that size; the
    queries here are keyword-shaped ("Notion pricing", "quant papers"),
    which is precisely where lexical matching is strongest.
  - Every embedding backend puts a NETWORK CALL on the recall path. The
    whole point of the audit this work belongs to was that the system
    lies when a dependency fails. A recall path that can time out is a
    new way to produce a confident wrong answer, in exchange for
    nothing measurable at this scale.

So this is dependency-free and synchronous, and it cannot fail. The
ranking interface (`rank`) is the seam an embedding backend would slot
into later, once the corpus is large enough for the trade to flip.

SCORING is Okapi BM25, with one addition: the raw BM25 score is
normalized into a 0..1 "coverage" figure — what fraction of the query's
total information content is strongly present in this document. Raw BM25
scores are unbounded and corpus-dependent, so thresholding them directly
means retuning the constant every time the corpus grows. Coverage is
stable, which lets MIN_RELEVANCE below be a real, defensible cutoff.

That cutoff matters more than the ranking does. Surfacing an unrelated
past deliverable is worse than surfacing nothing: it invites an employee
to answer the founder's question out of the wrong prior work, which is
the fabrication failure mode wearing yet another hat. When in doubt this
module returns nothing.
"""

from __future__ import annotations

import math
import re
from typing import Dict, Iterable, List, Sequence, Tuple

# Standard BM25 constants. k1 controls how fast term-frequency saturates
# (a word appearing 10x is not 10x more relevant); b controls how hard
# long documents are penalised. These are the textbook defaults and
# there is no tuning corpus here to justify moving them.
K1 = 1.5
B = 0.75

# Minimum coverage for a document to be considered a match at all.
#
# Coverage is measured against the query's DISCRIMINATING terms only —
# the ones the corpus could actually answer on (see the prune in
# `rank`), not every word the founder typed.
#
# Calibration: a document of average length containing such a term once
# scores s = (k1+1)/(1 + k1) = 1.0 against a per-term ceiling of k1+1 =
# 2.5, so matching EVERY discriminating term once gives coverage 0.4,
# and matching one in three lands near 0.13. 0.15 sits just above that.
# Verified from both directions in test_memory_recall.py: a genuine
# topic match clears it, and a run sharing only incidental vocabulary
# does not.
MIN_RELEVANCE = 0.15

# A query term appearing in more than this fraction of the corpus is
# treated as non-discriminating (see the prune in `rank`). Only applied
# once the corpus is big enough for the fraction to mean anything —
# below MIN_DOCS_FOR_COMMON_PRUNE, "half the documents" can be one
# document, and pruning on that would throw away real signal.
MAX_DOC_FRACTION = 0.5
MIN_DOCS_FOR_COMMON_PRUNE = 4

# A document must match at least this many distinct query terms to be
# recalled at all, no matter how well it scores.
#
# This exists because coverage alone cannot tell a strong single-term
# match from a meaningless one. Probed against 16 real runs, coverage
# happily returned the trading-papers run for "book me a table at a
# restaurant in Lisbon" (0.32, matching only 'table') and a cold-email
# run for "write me a poem about the sea" (0.55, matching only
# 'write'). In both, every distinctive word — restaurant, Lisbon, poem,
# sea — was absent from the corpus, so the ceiling collapsed onto one
# generic word and any document containing it scored well.
#
# The honest reading is that one shared word is not evidence of
# aboutness. Requiring two means a founder referring back to past work
# has to name it ("add Asana to that NOTION PRICING table"), which is
# how people actually refer to things; a bare "add it to that table"
# recalls nothing. That is the intended trade — silence costs a re-ask,
# a wrong match costs a confidently wrong deliverable.
MIN_MATCHED_TERMS = 2

# Function words carry no topical signal but do drag coverage upward if
# left in, because they match almost everything. Deliberately limited to
# true function words: content-ish verbs common in prompts ("build",
# "find", "report") are left alone, because IDF already discounts any
# term that turns out to be corpus-common, and doing that from the data
# beats guessing from a hand-written list.
_STOPWORDS = frozenset("""
a an the and or but if then than that this these those of in on at to for
with from by as is are was were be been being do does did doing have has
had having i me my we our you your it its he she they them their there
here what which who whom when where why how all any both each few more
most other some such no nor not only own same so too very can will just
about into over under again further once
""".split())

# Request scaffolding: the words a founder wraps around a subject rather
# than the subject itself. Distinct from _STOPWORDS above — these are
# real words, they are just never what a request is ABOUT.
#
# This list is not a guess. Probing real history, "add two more papers to
# that quant research list" matched a cold-email run on exactly ['add',
# 'two'] — two pieces of scaffolding, enough to clear MIN_MATCHED_TERMS
# and surface an unrelated deliverable. Corpus statistics could not
# rescue this: 'add' and 'two' appear in far fewer than half the runs,
# so the common-term prune correctly leaves them alone. Their problem is
# semantic, not distributional, which is the one thing IDF cannot see.
#
# Kept deliberately narrow — quantifiers, ordinals, and the handful of
# verbs that only ever introduce a request. Topical words stay out of
# here even when they feel generic ('report', 'email', 'papers'),
# because those genuinely are what some requests are about.
_SCAFFOLD = frozenset("""
add another also please kindly give show tell want need let make
one two three four five six seven eight nine ten
first second third fourth fifth next last previous
""".split())

_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9'\-\.]*", re.IGNORECASE)

# Below this length a token is almost always noise ("a", "of", a stray
# list bullet). Two-character tokens are kept when numeric so figures
# like "4o" or "22" survive.
_MIN_TOKEN_LEN = 3


def tokenize(text: str) -> List[str]:
    """Lowercase content tokens, stopwords removed.

    Internal punctuation is preserved on purpose — a DOI fragment
    ("10.1016"), a version ("gpt-4o") and a domain ("notion.so") are all
    high-signal identifiers that splitting on '.' or '-' would shred into
    useless pieces.
    """
    out: List[str] = []
    for raw in _TOKEN_RE.findall(text or ""):
        tok = raw.lower().strip(".-'")
        if not tok or tok in _STOPWORDS or tok in _SCAFFOLD:
            continue
        if len(tok) < _MIN_TOKEN_LEN and not tok.isdigit():
            continue
        out.append(tok)
    return out


def _idf(n_docs: int, doc_freq: int) -> float:
    """BM25+ style IDF. The +1 inside the log keeps this strictly
    positive, unlike the classic Robertson form which goes negative for
    terms appearing in more than half the corpus — with a corpus this
    small, a negative weight would let a common word actively push a
    genuinely relevant document below the threshold."""
    return math.log(1.0 + (n_docs - doc_freq + 0.5) / (doc_freq + 0.5))


def rank(
    query: str,
    documents: Sequence[Tuple[str, str]],
    *,
    limit: int = 3,
    min_relevance: float = MIN_RELEVANCE,
) -> List[Tuple[str, float]]:
    """Rank `documents` — (doc_id, text) pairs — against `query`.

    Returns (doc_id, coverage) for matches at or above `min_relevance`,
    best first, at most `limit` of them. An empty list is a normal and
    frequent result: most tasks have no relevant history, and saying so
    is the correct answer.
    """
    q_terms = tokenize(query)
    if not q_terms or not documents:
        return []

    tokenized: List[Tuple[str, List[str]]] = [
        (doc_id, tokenize(text)) for doc_id, text in documents
    ]
    tokenized = [(d, t) for d, t in tokenized if t]
    if not tokenized:
        return []

    n_docs = len(tokenized)
    avg_len = sum(len(t) for _, t in tokenized) / n_docs

    # De-duplicate query terms: repeating a word in the query should not
    # let it count twice toward the ceiling.
    q_unique = list(dict.fromkeys(q_terms))

    doc_freq: Dict[str, int] = {}
    for _, toks in tokenized:
        present = set(toks)
        for term in q_unique:
            if term in present:
                doc_freq[term] = doc_freq.get(term, 0) + 1

    # Only DISCRIMINATING terms count. Two prunes, and both matter:
    #
    #  - df == 0: the term appears nowhere in the corpus. It cannot
    #    distinguish one document from another, but it carries maximum
    #    IDF, so leaving it in inflates the ceiling for every candidate
    #    equally. Concretely: "add Asana to that Notion pricing
    #    comparison" against a corpus with no Asana in it scored the
    #    correct document at 0.094 — three unmatched rare words
    #    ('asana', 'comparison', 'add') dominated the denominator and
    #    pushed a genuine match below the threshold. Recall of anything
    #    phrased as an EXTENSION of past work ("add X to that Y") was
    #    broken, which is the single most common way a founder refers
    #    back to a deliverable.
    #
    #  - df > MAX_DOC_FRACTION: the term appears in most of the corpus,
    #    so matching it means almost nothing. Without this, a query
    #    whose only corpus-present word is something like "report"
    #    would match everything at high coverage.
    #
    # What survives is "the part of the query this corpus could
    # actually answer", and coverage is measured against that.
    knowable = [t for t in q_unique if doc_freq.get(t, 0) > 0]
    if n_docs >= MIN_DOCS_FOR_COMMON_PRUNE:
        knowable = [
            t for t in knowable if doc_freq[t] / n_docs <= MAX_DOC_FRACTION
        ]
    if not knowable:
        return []

    idf: Dict[str, float] = {
        term: _idf(n_docs, doc_freq[term]) for term in knowable
    }
    # Ceiling: every discriminating term maximally saturated. Used to
    # normalize the raw score into the 0..1 coverage figure that
    # MIN_RELEVANCE assumes.
    ceiling = sum(idf.values()) * (K1 + 1.0)
    if ceiling <= 0:
        return []

    scored: List[Tuple[str, float]] = []
    for doc_id, toks in tokenized:
        counts: Dict[str, int] = {}
        for t in toks:
            counts[t] = counts.get(t, 0) + 1
        norm = K1 * (1.0 - B + B * (len(toks) / avg_len))
        score = 0.0
        matched = 0
        for term in knowable:
            f = counts.get(term, 0)
            if not f:
                continue
            matched += 1
            score += idf[term] * (f * (K1 + 1.0)) / (f + norm)
        if matched < MIN_MATCHED_TERMS:
            continue
        coverage = score / ceiling
        if coverage >= min_relevance:
            scored.append((doc_id, coverage))

    scored.sort(key=lambda pair: pair[1], reverse=True)
    return scored[:limit]


def best_match(
    query: str,
    documents: Sequence[Tuple[str, str]],
    *,
    min_relevance: float = MIN_RELEVANCE,
) -> Tuple[str, float] | None:
    """Single best match, or None. Convenience over `rank`."""
    hits = rank(query, documents, limit=1, min_relevance=min_relevance)
    return hits[0] if hits else None


def keyword_overlap(query: str, text: str) -> Iterable[str]:
    """Query terms that genuinely appear in `text`. Used for logging so a
    recall decision can be explained after the fact rather than guessed
    at from a bare score."""
    present = set(tokenize(text))
    return [t for t in dict.fromkeys(tokenize(query)) if t in present]
