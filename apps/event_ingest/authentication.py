from typing import Literal, Optional
from uuid import UUID

from django.conf import settings
from django.core.cache import cache
from django.http import HttpRequest
from ninja.errors import AuthenticationError, HttpError, ValidationError

from apps.projects.models import Project
from glitchtip.api.exceptions import ThrottleException
from sentry.utils.auth import parse_auth_header

from .constants import EVENT_BLOCK_CACHE_KEY


class EventAuthHttpRequest(HttpRequest):
    """Django HttpRequest that is known to be authenticated by a project DSN"""

    auth: Project


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

    raise AuthenticationError("Unable to find authentication information")


# One letter codes to save cache memory and map to various event rejection type exceptions
REJECTION_MAP: dict[Literal["v", "t"], Exception] = {
    "v": AuthenticationError([{"message": "Invalid DSN"}]),
    "t": ThrottleException(),
}
REJECTION_WAIT = 30


async def get_project(request: HttpRequest) -> Optional[Project]:
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

    # block cache check should be right before database call
    block_cache_key = EVENT_BLOCK_CACHE_KEY + str(project_id)
    if block_value := cache.get(block_cache_key):
        # Repeat the original message until cache expires
        raise REJECTION_MAP[block_value]

    project = (
        await Project.objects.filter(
            id=project_id,
            projectkey__public_key=sentry_key,
        )
        .select_related("organization")
        .only(
            "id",
            "scrub_ip_addresses",
            "organization_id",
            "organization__is_accepting_events",
            "organization__scrub_ip_addresses",
            "event_throttle_rate",
        )
        .afirst()
    )
    if not project:
        cache.set(block_cache_key, "v", REJECTION_WAIT)
        raise REJECTION_MAP["v"]
    if not project.organization.is_accepting_events:
        cache.set(block_cache_key, "t", REJECTION_WAIT)
        raise REJECTION_MAP["t"]
    if not project.is_accepting_events:
        raise REJECTION_MAP["t"]
    return project


async def event_auth(request: HttpRequest) -> Optional[Project]:
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
