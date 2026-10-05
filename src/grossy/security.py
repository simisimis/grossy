"""Shared prohibited-content scan for agent-originated proposal data (PRD 11).

Every agent-originated file crosses the browser/agent boundary into the
deterministic CLI as untrusted data. The primary safety property is the closed
schema (``extra="forbid"``); this scan is defense-in-depth that catches
obviously dangerous content embedded in legitimate-looking string fields
(executable text, SQL, cookies, headers, DOM, screenshots, credentials,
addresses, account identity). It is shared by every versioned proposal family.
"""

from __future__ import annotations

import re
from typing import Any


class ProhibitedContentError(ValueError):
    """Raised when agent-originated data contains prohibited content."""


PROHIBITED_CONTENT_RE = re.compile(
    r"(<\s*script\b"  # HTML script tag
    r"|javascript\s*:"  # JavaScript URI
    r"|data\s*:\s*image/"  # Base64 image data
    r"|\b(?:SET-COOKIE|COOKIE|AUTHORIZATION)\s*[:=]"  # HTTP credentials
    r"|\bBearer\s+\S+"
    r"|\b(?:password|token|secret|address|account|email|payment|delivery_slot)\s*[:=]"
    r"|ab-profile"
    r"|\b[^\s@]+@[^\s@]+\.[^\s@]+\b"
    r"|\bSELECT\s+\S+\s+FROM\b"  # SQL SELECT
    r"|\bDROP\s+TABLE\b"  # SQL DROP
    r")",
    re.IGNORECASE,
)


def scan_prohibited_content(obj: Any) -> None:
    """Recursively scan all string values for prohibited content patterns.

    Raises :class:`ProhibitedContentError` on the first prohibited match.
    """
    if isinstance(obj, str):
        if PROHIBITED_CONTENT_RE.search(obj):
            raise ProhibitedContentError("prohibited content in proposal")
    elif isinstance(obj, dict):
        for value in obj.values():
            scan_prohibited_content(value)
    elif isinstance(obj, list):
        for item in obj:
            scan_prohibited_content(item)


def sanitize_human_text(value: str, order_ids: set[str] | None = None) -> str:
    """Minimize recognizable private data in reports and diagnostics, not state.

    This is defense-in-depth, not permission to extract sensitive page content.
    """
    text = re.sub(r"\s+", " ", value).strip()
    text = re.sub(r"[\x00-\x1f\x7f]", "", text)
    patterns = (
        (r"(?i)Bearer\s+\S+", "[redacted-token]"),
        (
            r"(?:/(?:home|Users)/[^\s]+|[A-Za-z]:[\\/]+Users[\\/]+[^\s]+|\S*ab-profile\S*)",
            "[redacted-path]",
        ),
        (r"(?i)\b(?:Authorization|Set-Cookie|Cookie|headers?)\s*[:=].*", "[redacted-credential]"),
        (r"(?i)\b(?:password|token|secret)\s*[:=]\s*\S+", "[redacted-credential]"),
        (r"<[^>]*>", "[redacted-markup]"),
        (r"\b[^\s@]+@[^\s@]+\.[^\s@]+\b", "[redacted-email]"),
        (
            r"(?i)\b(?:address|account|email|payment|delivery_slot|screenshot|raw_dom)\s*[:=].*",
            "[redacted-private-data]",
        ),
    )
    for pattern, replacement in patterns:
        text = re.sub(pattern, replacement, text)
    ids = sorted((oid for oid in (order_ids or set()) if oid), key=len, reverse=True)
    if ids:
        text = re.sub(
            r"(?<!\d)(" + "|".join(re.escape(oid) for oid in ids) + r")(?!\d)", "[redacted]", text
        )
    return text
