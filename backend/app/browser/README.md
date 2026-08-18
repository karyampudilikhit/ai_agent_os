# The browser layer

Everything that drives a real web browser. Written to be reviewable on
its own — you should not need the rest of the repo to judge it.

**What it is for:** letting an AI employee *operate* a web application —
sort a table, apply a filter, fill a form, work a multi-screen flow — not
merely read pages. Reading was already solved; operating is what this
adds.

---

## The modules and how they depend on each other

```
        ┌─────────────────────────────────────────────┐
        │  primitives.py     the ONLY surface the      │
        │                    model can reach           │
        └───────┬─────────────────┬───────────────┬────┘
                │                 │               │
        ┌───────▼──────┐  ┌───────▼──────┐  ┌─────▼─────────┐
        │ observation  │  │   policy     │  │ session_      │
        │ what is on   │  │ is this      │  │ manager       │
        │ the page     │  │ allowed      │  │ owns Playwright│
        └──────────────┘  └──────────────┘  └───────────────┘

        task_flow.py  — the separate founder-in-the-loop path
                        (login → fill → approve). Blocking, human
                        present. Not driven by the agentic loop.
```

| File | Owns | Size |
|---|---|---|
| `session_manager.py` | The one Playwright instance, the one thread, session lifecycle |
| `observation.py` | Pages into addressable elements — across iframes — plus the data fingerprint and `browser_find` |
| `policy.py` | Scope: which verbs, which URLs; untrusted-content fencing |
| `primitives.py` | The 14 tools the model calls |
| `playbook.py` | What worked last time on this site |
| `task_flow.py` | Founder-present login/fill/approve flow, plus navigate/extract |

Outside this folder, because they are properties of the LOOP rather than
of the browser: `orchestrator/step_outcome.py` (did a step reach the
state it declared) and the budget/feedback logic in
`orchestrator/execution_loop.py`.

---

## Four design decisions worth reviewing

**1. Elements are addressed by a stamped attribute, not an index or an
XPath.** `observation.py` writes `data-vai-id="e17"` onto each element it
lists and the model says `click e17`. An index into a saved list breaks
on any re-render and then silently addresses the *wrong* element. An
XPath breaks on structural change and is unreadable in a log. A stamped
attribute either survives and is exact, or is gone and fails loudly —
which is what a stale reference should do. IDs are per-observation and
deliberately unstable; `ElementMap.check()` refuses one from an older
generation, and refuses an id the model invented.

**2. `BrowserPolicy` has no `allow_domain()` method.** This is not an
omission. A scope that page content can widen is not a scope. The policy
is built once from the task and is immutable for the run; SSRF targets
(`169.254.169.254`, `metadata.google.internal`, loopback) are blocked at
a hard floor that no configuration can lift.

**3. Page text is data, never instruction.** Everything a page says goes
through `policy.wrap_untrusted()`, which fences it and labels it. A page
containing "ignore your previous instructions" is content, and is
presented as content.

**4. Every primitive re-observes after acting.** The model is never told
"the click succeeded" — it is shown the page the click produced. This is
the whole reliability argument for the layer, and it is what makes the
per-step outcome check possible to build.

---

## Two constraints that are not obvious and will bite you

**One Playwright instance, one thread.** Playwright's sync API cannot be
touched from a different thread than the one that created it, and the
error it raises ("Sync API inside the asyncio loop") points somewhere
else entirely. `session_manager` pins all Playwright work to a single
`ThreadPoolExecutor` worker; every entry point goes through
`run_on_browser_thread()`. Never start or stop Playwright per session.

**One on-disk profile, one context.** The persistent profile that makes
"log in once and it sticks" work can be held by exactly one browser
context at a time — headless or headed, same profile. So a second
`launch_persistent_context` *cannot* succeed while the first is alive.
This is why `browser_navigate` reuses the open window rather than
launching (see blocker 2 below), and why parallel browser workers are not
possible without per-worker profiles.

---

## What it handles that a naive layer does not

| | |
|---|---|
| **Iframes** | Elements inside frames get frame-prefixed ids (`f1e3`) and resolve into the right document. Consent walls and embedded forms live there. |
| **Blocking overlays** | `browser_dismiss_overlay` finds the real buttons across every frame and declines cookies before accepting. Detected by what a click in the middle of the page would actually hit, so a sticky site header is not mistaken for a wall. |
| **Login walls** | Detected on every observation. `browser_await_login` opens nothing and asks nothing — the window is already visible, the founder signs in, and it notices the password field disappear. No password is ever typed by the AI. |
| **New tabs** | A click on `target="_blank"` moves the session to the new tab and starts a fresh id generation. A tab outside scope is closed rather than followed. |
| **Hover-revealed controls** | A sort arrow that is `visibility:hidden` until hovered is still clickable — Playwright's actionability checks run *before* it hovers, so the fallback hovers first and forces. |
| **Irreversible clicks** | "Send", "Publish", "Pay", "Delete" queue for founder approval instead of firing. The check is on the BUTTON, because the tool is `mutating=False` and usually harmless. |
| **Repeat work** | A verified path is recorded per site and replayed as a hint, so a site solved once is not re-solved. |

---

## Six blockers found by tracing live runs — all fixed, all ours

