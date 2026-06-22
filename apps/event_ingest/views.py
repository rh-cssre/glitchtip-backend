import io
import logging
import uuid
from dataclasses import asdict

import orjson
import sentry_sdk
from asgiref.sync import sync_to_async
from django.core.cache import cache
from django.core.exceptions import RequestDataTooBig
from django.http import HttpResponse, HttpResponseForbidden, JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from ninja.errors import AuthenticationError
from ninja.errors import ValidationError as NinjaValidationError
from pydantic import ValidationError
from sentry_sdk import capture_exception, set_context, set_level

from apps.event_ingest.interfaces import IngestTaskMessage, LogIngestTaskMessage
from apps.issue_events.constants import IssueEventType
from apps.logs.tasks import ingest_logs
from glitchtip.api.exceptions import ThrottleException
from glitchtip.partition_manager import UUID7Helper

from .api import get_ip_address
from .authentication import EventAuthHttpRequest, event_auth
from .minidump_event import minidump_to_event
from .rust_envelope import (
    EnvelopeTooBig,
    decompress_body,
    envelope_error_response,
    frame_envelope,
    request_content_encoding,
)
from .schema import (
    SUPPORTED_ITEMS,
    EnvelopeHeaderSchema,
    EventUser,
    FeedbackPayload,
    ItemHeaderSchema,
    LogEnvelopePayload,
    LogItemSchema,
    TransactionEventSchema,
    UserReportPayload,
    UserReportTaskMessage,
    WebIngestIssueEvent,
    otel_log_to_log_item,
)
from .tasks import ingest_event, ingest_transaction, ingest_user_report
from .utils import serialize_for_vtasks

logger = logging.getLogger(__name__)

