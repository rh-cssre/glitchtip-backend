import uuid
from django.conf import settings
from django.http import HttpRequest
from urllib.parse import urlparse
from ninja.errors import HttpError, ValidationError, AuthenticationError
from ninja import NinjaAPI, Schema

from projects.models import Project
from sentry.utils.auth import parse_auth_header
from .data_models import EventIngestSchema, EnvelopeSchema
from .tasks import ingest_event
from .parsers import EnvelopeParser

api = NinjaAPI(parser=EnvelopeParser())


class EventIngestOut(Schema):
    event_id: str


def auth_from_request(request: HttpRequest, payload: EventIngestSchema):
    # Accept both sentry or glitchtip prefix.
    for k in request.GET.keys():
        if k in ["sentry_key", "glitchtip_key"]:
            return request.GET[k]

    if auth_header := request.META.get(
        "HTTP_X_SENTRY_AUTH", request.META.get("HTTP_AUTHORIZATION")
    ):
        result = parse_auth_header(auth_header)
        return result.get("sentry_key", result.get("glitchtip_key"))

    # if isinstance(request.data, list):
    #     if data_first := next(iter(request.data), None):
    #         if isinstance(data_first, dict):
    #             dsn = urlparse(data_first.get("dsn"))
    #             if username := dsn.username:
    #                 return username
    raise AuthenticationError("Unable to find authentication information")


def check_status():
    if settings.MAINTENANCE_EVENT_FREEZE:
        raise HttpError(
            503, "Events are not currently being accepted due to maintenance."
        )
    if settings.EVENT_STORE_DEBUG:
        print(json.dumps(self.request.data))


async def get_project(
    request: HttpRequest, project_id: int, payload: EventIngestSchema
):
    sentry_key = auth_from_request(request, payload)
    project = (
        await Project.objects.filter(
            id=project_id,
            projectkey__public_key=sentry_key,
        )
        .select_related("organization")
        .only(
            "id",
            "organization__is_accepting_events",
        )
        .afirst()
    )
    if not project:
        raise ValidationError([{"message": "Invalid DSN"}])
    if not project.organization.is_accepting_events:
        raise HttpError(429, "event rejected due to rate limit")
    return project


@api.post("/{project_id}/store/", response=EventIngestOut)
async def event_store(
    request: HttpRequest,
    payload: EventIngestSchema,
    project_id: int,
):
    check_status()
    project = await get_project(request, project_id, payload)
    res = ingest_event.delay(project.id, payload.dict())
    return {"event_id": payload.event_id.hex}


@api.post("/{project_id}/envelope/", response=EventIngestOut)
async def event_envelope(
    request: HttpRequest,
    payload: EnvelopeSchema,
    project_id: int,
):
    check_status()


@api.post("/{project_id}/security/", response=EventIngestOut)
async def event_security(
    request: HttpRequest,
    payload: EventIngestSchema,
    project_id: int,
):
    check_status()
