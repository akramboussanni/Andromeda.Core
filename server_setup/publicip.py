"""Public IPv4 discovery.

Game and voice traffic goes straight to the server's public address, so this
module deliberately has no LAN/offline fallback: either HTTPS providers tell us
the public IPv4 address, the operator supplies one explicitly, or setup stops.
"""

from __future__ import annotations

import ipaddress
import re
import socket
from dataclasses import dataclass, field
from typing import Callable, Iterable
from urllib import request

from . import ANDROMEDA_VERSION

PROVIDERS: tuple[str, ...] = (
    "https://api4.ipify.org",
    "https://ipv4.icanhazip.com",
    "https://checkip.amazonaws.com",
    "https://v4.ident.me",
)
DEFAULT_TIMEOUT = 4.0
USER_AGENT = f"Andromeda-Setup/{ANDROMEDA_VERSION}"

_HOSTNAME_RE = re.compile(r"^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$", re.IGNORECASE)

Fetcher = Callable[[str, float], str]


class PublicIPError(RuntimeError):
    """Raised when no provider returned a usable public IPv4 address."""


@dataclass
class Detection:
    ip: str
    sources: list[str]
    # Different answers from other providers (several internet connections, a proxy, ...).
    disagreements: dict[str, str] = field(default_factory=dict)

    @property
    def source_text(self) -> str:
        return ", ".join(self.sources)


def _https_fetch(url: str, timeout: float) -> str:
    req = request.Request(url, headers={"User-Agent": USER_AGENT})
    with request.urlopen(req, timeout=timeout) as resp:
        return resp.read(64).decode("ascii", "replace")


def parse_public_ipv4(text: str) -> str | None:
    """Return the address if ``text`` is a single globally routable IPv4 address."""
    try:
        addr = ipaddress.IPv4Address(text.strip())
    except ValueError:
        return None
    return str(addr) if addr.is_global else None


def detect_public_ipv4(
    providers: Iterable[str] = PROVIDERS,
    timeout: float = DEFAULT_TIMEOUT,
    fetch: Fetcher | None = None,
    confirmations: int = 2,
) -> Detection:
    """Ask HTTPS providers until ``confirmations`` of them agree.

    One valid answer is still accepted when the others are unreachable; answers
    that disagree are reported so the operator can check them.
    """
    fetch = fetch or _https_fetch
    failures: list[str] = []
    answers: dict[str, list[str]] = {}
    order: list[str] = []
    for url in providers:
        try:
            answer = fetch(url, timeout)
        except Exception as exc:  # DNS, TLS, timeout, HTTP errors all mean "try the next one"
            failures.append(f"{url}: {exc.__class__.__name__}: {exc}")
            continue
        ip = parse_public_ipv4(answer)
        if not ip:
            failures.append(f"{url}: did not return a public IPv4 address ({answer.strip()[:40]!r})")
            continue
        if ip not in answers:
            order.append(ip)
        answers.setdefault(ip, []).append(url)
        if len(answers[ip]) >= confirmations:
            break
    if not answers:
        detail = "\n".join(f"    - {line}" for line in failures) or "    - no providers configured"
        raise PublicIPError(
            "Could not determine this server's public IPv4 address. An internet connection is required.\n"
            f"{detail}"
        )
    # Most confirmations wins; ties go to the first answer received.
    best = max(order, key=lambda ip: (len(answers[ip]), -order.index(ip)))
    others = {url: ip for ip in order if ip != best for url in answers[ip]}
    return Detection(best, answers[best], others)


def validate_manual_ipv4(value: str) -> str:
    """Validate an operator-supplied public IPv4 address and return it normalised.

    Players' games send UDP straight to this address, so hostnames (which could be
    Cloudflare-proxied), IPv6 and private/LAN addresses are all rejected.
    """
    value = (value or "").strip()
    if not value:
        raise ValueError("an address is required")
    try:
        addr = ipaddress.ip_address(value)
    except ValueError:
        if _HOSTNAME_RE.match(value):
            raise ValueError(
                f"{value!r} is a hostname; enter the server's public IPv4 address instead "
                "(game traffic is UDP and cannot go through Cloudflare or other proxies)"
            ) from None
        raise ValueError(f"{value!r} is not an IPv4 address") from None
    if addr.version != 4:
        raise ValueError("game hosting needs an IPv4 address")
    if not addr.is_global:
        raise ValueError(
            f"{value} is a private/LAN or reserved address; players on the internet cannot reach it. "
            "Enter the server's public IPv4 address"
        )
    return str(addr)


def primary_local_ipv4() -> str | None:
    """Return the LAN address of the default route (no packets are sent)."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("1.1.1.1", 53))
        return sock.getsockname()[0]
    except OSError:
        return None
    finally:
        sock.close()
