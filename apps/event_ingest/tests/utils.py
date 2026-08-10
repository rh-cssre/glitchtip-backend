import asyncio
import datetime
import json
import time
import uuid

from asgiref.sync import async_to_sync
from django.db.utils import IntegrityError
from django.test import TransactionTestCase
from django.utils import timezone
from django_async_backend.db import async_connections
from model_bakery import baker

from apps.organizations_ext.constants import OrganizationUserRole
from glitchtip.test_utils.test_case import GlitchTipTestCaseMixin

from ..process_event import process_issue_events
from ..schema import (
    IssueEventSchema,
    IssueTaskMessage,
)

# A globally-routable client address (TEST-NETs are non-global and would be
# discarded by the ipware port, exactly like the Python path).
CLIENT_IP = "93.184.216.34"


class AsgiIngestTestMixin:
    """Drive envelope requests at the ASGI seam — ``IngestDispatcher`` — the
    exact layer production traffic enters: matching envelope POSTs are
    served by the Rust handler; everything else (non-POST, minidump
    fallbacks) lands on the minimal-middleware Django ingest handler."""

    def _dispatcher(self):
        from django.core.asgi import get_asgi_application

        from glitchtip.ingest_asgi import IngestDispatcher

        return IngestDispatcher(get_asgi_application())

    async def _post(
        self,
        body: bytes,
        extra_headers: list | None = None,
        path: str | None = None,
        method: str = "POST",
        query: str | None = None,
    ):
        scope = {
            "type": "http",
            "method": method,
            "path": path or f"/api/{self.project.id}/envelope/",
            "query_string": (
                query
                if query is not None
                else f"sentry_key={self.projectkey.public_key}"
            ).encode(),
            "headers": [
                (b"content-type", b"application/x-sentry-envelope"),
                # Real servers (granian) always pass Content-Length through;
                # Django's DATA_UPLOAD_MAX_MEMORY_SIZE check reads it.
                (b"content-length", str(len(body)).encode()),
            ]
            + (extra_headers or []),
            "client": (CLIENT_IP, 4242),
        }
        messages = []
        body_sent = False

        async def receive():
            nonlocal body_sent
            if not body_sent:
                body_sent = True
                return {"type": "http.request", "body": body, "more_body": False}
            # Idle like a healthy keep-alive connection; a disconnect
            # listener parked here is cancelled when the response ends.
            await asyncio.Event().wait()

        async def send(message):
            messages.append(message)

        await self._dispatcher()(scope, receive, send)
        status = next(
            m["status"] for m in messages if m["type"] == "http.response.start"
        )
        headers = {
            name.decode(): value.decode()
            for m in messages
            if m["type"] == "http.response.start"
            for name, value in m.get("headers", [])
        }
        response_body = b"".join(
            m.get("body", b"") for m in messages if m["type"] == "http.response.body"
        )
        return status, headers, response_body


def fake_integrity_error(sqlstate: str) -> IntegrityError:
    """An IntegrityError chained the way Django raises it: ``__cause__``
    is the driver exception carrying the psycopg-shaped ``sqlstate``
    (gt_rust's dbapi mirrors psycopg's diagnostics). For faking copy_rows
    failures."""
    cause = Exception(f"[{sqlstate}] integrity constraint violation")
    cause.sqlstate = sqlstate
    error = IntegrityError("integrity constraint violation")
    error.__cause__ = cause
    return error


def list_to_envelope(data: list[dict]) -> str:
    result_lines = []
    for item in data:
        result_lines.append(json.dumps(item))
    return "\n".join(result_lines)


def flush_rust_ingest_state():
    """Reset the store the Rust ingest path writes its block/dedupe/queue
    state to: the dedicated test Valkey database, or — on a Postgres-only
    stack — the django_cache table and the db_queuedtask broker table.
    ``createcachetable`` is idempotent and the test database starts
    without the cache table (it is entrypoint-provisioned, not a
    migration), so create it here."""
    from django.core.management import call_command
    from django.db import connection

    from glitchtip.rust_ingest import _valkey_driver

    driver = _valkey_driver()
    if driver is not None:
        driver.flushdb_sync()
        return
    from django_vtasks.db.models import QueuedTask

    # Explicit table name: under the test runner CACHES is LocMem, so the
    # bare command (which walks CACHES) would create nothing.
    call_command("createcachetable", "django_cache", verbosity=0)
    with connection.cursor() as cursor:
        cursor.execute("DELETE FROM django_cache")
    QueuedTask.objects.all().delete()


async def pop_rust_ingest_messages() -> list[bytes]:
    """Pop every raw serialized task message the Rust arm enqueued onto the
    ingest queue, oldest first. Valkey: RPOP the queue list. Postgres: read
    and delete the db_queuedtask rows (created_at order; rows enqueued in
    one batch share a timestamp, so within-batch order is not defined —
    exactly the DB worker's own consumption contract)."""
    from glitchtip.rust_ingest import _valkey_driver

    driver = _valkey_driver()
    messages = []
    if driver is not None:
        while True:
            raw = await driver.rpop("{vt}:q:ingest")
            if raw is None:
                break
            messages.append(bytes(raw))
        return messages
    from django_vtasks.db.models import QueuedTask

    queued = QueuedTask.objects.filter(queue="ingest")
    async for task in queued.order_by("created_at"):
        messages.append(bytes(task.data))
    await queued.adelete()
    return messages


