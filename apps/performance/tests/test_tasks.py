from datetime import timedelta

from django.conf import settings
from django.db import connection, models
from django.utils import timezone
from freezegun import freeze_time
from model_bakery import baker

from glitchtip.test_utils.test_case import GlitchTipTestCase

from ..maintenance import cleanup_old_transaction_events
from ..models import TransactionEvent, TransactionGroup


def _is_table_partitioned(table_name):
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT relkind FROM pg_class WHERE relname = %s",
            [table_name],
        )
        row = cursor.fetchone()
        return row is not None and row[0] == "p"


class TasksTestCase(GlitchTipTestCase):
    def test_cleanup_old_events(self):
        groups = baker.make("performance.TransactionGroup", _quantity=2)
        baker.make("performance.TransactionEvent", group=groups[0])
        with freeze_time(
            timezone.now()
            + timedelta(days=settings.GLITCHTIP_MAX_TRANSACTION_EVENT_LIFE_DAYS + 1)
        ):
            cleanup_old_transaction_events()
        self.assertEqual(TransactionGroup.objects.count(), 1)

        TransactionEvent.objects.all().delete()
        with freeze_time(
            timezone.now()
            + timedelta(days=settings.GLITCHTIP_MAX_TRANSACTION_EVENT_LIFE_DAYS + 1)
        ):
            cleanup_old_transaction_events()
        self.assertEqual(TransactionGroup.objects.count(), 0)

    def test_cleanup_handles_all_fk_relations(self):
        """
        Verify cleanup correctly handles CASCADE FK relations to TransactionGroup.

        Partitioned FK tables must have a =None filter in the queryset to
        avoid cascading deletes into partitioned sub-tables.
        Non-partitioned FK tables are handled by DB ON DELETE CASCADE and
        should NOT be filtered.
        """
        for rel in TransactionGroup._meta.related_objects:
            if rel.on_delete != models.CASCADE:
                continue
            if rel.related_model._meta.auto_created:
                continue
            accessor = rel.get_accessor_name()
            db_table = rel.related_model._meta.db_table
            partitioned = _is_table_partitioned(db_table)
            with self.subTest(relation=accessor, partitioned=partitioned):
                group = baker.make("performance.TransactionGroup")
                kwargs = {rel.field.name: group}
                for f in rel.related_model._meta.concrete_fields:
                    if (
                        isinstance(f, models.DateTimeField)
                        and not f.has_default()
                        and not f.null
                    ):
                        kwargs[f.name] = timezone.now()
                baker.make(rel.related_model, **kwargs)

                with freeze_time(
                    timezone.now()
                    + timedelta(
                        days=settings.GLITCHTIP_MAX_TRANSACTION_EVENT_LIFE_DAYS + 1
                    )
                ):
                    cleanup_old_transaction_events()

                if partitioned:
                    self.assertTrue(
                        TransactionGroup.objects.filter(id=group.id).exists(),
                        f"Group with {accessor} (partitioned table {db_table}) "
                        f"was deleted — add {accessor}=None filter to "
                        f"cleanup_old_transaction_events()",
                    )
                else:
                    self.assertFalse(
                        TransactionGroup.objects.filter(id=group.id).exists(),
                        f"Group with {accessor} (non-partitioned table "
                        f"{db_table}) was NOT deleted — DB CASCADE should "
                        f"handle this, remove any {accessor}=None filter",
                    )
