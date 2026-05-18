import logging
import math
import random
from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from uuid import UUID

from django.conf import settings
from django.core.cache import cache
from django.db.utils import OperationalError
from django.http import HttpRequest
from django_async_backend.db import async_connections
from ninja.errors import AuthenticationError, HttpError, ValidationError

from apps.organizations_ext.tasks import check_organization_throttle
from glitchtip.api.exceptions import ThrottleException
from sentry.utils.auth import parse_auth_header

from .constants import EVENT_BLOCK_CACHE_KEY

logger = logging.getLogger(__name__)


@dataclass
class OrganizationInfo:
    id: int
    is_accepting_events: bool
    event_throttle_rate: int
    scrub_ip_addresses: bool


@dataclass
class ProjectAuthInfo:
    id: int
    scrub_ip_addresses: bool
    event_throttle_rate: int
    organization_id: int
    first_event: datetime | None
    organization: OrganizationInfo

    @property
    def should_scrub_ip_addresses(self):
        """Organization overrides project setting"""
        return self.scrub_ip_addresses or self.organization.scrub_ip_addresses


class EventAuthHttpRequest(HttpRequest):
    """Django HttpRequest that is known to be authenticated by a project DSN"""

    auth: ProjectAuthInfo


def auth_from_request(request: HttpRequest):
    """
    Get DSN (sentry_key) from request header
    Accept both sentry or glitchtip prefix
    Do not read request body when possible. This may result in uncompression which is slow.
    """
    for k in request.GET.keys():
        if k in ["sentry_key", "glitchtip_key"]:
            return request.GET[k]

    if auth_header := request.META.get(
        "HTTP_X_SENTRY_AUTH", request.META.get("HTTP_AUTHORIZATION")
    ):
        result = parse_auth_header(auth_header)
        return result.get("sentry_key", result.get("glitchtip_key"))

    raise AuthenticationError(message="Unable to find authentication information")


# One letter codes to save cache memory and map to various event rejection type exceptions
REJECTION_MAP: dict[Literal["v", "t"], Exception] = {
    "v": AuthenticationError(message="Invalid DSN"),
    "t": ThrottleException(),
}
REJECTION_WAIT = 30


def serialize_throttle(org_throttle: int, project_throttle: int) -> str:
    """
    Format example "t:30:0" means throttle with 30% org throttle and 0% (disabled)
    project throttle
    """
    return f"t:{org_throttle}:{project_throttle}"


def deserialize_throttle(input: str) -> None | tuple[int, int]:
    """Return (org_throttle, project_throttle) as integer %"""
    if input == "t":
        return 0, 0
    if input.startswith("t:"):
        parts = input.split(":", 2)
        if len(parts) == 3:
            return int(parts[1]), int(parts[2])
    return None


def is_accepting_events(throttle_rate: int) -> bool:
    """Consider throttle to determine if event are being accepted"""
    if throttle_rate == 0:
        return True
    return random.randint(0, 100) > throttle_rate


def calculate_retry_after(throttle: int):
    """Calculates Retry-After using a power function."""
    return math.ceil(0.02 * throttle**2.3)


async def get_project_auth_info_row(project_id: int, sentry_key: UUID):
    # async-backend's AsyncCursor doesn't implement callproc; emulate
    # the same call via SELECT * FROM proc(...).
    sql = "SELECT * FROM get_project_auth_info(%s, %s)"
    params = [project_id, sentry_key]

    if "read_only" in settings.DATABASES:
        try:
            async with await async_connections["read_only"].cursor() as cursor:
                await cursor.execute(sql, params)
                return await cursor.fetchone()
        except OperationalError:
            pass
        except Exception as e:
            # Fail safe - don't let a read only db failure stop the request
            logger.warning("Failed to read from read_only database", exc_info=e)

    async with await async_connections["default"].cursor() as cursor:
        await cursor.execute(sql, params)
        return await cursor.fetchone()


async def get_project(request: HttpRequest) -> ProjectAuthInfo | None:
    """
    Return the valid and accepting events project based on a request.

    Throttle unwanted requests using cache to mitigate repeat attempts
    """
    if not request.resolver_match:
        raise ValidationError([{"message": "Invalid project ID"}])
    project_id: int = request.resolver_match.captured_kwargs.get("project_id")
    try:
        sentry_key = UUID(auth_from_request(request))
    except ValueError as err:
        raise ValidationError(
            [{"message": "dsn key badly formed hexadecimal UUID string"}]
        ) from err

    # Block cache check should be right before database call. Fetched in one
    # MGET to keep the hot path at one Valkey round-trip.
    # Throttle ("t") state is project-scoped: it applies regardless of which
    # valid DSN is used. Invalid-DSN ("v") blocks are scoped by (project, key)
    # so one bad key cannot lock out a project's legitimate DSNs.
    block_cache_key = EVENT_BLOCK_CACHE_KEY + str(project_id)
    dsn_block_cache_key = f"{block_cache_key}:{sentry_key}"
    cached = await cache.aget_many([block_cache_key, dsn_block_cache_key])
    if block_value := cached.get(block_cache_key):
        if block_value.startswith("t"):
            if throttle := deserialize_throttle(block_value):
                org_throttle, project_throttle = throttle
                if not is_accepting_events(org_throttle) or not is_accepting_events(
                    project_throttle
                ):
                    raise ThrottleException(calculate_retry_after(max(throttle)))
    if cached.get(dsn_block_cache_key) == "v":
        raise REJECTION_MAP["v"]

    row = await get_project_auth_info_row(project_id, sentry_key)

    if not row:
        await cache.aset(dsn_block_cache_key, "v", REJECTION_WAIT)
        raise REJECTION_MAP["v"]

    project = ProjectAuthInfo(
        id=row[0],
        scrub_ip_addresses=row[1],
        event_throttle_rate=row[2],
        organization_id=row[3],
        organization=OrganizationInfo(
            id=row[3],
            is_accepting_events=row[4],
            event_throttle_rate=row[5],
            scrub_ip_addresses=row[6],
        ),
        first_event=row[7],
    )

    if (
        not project.organization.is_accepting_events
        or project.organization.event_throttle_rate == 100
        or project.event_throttle_rate == 100
    ):
        await cache.aset(block_cache_key, "t", REJECTION_WAIT)
        raise ThrottleException(600)
    if project.organization.event_throttle_rate or project.event_throttle_rate:
        await cache.aset(
            block_cache_key,
            serialize_throttle(
                project.organization.event_throttle_rate,
                project.event_throttle_rate,
            ),
            REJECTION_WAIT,
        )
        if not is_accepting_events(
            project.organization.event_throttle_rate
        ) or not is_accepting_events(project.event_throttle_rate):
            raise ThrottleException(
                calculate_retry_after(
                    max(
                        project.organization.event_throttle_rate,
                        project.event_throttle_rate,
                    )
                )
            )

    # Check throttle needs every 1 out of X requests
    if (
        settings.BILLING_ENABLED
        and random.random() < 1 / settings.GLITCHTIP_THROTTLE_CHECK_INTERVAL
    ):
        await check_organization_throttle.aenqueue(project.organization_id)
    return project


async def event_auth(request: HttpRequest) -> ProjectAuthInfo | None:
    """
    Event Ingest authentication means validating the DSN (sentry_key).
    Throttling is also handled here.
    It does not include user authentication.
    """
    if settings.MAINTENANCE_EVENT_FREEZE:
        raise HttpError(
            503, "Events are not currently being accepted due to maintenance."
        )
    return await get_project(request)
