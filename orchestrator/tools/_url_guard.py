"""Shared URL pre-flight guard for outbound coder tools.

Blocks anything that is not plain ``http(s)`` or that resolves to a
loopback / link-local / private / reserved address. Used by both
``orchestrator.tools.web`` (when added) and ``orchestrator.tools.browser``
so the policy lives in exactly one place.
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse

ALLOWED_SCHEMES: frozenset[str] = frozenset({"http", "https"})


class UrlGuardError(ValueError):
    """Raised when a URL fails the pre-flight guard."""


def _is_blocked_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def check_url(url: str, *, resolver=socket.getaddrinfo) -> str:
    """Validate *url* and return its normalised form.

    The optional ``resolver`` argument mirrors :func:`socket.getaddrinfo` so
    tests can inject a fake without touching the network.
    """
    if not isinstance(url, str) or not url:
        raise UrlGuardError("url must be a non-empty string")

    parsed = urlparse(url)
    if parsed.scheme not in ALLOWED_SCHEMES:
        raise UrlGuardError(f"scheme {parsed.scheme!r} is not allowed")
    host = parsed.hostname
    if not host:
        raise UrlGuardError("url has no host")

    # Literal IP fast-path.
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None

    if ip is not None:
        if _is_blocked_ip(ip):
            raise UrlGuardError(f"host {host} resolves to a blocked address")
        return url

    try:
        infos = resolver(host, None)
    except OSError as exc:
        raise UrlGuardError(f"could not resolve host {host!r}: {exc}") from exc

    for info in infos:
        sockaddr = info[4]
        try:
            resolved = ipaddress.ip_address(sockaddr[0])
        except ValueError:
            continue
        if _is_blocked_ip(resolved):
            raise UrlGuardError(f"host {host} resolves to blocked address {resolved}")

    return url
