"""
Which saved web links Qronos may hand to a browser.

Only the browser may reach the public internet, and only through a visible,
permission-gated, audited path. This module answers the first question on
that path: is this string an ordinary public http(s) URL? It does not fetch,
resolve or open anything.

Refused, with a reason a person can read:

    every scheme except http and https (javascript:, file:, data:, custom
    protocol handlers, about:, ftp: ...);
    credentials in the URL (``user:pass@host``), which is also how a link
    disguises its real host;
    local and private destinations - localhost, loopback, private, link-local
    and other non-global IP literals, including the numeric spellings
    browsers accept (``2130706433``, ``0x7f000001``, ``127.1``) - because a
    browser pointed there talks to services on this machine or network,
    such as Qronos's own local sidecars;
    whitespace, control characters, backslashes, missing hosts, bad ports and
    over-long values.

A hostname that merely resolves to a private address cannot be detected here
without a network lookup, which this module must not make.
"""

from __future__ import annotations

import ipaddress
import re
from urllib.parse import urlsplit


MAX_URL_CHARS = 2048
ALLOWED_SCHEMES = frozenset({"http", "https"})

_NUMERIC_HOST = re.compile(r"(?i)^(0x[0-9a-f]+|[0-9]+)(\.(0x[0-9a-f]+|[0-9]+))*$")
_LOCAL_SUFFIXES = (".localhost", ".local", ".internal", ".home.arpa")


class UnsafeLink(ValueError):
    """A link that Qronos will not hand to a browser."""


def validate_web_url(value: object) -> str:
    """Return the link unchanged if it is a plain public http(s) URL; else raise."""
    if not isinstance(value, str):
        raise UnsafeLink("The link is not text.")
    text = value.strip()
    if not text:
        raise UnsafeLink("The link is empty.")
    if len(text) > MAX_URL_CHARS:
        raise UnsafeLink("The link is too long.")
    if any(ch.isspace() or ord(ch) < 32 or ch == "\x7f" for ch in text):
        raise UnsafeLink("The link contains spaces or control characters.")
    if "\\" in text:
        raise UnsafeLink("The link contains a backslash.")

    try:
        parts = urlsplit(text)
        port = parts.port
    except ValueError:
        raise UnsafeLink("The link is not a valid web address.") from None

    if parts.scheme.lower() not in ALLOWED_SCHEMES:
        raise UnsafeLink("Only http and https links can be opened.")
    if not parts.netloc or not text.lower().startswith(parts.scheme.lower() + "://"):
        raise UnsafeLink("The link has no web host.")
    if "@" in parts.netloc or parts.username is not None or parts.password is not None:
        raise UnsafeLink("Links with a user name or password are not opened.")
    if port == 0:
        raise UnsafeLink("The link has an invalid port.")

    host = (parts.hostname or "").rstrip(".").lower()
    if not host or len(host) > 253:
        raise UnsafeLink("The link has no web host.")
    if host == "localhost" or host.endswith(_LOCAL_SUFFIXES):
        raise UnsafeLink("Links to this computer or the local network are not opened.")

    literal = host.strip("[]")
    try:
        address = ipaddress.ip_address(literal)
    except ValueError:
        address = None
    if address is not None:
        if not address.is_global:
            raise UnsafeLink("Links to this computer or the local network are not opened.")
    elif _NUMERIC_HOST.match(host):
        # Browsers read "2130706433", "0x7f000001" and "127.1" as addresses.
        raise UnsafeLink("Links with a numeric host in a non-standard form are not opened.")
    elif any(not label or label.startswith("-") or label.endswith("-") for label in host.split(".")):
        raise UnsafeLink("The link has an invalid host name.")
    return text
