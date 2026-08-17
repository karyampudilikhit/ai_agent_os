"""FastAPI app — the thin slice of Phase 9.

Not the full manager terminal (roster view, approve/reject actions,
cost/output per employee). Just enough that someone can validate an
idea by clicking a button instead of running Python — the fastest path
to a demo the original phase plan called for.

    uvicorn backend.app.api.main:app --reload --port 8000
"""

from __future__ import annotations

# Load .env (TAVILY_API_KEY, future connector keys) BEFORE any module
# that reads os.environ — dynamic_employee grabs the Tavily client at
# import time, so this line has to run first.
from dotenv import load_dotenv
load_dotenv()

# Route the Python root logger to INFO so our app-side logs
# (dynamic_employee's "Tavily returned N result(s)", the MCP planner's
# "external tool results injected", etc.) actually reach the terminal.
# uvicorn's `--log-level` only controls uvicorn's OWN logs; without
# this the root logger defaults to WARNING and silently drops every
# INFO tool-fire message we rely on when diagnosing a run.
import logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from backend.app.api.oauth_routes import router as oauth_router
from backend.app.api.routes import router

app = FastAPI(title="Vision AI — Idea Validation (MVP slice)")

# Wide open for local MVP use — this is a single-user local tool right
# now, not a deployed multi-tenant service. Tighten before anything
# public-facing.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.middleware("http")
async def _no_stale_frontend(request: Request, call_next):
    """Force the browser to revalidate the app shell on every load.

    The whole UI is one hand-edited index.html with no build step and no
    content hash in its URL, so a browser that decides to reuse its
    cached copy silently pins the founder to an older version of the
    app. That is not hypothetical: a shipped feature was reported as
    "clicking does nothing" purely because the tab was serving HTML from
    before the feature existed, and an ordinary reload did not clear it.

    ETag/Last-Modified alone are not enough -- without an explicit
    directive a browser may serve a heuristically-fresh copy without
    asking us at all. `no-cache` does not mean "don't store", it means
    "always revalidate", so the 304 path still works and this stays
    cheap.

    Scoped to documents; hashed assets, if this ever gets a build step,
    should keep their long cache lifetimes.
    """
    response = await call_next(request)
    path = request.url.path
    if not path.startswith("/api") and (path.endswith((".html", "/")) or "." not in path.rsplit("/", 1)[-1]):
        response.headers["Cache-Control"] = "no-cache, must-revalidate"
    return response


app.include_router(router, prefix="/api")
# OAuth connect flow ("Connect GitHub" etc). Separate router so the
# provider config + token handling stay out of the main routes module.
app.include_router(oauth_router, prefix="/api")
app.mount("/", StaticFiles(directory="frontend_mvp", html=True), name="frontend")
