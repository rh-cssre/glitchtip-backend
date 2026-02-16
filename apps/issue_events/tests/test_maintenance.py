from datetime import timedelta

from django.conf import settings
from django.db import connection, models
from django.test import TestCase
from django.utils.timezone import now
from freezegun import freeze_time
from model_bakery import baker

from ..maintenance import cleanup_old_issues
from ..models import Issue, IssueEvent


def _is_table_partitioned(table_name):
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT relkind FROM pg_class WHERE relname = %s",
            [table_name],
        )
        row = cursor.fetchone()
        return row is not None and row[0] == "p"


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
            now() + timedelta(days=settings.GLITCHTIP_MAX_EVENT_LIFE_DAYS)
        ):
            cleanup_old_issues()
            self.assertEqual(Issue.objects.count(), 0)

    def test_cleanup_handles_all_fk_relations(self):
        """
        Verify cleanup correctly handles all FK relations to Issue, including
        auto-created M2M through tables.

        Since _raw_delete() bypasses Django's collector, ALL FK tables must
        be handled explicitly:
        - Partitioned FK tables: must have an exclude(Exists()) in the queryset
          so the issue is kept alive while data exists.
        - Non-partitioned FK tables (including M2M through tables): must be
          explicitly deleted per batch before deleting the issue.
        """
        for rel in Issue._meta.related_objects:
            if rel.on_delete != models.CASCADE:
                continue
            accessor = rel.get_accessor_name()
            db_table = rel.related_model._meta.db_table
            partitioned = _is_table_partitioned(db_table)
            with self.subTest(relation=accessor, partitioned=partitioned):
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
                    now() + timedelta(days=settings.GLITCHTIP_MAX_EVENT_LIFE_DAYS + 1)
                ):
                    cleanup_old_issues()

                if partitioned:
                    self.assertTrue(
                        Issue.objects.filter(id=issue.id).exists(),
                        f"Issue with {accessor} (partitioned table {db_table}) "
                        f"was deleted — add exclude(Exists()) filter to "
                        f"cleanup_old_issues()",
                    )
                else:
                    self.assertFalse(
                        Issue.objects.filter(id=issue.id).exists(),
                        f"Issue with {accessor} (non-partitioned table "
                        f"{db_table}) was NOT deleted — add explicit delete "
                        f"for this table in cleanup_old_issues()",
                    )
