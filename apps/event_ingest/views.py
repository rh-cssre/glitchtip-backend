import logging
import uuid
from dataclasses import asdict

import orjson
from django.core.cache import cache
from django.core.exceptions import RequestDataTooBig
from django.http import HttpResponse, HttpResponseForbidden, JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from ninja.errors import AuthenticationError
from ninja.errors import ValidationError as NinjaValidationError
from pydantic import ValidationError
from sentry_sdk import capture_exception, set_context, set_level

from apps.event_ingest.interfaces import IngestTaskMessage
from apps.issue_events.constants import IssueEventType
from glitchtip.api.exceptions import ThrottleException

from .api import get_ip_address
from .authentication import EventAuthHttpRequest, event_auth
from .schema import (
    SUPPORTED_ITEMS,
    EnvelopeHeaderSchema,
    IngestIssueEvent,
    ItemHeaderSchema,
    TransactionEventSchema,
)
from .tasks import ingest_event, ingest_transaction

logger = logging.getLogger(__name__)


def handle_supported_payload_error(
    message: str,
    item_header: ItemHeaderSchema,
    payload_bytes: bytes,
    e: ValidationError,
    request: EventAuthHttpRequest,
) -> None:
    set_level("warning")
    context = {"item_header": item_header.dict()}
    try:
        # Try to get a preview, limit size
        context["payload_preview"] = orjson.loads(payload_bytes[:1024])
    except orjson.JSONDecodeError:
        context["payload_preview"] = {
            "hex": payload_bytes[:100].hex()
        }  # Show hex if not JSON
    set_context("incoming event error", context)
    capture_exception(e)
    logger.warning(
        f"{message} on {request.path} for type '{item_header.type}'", exc_info=e
    )


