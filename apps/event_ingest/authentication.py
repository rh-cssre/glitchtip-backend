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
from ninja.errors import AuthenticationError, HttpError, ValidationError

from apps.organizations_ext.tasks import check_organization_throttle
from apps.projects.models import ProjectKey
from apps.shared.async_db import fetchone
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
    Get the DSN public key (sentry_key) from a request, for both the sentry
    envelope ingest and native OTLP ingest.

    Accepts, in order: a ``sentry_key``/``glitchtip_key`` query param, a plain
    ``Authorization: Bearer <key>`` (what OTLP exporters send — harmless on the
    envelope path since SDKs don't use it), or the sentry ``X-Sentry-Auth`` /
    ``Authorization`` header format. Avoids reading the request body, which
    could trigger slow decompression.
    """
    for k in request.GET.keys():
        if k in ["sentry_key", "glitchtip_key"]:
            return request.GET[k]

    authorization = request.META.get("HTTP_AUTHORIZATION", "")
    if authorization[:7].lower() == "bearer ":
        return authorization[7:].strip()

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
            return await fetchone(sql, params, db_alias="read_only")
        except OperationalError:
            pass
        except Exception as e:
            # Fail safe - don't let a read only db failure stop the request
            logger.warning("Failed to read from read_only database", exc_info=e)

    return await fetchone(sql, params, db_alias="default")


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


async def get_project_by_key(request: HttpRequest) -> ProjectAuthInfo:
    """Resolve the project from the DSN public key alone, with no project id
    in the URL.

    Native OTLP exporters point at a base endpoint (``/v1/logs``,
    ``/v1/traces``) and carry the DSN key in a header — there is no project id
    in the path. ``public_key`` is globally unique, so the key fully identifies
    the project, via a single indexed ORM lookup rather than the raw-cursor
    stored procedure that ``get_project`` uses on the hot path.

    Rejection must stay cheap: a flood of unpaid traffic (an invalid key, or a
    real key whose org is over quota) must not cost a database lookup — let
    alone reading the body — on every request. So this keeps the same block
    cache ``get_project`` does, keyed on the DSN public key (we have no project
    id here). Once a key is known-bad or known-throttled, repeat requests are
    bounced from Valkey in one round trip, before any DB hit or body read.
    """
    if settings.MAINTENANCE_EVENT_FREEZE:
        raise HttpError(
            503, "Events are not currently being accepted due to maintenance."
        )
    try:
        sentry_key = UUID(auth_from_request(request))
    except ValueError as err:
        raise AuthenticationError(
            message="dsn key badly formed hexadecimal UUID string"
        ) from err

    # Cheap-rejection cache, keyed on the public key (a key is either unknown
    # -> "v" or throttled -> "t:org:project", never both, so one slot suffices).
    block_cache_key = f"{EVENT_BLOCK_CACHE_KEY}otlp:{sentry_key}"
    cached = await cache.aget(block_cache_key)
    if cached == "v":
        raise REJECTION_MAP["v"]
    if cached and (throttle := deserialize_throttle(cached)):
        org_throttle, project_throttle = throttle
        if not is_accepting_events(org_throttle) or not is_accepting_events(
            project_throttle
        ):
            raise ThrottleException(calculate_retry_after(max(throttle)))

    try:
        key = await ProjectKey.objects.select_related("project__organization").aget(
            public_key=sentry_key, is_active=True
        )
    except ProjectKey.DoesNotExist as err:
        await cache.aset(block_cache_key, "v", REJECTION_WAIT)
        raise REJECTION_MAP["v"] from err

    project = key.project
    organization = project.organization
    info = ProjectAuthInfo(
        id=project.id,
        scrub_ip_addresses=project.scrub_ip_addresses,
        event_throttle_rate=project.event_throttle_rate,
        organization_id=organization.id,
        organization=OrganizationInfo(
            id=organization.id,
            is_accepting_events=organization.is_accepting_events,
            event_throttle_rate=organization.event_throttle_rate,
            scrub_ip_addresses=organization.scrub_ip_addresses,
        ),
        first_event=project.first_event,
    )
    org_throttle = organization.event_throttle_rate
    project_throttle = project.event_throttle_rate
    if (
        not organization.is_accepting_events
        or org_throttle == 100
        or project_throttle == 100
    ):
        # Cache as a 100% throttle so the next flood request re-blocks from
        # Valkey instead of repeating the lookup.
        await cache.aset(block_cache_key, serialize_throttle(100, 100), REJECTION_WAIT)
        raise ThrottleException(600)
    # Honor partial throttle rates the same way the envelope hot path does, so
    # a throttled org/project doesn't get a free pass on OTLP ingest.
    if org_throttle or project_throttle:
        await cache.aset(
            block_cache_key,
            serialize_throttle(org_throttle, project_throttle),
            REJECTION_WAIT,
        )
        if not is_accepting_events(org_throttle) or not is_accepting_events(
            project_throttle
        ):
            raise ThrottleException(
                calculate_retry_after(max(org_throttle, project_throttle))
            )

    # Recompute the org's quota throttle out of band, like the envelope path,
    # so an over-quota OTLP-only org gets its throttle set promptly (and then
    # cached above) rather than waiting on the periodic sweep.
    if (
        settings.BILLING_ENABLED
        and random.random() < 1 / settings.GLITCHTIP_THROTTLE_CHECK_INTERVAL
    ):
        await check_organization_throttle.aenqueue(organization.id)
    return info


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
