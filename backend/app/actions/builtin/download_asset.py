"""download_asset — save an image, font or other binary into the workspace.

WHY THIS EXISTS. `write_file` writes UTF-8 text and nothing else, so
until now an employee could produce an HTML page and had no way to put a
single image next to it. Asked to rebuild a site, the best it could do
was hotlink URLs it had never checked -- which is the same failure shape
as citing a page it never opened, one layer down.

This is the read-half of the file story: the employee already found the
asset (on the page it fetched, in the brief you gave it), and this makes
the bytes actually land on disk where the rest of its work can reference
them.

NOT MUTATING, deliberately. Every other write action asks for approval
because it changes something OUTSIDE the founder's machine -- an email
sent, a Slack posted, a repo created. This one pulls a public URL into a
sandboxed folder the founder already owns. Making a founder tap Approve
twelve times to fetch twelve images of their own site is friction with no
safety bought, and friction is what makes people turn guards off.

The bounds below are what make that safe, and they are checked rather
than trusted:

  - the destination resolves inside the workspace or the call is rejected
  - http/https only -- no file://, no data:, no ftp://
  - private and loopback addresses are refused (SSRF): the URL an agent
    fetches is model-chosen text, and "fetch this URL" pointed at
    169.254.169.254 or 127.0.0.1 is how an agent gets talked into
    reading a cloud metadata endpoint or an internal admin page
  - a size cap, enforced while streaming rather than after, so a
    hostile or accidental multi-GB response cannot fill the disk
"""

from __future__ import annotations

import ipaddress
import mimetypes
import os
import socket
from typing import Any, Dict
from urllib.parse import urlparse, unquote

import httpx

from backend.app.actions.action_registry import ActionSpec
from backend.app.actions.builtin._workspace import resolve_within, workspace_root

# 25 MB. Generous for a hero image or a webfont, far below anything that
# would matter on disk, and small enough that a runaway download is
# stopped in seconds rather than minutes.
MAX_BYTES = int(os.environ.get("VISION_ASSET_MAX_BYTES", str(25 * 1024 * 1024)))
TIMEOUT = float(os.environ.get("VISION_ASSET_TIMEOUT", "30"))
CHUNK = 64 * 1024

# Extension is taken from the URL when it has one, and from the response
# Content-Type when it does not -- CDN URLs frequently end in a hash with
# no suffix at all.
_TYPE_EXT = {
    "image/jpeg": ".jpg", "image/png": ".png", "image/gif": ".gif",
    "image/webp": ".webp", "image/avif": ".avif", "image/svg+xml": ".svg",
    "image/x-icon": ".ico", "image/vnd.microsoft.icon": ".ico",
    "font/woff2": ".woff2", "font/woff": ".woff", "font/ttf": ".ttf",
    "font/otf": ".otf", "application/font-woff2": ".woff2",
    "video/mp4": ".mp4", "application/pdf": ".pdf",
}


def _is_public_host(host: str) -> bool:
    """False for anything that resolves to a private, loopback, or
    link-local address.

    Resolved rather than pattern-matched: `localtest.me` and countless
    other public hostnames resolve to 127.0.0.1, so checking the string
    catches nothing. Every address the name resolves to must be public,
    because a hostname with one public and one private A record would
    otherwise slip through.
    """
    try:
        infos = socket.getaddrinfo(host, None)
    except (socket.gaierror, UnicodeError):
        return False
    if not infos:
        return False
    for info in infos:
        addr = info[4][0]
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            return False
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified):
            return False
    return True


def _guess_ext(url: str, content_type: str) -> str:
    path = unquote(urlparse(url).path)
    _, ext = os.path.splitext(path)
    if ext and len(ext) <= 6 and ext[1:].isalnum():
        return ext.lower()
    ct = (content_type or "").split(";")[0].strip().lower()
    if ct in _TYPE_EXT:
        return _TYPE_EXT[ct]
    guessed = mimetypes.guess_extension(ct) if ct else None
    return guessed or ".bin"