MINIDUMP_MAGIC = b"MDMP"


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
async def event_envelope_view(request: EventAuthHttpRequest, project_id: int):
    if request.method != "POST":
        return JsonResponse({"detail": "Method not allowed"}, status=405)

    try:
        project = await event_auth(request)
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
    update_first_event = project.first_event is None
    client_ip = get_ip_address(request)

    content_encoding = request_content_encoding(request)
    try:
        body = await sync_to_async(lambda: request.body)()
    except RequestDataTooBig as e:
        return HttpResponseForbidden(f"{e}", status=413)

    # Decompress (if Content-Encoded) and frame the envelope in Rust: gt_rust
    # takes the raw body plus its Content-Encoding and returns the lifted header
    # fields plus the item list, enforcing the decompressed-size cap in its own
    # allocator. The body is never decompressed in Python.
    try:
        envelope = frame_envelope(body, content_encoding)
    except (ValueError, EnvelopeTooBig) as e:
        response = envelope_error_response(e)
        if response is not None:
            return response
        raise

    # Validate Envelope Header
    try:
        envelope_header = EnvelopeHeaderSchema.model_validate_json(envelope.header)
    except ValidationError as e:
        set_level("warning")
        capture_exception(e)
        logger.warning(
            f"Envelope Header validation error on {request.path}", exc_info=e
        )
        # Return 400 Bad Request for malformed envelope structure
        return JsonResponse({"detail": "Invalid envelope header"}, status=400)
    envelope_header_event_id = envelope_header.event_id

    # Track minidump attachment for SDK-based minidump submissions.
    # If the envelope already contains a processed event or transaction,
    # skip the minidump fallback (the SDK already sent a rich event).
    minidump_bytes: bytes | None = None
    event_processed = False

    # Loop through items. gt_rust has already split each item into its header
    # line and verbatim payload bytes (length- or newline-delimited) and stopped
    # at the first non-JSON item header, so this loop does no framing or
    # short-read handling — just validate the header and dispatch on type.
    for envelope_item in envelope.items:
        item_header_line = envelope_item.header
        payload_bytes = envelope_item.payload

        # Validate Item Header
        try:
            item_header = ItemHeaderSchema.model_validate_json(item_header_line)
        except ValidationError as e:
            errors = e.errors()
            if any(err["type"] == "json_invalid" for err in errors):
                # Not valid JSON — corrupted/binary data, typically
                # from a tunnel proxy mangling binary envelope payloads.
                break
            # An unknown item type (a `literal_error` on the `type` field) is
            # a valid header for a type that is in neither the supported nor
            # the ignored list — e.g. the SDK spec added something new. Give
            # each distinct type its own fingerprint so a genuinely-new type
            # surfaces as its own issue instead of folding every unknown type
            # into one. Other schema failures (e.g. a malformed `length`) keep
            # the generic grouping so they aren't merged under a type.
            type_error = next(
                (
                    err
                    for err in errors
                    if err["type"] == "literal_error" and err["loc"][:1] == ("type",)
                ),
                None,
            )
            if type_error is not None:
                item_type = type_error.get("input")
                if not isinstance(item_type, str):
                    try:
                        item_type = orjson.loads(item_header_line).get("type")
                    except orjson.JSONDecodeError:
                        item_type = None
                    if not isinstance(item_type, str):
                        item_type = "unknown"
                with sentry_sdk.new_scope() as scope:
                    scope.level = "warning"
                    scope.fingerprint = [
                        "envelope-unsupported-item-type",
                        item_type,
                    ]
                    scope.set_tag("envelope_item_type", item_type)
                    scope.set_context(
                        "invalid item header",
                        {"line": item_header_line.decode(errors="replace")[:1024]},
                    )
                    sentry_sdk.capture_exception(e)
                break
            # Valid JSON that our schema doesn't understand — worth
            # knowing about in case the SDK spec evolved.
            set_level("warning")
            set_context(
                "invalid item header",
                {"line": item_header_line.decode(errors="replace")[:1024]},
            )
            capture_exception(e)
            break

        # Handle Payload based on Type
        if item_header.type in SUPPORTED_ITEMS:
            try:
                if item_header.type == "event":
                    # Heavy validation and normalization happens here
                    item = WebIngestIssueEvent.model_validate_json(payload_bytes)
                    issue_type = (
                        IssueEventType.ERROR
                        if item.exception
                        else IssueEventType.DEFAULT
                    )

                    if client_ip:
                        if item.user:
                            item.user.ip_address = client_ip
                        else:
                            item.user = EventUser(ip_address=client_ip)

                    # Prefer event item uuid, then enveloper header uuid, then if all else fails, generate one
                    if item.event_id is None:
                        item.event_id = envelope_header_event_id or uuid.uuid4()

                    primary_id = UUID7Helper.from_datetime()
                    interchange_event = IngestTaskMessage(
                        project_id=project_id,
                        organization_id=project.organization_id,
                        payload=item.dict() | {"type": issue_type},
                        received=timezone.now(),
                        update_first_event=update_first_event,
                        uuid=primary_id.hex,
                    )
                    if await cache.aadd("uuid" + item.event_id.hex, True):
                        await ingest_event.aenqueue(
                            serialize_for_vtasks(asdict(interchange_event))
                        )
                    event_processed = True

                elif item_header.type == "transaction":
                    item = TransactionEventSchema.model_validate_json(payload_bytes)
                    primary_id = UUID7Helper.from_datetime()
                    interchange_event = IngestTaskMessage(
                        project_id=project_id,
                        organization_id=project.organization_id,  # Use project from auth
                        payload=item.dict(),
                        received=timezone.now(),
                        update_first_event=update_first_event,
                        uuid=primary_id.hex,
                    )
                    if await cache.aadd("uuid" + item.event_id.hex, True):
                        await ingest_transaction.aenqueue(
                            serialize_for_vtasks(asdict(interchange_event))
                        )
                    event_processed = True

                elif item_header.type in ("user_report", "feedback"):
                    if item_header.type == "feedback":
                        item = FeedbackPayload.model_validate_json(payload_bytes)
                    else:
                        item = UserReportPayload.model_validate_json(payload_bytes)
                    report_data = item.to_user_report_data()
                    msg = UserReportTaskMessage(
                        project_id=project_id,
                        organization_id=project.organization_id,
                        **report_data,
                    )
                    await ingest_user_report.aenqueue(
                        serialize_for_vtasks(msg.model_dump())
                    )

                elif item_header.type in ("log", "otel_log"):
                    # Check if logs feature is enabled
                    from django.conf import settings

                    if not settings.GLITCHTIP_ENABLE_LOGS:
                        # Silently ignore logs when feature is disabled
                        continue

                    if item_header.type == "otel_log":
                        # OTel log: single record per item, OTel data model format
                        otel_record = orjson.loads(payload_bytes)
                        converted = otel_log_to_log_item(otel_record)
                        log_items = [LogItemSchema(**converted)]
                    else:
                        # sentry-sdk log: multiple items in {"items": [...]} wrapper
                        log_payload = LogEnvelopePayload.model_validate_json(
                            payload_bytes
                        )
                        log_items = log_payload.items

                    log_message = LogIngestTaskMessage(
                        project_id=project_id,
                        organization_id=project.organization_id,
                        received=timezone.now(),
                        logs=[log_item.dict() for log_item in log_items],
                    )
                    await ingest_logs.aenqueue(
                        serialize_for_vtasks(asdict(log_message))
                    )

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
            if (
                item_header.type == "attachment"
                and item_header.attachment_type == "event.minidump"
                and len(payload_bytes) >= 4
                and payload_bytes[:4] == MINIDUMP_MAGIC
            ):
                minidump_bytes = payload_bytes

    # If we got a minidump attachment but no event was processed from the
    # envelope, parse the minidump into a full event and enqueue it.
    if minidump_bytes and not event_processed:
        try:
            event_data = await sync_to_async(minidump_to_event)(minidump_bytes)
            item = WebIngestIssueEvent.model_validate(event_data)
            if item.event_id is None:
                item.event_id = envelope_header_event_id or uuid.uuid4()
            issue_type = (
                IssueEventType.ERROR if item.exception else IssueEventType.DEFAULT
            )
            primary_id = UUID7Helper.from_datetime()
            interchange_event = IngestTaskMessage(
                project_id=project_id,
                organization_id=project.organization_id,
                payload=item.dict() | {"type": issue_type},
                received=timezone.now(),
                update_first_event=update_first_event,
                uuid=primary_id.hex,
            )
            if await cache.aadd("uuid" + item.event_id.hex, True):
                await ingest_event.aenqueue(
                    serialize_for_vtasks(asdict(interchange_event))
                )
        except Exception as e:
            capture_exception(e)
            logger.error(
                "Failed to process minidump attachment on %s",
                request.path,
                exc_info=e,
            )

    # Final Response
    # Return event_id from envelope header if it exists, as it might relate
    # to the overall submission even if items have their own IDs.
    if envelope_header.event_id:
        return JsonResponse({"id": envelope_header.event_id.hex})
    return JsonResponse({})  # Success, but maybe no specific ID to return


