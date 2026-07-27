"""
Sanity-check that batched deletion actually batches and keeps lock counts low.

Seeds >batch_size rows into partitioned tables, deletes via the task, and
monitors pg_locks throughout.  Not a unit test — run manually or in CI with:

    python manage.py test apps.organizations_ext.tests.test_batched_delete -v2
"""

import threading
import time

from asgiref.sync import async_to_sync
from django.db import connections
from django.test import TransactionTestCase
from django.utils import timezone
from model_bakery import baker

from apps.issue_events.models import (
    Issue,
    IssueAggregate,
    IssueEvent,
    IssueTag,
    TagKey,
    TagValue,
)
from apps.logs.models import LogEvent
from apps.organizations_ext.models import Organization
from apps.projects.models import (
    IssueEventProjectHourlyStatistic,
    Project,
)
from apps.uptime.models import MonitorCheck
from glitchtip.partition_manager import UUID7Helper

from ..tasks import delete_organization

_delete_organization_sync = async_to_sync(delete_organization.func)

# Must exceed the 1000-row batch_size in raw_delete_in_batches
EVENT_COUNT = 2500
LOG_COUNT = 1500
CHECK_COUNT = 1500


class BatchedDeleteSanityTestCase(TransactionTestCase):
    """Seed enough data to force multi-batch deletion, monitor lock counts."""

    def _poll_locks(self, stop_event, results):
        """Background thread: sample pg_locks every 50ms."""
        conn = connections["default"]
        # Need a separate connection so we don't interfere with the
        # deletion transaction.  Use a raw psycopg connection.
        db_settings = conn.settings_dict
        import psycopg

        dsn = (
            f"host={db_settings['HOST']} "
            f"port={db_settings['PORT']} "
            f"dbname={db_settings['NAME']} "
            f"user={db_settings['USER']} "
            f"password={db_settings['PASSWORD']}"
        )
        raw = psycopg.connect(dsn, autocommit=True)
        try:
            cur = raw.cursor()
            while not stop_event.is_set():
                # Scope to THIS test's database. pg_locks is cluster-wide, so a
                # bare count(*) sums every parallel test worker's locks (each
                # worker runs in its own test DB) — which conflates unrelated
                # concurrent tests and makes the threshold depend on driver
                # connection footprint rather than on whether *this* delete
                # batches. Counting only the current database measures the
                # batching we actually care about.
                cur.execute(
                    "SELECT count(*) FROM pg_locks "
                    "WHERE database = ("
                    "  SELECT oid FROM pg_database WHERE datname = current_database()"
                    ")"
                )
                results.append(cur.fetchone()[0])
                time.sleep(0.05)
        finally:
            raw.close()

    def test_delete_org_batches_and_low_locks(self):
        """
        Create an org with >batch_size rows in each partitioned table,
        delete it, and assert peak lock count stays reasonable.
        """
        org = baker.make("organizations_ext.Organization")
        project = baker.make("projects.Project", organization=org)
        issues = baker.make("issue_events.Issue", project=project, _quantity=5)
        monitor = baker.make("uptime.Monitor", project=project, organization=org)

        now = timezone.now()

        # -- Seed IssueEvents (batch_size=1000, so 2500 → 3 batches) --
        events = []
        for i in range(EVENT_COUNT):
            issue = issues[i % len(issues)]
            events.append(
                IssueEvent(
                    issue=issue,
                    organization=org,
                    timestamp=now,
                    type=0,
                    level=4,
                    title=f"evt-{i}",
                    data={},
                    tags={},
                )
            )
        IssueEvent.objects.bulk_create(events)
        self.assertEqual(
            IssueEvent.objects.filter(organization=org).count(), EVENT_COUNT
        )

        # -- Seed IssueAggregates --
        aggs = []
        for issue in issues:
            aggs.append(
                IssueAggregate(issue=issue, organization=org, date=now, count=10)
            )
        IssueAggregate.objects.bulk_create(aggs)

        # -- Seed IssueTags --
        tag_key, _ = TagKey.objects.get_or_create(key="browser")
        tag_val, _ = TagValue.objects.get_or_create(value="chrome")
        tags = []
        for issue in issues:
            tags.append(
                IssueTag(
                    issue=issue,
                    organization=org,
                    date=now,
                    tag_key=tag_key,
                    tag_value=tag_val,
                    count=5,
                )
            )
        IssueTag.objects.bulk_create(tags)

        # -- Seed LogEvents --
        logs = []
        for i in range(LOG_COUNT):
            logs.append(
                LogEvent(
                    id=UUID7Helper.from_datetime(now),
                    organization=org,
                    project=project,
                    level=4,
                    body=f"log-{i}",
                    data={},
                )
            )
        LogEvent.objects.bulk_create(logs)

        # -- Seed MonitorChecks --
        checks = []
        for i in range(CHECK_COUNT):
            checks.append(
                MonitorCheck(
                    monitor=monitor,
                    organization=org,
                    is_up=True,
                    is_change=False,
                    start_check=now,
                )
            )
        MonitorCheck.objects.bulk_create(checks)

        # -- Seed hourly stats --
        IssueEventProjectHourlyStatistic.objects.create(
            project=project, organization=org, date=now, count=100
        )

        # -- Poll locks in background --
        lock_samples = []
        stop = threading.Event()
        poller = threading.Thread(
            target=self._poll_locks, args=(stop, lock_samples), daemon=True
        )
        poller.start()

        # -- Delete the org --
        _delete_organization_sync(org.id)

        stop.set()
        poller.join(timeout=2)

        # -- Verify everything is gone --
        self.assertFalse(Organization.objects.filter(id=org.id).exists())
        self.assertFalse(Project.objects.filter(id=project.id).exists())
        self.assertEqual(IssueEvent.objects.filter(organization_id=org.id).count(), 0)
        self.assertEqual(LogEvent.objects.filter(organization_id=org.id).count(), 0)
        self.assertEqual(MonitorCheck.objects.filter(organization_id=org.id).count(), 0)
        self.assertEqual(
            Issue.objects.filter(project__organization_id=org.id).count(), 0
        )
        self.assertEqual(
            IssueAggregate.objects.filter(organization_id=org.id).count(), 0
        )
        self.assertEqual(IssueTag.objects.filter(organization_id=org.id).count(), 0)
        self.assertEqual(
            IssueEventProjectHourlyStatistic.objects.filter(
                organization_id=org.id
            ).count(),
            0,
        )

        # -- Check lock counts --
        if lock_samples:
            peak = max(lock_samples)
            avg = sum(lock_samples) / len(lock_samples)
            print(
                f"\n  Lock samples: {len(lock_samples)}, peak: {peak}, avg: {avg:.0f}"
            )
            # In local test DB max_locks_per_transaction * max_connections
            # gives the hard ceiling.  With batching we should stay well
            # under it. 2000 is a generous upper bound for a test DB with
            # only a handful of partitions.
            self.assertLess(
                peak,
                2000,
                f"Peak lock count {peak} is too high — batching may not be working",
            )
        else:
            print("\n  (no lock samples captured — deletion was very fast)")

    def test_cascade_delete_baseline_lock_count(self):
        """
        Baseline: show how many locks a naive Django cascade delete acquires.

        This demonstrates the problem we're fixing — a single super().delete()
        on an org with data in partitioned tables acquires a lock per partition
        and index. On prod this would be 2700+ partitions.
        """
        org = baker.make("organizations_ext.Organization")
        project = baker.make("projects.Project", organization=org)
        issue = baker.make("issue_events.Issue", project=project)

        # Seed a modest amount — even 100 events spread across partitions are enough
        events = [
            IssueEvent(
                issue=issue,
                organization=org,
                timestamp=timezone.now(),
                type=0,
                level=4,
                title=f"evt-{i}",
                data={},
                tags={},
            )
            for i in range(100)
        ]
        IssueEvent.objects.bulk_create(events)

        lock_samples = []
        stop = threading.Event()
        poller = threading.Thread(
            target=self._poll_locks, args=(stop, lock_samples), daemon=True
        )
        poller.start()

        # Raw Django cascade — what we're trying to avoid
        from django.db import models as _m

        _m.Model.delete(org)

        stop.set()
        poller.join(timeout=2)

        cascade_peak = max(lock_samples) if lock_samples else 0
        print(
            f"\n  Cascade baseline — samples: {len(lock_samples)}, peak: {cascade_peak}"
        )
        # This isn't an assertion — just showing the number for comparison.
        # On prod with 2700 partitions this would be ~10x higher.
