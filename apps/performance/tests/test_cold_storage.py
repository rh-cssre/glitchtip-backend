"""
Integration tests for the span promotion and cold storage query pipeline.

Tests the full lifecycle: SpanStaging → Parquet promotion → DuckDB queries,
and the compaction pipeline including crash-recovery safety.
"""

import os
import shutil
import tempfile
from datetime import datetime, timedelta, timezone
from unittest import mock

from django.core.files.storage import FileSystemStorage
from django.test import TestCase
from freezegun import freeze_time
from model_bakery import baker

from apps.performance.cold_storage import (
    TABLE_NAME,
    query_n_plus_one_patterns,
    query_span_groups,
    query_span_groups_for_transaction,
    query_transaction_trend,
)
from apps.performance.models import SpanStaging
from apps.performance.promotion import (
    _write_chunk_parquet,
    compact_span_chunks,
    promote_spans,
)
from glitchtip.cold_storage import enumerate_org_parquet_files
from glitchtip.partition_manager import UUID7Helper


def _make_span_staging_row(
    org_id: int,
    project_id: int,
    transaction_name: str = "/api/test/",
    op: str = "db",
    description: str = "SELECT %s FROM users",
    duration: float = 10.0,
    timestamp: datetime | None = None,
    span_id: str = "abc123",
    transaction_id: str = "txn001",
):
    """Create a SpanStaging row with sensible defaults."""
    if timestamp is None:
        timestamp = datetime.now(timezone.utc) - timedelta(minutes=10)
    return SpanStaging(
        id=UUID7Helper.from_datetime(timestamp),
        organization_id=org_id,
        project_id=project_id,
        transaction_name=transaction_name,
        op=op,
        description=description,
        duration=duration,
        timestamp=timestamp,
        span_id=span_id,
        transaction_id=transaction_id,
    )


class ColdStorageTestMixin:
    """Mixin that sets up a temp directory and patches cold storage settings."""

    def setUp(self):
        super().setUp()
        self.cold_dir = tempfile.mkdtemp()
        self.storage = FileSystemStorage(location=self.cold_dir)

        self.duckdb_patch = mock.patch(
            "apps.performance.cold_storage.is_duckdb_available", return_value=True
        )
        self.storage_patch = mock.patch(
            "apps.performance.cold_storage.get_cold_storage_backend",
            return_value=self.storage,
        )
        self.promo_duckdb_patch = mock.patch(
            "apps.performance.promotion.is_duckdb_available", return_value=True
        )
        self.promo_storage_patch = mock.patch(
            "apps.performance.promotion.get_cold_storage_backend",
            return_value=self.storage,
        )
        self.duckdb_patch.start()
        self.storage_patch.start()
        self.promo_duckdb_patch.start()
        self.promo_storage_patch.start()

    def tearDown(self):
        from glitchtip.cold_storage import close_duckdb_read_connection

        close_duckdb_read_connection()
        self.duckdb_patch.stop()
        self.storage_patch.stop()
        self.promo_duckdb_patch.stop()
        self.promo_storage_patch.stop()
        shutil.rmtree(self.cold_dir, ignore_errors=True)
        super().tearDown()


