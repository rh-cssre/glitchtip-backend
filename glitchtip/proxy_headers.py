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

``TRUSTED_PROXIES`` lists addresses, networks and the keywords below. Unset,
it is ``private``, which works behind a proxy on the same host, network,
cluster or hosting platform with no configuration. That default cannot be
fooled from the internet, but it also trusts clients whose address is private
when the proxy sees them (a LAN, a VPN, or a load balancer that rewrites
source addresses), so they can still choose their address. Setting the
variable replaces the default, which is how an operator narrows it to their
own proxies; loopback is always trusted. ``*`` trusts every peer and takes
the leftmost address, which any client can forge.

Getting the list wrong never breaks requests: an untrusted hop just becomes
the client address, so users behind it share one throttle bucket. The log
says what was seen and what to set.
"""

import bisect
import ipaddress
import logging
import socket
import time

logger = logging.getLogger(__name__)

_FORWARDED_PROTO = b"x-forwarded-proto"
_FORWARDED_FOR = b"x-forwarded-for"
_IPV4_MAPPED = bytes(10) + b"\xff\xff"

# Longer than any address with a port; anything bigger is not one.
_MAX_HOP_LENGTH = 64
# Hops examined from the right. Real chains are a handful of proxies; the
# bound keeps a huge client-written header from costing per-request work.
_MAX_HOPS = 16
_WARN_INTERVAL = 3600
_MAX_REPORTED_PROXIES = 32
_REPORT_EVERY = 64

LOOPBACK = ("127.0.0.0/8", "::1/128")
PRIVATE = (
    "10.0.0.0/8",
    "172.16.0.0/12",
    "192.168.0.0/16",
    "100.64.0.0/10",  # carrier-grade NAT, used by some platforms internally
    "169.254.0.0/16",
    "fc00::/7",
    "fe80::/10",
)
# https://www.cloudflare.com/ips/ as of this release. It changes rarely;
# ranges added later can be listed in TRUSTED_PROXIES alongside the keyword.
CLOUDFLARE = (
    "173.245.48.0/20",
    "103.21.244.0/22",
    "103.22.200.0/22",
    "103.31.4.0/22",
    "141.101.64.0/18",
    "108.162.192.0/18",
    "190.93.240.0/20",
    "188.114.96.0/20",
    "197.234.240.0/22",
    "198.41.128.0/17",
    "162.158.0.0/15",
    "104.16.0.0/13",
    "104.24.0.0/14",
    "172.64.0.0/13",
    "131.0.72.0/22",
    "2400:cb00::/32",
    "2606:4700::/32",
    "2803:f800::/32",
    "2405:b500::/32",
    "2405:8100::/32",
    "2a06:98c0::/29",
    "2c0f:f248::/32",
)
KEYWORDS = {"loopback": LOOPBACK, "private": PRIVATE, "cloudflare": CLOUDFLARE}


def parse_address(host: str):
    """``(version, packed, canonical text)`` for a hop or peer, or None.

    A port, brackets and the IPv4-mapped IPv6 prefix are removed. The text is
    canonical so one address cannot be spelled several ways.
    """
    if len(host) > _MAX_HOP_LENGTH:
        return None
    if host.startswith("["):  # [2001:db8::1]:443
        host = host[1 : host.find("]")]
    elif host.count(":") == 1:  # 192.0.2.1:443
        host = host.partition(":")[0]
    try:
        # Only canonical dotted-quad text parses, so it is returned as is.
        return 4, socket.inet_pton(socket.AF_INET, host), host
    except OSError:
        try:
            packed = socket.inet_pton(socket.AF_INET6, host)
        except OSError:
            return None
    if packed.startswith(_IPV4_MAPPED):
        return 4, packed[12:], socket.inet_ntop(socket.AF_INET, packed[12:])
    return 6, packed, socket.inet_ntop(socket.AF_INET6, packed)


class AddressSet:
    """Membership of a parsed address in a set of networks."""

    def __init__(self, networks):
        self.ranges = {}
        for version in (4, 6):
            nets = ipaddress.collapse_addresses(
                n for n in networks if n.version == version
            )
            ranges = [(int(n.network_address), int(n.broadcast_address)) for n in nets]
            self.ranges[version] = ([r[0] for r in ranges], [r[1] for r in ranges])

    def __contains__(self, address) -> bool:
        version, packed, _ = address
        value = int.from_bytes(packed)
        starts, ends = self.ranges[version]
        i = bisect.bisect_right(starts, value) - 1
        return i >= 0 and value <= ends[i]


def parse_trusted_proxies(value: str | None):
    """Networks for a TRUSTED_PROXIES value, or None for ``*``."""
    value = (value or "").strip() or "private"
    if value == "*":
        return None
    networks = [ipaddress.ip_network(n) for n in LOOPBACK]
    for entry in filter(None, (e.strip() for e in value.split(","))):
        if entry.lower() in KEYWORDS:
            networks += [ipaddress.ip_network(n) for n in KEYWORDS[entry.lower()]]
            continue
        try:
            networks.append(ipaddress.ip_network(entry, strict=False))
        except ValueError:
            logger.warning("TRUSTED_PROXIES: ignoring invalid entry %r", entry)
    return networks


def describe_trusted_proxies(value: str | None) -> str:
    """One line for the startup banner."""
    value = (value or "").strip()
    if not value:
        return "private networks (default)"
    if value == "*":
        return "* (any client can set its own address)"
    return value


class ProxyHeaders:
    """ASGI wrapper that applies ``X-Forwarded-Proto`` and ``X-Forwarded-For``.

    ``trusted_proxies`` is the raw ``TRUSTED_PROXIES`` value.
    """

    def __init__(self, app, trusted_proxies: str | None = None):
        self.app = app
        self.setting = (trusted_proxies or "").strip() or "private"
        networks = parse_trusted_proxies(trusted_proxies)
        self.trust_all = networks is None
        self.trusted = AddressSet(networks or [])
        self.cloudflare = AddressSet(ipaddress.ip_network(n) for n in CLOUDFLARE)
        # Peers are few and repeat, so their classification is memoised.
        self.trusted_peers = {}
        self.last_warned = {}
        self.seen_proxies = set()
        self.requests = 0

    def is_trusted_peer(self, peer) -> bool:
        if peer is None:  # unix socket: only a local process can connect
            return True
        trusted = self.trusted_peers.get(peer)
        if trusted is None:
            address = parse_address(peer)
            trusted = address is not None and address in self.trusted
            if len(self.trusted_peers) >= 1024:
                self.trusted_peers.clear()
            self.trusted_peers[peer] = trusted
        return trusted

    def client_address(self, forwarded_for: bytes, peer: str | None) -> str | None:
        """The client address, or None to keep the peer address."""
        if self.trust_all:
            address = parse_address(
                forwarded_for.partition(b",")[0].decode("latin1").strip()
            )
            return address[2] if address is not None else None
        hops = forwarded_for.rsplit(b",", _MAX_HOPS)
        # Reporting only feeds the log, so a sample of requests is enough.
        self.requests += 1
        sample = self.requests % _REPORT_EVERY == 1
        walked = []
        for i in range(len(hops) - 1, -1, -1):
            address = parse_address(hops[i].decode("latin1").strip())
            if address is None:
                break
            if i == 0 or address not in self.trusted:
                if sample:
                    self.report(peer, walked, address)
                return address[2]
            walked.append(address[2])
        # A hop that isn't an address, or more trusted hops than are examined:
        # the last trusted proxy is the best remaining answer.
        if sample:
            self.report(peer, walked[:-1], None)
        return walked[-1] if walked else None

    def warn(self, key, message, *args):
        now = time.monotonic()
        if now - self.last_warned.get(key, -_WARN_INTERVAL) < _WARN_INTERVAL:
            return
        if len(self.last_warned) >= 256:
            self.last_warned.clear()
        self.last_warned[key] = now
        logger.warning(message, *args)

    def report(self, peer, proxies, client):
        """Log what the operator could set, when it is news."""
        if peer is not None:
            proxies = [peer, *proxies]
        if client is not None and client in self.cloudflare:
            self.warn(
                "cloudflare",
                "Client addresses resolve to Cloudflare (%s), so users behind "
                "it share a login rate limit. Add cloudflare to TRUSTED_PROXIES "
                "(currently %r).",
                client[2],
                self.setting,
            )
        new = [p for p in proxies if p not in self.seen_proxies]
        if new and len(self.seen_proxies) < _MAX_REPORTED_PROXIES:
            self.seen_proxies.update(new)
            logger.info(
                "Trusted proxies seen so far: %s. To trust only these, set "
                "TRUSTED_PROXIES to their addresses or ranges (currently %r).",
                ", ".join(sorted(self.seen_proxies)),
                self.setting,
            )

    def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            proto = None
            forwarded_for = []
            for name, value in scope["headers"]:
                if name == _FORWARDED_PROTO:
                    proto = value
                elif name == _FORWARDED_FOR:
                    # Proxies may add a second header rather than append.
                    forwarded_for.append(value)

            if proto is not None:
                # Chained proxies may append: the first value is the client's.
                proto = proto.partition(b",")[0].strip().lower()
                if proto in (b"http", b"https"):
                    scope["scheme"] = proto.decode()

            if forwarded_for:
                client = scope.get("client")
                peer = client[0] if client else None
                if self.trust_all or self.is_trusted_peer(peer):
                    if len(forwarded_for) > 1:
                        forwarded_for = [b",".join(forwarded_for)]
                    host = self.client_address(forwarded_for[0], peer)
                    if host is not None:
                        scope["client"] = (host, client[1] if client else 0)
                else:
                    self.warn(
                        ("peer", peer),
                        "Ignoring X-Forwarded-For from %s, which is not in "
                        "TRUSTED_PROXIES (currently %r). If it is your proxy, "
                        "add its address or range.",
                        peer,
                        self.setting,
                    )
        # Returned rather than awaited, which saves a coroutine frame per request.
        return self.app(scope, receive, send)
