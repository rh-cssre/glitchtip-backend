import asyncio
import time
from datetime import timedelta
from ssl import SSLError

import aiohttp
from aiohttp import ClientTimeout
from aiohttp.client_exceptions import ClientConnectorError
from django.conf import settings
from django.utils import timezone

from .constants import MonitorCheckReason, MonitorType
from .models import MonitorCheck

DEFAULT_TIMEOUT = 20  # Seconds
PAYLOAD_LIMIT = 2_000_000  # 2mb
PAYLOAD_SAVE_LIMIT = 500_000  # pseudo 500kb


async def process_response(monitor, response):
    if response.status == monitor["expected_status"]:
        if monitor["expected_body"]:
            # Limit size to 2MB
            body = await response.content.read(PAYLOAD_LIMIT)
            encoding = response.get_encoding()
            payload = body.decode(encoding, errors="ignore")
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
        if await MonitorCheck.objects.filter(
            monitor_id=monitor["id"],
            start_check__gte=timezone.now() - monitor["interval"],
        ).aexists():
            monitor["is_up"] = True
        return monitor

    url = monitor["url"]
    timeout = ClientTimeout(total=monitor["timeout"] or DEFAULT_TIMEOUT)
    start = time.monotonic()
    try:
        if monitor["monitor_type"] == MonitorType.PING:
            async with session.head(url, timeout=timeout):
                monitor["is_up"] = True
        elif monitor["monitor_type"] == MonitorType.GET:
            async with session.get(url, timeout=timeout) as response:
                await process_response(monitor, response)
        elif monitor["monitor_type"] == MonitorType.POST:
            async with session.post(url, timeout=timeout) as response:
                await process_response(monitor, response)
        monitor["response_time"] = timedelta(seconds=time.monotonic() - start)
    except SSLError:
        monitor["reason"] = MonitorCheckReason.SSL
    except asyncio.TimeoutError:
        monitor["reason"] = MonitorCheckReason.TIMEOUT
    except ClientConnectorError:
        monitor["reason"] = MonitorCheckReason.NETWORK
    except OSError:
        monitor["reason"] = MonitorCheckReason.UNKNOWN
    return monitor


async def fetch_all(monitors):
    async with aiohttp.ClientSession(
        headers={"User-Agent": "GlitchTip/" + settings.GLITCHTIP_VERSION}
    ) as session:
        results = await asyncio.gather(
            *[fetch(session, monitor) for monitor in monitors], return_exceptions=True
        )
        return results