def _handler(args: Dict[str, Any]) -> str:
    url = str(args.get("url") or "").strip()
    rel = str(args.get("path") or "").strip()
    if not url:
        return "(missing 'url')"
    if not rel:
        return "(missing 'path' — where to save it, e.g. 'site/img/hero.jpg')"

    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return (f"(rejected: only http and https are allowed, got "
                f"{parsed.scheme or 'no scheme'!r})")
    if not parsed.hostname:
        return "(rejected: URL has no host)"
    if not _is_public_host(parsed.hostname):
        return (f"(rejected: {parsed.hostname} is not a public address. "
                "Only assets on the public internet can be downloaded.)")

    root = workspace_root()
    try:
        target = resolve_within(root, rel)
    except ValueError as exc:
        return f"(rejected: {exc})"

    try:
        with httpx.Client(timeout=TIMEOUT, follow_redirects=True) as client:
            with client.stream("GET", url) as resp:
                if resp.status_code >= 400:
                    return (f"(download failed: HTTP {resp.status_code} from "
                            f"{url[:120]})")
                # Redirects are followed, so re-check where we actually
                # landed -- an open redirect on a public host is a
                # perfectly ordinary way to reach an internal one.
                final_host = resp.url.host
                if final_host and not _is_public_host(final_host):
                    return (f"(rejected: redirected to non-public host "
                            f"{final_host})")

                ctype = resp.headers.get("content-type", "")
                if not os.path.splitext(target.name)[1]:
                    target = target.with_suffix(_guess_ext(url, ctype))

                target.parent.mkdir(parents=True, exist_ok=True)
                size = 0
                try:
                    with open(target, "wb") as fh:
                        for chunk in resp.iter_bytes(CHUNK):
                            size += len(chunk)
                            if size > MAX_BYTES:
                                fh.close()
                                target.unlink(missing_ok=True)
                                return (f"(rejected: asset exceeds "
                                        f"{MAX_BYTES // (1024 * 1024)}MB — "
                                        "stopped mid-download)")
                            fh.write(chunk)
                except Exception:
                    target.unlink(missing_ok=True)
                    raise
    except httpx.TimeoutException:
        return f"(download timed out after {TIMEOUT:.0f}s: {url[:120]})"
    except Exception as exc:  # noqa: BLE001
        return f"(download failed: {exc})"

    if size == 0:
        target.unlink(missing_ok=True)
        return f"(download failed: {url[:120]} returned an empty body)"

    saved = target.relative_to(root).as_posix()
    from backend.app.state import run_artifacts
    run_artifacts.register_file(str(target), producer="download_asset",
                                summary=f"downloaded from {url[:100]}")
    return (f"Saved {size:,} bytes to {saved} "
            f"(content-type: {ctype or 'unknown'}). "
            f"Reference it from your HTML as a path relative to the "
            f"workspace, e.g. src=\"{saved}\".")


def _preview(args: Dict[str, Any]) -> str:
    """Never reaches the approval UI (this action is non-mutating), but
    ActionSpec requires one and the run log reads better with it."""
    url = str(args.get("url") or "?")
    rel = str(args.get("path") or "?")
    return f"Download {url[:80]} → workspace/{rel}"


SPEC = ActionSpec(
    name="download_asset",
    description=(
        "Download an image, font, icon or other binary file from a public URL "
        "and save it inside the Vision AI workspace. Use this for EVERY asset a "
        "page needs — write_file only writes text, so it cannot save an image. "
        "Save assets before writing the HTML that references them, then point "
        "the HTML at the returned path. Public http/https URLs only; size-capped."
    ),
    parameters=[
        {"name": "url", "type": "string",
         "description": "Public http(s) URL of the asset to download.",
         "required": True},
        {"name": "path", "type": "string",
         "description": ("Where to save it, relative to the workspace, e.g. "
                         "'site/img/hero.jpg'. If you omit the extension one is "
                         "chosen from the response content-type."),
         "required": True},
    ],
    handler=_handler,
    preview=_preview,
    mutating=False,
)
