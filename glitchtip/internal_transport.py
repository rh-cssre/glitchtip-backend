import asyncio
import contextvars
import logging
import time
from dataclasses import asdict

from asgiref.sync import sync_to_async
from sentry_sdk.transport import Transport

logger = logging.getLogger(__name__)

# Equivalent to old Sentry's NOOP_HUB — suppresses SDK capture during
# internal event processing to prevent synchronous recursion.
_processing_internal = contextvars.ContextVar("_processing_internal", default=False)

# Rate limit for self-reported error events to prevent queue amplification
# when ingest is broadly broken. Generous enough to never drop real errors;
# tight enough to prevent OOM/resource exhaustion from runaway loops.
_INTERNAL_EVENT_MAX = 50  # max events per window
_INTERNAL_EVENT_WINDOW = 60  # seconds


class InternalTransport(Transport):
    """
    Custom SDK transport that bypasses HTTP and injects events directly
    into the ingest task queue. Prevents self-DOS feedback loops when
    SENTRY_DSN points to the same GlitchTip instance.

    capture_envelope is sync (SDK interface) but may be called from both
    sync (worker) and async (ASGI web) contexts. We detect the calling
    context and dispatch accordingly so that ORM calls (project_key
    lookup, task enqueue) never hit SynchronousOnlyOperation.
    """

    def __init__(self, options=None):
        super().__init__(options)
        self._project_key = None
        self._error_timestamps: list[float] = []

    @property
    def project_key(self):
        """Lazy lookup — avoids import/DB access at init time."""
        if self._project_key is None:
            from apps.projects.models import ProjectKey

            if self.parsed_dsn:
                try:
                    self._project_key = ProjectKey.objects.select_related(
                        "project"
                    ).get(public_key=self.parsed_dsn.public_key)
                except ProjectKey.DoesNotExist:
                    logger.warning("InternalTransport: project key not found for DSN")
        return self._project_key

    def _is_rate_limited(self) -> bool:
        """Sliding window rate limit to prevent queue amplification loops.

        Not strictly thread-safe (compound read-filter-append can race),
        but the limit is a coarse safety net — being off by a few events
        out of 50 is fine. A lock is not worth the overhead here.
        """
        now = time.monotonic()
        cutoff = now - _INTERNAL_EVENT_WINDOW
        self._error_timestamps = [t for t in self._error_timestamps if t > cutoff]
        if len(self._error_timestamps) >= _INTERNAL_EVENT_MAX:
            return True
        self._error_timestamps.append(now)
        return False

    def capture_envelope(self, envelope):
        if self._is_rate_limited():
            return

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # Sync context (worker, management commands) — process directly
            token = _processing_internal.set(True)
            try:
                self._process_envelope(envelope)
            except Exception as e:
                logger.warning("InternalTransport: failed to process envelope: %s", e)
            finally:
                _processing_internal.reset(token)
        else:
            # Async context (ASGI web) — schedule in thread pool
            loop.create_task(self._process_envelope_in_thread(envelope))

    async def _process_envelope_in_thread(self, envelope):
        """Run _process_envelope in a thread pool for async contexts."""
        token = _processing_internal.set(True)
        try:
            await sync_to_async(self._process_envelope, thread_sensitive=False)(
                envelope
            )
        except Exception as e:
            logger.warning("InternalTransport: failed to process envelope: %s", e)
        finally:
            _processing_internal.reset(token)

    def _process_envelope(self, envelope):
        from django.utils import timezone

        from apps.event_ingest.interfaces import IngestTaskMessage
        from apps.event_ingest.tasks import ingest_event, ingest_transaction
        from apps.event_ingest.utils import serialize_for_vtasks
        from apps.issue_events.constants import IssueEventType
        from glitchtip.partition_manager import UUID7Helper

        key = self.project_key
        if key is None:
            return

        now = timezone.now

        for item in envelope:
            if item.type == "event":
                event = item.payload.json
                issue_type = (
                    IssueEventType.ERROR
                    if "exception" in event
                    else IssueEventType.DEFAULT
                )
                msg = IngestTaskMessage(
                    project_id=key.project_id,
                    organization_id=key.project.organization_id,
                    payload=event | {"type": issue_type},
                    received=now(),
                    update_first_event=False,
                    uuid=UUID7Helper.from_datetime().hex,
                )
                ingest_event.enqueue(serialize_for_vtasks(asdict(msg)))

            elif item.type == "transaction":
                msg = IngestTaskMessage(
                    project_id=key.project_id,
                    organization_id=key.project.organization_id,
                    payload=item.payload.json,
                    received=now(),
                    update_first_event=False,
                    uuid=UUID7Helper.from_datetime().hex,
                )
                ingest_transaction.enqueue(serialize_for_vtasks(asdict(msg)))

            elif item.type == "log":
                self._process_log_item(item, key, now())

    def _process_log_item(self, item, key, received):
        from django.conf import settings

        if not settings.GLITCHTIP_ENABLE_LOGS:
            return

        from apps.event_ingest.interfaces import LogIngestTaskMessage
        from apps.event_ingest.schema import LogEnvelopePayload
        from apps.event_ingest.utils import serialize_for_vtasks
        from apps.logs.tasks import ingest_logs

        payload_bytes = item.get_bytes()
        log_payload = LogEnvelopePayload.model_validate_json(payload_bytes)
        log_message = LogIngestTaskMessage(
            project_id=key.project_id,
            organization_id=key.project.organization_id,
            received=received,
            logs=[log_item.dict() for log_item in log_payload.items],
        )
        ingest_logs.enqueue(serialize_for_vtasks(asdict(log_message)))
