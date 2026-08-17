"""RunArtifacts — the register of what a run actually produced.

THE FAILURE THIS CLOSES, from the 2026-08-15 incident report. Specialists
on the same run could not hand files to each other, so they guessed:

    data.csv          chosen_etf.txt        market_analysis.json
    clean_data/SPY_5y_1d_clean.csv          reports/SPY_SMA_Backtest_Report.pdf

Two of those were reported to the founder as finished work. Neither had
ever been written. And in one run a specialist that could not find its
input emailed `dataengineer@example.com` -- asking a colleague who does
not exist to send a file that was never made. That is not a hallucination
in the usual sense; it is a correct inference from the role fiction the
system gave it, with no mechanism behind it.

The mechanism is this: producers REGISTER what they made, consumers are
handed the list. Nobody guesses a filename again, because nobody has to.

WHY A REGISTRY RATHER THAN A SHARED FOLDER. A folder tells you a path
exists. It does not tell you which specialist made it, what it contains,
whether it was verified, or whether it is the current version. A
downstream specialist reading a directory listing is still guessing --
just from a shorter list. An artifact carries its own provenance.

EVERY ENTRY IS CHECKED, NOT CLAIMED. `register_file` stats the path and
refuses to record a file that is not there. That is the difference
between this and a manifest the model writes for itself: a producer
cannot register an intention.

Run-scoped, and the scoping is load-bearing -- the same lesson as the
compute gate's `since=` and the ledger's per-run query. An artifact from
an earlier run vouching for this one is exactly the class of bug that
made a hollow deliverable pass every guard.
"""

from __future__ import annotations

import logging
import os
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Types are open on purpose -- a registry that only understands the kinds
# we thought of today becomes something producers route around.
FILE = "file"
DATASET = "dataset"
DOCUMENT = "document"
DEPLOYMENT = "deployment"
FINDING = "finding"

MAX_PER_RUN = 500


@dataclass
class Artifact:
    artifact_id: str
    type: str
    producer: str
    name: str
    path: str = ""
    url: str = ""
    size: int = 0
    verified: bool = False
    metadata: Dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)

    def describe(self) -> str:
        """One line, written for a specialist reading its brief."""
        where = self.path or self.url or "(no location)"
        bits = [f"{self.artifact_id}  {self.type}  {where}"]
        if self.size:
            bits.append(f"({self.size:,} bytes)")
        if self.name and self.name not in where:
            bits.append(f"— {self.name}")
        bits.append(f"[from {self.producer}]")
        if not self.verified:
            bits.append("[UNVERIFIED]")
        extra = self.metadata.get("summary")
        line = " ".join(bits)
        return f"{line}\n      {extra}" if extra else line