@csrf_exempt
def event_envelope_view(request: EventAuthHttpRequest, project_id: int):
    if request.method != "POST":
        return JsonResponse({"detail": "Method not allowed"}, status=405)

    try:
        project = event_auth(request)
    except ThrottleException as e:
        response = HttpResponse("Too Many Requests", status=429)
        response["Retry-After"] = str(e.retry_after)
        return response
    except AuthenticationError:
        return JsonResponse({"detail": "Denied"}, status=403)
    except NinjaValidationError:
        return JsonResponse({"detail": "Invalid DSN"}, status=403)

    if project is None:
        # Should be caught by event_auth, but defensive check
        return JsonResponse({"detail": "Denied"}, status=403)
    request.auth = project  # Assuming event_auth returns the project object
    client_ip = get_ip_address(request)

    # Read and validate Envelope Header
    header_line = request.readline()
    if not header_line:
        return JsonResponse({"detail": "Empty request body"}, status=400)
    try:
        envelope_header = EnvelopeHeaderSchema.model_validate_json(header_line)
    except ValidationError as e:
        set_level("warning")
        capture_exception(e)
        logger.warning(
            f"Envelope Header validation error on {request.path}", exc_info=e
        )
        # Consider adding context about the invalid line if possible
        # Return 400 Bad Request for malformed envelope structure
        return JsonResponse({"detail": "Invalid envelope header"}, status=400)
    envelope_header_event_id = envelope_header.event_id

    # Loop through items
    while True:
        # Read Item Header line
        item_header_line = request.readline()
        if not item_header_line:
            break  # End of stream, normal exit

        # Validate Item Header
        try:
            item_header = ItemHeaderSchema.model_validate_json(item_header_line)
        except ValidationError as e:
            set_level("warning")
            # Log context about the invalid line itself
            set_context(
                "invalid item header line",
                {"line": item_header_line.decode(errors="replace")[:1024]},
            )
            capture_exception(e)
            logger.warning(
                f"Item Header validation error on {request.path}. Skipping rest of envelope.",
                exc_info=e,
            )
            # If an item header is invalid, it's hard to know how to recover.
            # Safest might be to stop processing this envelope.
            break  # Exit the loop

        # Read Payload (conditionally depends on type)
        payload_bytes = b""
        read_failed = False
        try:
            if item_header.length is not None and item_header.length >= 0:
                try:
                    payload_bytes = request.read(item_header.length)
                except RequestDataTooBig as e:
                    return HttpResponseForbidden(f"{e}", status=413)
                if len(payload_bytes) != item_header.length:
                    logger.warning(
                        f"Read incomplete payload for type {item_header.type}. "
                        f"Expected {item_header.length}, got {len(payload_bytes)}. Stopping."
                    )
                    read_failed = True  # Treat as read failure
                else:
                    # Consume the trailing newline after length-specified payload
                    request.readline()
            else:
                # Read newline-terminated payload (common for JSON items without length)
                payload_bytes = request.readline()
        except Exception as e:  # Catch potential read errors
            set_level("error")
            capture_exception(e)
            logger.error(
                f"Error reading payload for item type {item_header.type} on {request.path}",
                exc_info=e,
            )
            read_failed = True

        if read_failed:
            break  # Stop processing envelope on read error or incomplete read

        # Handle Payload based on Type
        if item_header.type in SUPPORTED_ITEMS:
            try:
                if item_header.type == "event":
                    item = IngestIssueEvent.model_validate_json(payload_bytes)
                    issue_type = (
                        IssueEventType.ERROR
                        if item.exception
                        else IssueEventType.DEFAULT
                    )

                    if hasattr(item, "user") and item.user:  # Check if user attr exists
                        # Assuming item.user is mutable or replace it
                        # Simplest: item.user = item.user.copy(update={'ip_address': client_ip}) if using Pydantic models properly
                        # Or if just dict: item.user['ip_address'] = client_ip
                        # Let's assume LaxIngestSchema works like a dict for now
                        if isinstance(item.user, dict):
                            item.user["ip_address"] = client_ip
                        # Else if Pydantic model: Need a way to update immutable field or ensure mutable schema
                        # item.user.ip_address = client_ip

                    # Prefer event item uuid, then enveloper header uuid, then if all else fails, generate one
                    if item.event_id is None:
                        item.event_id = envelope_header_event_id or uuid.uuid4()
                    interchange_event = IngestTaskMessage(
                        project_id=project_id,
                        organization_id=project.organization_id,
                        payload=item.dict() | {"type": issue_type},
                        received=timezone.now(),
                    )
                    if cache.add("uuid" + item.event_id.hex, True):
                        ingest_event.delay(asdict(interchange_event))

                elif item_header.type == "transaction":
                    item = TransactionEventSchema.model_validate_json(payload_bytes)
                    interchange_event = IngestTaskMessage(
                        project_id=project_id,
                        organization_id=project.organization_id,  # Use project from auth
                        payload=item.dict(),
                        received=timezone.now(),
                    )
                    if cache.add("uuid" + item.event_id.hex, True):
                        ingest_transaction.delay(asdict(interchange_event))

            except ValidationError as e:
                # Payload validation failed for a supported type. Log it.
                handle_supported_payload_error(
                    f"{item_header.type.capitalize()} Item validation error",
                    item_header,
                    payload_bytes,
                    e,
                    request,
                )
                # Continue to the next item
                continue
            except (
                Exception
            ) as e:  # Catch other processing errors (like task enqueueing?)
                set_level("error")
                capture_exception(e)
                logger.error(
                    f"Error processing supported item type {item_header.type} on {request.path}",
                    exc_info=e,
                )
                # Decide whether to continue or break, maybe continue is okay
                continue

        else:
            # Item type is IgnoredItemType or unknown.
            # The payload_bytes were already read and are now implicitly discarded.
            # No logging, no processing. Silently continue.
            pass

    # Final Response
    # Return event_id from envelope header if it exists, as it might relate
    # to the overall submission even if items have their own IDs.
    if envelope_header.event_id:
        return JsonResponse({"id": envelope_header.event_id.hex})
    return JsonResponse({})  # Success, but maybe no specific ID to return
