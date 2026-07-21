"""Poison-message isolation for the batched ingest tasks.

django-vtasks delivers ingest work in batches; a single malformed message
must drop (and report) without failing its ~99 batch siblings. The ingest
views bound what reaches the queue, but they validate more lightly than
the worker-side schemas — deliberately so for the Rust ingest path — so
the worker is the enforcement point.
"""

import uuid
from datetime import timezone as dt_timezone
from unittest.mock import patch

from django.utils import timezone

from apps.issue_events.constants import IssueEventType
from apps.issue_events.models import IssueEvent, UserReport

from ..tasks import ingest_event, ingest_user_report
from .utils import EventIngestTestCase, generate_event


def _task_batch(*message_dicts):
    return [{"args": [m]} for m in message_dicts]


class IngestEventPoisonMessageTestCase(EventIngestTestCase):
    async def test_poison_message_does_not_fail_batch(self):
        project = self.project
        valid = {
            "project_id": project.id,
            "organization_id": project.organization_id,
            "received": timezone.now().astimezone(dt_timezone.utc).isoformat(),
            # The view enqueues the scrubbed event dict with the interchange
            # type discriminator appended (IssueEventType integer value).
            "payload": generate_event() | {"type": IssueEventType.ERROR.value},
            "uuid": uuid.uuid4().hex,
        }
        poison = {"project_id": "not-an-int", "payload": None}

        with patch("apps.event_ingest.tasks.capture_exception") as capture:
            await ingest_event.func(_task_batch(poison, valid))

        capture.assert_called_once()
        self.assertEqual(
            await IssueEvent.objects.filter(issue__project_id=project.id).acount(),
            1,
            "valid sibling must survive the poison message",
        )


class IngestUserReportPoisonTestCase(EventIngestTestCase):
    async def test_invalid_event_id_dropped_sibling_survives(self):
        project = self.project
        base = {
            "project_id": project.id,
            "organization_id": project.organization_id,
            "received": timezone.now().astimezone(dt_timezone.utc).isoformat(),
            "payload": {
                "name": "Reporter",
                "email": "r@example.com",
                "comments": "it broke",
            },
        }
        # feedback items forward associated_event_id raw — a non-UUID value
        # must drop only its own report.
        poison = base | {"event_id": "12345"}
        valid = base | {"event_id": None}

        with patch("apps.event_ingest.tasks.capture_exception") as capture:
            await ingest_user_report.func(_task_batch(poison, valid))

        capture.assert_called_once()
        self.assertEqual(await UserReport.objects.acount(), 1)


class IngestLogsPoisonTestCase(EventIngestTestCase):
    async def test_malformed_log_message_dropped(self):
        from apps.logs.models import LogEvent
        from apps.logs.tasks import ingest_logs

        project = self.project
        valid = {
            "project_id": project.id,
            "organization_id": project.organization_id,
            "received": timezone.now().astimezone(dt_timezone.utc).isoformat(),
            "logs": [
                {
                    "timestamp": timezone.now().timestamp(),
                    "body": "hello",
                    "level": "info",
                    "attributes": {},
                }
            ],
        }
        poison = {"nope": True}

        await ingest_logs.func(_task_batch(poison, valid))

        self.assertEqual(
            await LogEvent.objects.filter(project_id=project.id).acount(), 1
        )