class RunArtifacts:
    """One run's artifacts. Thread-safe: specialists run sequentially
    today but worker parallelism is the whole point of registering."""

    def __init__(self, run_id: str) -> None:
        self.run_id = run_id
        self._items: List[Artifact] = []
        self._lock = threading.Lock()

    # -- registration -------------------------------------------------

    def _add(self, art: Artifact) -> Artifact:
        with self._lock:
            self._items.append(art)
            if len(self._items) > MAX_PER_RUN:
                del self._items[: MAX_PER_RUN // 4]
        logger.info("artifact %s registered by %s: %s",
                    art.artifact_id, art.producer, art.path or art.url)
        return art

    def register_file(self, path: str, producer: str, type: str = FILE,
                      name: str = "", **metadata) -> Optional[Artifact]:
        """Record a file, but only if it is actually on disk.

        Returns None when it is not. A producer cannot register an
        intention -- that is precisely what produced a run reporting
        figures "saved at" a directory that had never been created.
        """
        p = str(path or "").strip()
        if not p:
            return None
        try:
            if not os.path.isfile(p):
                logger.warning("refused artifact from %s: %s does not exist", producer, p)
                return None
            size = os.path.getsize(p)
        except OSError as exc:
            logger.warning("refused artifact from %s: %s (%s)", producer, p, exc)
            return None
        return self._add(Artifact(
            artifact_id=_new_id("art"), type=type, producer=producer,
            name=name or os.path.basename(p), path=p, size=size,
            verified=True, metadata=dict(metadata),
        ))

    def register_url(self, url: str, producer: str, type: str = DEPLOYMENT,
                     name: str = "", verified: bool = False,
                     **metadata) -> Optional[Artifact]:
        """Record a URL. `verified` means someone OPENED it and it
        responded -- not that it was printed by a deploy step."""
        u = str(url or "").strip()
        if not u.startswith(("http://", "https://")):
            return None
        return self._add(Artifact(
            artifact_id=_new_id("art"), type=type, producer=producer,
            name=name or u, url=u, verified=bool(verified),
            metadata=dict(metadata),
        ))

    def register_finding(self, summary: str, producer: str, **metadata) -> Artifact:
        """A result with no file behind it — a number, a decision, a
        chosen ticker. Registered so the next specialist inherits it
        instead of being handed prose to re-parse."""
        return self._add(Artifact(
            artifact_id=_new_id("art"), type=FINDING, producer=producer,
            name=summary[:80], verified=True,
            metadata={**metadata, "summary": summary},
        ))

    def mark_verified(self, artifact_id: str, evidence: str = "") -> bool:
        with self._lock:
            for a in self._items:
                if a.artifact_id == artifact_id:
                    a.verified = True
                    if evidence:
                        a.metadata["evidence"] = evidence
                    return True
        return False

    # -- reading ------------------------------------------------------

    def all(self) -> List[Artifact]:
        with self._lock:
            return list(self._items)

    def of_type(self, type: str) -> List[Artifact]:
        return [a for a in self.all() if a.type == type]

    def by_id(self, artifact_id: str) -> Optional[Artifact]:
        return next((a for a in self.all() if a.artifact_id == artifact_id), None)

    def as_dicts(self) -> List[Dict[str, Any]]:
        return [asdict(a) for a in self.all()]

    def manifest(self) -> str:
        """The block injected into a downstream specialist's brief.

        This is the whole feature in one method: a specialist that reads
        this has no reason to invent `data.csv`, and no reason to email a
        colleague about a file — the real path is right here, with who
        made it.
        """
        items = self.all()
        if not items:
            return ""
        lines = [
            "FILES AND RESULTS YOUR TEAMMATES HAVE ALREADY PRODUCED — use these "
            "exact paths. Do NOT invent filenames, and do NOT ask anyone to send "
            "you a file; everything available to you is listed here:",
        ]
        for a in items:
            lines.append("  " + a.describe())
        lines.append(
            "If what you need is not on this list, it does not exist yet — say so "
            "plainly rather than guessing a path or naming a colleague."
        )
        return "\n".join(lines)


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


# ---------------------------------------------------------------------
# Registry of registries, keyed by run
# ---------------------------------------------------------------------
#
# Process-wide and never swept, matching RunStore/ProgressStore for this
# single-user MVP. Bounded so a long-lived server cannot grow without
# limit -- the oldest runs are dropped, which is safe because a finished
# run's artifacts have already been reported.
_MAX_RUNS = 200
_runs: Dict[str, RunArtifacts] = {}
_runs_lock = threading.Lock()

# The run a specialist is currently executing inside. Set by the run
# entry points so producers (write_file, download_asset, deploy_vercel)
# can register without every one of them growing a run_id parameter --
# they are called by the model, which has no idea what a run id is.
_current_run: threading.local = threading.local()


def for_run(run_id: str) -> RunArtifacts:
    with _runs_lock:
        reg = _runs.get(run_id)
        if reg is None:
            reg = RunArtifacts(run_id)
            _runs[run_id] = reg
            if len(_runs) > _MAX_RUNS:
                for old in list(_runs)[: _MAX_RUNS // 4]:
                    _runs.pop(old, None)
        return reg


def set_current_run(run_id: Optional[str]) -> None:
    """Bind this thread to a run. Thread-local because browser work is
    marshalled onto its own thread and the agentic loop runs on another;
    a module global would leak one run's artifacts into another's."""
    _current_run.run_id = run_id


def current_run_id() -> Optional[str]:
    return getattr(_current_run, "run_id", None)


def current() -> Optional[RunArtifacts]:
    rid = current_run_id()
    return for_run(rid) if rid else None


def register_file(path: str, producer: str, **kw) -> Optional[Artifact]:
    """Convenience for producers: register into the current run, or do
    nothing outside one. Never raises — a bookkeeping failure must not
    break the action it is recording."""
    try:
        reg = current()
        return reg.register_file(path, producer, **kw) if reg else None
    except Exception as exc:  # noqa: BLE001
        logger.warning("artifact registration failed: %s", exc)
        return None


def register_url(url: str, producer: str, **kw) -> Optional[Artifact]:
    try:
        reg = current()
        return reg.register_url(url, producer, **kw) if reg else None
    except Exception as exc:  # noqa: BLE001
        logger.warning("artifact registration failed: %s", exc)
        return None


def manifest_for_current() -> str:
    try:
        reg = current()
        return reg.manifest() if reg else ""
    except Exception:  # noqa: BLE001
        return ""