class PromoteSpansTestCase(ColdStorageTestMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.project = baker.make(
            "projects.Project", organization__scrub_ip_addresses=False
        )
        self.org = self.project.organization

    def test_promote_creates_parquet_and_deletes_staging(self):
        """Promotion writes a chunk Parquet file and deletes staging rows."""
        ts = datetime.now(timezone.utc) - timedelta(minutes=10)
        spans = [
            _make_span_staging_row(
                self.org.id,
                self.project.id,
                timestamp=ts + timedelta(seconds=i),
                span_id=f"span{i}",
                transaction_id=f"txn{i}",
            )
            for i in range(5)
        ]
        SpanStaging.objects.bulk_create(spans)
        self.assertEqual(SpanStaging.objects.count(), 5)

        promoted, truncated = promote_spans()

        self.assertEqual(promoted, 5)
        self.assertFalse(truncated)
        self.assertEqual(SpanStaging.objects.count(), 0)

        # Verify parquet file was created
        org_dir = os.path.join(
            self.cold_dir,
            f"cold_storage/performance_spans/org_{self.org.id}",
        )
        self.assertTrue(os.path.isdir(org_dir))
        # Should have a date subdirectory with a chunk file
        date_dirs = [
            d for d in os.listdir(org_dir) if os.path.isdir(os.path.join(org_dir, d))
        ]
        self.assertEqual(len(date_dirs), 1)
        chunk_files = os.listdir(os.path.join(org_dir, date_dirs[0]))
        self.assertEqual(len(chunk_files), 1)
        self.assertTrue(chunk_files[0].endswith(".parquet"))

    @freeze_time("2026-02-23 12:00:00")
    def test_promote_skips_recent_rows(self):
        """Rows newer than 5 minutes are not promoted."""
        recent_ts = datetime(2026, 2, 23, 11, 59, 0, tzinfo=timezone.utc)
        span = _make_span_staging_row(self.org.id, self.project.id, timestamp=recent_ts)
        SpanStaging.objects.bulk_create([span])

        promoted, truncated = promote_spans()

        self.assertEqual(promoted, 0)
        self.assertFalse(truncated)
        self.assertEqual(SpanStaging.objects.count(), 1)

    def test_promote_groups_by_org(self):
        """Each org gets its own Parquet directory."""
        project2 = baker.make("projects.Project")
        org2 = project2.organization
        ts = datetime.now(timezone.utc) - timedelta(minutes=10)

        SpanStaging.objects.bulk_create(
            [
                _make_span_staging_row(self.org.id, self.project.id, timestamp=ts),
                _make_span_staging_row(org2.id, project2.id, timestamp=ts, span_id="x"),
            ]
        )

        promoted, truncated = promote_spans()

        self.assertEqual(promoted, 2)
        self.assertFalse(truncated)
        self.assertEqual(SpanStaging.objects.count(), 0)

        spans_dir = os.path.join(self.cold_dir, "cold_storage/performance_spans")
        org_dirs = sorted(os.listdir(spans_dir))
        self.assertEqual(len(org_dirs), 2)
        self.assertIn(f"org_{self.org.id}", org_dirs)
        self.assertIn(f"org_{org2.id}", org_dirs)


class CompactSpansTestCase(ColdStorageTestMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.project = baker.make(
            "projects.Project", organization__scrub_ip_addresses=False
        )
        self.org = self.project.organization

    def _write_test_chunks(self, date_str: str, num_chunks: int = 3):
        """Write multiple chunk Parquet files for testing compaction."""
        from glitchtip.cold_storage import (
            COLD_STORAGE_PREFIX,
            get_duckdb_connection,
            get_duckdb_parquet_path,
        )

        ts = datetime.strptime(date_str, "%Y%m%d").replace(tzinfo=timezone.utc)
        for i in range(num_chunks):
            rows = [
                (
                    self.org.id,
                    self.project.id,
                    "/api/test/",
                    f"span{i}_{j}",
                    f"txn{i}",
                    "db",
                    "SELECT %s",
                    10.0 + j,
                    ts + timedelta(seconds=i * 10 + j),
                )
                for j in range(3)
            ]
            # Write directly with unique filenames to avoid timestamp collision
            org_dir = (
                f"{COLD_STORAGE_PREFIX}/performance_spans/org_{self.org.id}/{date_str}"
            )
            relative_path = f"{org_dir}/chunk_{i}.parquet"
            parquet_path = get_duckdb_parquet_path(self.storage, relative_path)
            os.makedirs(os.path.dirname(parquet_path), exist_ok=True)

            duck_conn = get_duckdb_connection(self.storage)
            try:
                duck_conn.execute("""
                    CREATE TEMPORARY TABLE staging (
                        organization_id INTEGER, project_id INTEGER,
                        transaction_name VARCHAR, span_id VARCHAR,
                        transaction_id VARCHAR, op VARCHAR,
                        description VARCHAR, duration DOUBLE,
                        timestamp TIMESTAMP WITH TIME ZONE
                    )
                """)
                duck_conn.executemany(
                    "INSERT INTO staging VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    rows,
                )
                duck_conn.execute(
                    f"COPY staging TO '{parquet_path}' (FORMAT PARQUET, COMPRESSION ZSTD)"
                )
            finally:
                duck_conn.close()

    def test_compact_merges_chunks(self):
        """Compaction merges multiple chunk files into a single flat file."""
        old_date = (datetime.now(timezone.utc) - timedelta(days=3)).strftime("%Y%m%d")
        self._write_test_chunks(old_date, num_chunks=3)

        # Verify chunks exist
        date_dir = os.path.join(
            self.cold_dir,
            f"cold_storage/performance_spans/org_{self.org.id}/{old_date}",
        )
        self.assertEqual(len(os.listdir(date_dir)), 3)

        compacted = compact_span_chunks()

        self.assertGreater(compacted, 0)
        # Flat file should exist
        flat_file = os.path.join(
            self.cold_dir,
            f"cold_storage/performance_spans/org_{self.org.id}/{old_date}.parquet",
        )
        self.assertTrue(os.path.exists(flat_file))

    def test_compact_skips_recent_dates(self):
        """Today's and yesterday's chunks are not compacted (promotion may still write)."""
        today = datetime.now(timezone.utc).strftime("%Y%m%d")
        yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y%m%d")
        self._write_test_chunks(today, num_chunks=3)
        self._write_test_chunks(yesterday, num_chunks=3)

        compacted = compact_span_chunks()

        self.assertEqual(compacted, 0)

    def test_compact_skips_single_chunk(self):
        """A date with only one chunk file is not compacted."""
        old_date = (datetime.now(timezone.utc) - timedelta(days=3)).strftime("%Y%m%d")
        self._write_test_chunks(old_date, num_chunks=1)

        compacted = compact_span_chunks()

        self.assertEqual(compacted, 0)


class EnumerateParquetCrashSafetyTestCase(ColdStorageTestMixin, TestCase):
    """Test that enumerate_org_parquet_files handles compaction crash recovery."""

    def setUp(self):
        super().setUp()
        self.project = baker.make(
            "projects.Project", organization__scrub_ip_addresses=False
        )
        self.org = self.project.organization

    def test_flat_file_shadows_chunk_files(self):
        """When a compacted flat file exists, chunks for the same date are skipped."""
        date_str = "20260220"
        ts = datetime(2026, 2, 20, 12, 0, 0, tzinfo=timezone.utc)

        # Write chunk files
        chunk_rows = [
            (
                str(UUID7Helper.from_datetime(ts)),
                self.org.id,
                self.project.id,
                "/api/test/",
                "span1",
                "txn1",
                "db",
                "SELECT %s",
                10.0,
                ts,
            )
        ]
        _write_chunk_parquet(self.storage, self.org.id, date_str, chunk_rows)

        # Also write a compacted flat file (simulating post-crash state)
        from glitchtip.cold_storage import (
            get_duckdb_connection,
            get_duckdb_parquet_path,
        )

        org_prefix = f"cold_storage/performance_spans/org_{self.org.id}"
        flat_path = get_duckdb_parquet_path(
            self.storage, f"{org_prefix}/{date_str}.parquet"
        )

        duck_conn = get_duckdb_connection(self.storage)
        try:
            duck_conn.execute("""
                CREATE TABLE flat_data (
                    organization_id INTEGER, project_id INTEGER,
                    transaction_name VARCHAR, span_id VARCHAR,
                    transaction_id VARCHAR, op VARCHAR,
                    description VARCHAR, duration DOUBLE,
                    timestamp TIMESTAMP WITH TIME ZONE
                )
            """)
            duck_conn.execute(
                "INSERT INTO flat_data VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    self.org.id,
                    self.project.id,
                    "/api/test/",
                    "span1",
                    "txn1",
                    "db",
                    "SELECT %s",
                    10.0,
                    ts,
                ],
            )
            duck_conn.execute(
                f"COPY flat_data TO '{flat_path}' (FORMAT PARQUET, COMPRESSION ZSTD)"
            )
        finally:
            duck_conn.close()

        # Enumerate — should return only the flat file, not chunks
        start_dt = datetime(2026, 2, 20, 0, 0, 0, tzinfo=timezone.utc)
        end_dt = datetime(2026, 2, 21, 0, 0, 0, tzinfo=timezone.utc)
        paths = enumerate_org_parquet_files(
            self.storage, TABLE_NAME, self.org.id, start_dt, end_dt
        )

        self.assertEqual(len(paths), 1)
        self.assertIn(f"{date_str}.parquet", paths[0])
        self.assertNotIn("chunk_", paths[0])


class QueryColdStorageTestCase(ColdStorageTestMixin, TestCase):
    """Test DuckDB query functions against real Parquet files."""

    def setUp(self):
        super().setUp()
        self.project = baker.make(
            "projects.Project", organization__scrub_ip_addresses=False
        )
        self.org = self.project.organization
        self.ts = datetime(2026, 2, 20, 12, 0, 0, tzinfo=timezone.utc)

    def _write_test_data(self, rows: list[tuple]):
        """Write test rows to a chunk Parquet file."""
        date_str = self.ts.strftime("%Y%m%d")
        _write_chunk_parquet(self.storage, self.org.id, date_str, rows)

    def _make_row(self, **kwargs):
        """Build a tuple suitable for _write_chunk_parquet."""
        defaults = {
            "id": str(UUID7Helper.from_datetime(self.ts)),
            "org_id": self.org.id,
            "project_id": self.project.id,
            "transaction_name": "/api/test/",
            "span_id": "span1",
            "transaction_id": "txn1",
            "op": "db",
            "description": "SELECT %s FROM users",
            "duration": 10.0,
            "timestamp": self.ts,
        }
        defaults.update(kwargs)
        return (
            defaults["id"],
            defaults["org_id"],
            defaults["project_id"],
            defaults["transaction_name"],
            defaults["span_id"],
            defaults["transaction_id"],
            defaults["op"],
            defaults["description"],
            defaults["duration"],
            defaults["timestamp"],
        )

    def test_query_span_groups_for_transaction(self):
        """Query span groups for a specific transaction."""
        rows = [
            self._make_row(
                id=str(UUID7Helper.from_datetime(self.ts + timedelta(seconds=i))),
                span_id=f"s{i}",
                op="db",
                description="SELECT %s FROM users",
                duration=10.0 + i,
            )
            for i in range(5)
        ]
        self._write_test_data(rows)

        start = datetime(2026, 2, 20, 0, 0, 0, tzinfo=timezone.utc)
        end = datetime(2026, 2, 21, 0, 0, 0, tzinfo=timezone.utc)
        results = query_span_groups_for_transaction(
            self.org.id, "/api/test/", start, end
        )

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["op"], "db")
        self.assertEqual(results[0]["count"], 5)
        self.assertAlmostEqual(results[0]["avg_duration"], 12.0, places=0)

    def test_query_span_groups(self):
        """Query span groups across the organization."""
        rows = [
            self._make_row(
                id=str(UUID7Helper.from_datetime(self.ts + timedelta(seconds=i))),
                span_id=f"s{i}",
                op="db" if i < 3 else "http.client",
                description="SELECT %s" if i < 3 else "GET /api",
                duration=10.0,
            )
            for i in range(5)
        ]
        self._write_test_data(rows)

        start = datetime(2026, 2, 20, 0, 0, 0, tzinfo=timezone.utc)
        end = datetime(2026, 2, 21, 0, 0, 0, tzinfo=timezone.utc)
        results = query_span_groups(self.org.id, None, start, end)

        self.assertEqual(len(results), 2)
        ops = {r["op"] for r in results}
        self.assertEqual(ops, {"db", "http.client"})

    def test_query_n_plus_one_patterns(self):
        """Detect N+1 patterns from span data."""
        # 20 DB spans across 2 transactions → 10 spans/txn (above threshold=5)
        rows = []
        for txn_idx in range(2):
            for span_idx in range(10):
                i = txn_idx * 10 + span_idx
                rows.append(
                    self._make_row(
                        id=str(
                            UUID7Helper.from_datetime(self.ts + timedelta(seconds=i))
                        ),
                        span_id=f"s{i}",
                        transaction_id=f"txn{txn_idx}",
                        op="db",
                        description="SELECT %s FROM users",
                        duration=5.0,
                    )
                )
        self._write_test_data(rows)

        start = datetime(2026, 2, 20, 0, 0, 0, tzinfo=timezone.utc)
        end = datetime(2026, 2, 21, 0, 0, 0, tzinfo=timezone.utc)
        results = query_n_plus_one_patterns(
            self.org.id, None, start, end, threshold=5.0
        )

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["total_spans"], 20)
        self.assertEqual(results[0]["transaction_count"], 2)
        self.assertAlmostEqual(results[0]["spans_per_txn"], 10.0, places=0)

    def test_query_transaction_trend(self):
        """Query daily trend for a transaction."""
        # Spans across 2 days
        day1 = datetime(2026, 2, 20, 12, 0, 0, tzinfo=timezone.utc)
        day2 = datetime(2026, 2, 21, 12, 0, 0, tzinfo=timezone.utc)

        rows_day1 = [
            self._make_row(
                id=str(UUID7Helper.from_datetime(day1 + timedelta(seconds=i))),
                span_id=f"d1s{i}",
                duration=10.0,
                timestamp=day1 + timedelta(seconds=i),
            )
            for i in range(3)
        ]
        rows_day2 = [
            self._make_row(
                id=str(UUID7Helper.from_datetime(day2 + timedelta(seconds=i))),
                span_id=f"d2s{i}",
                duration=20.0,
                timestamp=day2 + timedelta(seconds=i),
            )
            for i in range(2)
        ]

        # Write day 1 data
        _write_chunk_parquet(
            self.storage, self.org.id, day1.strftime("%Y%m%d"), rows_day1
        )
        # Write day 2 data
        _write_chunk_parquet(
            self.storage, self.org.id, day2.strftime("%Y%m%d"), rows_day2
        )

        start = datetime(2026, 2, 20, 0, 0, 0, tzinfo=timezone.utc)
        end = datetime(2026, 2, 22, 0, 0, 0, tzinfo=timezone.utc)
        results = query_transaction_trend(self.org.id, "/api/test/", start, end)

        self.assertEqual(len(results), 2)
        # Day 1: 3 spans at 10ms
        self.assertEqual(results[0]["count"], 3)
        self.assertAlmostEqual(results[0]["avg_duration"], 10.0, places=0)
        # Day 2: 2 spans at 20ms
        self.assertEqual(results[1]["count"], 2)
        self.assertAlmostEqual(results[1]["avg_duration"], 20.0, places=0)

    def test_query_empty_org(self):
        """Queries return empty results when no data exists."""
        start = datetime(2026, 2, 20, 0, 0, 0, tzinfo=timezone.utc)
        end = datetime(2026, 2, 21, 0, 0, 0, tzinfo=timezone.utc)

        results = query_span_groups(self.org.id, None, start, end)
        self.assertEqual(results, [])

    def test_query_date_range_filtering(self):
        """Only data within the date range is returned."""
        rows = [
            self._make_row(
                id=str(UUID7Helper.from_datetime(self.ts)),
                duration=10.0,
            )
        ]
        self._write_test_data(rows)

        # Query a different date range — should find nothing
        start = datetime(2026, 3, 1, 0, 0, 0, tzinfo=timezone.utc)
        end = datetime(2026, 3, 2, 0, 0, 0, tzinfo=timezone.utc)
        results = query_span_groups(self.org.id, None, start, end)
        self.assertEqual(results, [])
