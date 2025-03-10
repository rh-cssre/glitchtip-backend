import logging

import orjson
from django.core.cache import cache
from django.http import HttpResponse, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from ninja.errors import AuthenticationError
from pydantic import ValidationError
from sentry_sdk import capture_exception, set_context, set_level

from glitchtip.api.exceptions import ThrottleException

from .api import get_ip_address, get_issue_event_class
from .authentication import EventAuthHttpRequest, event_auth
from .schema import (
    EnvelopeHeaderSchema,
    IngestIssueEvent,
    InterchangeIssueEvent,
    ItemHeaderSchema,
    TransactionEventSchema,
)
from .tasks import ingest_event, ingest_transaction

logger = logging.getLogger(__name__)


def handle_validation_error(
    message: str, line: bytes, e: ValidationError, request: EventAuthHttpRequest
) -> JsonResponse:
    set_level("warning")
    try:
        set_context("incoming event", orjson.loads(line))
    except orjson.JSONDecodeError:
        pass
    capture_exception(e)
    logger.warning(f"{message} on {request.path}", exc_info=e)
    return JsonResponse({"detail": e.json()}, status=422)


@csrf_exempt
def event_envelope_view(request: EventAuthHttpRequest, project_id: int):
    """
    Envelopes can contain various types of data.
    GlitchTip supports issue events and transaction events.
    Ignore other data types.
    Do support multiple valid events
    Make as few io calls as possible. Some language SDKs (PHP) cannot run async code
    and will block while waiting for GlitchTip to respond.
    """
    if request.method != "POST":
        return JsonResponse({"detail": "Method not allowed"}, status=405)

    try:
        project = event_auth(request)
    except ThrottleException as e:
        response = HttpResponse("Too Many Requests", status=429)
        response["Retry-After"] = e.retry_after
        return response
    except AuthenticationError:
        return JsonResponse({"detail": "Denied"}, status=403)

    if project is None:
        return JsonResponse({"detail": "Denied"}, status=403)
    request.auth = project
    client_ip = get_ip_address(request)

    line = request.readline()
    try:
        header = EnvelopeHeaderSchema.model_validate_json(line)
    except ValidationError as e:
        return handle_validation_error(
            "Envelope Header validation error", line, e, request
        )
    for line in request:
        try:
            item_header = ItemHeaderSchema.model_validate_json(line)
        except ValidationError as e:
            handle_validation_error("Item Header validation error", line, e, request)
            request.readline()  # Skip line
            continue
        line = request.readline()
        if item_header.type == "event":
            try:
                item = IngestIssueEvent.model_validate_json(line)
            except ValidationError as e:
                handle_validation_error("Event Item validation error", line, e, request)
                continue
            issue_event_class = get_issue_event_class(item)
            if item.user:
                item.user.ip_address = client_ip
            interchange_event_kwargs = {
                "project_id": project_id,
                "organization_id": project.organization_id,
                "payload": issue_event_class(**item.dict()),
            }
            if header.event_id:
                interchange_event_kwargs["event_id"] = header.event_id
            interchange_event = InterchangeIssueEvent(**interchange_event_kwargs)
            # Faux unique uuid as GlitchTip can accept duplicate UUIDs
            # The primary key of an event is uuid, received
            if cache.add("uuid" + interchange_event.event_id.hex, True) is True:
                ingest_event.delay(interchange_event.dict())
        elif item_header.type == "transaction":
            try:
                item = TransactionEventSchema.model_validate_json(line)
            except ValidationError as e:
                handle_validation_error(
                    "Transaction Item validation error", line, e, request
                )
                continue
            interchange_event_kwargs = {
                "project_id": project_id,
                "organization_id": request.auth.organization_id,
                "payload": TransactionEventSchema(**item.dict()),
            }
            interchange_event = InterchangeIssueEvent(**interchange_event_kwargs)
            if cache.add("uuid" + interchange_event.event_id.hex, True) is True:
                ingest_transaction.delay(interchange_event.dict())

    if header.event_id:
        return JsonResponse({"id": header.event_id.hex})
    return JsonResponse({})
