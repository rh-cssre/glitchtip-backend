import uuid
from urllib.parse import urlparse

from django.http import HttpRequest
from ninja import NinjaAPI, Schema
from ninja.errors import AuthenticationError, HttpError, ValidationError

from projects.models import Project

from .authentication import event_auth, get_project
from .data_models import EnvelopeSchema, EventIngestSchema
from .exceptions import ThrottleException
from .parsers import EnvelopeParser
from .renderers import ORJSONRenderer
from .tasks import ingest_event

api = NinjaAPI(parser=EnvelopeParser(), renderer=ORJSONRenderer())


@api.exception_handler(ThrottleException)
def throttled(request: HttpRequest, exc: ThrottleException):
    response = api.create_response(
        request,
        {"message": "Please retry later"},
        status=429,
    )
    if retry_after := exc.retry_after:
        if isinstance(retry_after, int):
            response["Retry-After"] = retry_after
        else:
            response["Retry-After"] = retry_after.strftime("%a, %d %b %Y %H:%M:%S GMT")

    return response


class EventIngestOut(Schema):
    event_id: str


def check_status():
    if settings.EVENT_STORE_DEBUG:
        print(json.dumps(self.request.data))


@api.post("/{project_id}/store/", response=EventIngestOut)
async def event_store(
    request: HttpRequest,
    payload: EventIngestSchema,
    project_id: int,
):
    check_status()
    project = await get_project(request)
    res = ingest_event.delay(project.id, payload.dict())
    return {"event_id": payload.event_id.hex}


@api.post("/{project_id}/envelope/", response=EventIngestOut, auth=event_auth)
async def event_envelope(
    request: HttpRequest,
    payload: EnvelopeSchema,
    project_id: int,
):
    check_status()

    return {"event_id": "aaaaaaaaaaa"}


@api.post("/{project_id}/security/", response=EventIngestOut)
async def event_security(
    request: HttpRequest,
    payload: EventIngestSchema,
    project_id: int,
):
    check_status()
