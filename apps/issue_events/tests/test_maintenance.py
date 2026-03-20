from datetime import timedelta

from django.conf import settings
from django.db import models
from django.test import TestCase
from django.utils.timezone import now
from freezegun import freeze_time
from model_bakery import baker

from ..maintenance import cleanup_old_issues
from ..models import Issue, IssueEvent

# cleanup_old_issues adds a 7-day buffer beyond retention
_BUFFER_DAYS = 7


class MaintenanceTestCase(TestCase):
    def test_cleanup_old_issues(self):
        events = baker.make(
            "issue_events.IssueEvent", _quantity=5, _fill_optional=["issue"]
        )
        baker.make("issue_events.IssueEvent", issue=events[0].issue, _quantity=5)
        cleanup_old_issues()
        self.assertEqual(Issue.objects.count(), 5)

        IssueEvent.objects.all().delete()
        with freeze_time(
            now()
            + timedelta(
                days=settings.GLITCHTIP_EVENT_RETENTION_DAYS + _BUFFER_DAYS + 1
            )
        ):
            cleanup_old_issues()
            self.assertEqual(Issue.objects.count(), 0)

    def test_cleanup_within_buffer_keeps_issues(self):
        """Issues within the buffer window (retention + 7 days) are kept."""
        baker.make("issue_events.Issue")
        with freeze_time(
            now()
            + timedelta(
                days=settings.GLITCHTIP_EVENT_RETENTION_DAYS + _BUFFER_DAYS - 1
            )
        ):
            cleanup_old_issues()
            self.assertEqual(Issue.objects.count(), 1)

    def test_cleanup_handles_all_nonpartitioned_fk_relations(self):
        """
        Verify cleanup explicitly deletes from all non-partitioned FK tables.

        Since _raw_delete() bypasses Django's collector, non-partitioned FK
        tables (including M2M through tables) must be explicitly deleted per
        batch before deleting the issue. Partitioned FK tables are handled by
        maintain_partitions dropping old partitions before cleanup runs.
        """
        for rel in Issue._meta.related_objects:
            if rel.on_delete != models.CASCADE:
                continue
            accessor = rel.get_accessor_name()
            db_table = rel.related_model._meta.db_table
            with self.subTest(relation=accessor):
                issue = baker.make("issue_events.Issue")
                kwargs = {rel.field.name: issue}
                for f in rel.related_model._meta.concrete_fields:
                    if (
                        isinstance(f, models.DateTimeField)
                        and not f.has_default()
                        and not f.null
                    ):
                        kwargs[f.name] = now()
                baker.make(rel.related_model, **kwargs)

                with freeze_time(
                    now()
                    + timedelta(
                        days=settings.GLITCHTIP_EVENT_RETENTION_DAYS
                        + _BUFFER_DAYS
                        + 1
                    )
                ):
                    cleanup_old_issues()

                # Non-partitioned tables must be explicitly deleted so the
                # issue delete succeeds. Partitioned tables may cause
                # IntegrityError (caught and skipped) if data still exists
                # in tests, but in production partitions are already dropped.
                self.assertFalse(
                    Issue.objects.filter(id=issue.id).exists(),
                    f"Issue with {accessor} (table {db_table}) "
                    f"was NOT deleted — if non-partitioned, add explicit "
                    f"delete in cleanup_old_issues()",
                )
