"""Reddit reader — hand-rolled read-only connector.

Uses Reddit's OAuth API (script-app credentials) because Reddit killed
unauthenticated JSON access in 2024 — every anonymous request from a
programmatic client now 403s regardless of user agent. Read-only public
data still works, but the request has to be authenticated.

Auth is minimal: a "script" or "web app" from reddit.com/prefs/apps
gives you a client_id + client_secret. We do the client-credentials
grant (no user login) and hit oauth.reddit.com with the returned
Bearer token. Token is cached in-memory and refreshed on 401.

Env vars needed (put in `.env` next to TAVILY_API_KEY / NOTION_TOKEN):
    REDDIT_CLIENT_ID=...
    REDDIT_CLIENT_SECRET=...

If either is missing, the tool disables itself silently. Employees run
without Reddit context — same "quiet failures" philosophy as the other
connectors.

Two capabilities:
  - Subreddit top posts (`fetch_subreddit_top`) — "research trends on
    r/marketing" -> top-of-week from r/marketing gets pulled.
  - Thread comments (`fetch_thread`) — any reddit.com/r/*/comments/*
    URL in the task gets the top N comments extracted.

Never posts, votes, DMs, or does anything write-shaped. That's v2.
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)

OAUTH_TOKEN_URL = "https://www.reddit.com/api/v1/access_token"
OAUTH_API_BASE = "https://oauth.reddit.com"
DEFAULT_TIMEOUT = 12.0
DEFAULT_POST_LIMIT = 10
DEFAULT_COMMENT_LIMIT = 5
MAX_SUBREDDITS_PER_TASK = 3
MAX_THREADS_PER_TASK = 2
MAX_SNIPPET_CHARS = 300
TOKEN_LIFETIME_SEC = 3600 - 60  # Reddit tokens are 1h; refresh at 59m

# Descriptive User-Agent — Reddit's docs specifically require this and
# 429s generic UAs even after auth. Include contact for their abuse team.
USER_AGENT = (
    "vision-ai/0.1 (read-only research bot; "
    "https://github.com/karyampudilikhit/ai_agent_os)"
)

_SUBREDDIT_RE = re.compile(r"\br/([A-Za-z0-9_]{3,21})\b")
_THREAD_URL_RE = re.compile(
    r"https?://(?:www\.|old\.|new\.)?reddit\.com/r/([A-Za-z0-9_]{3,21})/comments/([a-z0-9]+)",
    re.IGNORECASE,
)

_TRIGGER_WORDS = (
    "reddit", "subreddit", "r/",
    "what are people saying",
    "community sentiment", "community feedback",
    "indie hacker", "indiehackers",
)


def extract_subreddits(text: str) -> List[str]:
    if not text:
        return []
    seen: List[str] = []
    for name in _SUBREDDIT_RE.findall(text):
        low = name.lower()
        if low not in (s.lower() for s in seen):
            seen.append(name)
    return seen


def extract_thread_urls(text: str) -> List[Dict[str, str]]:
    if not text:
        return []
    out: List[Dict[str, str]] = []
    seen_ids = set()
    for m in _THREAD_URL_RE.finditer(text):
        thread_id = m.group(2)
        if thread_id in seen_ids:
            continue
        seen_ids.add(thread_id)
        out.append({
            "url": m.group(0).rstrip(".,);:]!?").rstrip("'\""),
            "subreddit": m.group(1),
            "thread_id": thread_id,
        })
    return out


def should_read_reddit(text: str) -> bool:
    if not text:
        return False
    lowered = text.lower()
    if _SUBREDDIT_RE.search(text) or _THREAD_URL_RE.search(text):
        return True
    return any(t in lowered for t in _TRIGGER_WORDS)


class RedditReader:
    """Read-only OAuth-backed Reddit client. `enabled` is False when
    creds aren't set — callers should check before calling."""

    def __init__(
        self,
        client_id: Optional[str] = None,
        client_secret: Optional[str] = None,
        timeout: float = DEFAULT_TIMEOUT,
    ):
        self.client_id = client_id or os.environ.get("REDDIT_CLIENT_ID")
        self.client_secret = client_secret or os.environ.get("REDDIT_CLIENT_SECRET")
        self.timeout = timeout
        self.enabled = bool(self.client_id and self.client_secret)
        self._token: Optional[str] = None
        self._token_expiry: float = 0.0
        self._token_lock = threading.Lock()
        if not self.enabled:
            logger.info(
                "Reddit: REDDIT_CLIENT_ID / REDDIT_CLIENT_SECRET missing — "
                "Reddit tool disabled, employees run without Reddit context."
            )

    # ---- auth ------------------------------------------------------

    def _get_token(self) -> Optional[str]:
        """Return a valid Bearer token, minting one if cache is empty
        or expired. Thread-safe."""
        if not self.enabled:
            return None
        with self._token_lock:
            if self._token and time.time() < self._token_expiry:
                return self._token
            try:
                resp = httpx.post(
                    OAUTH_TOKEN_URL,
                    data={"grant_type": "client_credentials"},
                    auth=(self.client_id, self.client_secret),
                    headers={"User-Agent": USER_AGENT},
                    timeout=self.timeout,
                )
                resp.raise_for_status()
                data = resp.json()
            except (httpx.HTTPError, ValueError) as exc:
                logger.warning("Reddit token request failed: %s", exc)
                return None
            token = data.get("access_token")
            if not token:
                logger.warning("Reddit token response missing access_token: %s", data)
                return None
            self._token = token
            self._token_expiry = time.time() + TOKEN_LIFETIME_SEC
            return self._token

    # ---- raw fetch -------------------------------------------------

    def _get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Optional[Any]:
        token = self._get_token()
        if not token:
            return None
        url = f"{OAUTH_API_BASE}{path}"
        try:
            resp = httpx.get(
                url,
                params=params,
                headers={
                    "Authorization": f"Bearer {token}",
                    "User-Agent": USER_AGENT,
                    "Accept": "application/json",
                },
                timeout=self.timeout,
                follow_redirects=True,
            )
            # If token expired mid-flight, drop it and retry once.
            if resp.status_code == 401:
                logger.info("Reddit 401 — refreshing token and retrying once")
                with self._token_lock:
                    self._token = None
                    self._token_expiry = 0.0
                token = self._get_token()
                if not token:
                    return None
                resp = httpx.get(
                    url,
                    params=params,
                    headers={
                        "Authorization": f"Bearer {token}",
                        "User-Agent": USER_AGENT,
                        "Accept": "application/json",
                    },
                    timeout=self.timeout,
                    follow_redirects=True,
                )
            resp.raise_for_status()
            return resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            logger.info("Reddit fetch failed for %s: %s", url[:100], exc)
            return None

    # ---- subreddit top ---------------------------------------------

    def fetch_subreddit_top(
        self,
        subreddit: str,
        window: str = "week",
        limit: int = DEFAULT_POST_LIMIT,
    ) -> List[Dict[str, Any]]:
        if not subreddit:
            return []
        data = self._get(
            f"/r/{subreddit}/top", params={"t": window, "limit": limit}
        )
        if not isinstance(data, dict):
            return []
        children = ((data.get("data") or {}).get("children") or [])
        out: List[Dict[str, Any]] = []
        for child in children[:limit]:
            post = (child or {}).get("data") or {}
            if not isinstance(post, dict):
                continue
            title = str(post.get("title", "")).strip()
            if not title:
                continue
            out.append({
                "title": title,
                "score": int(post.get("score") or 0),
                "num_comments": int(post.get("num_comments") or 0),
                "author": str(post.get("author") or ""),
                "flair": str(post.get("link_flair_text") or "").strip(),
                "url": f"https://www.reddit.com{post.get('permalink', '')}",
                "selftext": str(post.get("selftext") or "").strip(),
            })
        return out

    # ---- single thread ---------------------------------------------

    def fetch_thread(
        self,
        subreddit: str,
        thread_id: str,
        comment_limit: int = DEFAULT_COMMENT_LIMIT,
    ) -> Optional[Dict[str, Any]]:
        if not subreddit or not thread_id:
            return None
        data = self._get(
            f"/r/{subreddit}/comments/{thread_id}",
            params={"limit": comment_limit},
        )
        if not isinstance(data, list) or len(data) < 2:
            return None
        try:
            post_data = data[0]["data"]["children"][0]["data"]
        except (KeyError, IndexError, TypeError):
            return None
        comments: List[Dict[str, Any]] = []
        try:
            for c in data[1]["data"]["children"][:comment_limit]:
                cd = (c or {}).get("data") or {}
                body = str(cd.get("body") or "").strip()
                if not body or body in {"[removed]", "[deleted]"}:
                    continue
                comments.append({
                    "author": str(cd.get("author") or ""),
                    "score": int(cd.get("score") or 0),
                    "body": body,
                })
        except (KeyError, IndexError, TypeError):
            pass
        return {
            "title": str(post_data.get("title", "")).strip(),
            "selftext": str(post_data.get("selftext") or "").strip(),
            "score": int(post_data.get("score") or 0),
            "num_comments": int(post_data.get("num_comments") or 0),
            "url": f"https://www.reddit.com{post_data.get('permalink', '')}",
            "comments": comments,
        }

    # ---- prompt formatting -----------------------------------------

    def format_subreddit_for_prompt(
        self, subreddit: str, posts: List[Dict[str, Any]]
    ) -> str:
        if not posts:
            return ""
        lines = [
            f"REDDIT — TOP POSTS FROM r/{subreddit} (last week)",
            "(Real posts. Score = upvotes. Use these as community signal, not gospel.)",
            "",
        ]
        for i, p in enumerate(posts, 1):
            flair = f" [{p['flair']}]" if p.get("flair") else ""
            lines.append(f"{i}. {p['title']}{flair}")
            lines.append(
                f"   {p['score']} up / {p['num_comments']} comments — {p['url']}"
            )
            body = (p.get("selftext") or "").replace("\n", " ").strip()
            if body:
                if len(body) > MAX_SNIPPET_CHARS:
                    body = body[:MAX_SNIPPET_CHARS].rstrip() + "…"
                lines.append(f"   {body}")
            lines.append("")
        return "\n".join(lines).rstrip() + "\n"

    def format_thread_for_prompt(self, thread: Dict[str, Any]) -> str:
        if not thread:
            return ""
        lines = [
            f"REDDIT THREAD: {thread.get('title', '(untitled)')}",
            f"({thread.get('score', 0)} up / "
            f"{thread.get('num_comments', 0)} comments — {thread.get('url', '')})",
        ]
        selftext = (thread.get("selftext") or "").strip()
        if selftext:
            if len(selftext) > MAX_SNIPPET_CHARS * 2:
                selftext = selftext[: MAX_SNIPPET_CHARS * 2].rstrip() + "…"
            lines.append("")
            lines.append(selftext)
        comments = thread.get("comments") or []
        if comments:
            lines.append("")
            lines.append("TOP COMMENTS:")
            for c in comments:
                body = c["body"].replace("\n", " ").strip()
                if len(body) > MAX_SNIPPET_CHARS:
                    body = body[:MAX_SNIPPET_CHARS].rstrip() + "…"
                lines.append(f"- ({c['score']} up) {c['author']}: {body}")
        return "\n".join(lines).rstrip() + "\n"

    # ---- one-shot: task -> context block ---------------------------

    def read_for_task(self, task: str) -> str:
        if not self.enabled or not task or not should_read_reddit(task):
            return ""
        blocks: List[str] = []
        for thread_ref in extract_thread_urls(task)[:MAX_THREADS_PER_TASK]:
            thread = self.fetch_thread(
                thread_ref["subreddit"], thread_ref["thread_id"]
            )
            if thread:
                blocks.append(self.format_thread_for_prompt(thread))
        for sub in extract_subreddits(task)[:MAX_SUBREDDITS_PER_TASK]:
            posts = self.fetch_subreddit_top(sub, window="week")
            if posts:
                blocks.append(self.format_subreddit_for_prompt(sub, posts))
        return "\n\n".join(blocks)
