"""Ingest's batch lookups on hash-partitioned tables must reach one partition
per row, and must still match the right rows when a batch spans several
organizations.

IssueIndex and TransactionGroup are hash-partitioned on organization_id. A
lookup that joins them on anything else has every row probe every partition.
"""

import json
from contextlib import contextmanager
from unittest import mock

from django.db import connection, transaction
from django.utils import timezone
from django_async_backend.db.backends.utils import AsyncCursorWrapper
from model_bakery import baker

from apps.issue_events.constants import EventStatus
from apps.issue_events.models import Issue, IssueIndex
from apps.performance.models import TransactionGroup
from apps.performance.tests.test_ingest import (
    _make_interchange_event,
    _make_transaction_payload,
    _process_transaction_events,
)

from ..process_event import process_issue_events
from ..schema import IssueEventSchema, IssueTaskMessage
from .utils import EventIngestTestCase, run_async_closing


@contextmanager
def _capture_async_statements():
    """Record every (sql, params) sent through an async-backend cursor."""
    captured = []
    original = AsyncCursorWrapper._execute_with_wrappers

    async def recording(self, sql, params, many, executor):
        if not many:
            captured.append((sql, params))
        return await original(self, sql, params, many, executor)

    with mock.patch.object(AsyncCursorWrapper, "_execute_with_wrappers", recording):
        yield captured


def _partitions(table: str) -> set[str]:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT c.relname FROM pg_inherits i "
            "JOIN pg_class c ON c.oid = i.inhrelid "
            "JOIN pg_class p ON p.oid = i.inhparent WHERE p.relname = %s",
            [table],
        )
        return {row[0] for row in cursor.fetchall()}


def _executed(plan, partitions: set[str]) -> set[str]:
    """Partitions an EXPLAIN ANALYZE plan actually scanned."""
    found = set()
    stack = [plan]
    while stack:
        item = stack.pop()
        if isinstance(item, list):
            stack.extend(item)
        elif isinstance(item, dict):
            if item.get("Relation Name") in partitions and item.get("Actual Loops"):
                found.add(item["Relation Name"])
            stack.extend(v for v in item.values() if isinstance(v, (dict, list)))
    return found


def _scanned_partitions(sql: str, params, partitions: set[str]) -> set[str]:
    # Fixture tables are tiny, so the planner would rather hash-join and scan
    # every partition whatever the query says. Steer it to the index nested
    # loop that production data gets, where run-time pruning is visible.
    with transaction.atomic(), connection.cursor() as cursor:
        for setting in ("hashjoin", "mergejoin", "seqscan", "bitmapscan"):
            cursor.execute(f"SET LOCAL enable_{setting} = off")
        cursor.execute("EXPLAIN (ANALYZE, FORMAT JSON) " + sql, params)
        plan = cursor.fetchone()[0]
    return _executed(json.loads(plan) if isinstance(plan, str) else plan, partitions)


class IngestLookupPruningTestCase(EventIngestTestCase):
    def assertLookupScansOnePartition(self, captured, table, marker):
        partitions = _partitions(table)
        self.assertGreater(len(partitions), 1, "test needs several hash buckets")
        lookups = [(sql, params) for sql, params in captured if marker in sql]
        self.assertTrue(lookups, f"{marker!r} was never sent")
        for sql, params in lookups:
            scanned = _scanned_partitions(sql, params, partitions)
            self.assertEqual(len(scanned), 1, f"{marker!r} scans {sorted(scanned)}")

    def test_issue_hash_lookup(self):
        payload = {"message": "partition pruning"}
        self.process_events(payload)
        with _capture_async_statements() as captured:
            self.process_events(payload)
        self.assertLookupScansOnePartition(
            captured, "issue_events_issueindex", "AS issue__status"
        )

    def test_transaction_group_lookup(self):
        def ingest():
            _process_transaction_events(
                [
                    _make_interchange_event(
                        self.project.id,
                        self.organization.id,
                        _make_transaction_payload(duration_ms=100.0),
                    )
                ]
            )

        ingest()
        with _capture_async_statements() as captured:
            ingest()
        self.assertLookupScansOnePartition(
            captured,
            "performance_transactiongroup",
            "JOIN performance_transactiongroup tg",
        )


class MultiOrganizationBatchTestCase(EventIngestTestCase):
    """One batch spanning several organizations matches each row to its own
    organization's issue and transaction group."""

    def setUp(self):
        super().setUp()
        self.projects = [self.project] + [
            baker.make("projects.Project", organization__scrub_ip_addresses=False)
            for _ in range(3)
        ]

    def _ingest_issues(self):
        run_async_closing(
            process_issue_events,
            [
                IssueTaskMessage(
                    project_id=project.id,
                    organization_id=project.organization_id,
                    received=timezone.now(),
                    payload=IssueEventSchema(message="multi-org batch"),
                )
                for project in self.projects
            ],
        )

    def _ingest_transactions(self):
        _process_transaction_events(
            [
                _make_interchange_event(
                    project.id, project.organization_id, _make_transaction_payload()
                )
                for project in self.projects
            ]
        )

    def test_issues(self):
        self._ingest_issues()
        IssueIndex.objects.update(status=EventStatus.RESOLVED)
        self._ingest_issues()

        self.assertEqual(Issue.objects.count(), len(self.projects))
        self.assertEqual(IssueIndex.objects.count(), len(self.projects))
        for project in self.projects:
            leaf = IssueIndex.objects.get(issue__project=project)
            self.assertEqual(leaf.organization_id, project.organization_id)
            self.assertEqual(leaf.count, 2)
            self.assertEqual(leaf.status, EventStatus.UNRESOLVED)

    def test_transactions(self):
        self._ingest_transactions()
        self._ingest_transactions()

        self.assertEqual(TransactionGroup.objects.count(), len(self.projects))
        for project in self.projects:
            group = TransactionGroup.objects.get(project=project)
            self.assertEqual(group.organization_id, project.organization_id)
            self.assertEqual(group.count, 2)
