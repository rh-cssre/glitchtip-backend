"""Client address and scheme from reverse-proxy headers.

This replaces granian's ``wrap_asgi_with_proxy_headers``, which applies one
trust rule to both headers. Here the two headers follow different rules,
because getting each one wrong costs different things:

- ``X-Forwarded-Proto`` is honoured from any peer. GlitchTip has no other way
  to learn that a request arrived over https. If it is missed, Django's CSRF
  check compares an https ``Origin`` with an http request and rejects every
  POST, so nobody can log in. A client that forges it gains little.
- ``X-Forwarded-For`` sets the client address that login throttling is keyed
  on, and a client can write anything into it. It is read only when the
  connecting peer is a trusted proxy, and then from the right. Each proxy
  appends the address its connection came from, so after skipping trusted
  proxies the first address left is the one the outermost trusted proxy saw.
  Anything further left was written by the client.

Trusted proxies are any non-public address (private, loopback, CGNAT, IPv6
ULA, link-local), so a proxy on the same host, network, cluster or platform
works with no configuration, plus whatever ``TRUSTED_PROXIES`` lists. A proxy
with a public address, such as a CDN, has to be listed; until then its
forwarding header is ignored and a warning names it. ``TRUSTED_PROXIES="*"``
trusts every peer and takes the leftmost address, which any client can forge.
"""

import ipaddress
import logging
from functools import lru_cache

logger = logging.getLogger(__name__)

_FORWARDED_PROTO = b"x-forwarded-proto"
_FORWARDED_FOR = b"x-forwarded-for"
# Bounds the log noise from clients that send X-Forwarded-For straight to an
# exposed port.
_MAX_WARNED_PEERS = 16


def _strip_port(host: str) -> str:
    """Drop a port that some platforms append to forwarded addresses."""
    if host.startswith("["):  # [2001:db8::1]:443
        return host[1 : host.find("]")] if "]" in host else host
    if host.count(":") == 1 and "." in host:  # 192.0.2.1:443
        return host.partition(":")[0]
    return host


class ProxyHeaders:
    """ASGI wrapper that applies ``X-Forwarded-Proto`` and ``X-Forwarded-For``.

    ``trusted_proxies`` is the raw ``TRUSTED_PROXIES`` value: comma-separated
    addresses and networks, ``*``, or empty for the default.
    """

    def __init__(self, app, trusted_proxies: str | None = None):
        self.app = app
        trusted_proxies = (trusted_proxies or "").strip()
        self.trust_all = trusted_proxies == "*"
        self.networks = []
        if not self.trust_all:
            for entry in filter(None, (e.strip() for e in trusted_proxies.split(","))):
                try:
                    self.networks.append(ipaddress.ip_network(entry, strict=False))
                except ValueError:
                    logger.warning("TRUSTED_PROXIES: ignoring invalid entry %r", entry)
        # Few distinct proxies connect, so classifying a peer is nearly
        # always a cache hit.
        self.is_trusted = lru_cache(maxsize=4096)(self._is_trusted)
        self.warned_peers = set()

    def _is_trusted(self, host: str) -> bool:
        try:
            address = ipaddress.ip_address(_strip_port(host))
        except ValueError:
            return False
        if not address.is_global:
            return True
        return any(address in network for network in self.networks)

    def client_host(self, forwarded_for: str) -> str:
        hops = [hop.strip() for hop in forwarded_for.split(",")]
        if not self.trust_all:
            # The leftmost hop is the answer whether or not it is trusted, so
            # it is never classified. A single-hop header, the common case,
            # needs no address parsing at all.
            for hop in reversed(hops[1:]):
                if not self.is_trusted(hop):
                    return _strip_port(hop)
        return _strip_port(hops[0])

    def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            proto = forwarded_for = None
            for name, value in scope["headers"]:
                if name == _FORWARDED_PROTO:
                    proto = value
                elif name == _FORWARDED_FOR:
                    # Proxies may add a second header rather than append.
                    forwarded_for = (
                        value if forwarded_for is None else forwarded_for + b"," + value
                    )

            if proto is not None:
                proto = proto.decode("latin1").strip().lower()
                if proto in ("http", "https"):
                    scope["scheme"] = proto

            if forwarded_for is not None:
                client = scope.get("client")
                peer = client[0] if client else None
                if self.trust_all or (peer and self.is_trusted(peer)):
                    host = self.client_host(forwarded_for.decode("latin1"))
                    if host:
                        scope["client"] = (host, client[1] if client else 0)
                elif (
                    peer
                    and peer not in self.warned_peers
                    and len(self.warned_peers) < _MAX_WARNED_PEERS
                ):
                    self.warned_peers.add(peer)
                    logger.warning(
                        "Ignoring X-Forwarded-For from %s, which is not a trusted "
                        "proxy. If it is your proxy or CDN, add its addresses to "
                        "TRUSTED_PROXIES so client addresses are recorded correctly.",
                        peer,
                    )
        # Returned rather than awaited, which saves a coroutine frame per request.
        return self.app(scope, receive, send)
