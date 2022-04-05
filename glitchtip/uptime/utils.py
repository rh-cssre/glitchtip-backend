import asyncio
import time
from datetime import timedelta
from ssl import SSLError

import aiohttp
from aiohttp.client_exceptions import ClientConnectorError
from django.conf import settings

from .constants import MonitorCheckReason, MonitorType

DEFAULT_TIMEOUT = 30
DEFAULT_PING_TIMEOUT = 30
DEFAULT_AIOHTTP_TIMEOUT = aiohttp.ClientTimeout(total=DEFAULT_TIMEOUT)
PING_AIOHTTP_TIMEOUT = aiohttp.ClientTimeout(total=DEFAULT_PING_TIMEOUT)


async def process_response(monitor, response):
    if response.status == monitor["expected_status"]:
        if monitor["expected_body"]:
            if monitor["expected_body"] in await response.text():
                monitor["is_up"] = True
            else:
                monitor["reason"] = MonitorCheckReason.BODY
        else:
            monitor["is_up"] = True
    else:
        monitor["reason"] = MonitorCheckReason.STATUS


async def fetch(session, monitor):
    url = monitor["url"]
    monitor["is_up"] = False
    start = time.monotonic()
    try:
        if monitor["monitor_type"] == MonitorType.PING:
            async with session.head(url, timeout=PING_AIOHTTP_TIMEOUT):
                monitor["is_up"] = True
        elif monitor["monitor_type"] == MonitorType.GET:
            async with session.get(url, timeout=DEFAULT_AIOHTTP_TIMEOUT) as response:
                await process_response(monitor, response)
        elif monitor["monitor_type"] == MonitorType.POST:
            async with session.post(url, timeout=DEFAULT_AIOHTTP_TIMEOUT) as response:
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


async def fetch_all(monitors, loop):
    async with aiohttp.ClientSession(loop=loop, headers={"User-Agent": "GlitchTip/" + settings.GLITCHTIP_VERSION}) as session:
        results = await asyncio.gather(
            *[fetch(session, monitor) for monitor in monitors], return_exceptions=True
        )
        return results