@csrf_exempt
async def minidump_view(request: EventAuthHttpRequest, project_id: int):
    """Accept Crashpad/Breakpad minidump uploads.

    POST /api/<project_id>/minidump/?sentry_key=<public_key>
    Content-Type: multipart/form-data

    Fields:
        upload_file_minidump: The binary minidump file
        sentry: Optional JSON with release, environment, tags
    """
    if request.method != "POST":
        return JsonResponse({"detail": "Method not allowed"}, status=405)

    try:
        project = await event_auth(request)
    except ThrottleException as e:
        response = HttpResponse("Too Many Requests", status=429)
        response["Retry-After"] = str(e.retry_after)
        return response
    except AuthenticationError:
        return JsonResponse({"detail": "Denied"}, status=403)
    except NinjaValidationError:
        return JsonResponse({"detail": "Invalid DSN"}, status=403)

    if project is None:
        return JsonResponse({"detail": "Denied"}, status=403)

    update_first_event = project.first_event is None

    # A Content-Encoded multipart upload arrives still compressed (nothing
    # decompresses the body upstream of this view). Decompress it in Rust and
    # swap in a plain stream before Django parses request.FILES. (Minidump
    # uploaders rarely set Content-Encoding, so this is usually skipped.)
    content_encoding = request_content_encoding(request)
    if content_encoding:
        try:
            compressed = await sync_to_async(request._stream.read)()
            decompressed = decompress_body(compressed, content_encoding)
        except EnvelopeTooBig as e:
            return HttpResponse(str(e), status=413)
        except ValueError:
            return JsonResponse({"detail": "Invalid compressed body"}, status=400)
        request._stream = io.BytesIO(decompressed)
        request.META["CONTENT_LENGTH"] = str(len(decompressed))
        request.META.pop("HTTP_CONTENT_ENCODING", None)

    # Extract multipart fields
    upload_file = request.FILES.get("upload_file_minidump")
    if not upload_file:
        return JsonResponse({"detail": "Missing upload_file_minidump"}, status=400)

    minidump_data = upload_file.read()
    if len(minidump_data) < 4 or minidump_data[:4] != MINIDUMP_MAGIC:
        return JsonResponse({"detail": "Invalid minidump file"}, status=400)

    # Parse optional sentry metadata
    sentry_meta = {}
    sentry_raw = request.POST.get("sentry")
    if sentry_raw:
        try:
            sentry_meta = orjson.loads(sentry_raw)
        except orjson.JSONDecodeError:
            pass  # Ignore malformed metadata

    # Parse minidump into event
    try:
        event_data = await sync_to_async(minidump_to_event)(minidump_data, sentry_meta)
    except Exception as e:
        capture_exception(e)
        logger.error("Failed to parse minidump on %s", request.path, exc_info=e)
        return JsonResponse({"detail": "Failed to parse minidump"}, status=400)

    # Validate and enqueue
    try:
        item = WebIngestIssueEvent.model_validate(event_data)
    except ValidationError as e:
        set_level("warning")
        capture_exception(e)
        logger.warning(
            "Minidump event validation error on %s", request.path, exc_info=e
        )
        return JsonResponse({"detail": "Event validation failed"}, status=400)

    if item.event_id is None:
        item.event_id = uuid.uuid4()

    issue_type = IssueEventType.ERROR if item.exception else IssueEventType.DEFAULT

    primary_id = UUID7Helper.from_datetime()
    interchange_event = IngestTaskMessage(
        project_id=project_id,
        organization_id=project.organization_id,
        payload=item.dict() | {"type": issue_type},
        received=timezone.now(),
        update_first_event=update_first_event,
        uuid=primary_id.hex,
    )
    if await cache.aadd("uuid" + item.event_id.hex, True):
        await ingest_event.aenqueue(serialize_for_vtasks(asdict(interchange_event)))

    return JsonResponse({"id": item.event_id.hex})
