"""SSRF guard (PLAN05 §6.2): bmk only ever connects to public internet addresses.

A saved URL, or any redirect it leads to, could point at this Mac, the home router or a cloud metadata
endpoint. Every hop is checked before the request (`check_url`), and the connection itself is made by
`GuardedBackend`, which resolves the host, rejects non-public addresses and connects to the vetted IP, so a
DNS answer that changes between the check and the connect (DNS rebinding) cannot slip through.
"""

import ipaddress
import socket
from collections.abc import Callable, Iterable
from urllib.parse import urlsplit

import httpcore

Resolver = Callable[[str, int], list[str]]


class BlockedAddress(Exception):
    """The URL's host is not a public internet address."""


def system_resolver(host: str, port: int) -> list[str]:
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return list(dict.fromkeys(str(info[4][0]) for info in infos))


def is_public(address: str) -> bool:
    """True only for globally routable unicast addresses (no loopback, RFC 1918, link-local, CGNAT, …)."""
    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address):
        embedded = ip.ipv4_mapped or ip.sixtofour or (ip.teredo[1] if ip.teredo else None)
        if embedded is not None:
            ip = embedded
        elif ip in ipaddress.IPv6Network("64:ff9b::/96"):  # NAT64: the low 32 bits are the IPv4 target
            ip = ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
    return ip.is_global and not ip.is_multicast


def vet_host(host: str, port: int, resolver: Resolver) -> list[str]:
    """Resolve `host` and return its addresses, or raise BlockedAddress if any of them is not public."""
    try:
        addresses = resolver(host, port)
    except OSError as exc:
        raise httpcore.ConnectError(f"cannot resolve {host}: {exc}") from exc
    bad = [a for a in addresses if not is_public(a)]
    if bad or not addresses:
        raise BlockedAddress(f"{host} resolves to a non-public address ({bad[0] if bad else 'none'})")
    return addresses


def check_url(url: str, resolver: Resolver) -> None:
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise BlockedAddress(f"only http and https are fetched, not {parts.scheme or 'no scheme'}")
    if not parts.hostname:
        raise BlockedAddress("URL has no host")
    if parts.username or parts.password:
        raise BlockedAddress("URLs with credentials are not fetched")
    vet_host(parts.hostname, parts.port or (443 if parts.scheme == "https" else 80), resolver)


class GuardedBackend(httpcore.SyncBackend):
    """httpcore network backend that connects only to vetted public addresses (TLS still uses the hostname)."""

    def __init__(self, resolver: Resolver = system_resolver):
        self.resolver = resolver

    def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable | None = None,
    ) -> httpcore.NetworkStream:
        last: Exception | None = None
        for address in vet_host(host, port, self.resolver):
            try:
                return super().connect_tcp(address, port, timeout, local_address, socket_options)
            except httpcore.ConnectError as exc:
                last = exc
        assert last is not None
        raise last

    def connect_unix_socket(self, *args, **kwargs):  # pragma: no cover - never used
        raise BlockedAddress("unix sockets are not allowed")
