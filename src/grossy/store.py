"""Store base URL setting (``GROSSY_STORE_URL``).

The store's base URL is the single source of every host rule in Grossy: the
canonical product URL grammar, the order-details navigation route and the
doctor's ``store_url`` check all derive from it. The value is read from
``os.environ`` at use time; Grossy never parses ``.env`` and ships no default.
Validation checks the ``urlsplit`` shape only; a wrong host fails at first use.
"""

from __future__ import annotations

import os
from urllib.parse import urlsplit

STORE_URL_ENV = "GROSSY_STORE_URL"
STORE_URL_EXPECTED_FORM = "https://HOST"


class StoreConfigError(ValueError):
    """``GROSSY_STORE_URL`` is missing or malformed (a ``ValueError`` for pydantic)."""


def _reason(value: str | None) -> str | None:
    if value is None:
        return "is not set"
    try:
        parsed = urlsplit(value)
        _ = parsed.port  # raises ValueError for a malformed port
    except ValueError:
        return "has a malformed host or port"
    if parsed.scheme != "https":
        return "must use the https scheme"
    if not parsed.hostname:
        return "has no host"
    if parsed.username is not None or parsed.password is not None:
        return "must not contain user info"
    if parsed.path or parsed.query or parsed.fragment:
        return "must not contain a path, query, fragment or trailing slash"
    return None


def store_base_url() -> str:
    """Return the validated bare ``https://<host>[:port]`` base URL of the store.

    Reads ``GROSSY_STORE_URL`` on every call; fails closed with ``StoreConfigError``.
    """
    value = os.environ.get(STORE_URL_ENV)
    reason = _reason(value)
    if reason is not None or value is None:
        raise StoreConfigError(
            f"{STORE_URL_ENV} {reason}; expected a bare base URL of the form "
            f"{STORE_URL_EXPECTED_FORM} (no path, query, fragment, user info or trailing slash)"
        )
    return f"https://{urlsplit(value).netloc}"


def store_host() -> str:
    """Return the store's exact URL authority (host, with its port if one is set)."""
    return store_base_url().removeprefix("https://")