def generate_event(
    event_type="error",
    level="error",
    platform="python",
    release="default-release",
    environment="production",
    num_events=1,
    event=None,
    envelope=False,
):
    """
    Generates sentry compatible events for use in unit tests.

    Args:
      event_type (str): The type of event ('error', 'warning', 'transaction', etc.). Default is 'error'.
      level (str): The event level ('error', 'warning', 'info', etc.). Default is 'error'.
      platform (str): The platform the event originated from ('python', 'javascript', 'java', etc.). Default is 'python'.
      release (str): The release version. Default is 'default-release'.
      environment (str): The environment (e.g., 'production', 'staging'). Default is 'production'.
      num_events (int): The number of events to generate. Default is 1.
      event (dict): A dictionary of additional fields to override in the base event.
      envelope (bool): Whether to wrap the event(s) in an envelope list. Default is False.

    Returns:
      dict or list: A single event dictionary, a list of event dictionaries, or an envelope list containing event data.
    """

    base_event = {
        "event_id": str(uuid.uuid4()),
        "timestamp": time.time(),
        "sdk": {"name": "sentry.python", "version": "1.11.0"},
        "platform": platform,
        "level": level,
        "exception": {
            "values": [
                {
                    "type": "TypeError",
                    "value": "unsupported operand type(s) for +: 'int' and 'str'",
                    "stacktrace": {
                        "frames": [
                            {
                                "filename": "my_module.py",
                                "function": "my_function",
                                "in_app": True,
                                "lineno": 10,
                            }
                        ]
                    },
                }
            ]
        },
        "release": release,
        "environment": environment,
        "request": {
            "url": "http://example.com",
            "headers": {"User-Agent": "Test Agent"},
        },
    }

    if event:
        base_event.update(event)

    if num_events == 1:
        if envelope:
            return [
                {
                    "event_id": base_event["event_id"],
                    "sent_at": datetime.datetime.now().isoformat() + "Z",
                },
                {"type": "event"},
                base_event,
            ]
        else:
            return base_event
    else:
        events = []
        for _ in range(num_events):
            new_event = base_event.copy()
            new_event["event_id"] = str(uuid.uuid4())
            if envelope:
                events.append(
                    [
                        {
                            "event_id": new_event["event_id"],
                            "sent_at": datetime.datetime.now().isoformat() + "Z",
                        },
                        {"type": "event"},
                        new_event,
                    ]
                )
            else:
                events.append(new_event)
        return events


def run_async_closing(coro_func, *args, **kwargs):
    """``async_to_sync`` for tests that opens a fresh task and closes the
    task's async-backend connections before the task ends.

    Each ``async_to_sync`` invocation runs in a new task, which gets its
    own task-local async-DB wrapper. ``psycopg.AsyncConnection`` can't
    close synchronously from ``__del__``, so the socket leaks until
    process exit. Closing inside the task — same context that opened it —
    avoids exhausting Postgres' ``max_connections`` over a full suite.
    """

    async def _wrapper():
        try:
            return await coro_func(*args, **kwargs)
        finally:
            for alias in async_connections.settings:
                if hasattr(async_connections._connections, alias):
                    await async_connections[alias].close()

    return async_to_sync(_wrapper)()


class EventIngestTestCase(GlitchTipTestCaseMixin, TransactionTestCase):
    """
    Base class for event ingest tests with helper functions
    """

    def setUp(self):
        from django.tasks import task_backends

        # Clear any pending batches from previous tests to avoid cross-test pollution
        # (e.g. tasks for projects that have been rolled back/deleted)
        if "default" in task_backends:
            backend = task_backends["default"]
            if hasattr(backend, "pending_batches"):
                backend.pending_batches.clear()

        self.create_project()
        self.params = f"?sentry_key={self.projectkey.public_key}"

    def get_json_data(self, filename: str):
        with open(filename) as json_file:
            return json.load(json_file)

    def create_logged_in_user(self):
        self.user = baker.make("users.user")
        self.client.force_login(self.user)
        self.org_user = self.organization.add_user(
            self.user, OrganizationUserRole.ADMIN
        )
        self.team = baker.make("teams.Team", organization=self.organization)
        self.team.members.add(self.org_user)
        self.project = baker.make("projects.Project", organization=self.organization)
        self.project.teams.add(self.team)

    def process_events(self, data: dict | list[dict]) -> list:
        if isinstance(data, dict):
            data = [data]

        events = [
            IssueTaskMessage(
                project_id=self.project.id,
                organization_id=self.organization.id if self.organization else None,
                received=timezone.now(),
                payload=IssueEventSchema(**dat),
            )
            for dat in data
        ]
        run_async_closing(process_issue_events, events)
        return events