Six runs of one task ("top 5 weekly gainers from TradingView's screener")
never sorted the table. The obvious reading was that the model could not
operate a complex UI. **That reading was wrong six times in a row.** Five
in-process traces showed the model making a sensible next move at every
step, and six defects in the scaffolding under it:

1. **No feedback on a dead click.** The before/after page comparison
   existed but ran only at the end of the turn, so a model clicking
   ineffectively was told four steps too late. → moved into the loop.
2. **The URL shortcut could not execute.** `browser_navigate` always
   launched a *new* browser — impossible while one was open (see the
   profile constraint above). It fell back to a fresh session and
   silently lost all state. → reuses the open window.
3. **A failed navigation was recorded `ok=True`.** The failure text was
   not in a hand-maintained list of prefixes. Third time that bug class
   landed. → `_looks_failed` matches the *shape*, not the wording.
4. **The sort controls had no distinguishing name.** All twelve carry the
   identical `aria-label="Sort descending"`. → a control in a table cell
   takes its name from the cell (`"Chg % — Sort descending"`), with
   generic action words dropped so they cannot pollute the wrong
   column's identity.
5. **The repeat guard blocked verification.** `browser_observe` takes
   only a session token, so looking at a page *after* changing it is a
   byte-identical call and the guard stopped the run — making the
   observe → act → verify cycle impossible. → allowed on evidence the
   page really moved.
6. **Header cells were invisible to the observer.** Measured live: 14
   sort buttons inside `<th>` of which **3 visible** (the rest
   `visibility:hidden` until hovered), 13 header cells **all visible**,
   and **zero** matched the observer's fourteen selectors. → `th` and
   `[role="columnheader"]` added, plus a hover-then-force fallback in
   `_click_locator`, because Playwright runs actionability checks
   *before* it hovers.

**The transferable lesson:** every one of these was found by tracing what
a run *actually did*, never from the deliverable's account of itself.

---

## What verifies what

| Check | Question | Where |
|---|---|---|
| Provenance | Did a tool run? | `ToolCallLedger` |
| Activity | Did the page change? | `page_view_changed` |
| Outcome, per step | Did this step reach the state it declared? | `step_outcome.judge_step` |
| Outcome, per run | Is the table actually reordered? | `output_contract.RANKED_RESULT` |
| Row provenance | Were the reported records ever on a page it opened? | `compute_gate.unbacked_row_labels` |

Every one answers from a RECORDED FACT — a tool that really succeeded, a
page that really moved, a file that really exists. None asks the model
whether its own work went well. That is the single rule in this codebase
that has held every time it was applied and failed every time it was not.

## Known gap — read this before concluding the layer works

**No check asks whether an answer is SENSIBLE.**

A correctly-sorted list of delisted sub-penny shells passes every check
above: the column really was reordered, the rows really were read, every
ticker really exists. It is verified and useless. The one time a run
caught this, the judgement came from the model writing the deliverable,
not from any guard.

Every check here compares **page views** — element lists, URL, chrome,
text. None compares **the data**. Consequences seen live:

- A run navigated to `?sort=Perf%20%25&order=desc&timeframe=1W`, a
  parameter the site silently ignores. The page loaded, the address bar
  read as sorted, the rows were the defaults. Nothing flagged it.
- A click on a toolbar *filter* opened a dropdown. The page changed. It
  counted as progress.
- `output_contract.RANKED_RESULT` is satisfied by "an interaction changed
  the page and a read followed" — it proves the page was **operated**,
  not **sorted**, and a run satisfied it while shipping the default view.

The fix has the same shape as every guard that has held in this codebase
— read a record, not the model's account — applied to the data instead of
the DOM: capture the table rows before acting, compare after. With it the
loop stops guessing and starts searching, and site-specific knowledge
stops being a prerequisite.

---

## Where browser logic still lives outside this folder

Deliberately, because these are properties of the *loop*, not the
browser:

| File | What it holds |
|---|---|
| `orchestrator/execution_loop.py` | Browser budgets (22 steps / 600s / 3000 tokens, applied on evidence), the dead-click feedback, `BROWSER_RULES`, the page-moved exemption to the repeat guard |
| `orchestrator/output_contract.py` | `RANKED_RESULT`, `is_browser_tool`, `is_page_view_tool`, `page_view_changed` — the shared definition of "the page changed", so the loop and the gate can never disagree |
| `tools/tool_registry.py` | `_looks_failed` — how a failure-as-text is recognised |
| `actions/action_registry.py` | Registration; `niche_keywords` so unrelated tools cannot outrank browser ones |

---

## Tests

| File | Covers |
|---|---|
| `test_browser_operation.py` | 37 tests — the five loop fixes and the six blockers, each named for the failure it prevents |
| `test_browser_layer.py` | The primitives, policy, observation |
| `test_tradingview_fixes.py` | 11 tests — the ranked-result contract |
| `test_browser_task.py` | The founder-in-the-loop flow |

All offline: a scripted adapter returns canned decisions and a stubbed
`_execute` returns canned pages, so they test the layer rather than an
LLM's judgement.

**To trace a real run** — this is the tool to reach for first, because
the tool-call ledger is in-memory with no endpoint and every autopsy
before it was a reconstruction:

```bash
python trace_browser_run.py
python trace_browser_run.py --model nemotron-3-super:cloud
```

It drives the same executor the server does and prints every decision,
every tool result, and which fixes fired.
