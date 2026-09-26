"""Ingest's hash-partitioned lookups and updates must prune to the batch's
organizations when Postgres plans them.

Joining the partition key to an ``unnest`` or ``VALUES`` list only prunes at
run time: the planner still plans, and locks, every partition. These tests
EXPLAIN each statement ingest sends to a hash-partitioned table and require a
single-organization batch to plan exactly one partition.
"""

import json
from contextlib import contextmanager
from unittest import mock

from django.db import connection
from django_async_backend.db.backends.utils import AsyncCursorWrapper

from apps.issue_events.constants import EventStatus
from apps.issue_events.models import IssueIndex
from apps.performance.tests.test_ingest import (
    _make_interchange_event,
    _make_transaction_payload,
    _process_transaction_events,
)

from .utils import EventIngestTestCase


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


def _planned(sql: str, params, partitions: set[str]) -> set[str]:
    with connection.cursor() as cursor:
        cursor.execute("EXPLAIN (FORMAT JSON) " + sql, params)
        plan = cursor.fetchone()[0]
    text = plan if isinstance(plan, str) else json.dumps(plan)
    return {p for p in partitions if f'"{p}"' in text}


class IngestPartitionPruningTestCase(EventIngestTestCase):
    def assertPrunesToOnePartition(self, captured, table, markers):
        partitions = _partitions(table)
        self.assertGreater(len(partitions), 1, "test needs several hash buckets")
        seen = set()
        for sql, params in captured:
            marker = next((m for m in markers if m in sql), None)
            if marker is None:
                continue
            seen.add(marker)
            planned = _planned(sql, params, partitions)
            self.assertEqual(len(planned), 1, f"{marker!r} plans {sorted(planned)}")
        self.assertEqual(seen, set(markers), "a statement was never sent")

    def test_issue_event_statements(self):
        payload = {"message": "partition pruning"}
        self.process_events(payload)
        IssueIndex.objects.update(status=EventStatus.RESOLVED)
        with _capture_async_statements() as captured:
            self.process_events(payload)
        self.assertPrunesToOnePartition(
            captured,
            "issue_events_issueindex",
            [
                "JOIN issue_events_issueindex si",
                "UPDATE issue_events_issueindex t SET",
                "UPDATE issue_events_issueindex si SET status",
            ],
        )

    def test_transaction_statements(self):
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

        with _capture_async_statements() as first:
            ingest()
        with _capture_async_statements() as second:
            ingest()
        self.assertPrunesToOnePartition(
            first + second,
            "performance_transactiongroup",
            [
                "JOIN performance_transactiongroup tg",
                "SET count = tg.count + v.batch_count",
                "SET p50 = v.p50",
            ],
        )
