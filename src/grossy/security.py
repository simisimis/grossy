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
    r"|\bSET-COOKIE\s*:"  # HTTP cookie header
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
            raise ProhibitedContentError(f"prohibited content in proposal: {obj[:80]!r}")
    elif isinstance(obj, dict):
        for value in obj.values():
            scan_prohibited_content(value)
    elif isinstance(obj, list):
        for item in obj:
            scan_prohibited_content(item)
