import asyncio
import ipaddress
import logging
import time
from datetime import timedelta
from ssl import SSLError
from urllib.parse import urlparse

import aiohttp
from aiohttp import ClientTimeout
from aiohttp.client_exceptions import (
    ClientConnectorError,
    ClientResponseError,
    ServerConnectionError,
)
from django.conf import settings
from django.utils import timezone

from glitchtip.partition_manager import UUID7Helper

from .constants import MonitorCheckReason, MonitorType
from .models import MonitorCheck

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 20  # Seconds
PAYLOAD_LIMIT = 2_000_000  # 2mb
PAYLOAD_SAVE_LIMIT = 500_000  # pseudo 500kb


def is_ip_blocked(ip_str: str) -> bool:
    """
    Return True if IP is private, loopback, link-local, or reserved.

    Raises ValueError if ip_str is not a valid IP address.
    """
    ip = ipaddress.ip_address(ip_str)
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
    )


async def check_url_safe(url: str) -> bool:
    """
    Resolve hostname and verify it doesn't point to a private/internal IP.

    Returns True if the URL is safe to fetch, False if it targets a blocked IP.
    Always returns True when GLITCHTIP_UPTIME_ALLOW_PRIVATE_IPS is enabled.
    """
    if settings.GLITCHTIP_UPTIME_ALLOW_PRIVATE_IPS:
        return True

    try:
        # Handle both full URLs and host:port format (PORT monitors)
        if "://" in url:
            parsed = urlparse(url)
            hostname = parsed.hostname
        else:
            hostname = url.split(":")[0]

        if not hostname:
            return False

        # Check if hostname is already an IP literal
        try:
            if is_ip_blocked(hostname):
                return False
            return True  # Valid IP that is not blocked
        except ValueError:
            pass  # Not an IP literal, proceed to DNS resolution

        # Resolve hostname to IPs
        loop = asyncio.get_event_loop()
        infos = await loop.getaddrinfo(hostname, None)
        for family, type_, proto, canonname, sockaddr in infos:
            if is_ip_blocked(sockaddr[0]):
                return False
    except OSError:
        # DNS resolution failure is not an SSRF concern — the fetch will
        # also fail with a proper network error.
        pass
    return True


async def process_response(monitor, response):
    if response.status == monitor["expected_status"]:
        if monitor["expected_body"]:
            # Limit size to 2MB
            body = await response.content.read(PAYLOAD_LIMIT)
            try:
                encoding = response.get_encoding()
                payload = body.decode(encoding, errors="ignore")
            except RuntimeError:
                payload = body.decode(errors="ignore")
            if monitor["expected_body"] in payload:
                monitor["is_up"] = True
            else:
                monitor["reason"] = MonitorCheckReason.BODY
                if monitor["latest_is_up"] != monitor["is_up"]:
                    # Save only first 500k chars, to roughly reduce disk usage
                    # Note that a unicode char is not always one byte
                    # Only save on changes
                    monitor["data"] = {"payload": payload[:PAYLOAD_SAVE_LIMIT]}
        else:
            monitor["is_up"] = True
    else:
        monitor["reason"] = MonitorCheckReason.STATUS


async def fetch(session, monitor):
    monitor["is_up"] = False
    if monitor["monitor_type"] == MonitorType.HEARTBEAT:
        interval = timedelta(seconds=monitor["interval"])
        since = timezone.now() - interval
        # Partition-aware: organization_id prunes hash sub-partitions,
        # id__gte prunes RANGE (UUIDv7) partitions to the interval window.
        # Without both, Postgres locks every partition and exhausts
        # max_locks_per_transaction.
        if await MonitorCheck.objects.filter(
            organization_id=monitor["organization_id"],
            monitor_id=monitor["id"],
            id__gte=UUID7Helper._uuid7_for_timestamp(since, min_random=True),
            start_check__gte=since,
        ).aexists():
            monitor["is_up"] = True
        return monitor

    url = monitor["url"]
    timeout = monitor["timeout"] or DEFAULT_TIMEOUT

    if not await check_url_safe(url):
        logger.warning(
            "Monitor %s blocked: URL targets a private/reserved IP", monitor["id"]
        )
        monitor["reason"] = MonitorCheckReason.NETWORK
        return monitor

    try:
        start = time.monotonic()
        if monitor["monitor_type"] == MonitorType.PORT:
            fut = asyncio.open_connection(*url.split(":"))
            await asyncio.wait_for(fut, timeout=timeout)
            monitor["is_up"] = True
        else:
            client_timeout = ClientTimeout(total=monitor["timeout"] or DEFAULT_TIMEOUT)
            if monitor["monitor_type"] == MonitorType.PING:
                async with session.head(url, timeout=client_timeout):
                    monitor["is_up"] = True
            elif monitor["monitor_type"] == MonitorType.GET:
                async with session.get(url, timeout=client_timeout) as response:
                    await process_response(monitor, response)
            elif monitor["monitor_type"] == MonitorType.POST:
                async with session.post(url, timeout=client_timeout) as response:
                    await process_response(monitor, response)
        monitor["response_time"] = (
            timedelta(seconds=time.monotonic() - start).total_seconds() * 1000
        )
    except SSLError:
        monitor["reason"] = MonitorCheckReason.SSL
    except asyncio.TimeoutError:
        monitor["reason"] = MonitorCheckReason.TIMEOUT
    except (ClientConnectorError, ServerConnectionError):
        monitor["reason"] = MonitorCheckReason.NETWORK
    except (OSError, ClientResponseError) as e:
        logger.warning(f"Monitor {monitor['id']} check failed", exc_info=e)
        monitor["reason"] = MonitorCheckReason.UNKNOWN
    except Exception as e:
        logger.error(f"Monitor {monitor['id']} check failed", exc_info=e)
        monitor["reason"] = MonitorCheckReason.UNKNOWN
    return monitor


async def fetch_all(monitors):
    async with aiohttp.ClientSession(**settings.AIOHTTP_CONFIG) as session:
        results = await asyncio.gather(
            *[fetch(session, monitor) for monitor in monitors], return_exceptions=True
        )
        return results
