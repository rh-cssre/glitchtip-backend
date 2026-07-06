"""
Integration tests for the hour-tiered span cold-storage pipeline.

Covers: SpanStaging → hour-bucketed promotion → idempotent hourly seal →
lazy daily roll → trend rollups → split-retention cleanup → DuckDB reads,
plus the process-wide DuckDB concurrency bound.
"""

import os
import shutil
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from unittest import mock

from asgiref.sync import async_to_sync
from django.core.files.storage import FileSystemStorage
from django.test import TestCase
from freezegun import freeze_time
from model_bakery import baker

from apps.performance.cold_storage import (
    ROLLUP_STORAGE_PREFIX,
    STORAGE_PREFIX,
    enumerate_span_files,
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
from glitchtip import cold_storage as gcs
from glitchtip.cold_storage import get_duckdb_connection, get_duckdb_parquet_path
from glitchtip.partition_manager import UUID7Helper

RAW = f"cold_storage/{STORAGE_PREFIX}"
ROLLUP = f"cold_storage/{ROLLUP_STORAGE_PREFIX}"


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


def _row(org_id, project_id, ts, **kw):
    """A 10-tuple matching _write_chunk_parquet's row layout."""
    return (
        "id",
        org_id,
        project_id,
        kw.get("transaction_name", "/api/test/"),
        kw.get("span_id", "s1"),
        kw.get("transaction_id", "txn1"),
        kw.get("op", "db"),
        kw.get("description", "SELECT %s FROM users"),
        kw.get("duration", 10.0),
        ts,
    )


class ColdStorageTestMixin:
    """Temp dir + cold-storage backend patched to it."""

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

    def _write_chunk(self, org_id, dt, rows):
        """Write one chunk Parquet into {date}/{HH}/ for dt's hour."""
        async_to_sync(_write_chunk_parquet)(
            self.storage, org_id, dt.strftime("%Y%m%d"), dt.strftime("%H"), rows
        )

    def _exists(self, rel):
        return os.path.exists(os.path.join(self.cold_dir, rel))


class PromoteSpansTestCase(ColdStorageTestMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.project = baker.make(
            "projects.Project", organization__scrub_ip_addresses=False
        )
        self.org = self.project.organization

    async def test_promote_writes_hour_bucketed_chunk(self):
        """Promotion writes chunks under {date}/{HH}/ and clears staging."""
        ts = datetime.now(timezone.utc) - timedelta(minutes=10)
        await SpanStaging.objects.abulk_create(
            [
                _make_span_staging_row(
                    self.org.id,
                    self.project.id,
                    timestamp=ts,
                    span_id=f"s{i}",
                    transaction_id=f"t{i}",
                )
                for i in range(5)
            ]
        )
        promoted, truncated = await promote_spans()
        self.assertEqual(promoted, 5)
        self.assertFalse(truncated)
        self.assertEqual(await SpanStaging.objects.acount(), 0)

        hour_dir = os.path.join(
            self.cold_dir,
            f"{RAW}/org_{self.org.id}/{ts.strftime('%Y%m%d')}/{ts.strftime('%H')}",
        )
        self.assertTrue(os.path.isdir(hour_dir))
        chunks = os.listdir(hour_dir)
        self.assertEqual(len(chunks), 1)
        self.assertTrue(chunks[0].endswith(".parquet"))

    @freeze_time("2026-02-23 12:00:00")
    async def test_promote_skips_recent_rows(self):
        recent = datetime(2026, 2, 23, 11, 59, 0, tzinfo=timezone.utc)
        await SpanStaging.objects.abulk_create(
            [_make_span_staging_row(self.org.id, self.project.id, timestamp=recent)]
        )
        promoted, _ = await promote_spans()
        self.assertEqual(promoted, 0)
        self.assertEqual(await SpanStaging.objects.acount(), 1)

    async def test_promote_drops_and_consumes_garbage(self):
        """Far-future spans are dropped but still consumed from staging."""
        ts_ok = datetime.now(timezone.utc) - timedelta(minutes=10)
        # Older than raw retention — selected by promotion (old UUID7 id),
        # then dropped by the garbage guard.
        ts_old = datetime.now(timezone.utc) - timedelta(days=365)
        await SpanStaging.objects.abulk_create(
            [
                _make_span_staging_row(self.org.id, self.project.id, timestamp=ts_ok),
                _make_span_staging_row(
                    self.org.id, self.project.id, timestamp=ts_old, span_id="f"
                ),
            ]
        )
        promoted, _ = await promote_spans()
        self.assertEqual(promoted, 1)
        # Both consumed — the dropped one must not linger and re-loop.
        self.assertEqual(await SpanStaging.objects.acount(), 0)


class CompactSpansTestCase(ColdStorageTestMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.project = baker.make(
            "projects.Project", organization__scrub_ip_addresses=False
        )
        self.org = self.project.organization
        # A fixed hour well in the past.
        self.hour = datetime(2026, 5, 10, 2, 0, 0, tzinfo=timezone.utc)

    def _seed_hour(self, n_chunks=3, rows_per=3):
        for c in range(n_chunks):
            rows = [
                _row(
                    self.org.id,
                    self.project.id,
                    self.hour + timedelta(seconds=c * 10 + j),
                    span_id=f"s{c}_{j}",
                    duration=10.0 + j,
                )
                for j in range(rows_per)
            ]
            self._write_chunk(self.org.id, self.hour, rows)

    def _hour_file(self):
        return f"{RAW}/org_{self.org.id}/20260510/02.parquet"

    def _chunk_dir(self):
        return f"{RAW}/org_{self.org.id}/20260510/02"

    def _day_file(self):
        return f"{RAW}/org_{self.org.id}/20260510.parquet"

    def test_open_hour_not_sealed(self):
        """An hour still within SEAL_GRACE is left as chunks."""
        self._seed_hour(2)
        with freeze_time("2026-05-10T02:30:00Z"):
            self.assertEqual(compact_span_chunks(), 0)
        self.assertFalse(self._exists(self._hour_file()))
        self.assertTrue(self._exists(self._chunk_dir()))

    def test_hour_seals_after_grace(self):
        """Past SEAL_GRACE the hour collapses to one file + a rollup."""
        self._seed_hour(3)
        with freeze_time("2026-05-10T04:00:00Z"):  # day not yet rolled
            self.assertEqual(compact_span_chunks(), 3)
        self.assertTrue(self._exists(self._hour_file()))
        self.assertFalse(self._exists(self._chunk_dir()))
        self.assertTrue(self._exists(f"{ROLLUP}/org_{self.org.id}/20260510/02.parquet"))

    def test_seal_is_idempotent(self):
        """Re-running after a seal is a no-op."""
        self._seed_hour(2)
        with freeze_time("2026-05-10T04:00:00Z"):
            self.assertEqual(compact_span_chunks(), 2)
            self.assertEqual(compact_span_chunks(), 0)
        self.assertTrue(self._exists(self._hour_file()))

    def test_late_chunk_after_seal_is_dropped(self):
        """A chunk arriving after the hour sealed is dropped, never merged."""
        self._seed_hour(2)
        with freeze_time("2026-05-10T04:00:00Z"):
            self.assertEqual(compact_span_chunks(), 2)
            size_before = os.path.getsize(
                os.path.join(self.cold_dir, self._hour_file())
            )
            # A late chunk shows up for the already-sealed hour.
            self._write_chunk(
                self.org.id,
                self.hour,
                [_row(self.org.id, self.project.id, self.hour, span_id="late")],
            )
            self.assertEqual(compact_span_chunks(), 0)
        # Sealed file untouched; late chunk dir removed.
        self.assertEqual(
            os.path.getsize(os.path.join(self.cold_dir, self._hour_file())),
            size_before,
        )
        self.assertFalse(self._exists(self._chunk_dir()))

    def test_daily_roll_when_day_fully_sealed(self):
        """Once the whole day is sealed it collapses to {date}.parquet."""
        self._seed_hour(2)
        with freeze_time("2026-05-11T01:00:00Z"):  # > day_end + grace
            compact_span_chunks()
        self.assertTrue(self._exists(self._day_file()))
        self.assertFalse(self._exists(f"{RAW}/org_{self.org.id}/20260510"))
        self.assertTrue(self._exists(f"{ROLLUP}/org_{self.org.id}/20260510.parquet"))

    def test_rollup_content(self):
        """Rollup carries per-group aggregates bucketed by the hour."""
        self._seed_hour(n_chunks=1, rows_per=4)
        with freeze_time("2026-05-10T04:00:00Z"):
            compact_span_chunks()
        rollup = get_duckdb_parquet_path(
            self.storage, f"{ROLLUP}/org_{self.org.id}/20260510/02.parquet"
        )
        conn = get_duckdb_connection(self.storage)
        try:
            row = conn.execute(
                f"SELECT transaction_name, count, sum_duration, p95, hour_bucket "
                f"FROM read_parquet('{rollup}')"
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(row[0], "/api/test/")
        self.assertEqual(row[1], 4)  # 4 rows
        # hour_bucket is a naive UTC timestamp at the hour start.
        self.assertEqual(row[4], self.hour.replace(tzinfo=None))

    def test_recovers_missing_rollup_after_seal(self):
        """If the rollup write fails after a successful hour seal, the next
        compaction tick re-derives it from the sealed file. Otherwise a
        transient S3 error or slot timeout would silently drop the hour
        from trend queries forever."""
        self._seed_hour(n_chunks=1, rows_per=4)
        rollup_path = f"{ROLLUP}/org_{self.org.id}/20260510/02.parquet"

        # First pass: seal succeeds, rollup write blows up — exactly the
        # state we need to recover from.
        with (
            mock.patch(
                "apps.performance.promotion._write_hour_rollup",
                side_effect=RuntimeError("transient s3 error"),
            ),
            freeze_time("2026-05-10T04:00:00Z"),
        ):
            compact_span_chunks()
        self.assertTrue(self._exists(self._hour_file()))
        self.assertFalse(self._exists(rollup_path))
        self.assertFalse(self._exists(self._chunk_dir()))  # chunks already gone

        # Second pass without the mock: recovery loop fires and writes the
        # rollup from the sealed hour file.
        with freeze_time("2026-05-10T04:30:00Z"):
            compact_span_chunks()
        self.assertTrue(self._exists(rollup_path))

        # Recovered rollup matches what we'd have gotten on the first pass.
        rollup = get_duckdb_parquet_path(self.storage, rollup_path)
        conn = get_duckdb_connection(self.storage)
        try:
            row = conn.execute(
                f"SELECT count, hour_bucket FROM read_parquet('{rollup}')"
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(row[0], 4)
        self.assertEqual(row[1], self.hour.replace(tzinfo=None))


class EnumerateSpanFilesTestCase(ColdStorageTestMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.project = baker.make(
            "projects.Project", organization__scrub_ip_addresses=False
        )
        self.org = self.project.organization
        self.hour = datetime(2026, 5, 10, 2, 0, 0, tzinfo=timezone.utc)
        self.start = datetime(2026, 5, 10, tzinfo=timezone.utc)
        self.end = datetime(2026, 5, 11, tzinfo=timezone.utc)

    def _enum(self):
        return enumerate_span_files(self.storage, self.org.id, self.start, self.end)

    def test_open_hour_returns_chunks(self):
        self._write_chunk(
            self.org.id, self.hour, [_row(self.org.id, self.project.id, self.hour)]
        )
        paths = self._enum()
        self.assertEqual(len(paths), 1)
        self.assertIn("/02/chunk_", paths[0])

    def test_sealed_hour_shadows_chunks(self):
        self._write_chunk(
            self.org.id, self.hour, [_row(self.org.id, self.project.id, self.hour)]
        )
        with freeze_time("2026-05-10T04:00:00Z"):
            compact_span_chunks()
        paths = self._enum()
        self.assertEqual(len(paths), 1)
        self.assertTrue(paths[0].endswith("/02.parquet"))

    def test_daily_file_shadows_date_dir(self):
        self._write_chunk(
            self.org.id, self.hour, [_row(self.org.id, self.project.id, self.hour)]
        )
        with freeze_time("2026-05-11T01:00:00Z"):
            compact_span_chunks()
        paths = self._enum()
        self.assertEqual(len(paths), 1)
        self.assertTrue(paths[0].endswith("/20260510.parquet"))

    def test_legacy_flat_chunks_still_readable_and_swept_up(self):
        """Pre-MR layout sits at ``org/{date}/chunk_*.parquet`` (no
        ``{HH}/`` subdir). Two guarantees on upgrade:

        - The new enumerator returns them so reads keep working immediately
          post-deploy.
        - The daily-roll branch sweeps any ``.parquet`` file under the date
          dir into ``{date}.parquet`` regardless of whether it came from
          the legacy flat layout or the new sealed-hour layout, so legacy
          chunks get incorporated into the sealed-day file the first time
          the day rolls. No standalone backfill needed.
        """
        # Write a real parquet at the new path, then relocate it to the
        # legacy layout to simulate data ingested before this MR.
        self._write_chunk(
            self.org.id, self.hour, [_row(self.org.id, self.project.id, self.hour)]
        )
        hour_dir = os.path.join(
            self.cold_dir, RAW, f"org_{self.org.id}", "20260510", "02"
        )
        legacy_dir = os.path.join(self.cold_dir, RAW, f"org_{self.org.id}", "20260510")
        (chunk_name,) = os.listdir(hour_dir)
        legacy_chunk = f"legacy_{chunk_name}"
        os.rename(
            os.path.join(hour_dir, chunk_name),
            os.path.join(legacy_dir, legacy_chunk),
        )
        os.rmdir(hour_dir)

        # Reads pre-roll: enumerator finds the legacy chunk.
        paths = self._enum()
        self.assertEqual(len(paths), 1)
        self.assertTrue(paths[0].endswith(f"/{legacy_chunk}"))

        # Roll the day. Return value is 0 (counts hourly seals, not daily
        # rolls), but the legacy chunk gets folded into {date}.parquet.
        with freeze_time("2026-05-11T01:00:00Z"):
            self.assertEqual(compact_span_chunks(), 0)
        self.assertTrue(self._exists(f"{RAW}/org_{self.org.id}/20260510.parquet"))
        self.assertFalse(
            self._exists(f"{RAW}/org_{self.org.id}/20260510/{legacy_chunk}")
        )

        # Reads post-roll: same single-file result via the sealed-day path.
        paths = self._enum()
        self.assertEqual(len(paths), 1)
        self.assertTrue(paths[0].endswith("/20260510.parquet"))


class QueryColdStorageTestCase(ColdStorageTestMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.project = baker.make(
            "projects.Project", organization__scrub_ip_addresses=False
        )
        self.org = self.project.organization
        self.ts = datetime(2026, 2, 20, 12, 0, 0, tzinfo=timezone.utc)

    def _write(self, rows):
        self._write_chunk(self.org.id, self.ts, rows)

    def test_query_span_groups_for_transaction(self):
        self._write(
            [
                _row(
                    self.org.id,
                    self.project.id,
                    self.ts + timedelta(seconds=i),
                    span_id=f"s{i}",
                    duration=10.0 + i,
                )
                for i in range(5)
            ]
        )
        start = datetime(2026, 2, 20, tzinfo=timezone.utc)
        end = datetime(2026, 2, 21, tzinfo=timezone.utc)
        results = query_span_groups_for_transaction(
            self.org.id, "/api/test/", start, end
        )
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["count"], 5)
        self.assertAlmostEqual(results[0]["avg_duration"], 12.0, places=0)

    def test_query_span_groups(self):
        self._write(
            [
                _row(
                    self.org.id,
                    self.project.id,
                    self.ts + timedelta(seconds=i),
                    span_id=f"s{i}",
                    op="db" if i < 3 else "http.client",
                    description="SELECT %s" if i < 3 else "GET /api",
                )
                for i in range(5)
            ]
        )
        start = datetime(2026, 2, 20, tzinfo=timezone.utc)
        end = datetime(2026, 2, 21, tzinfo=timezone.utc)
        results = query_span_groups(self.org.id, None, start, end)
        self.assertEqual({r["op"] for r in results}, {"db", "http.client"})

    def test_query_n_plus_one_patterns(self):
        rows = []
        for txn_idx in range(2):
            for span_idx in range(10):
                rows.append(
                    _row(
                        self.org.id,
                        self.project.id,
                        self.ts + timedelta(seconds=txn_idx * 10 + span_idx),
                        span_id=f"s{txn_idx}_{span_idx}",
                        transaction_id=f"txn{txn_idx}",
                        duration=5.0,
                    )
                )
        self._write(rows)
        start = datetime(2026, 2, 20, tzinfo=timezone.utc)
        end = datetime(2026, 2, 21, tzinfo=timezone.utc)
        results = query_n_plus_one_patterns(
            self.org.id, None, start, end, threshold=5.0
        )
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["total_spans"], 20)
        self.assertEqual(results[0]["transaction_count"], 2)

    def test_query_transaction_trend_reads_rollup(self):
        """Trend reads the rollup tier produced by compaction."""
        day1 = datetime(2026, 5, 1, 12, 0, 0, tzinfo=timezone.utc)
        day2 = datetime(2026, 5, 2, 12, 0, 0, tzinfo=timezone.utc)
        self._write_chunk(
            self.org.id,
            day1,
            [
                _row(
                    self.org.id,
                    self.project.id,
                    day1 + timedelta(seconds=i),
                    span_id=f"a{i}",
                    duration=10.0,
                )
                for i in range(3)
            ],
        )
        self._write_chunk(
            self.org.id,
            day2,
            [
                _row(
                    self.org.id,
                    self.project.id,
                    day2 + timedelta(seconds=i),
                    span_id=f"b{i}",
                    duration=20.0,
                )
                for i in range(2)
            ],
        )
        # Seal + roll both days into rollups.
        with freeze_time("2026-05-05T00:00:00Z"):
            compact_span_chunks()

        results = query_transaction_trend(
            self.org.id,
            "/api/test/",
            datetime(2026, 5, 1, tzinfo=timezone.utc),
            datetime(2026, 5, 3, tzinfo=timezone.utc),
        )
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0]["count"], 3)
        self.assertAlmostEqual(results[0]["avg_duration"], 10.0, places=0)
        self.assertEqual(results[1]["count"], 2)
        self.assertAlmostEqual(results[1]["avg_duration"], 20.0, places=0)

    def test_query_empty_org(self):
        start = datetime(2026, 2, 20, tzinfo=timezone.utc)
        end = datetime(2026, 2, 21, tzinfo=timezone.utc)
        self.assertEqual(query_span_groups(self.org.id, None, start, end), [])


class DuckDBConcurrencyBoundTestCase(TestCase):
    """The process-wide DuckDB slot bounds concurrency, degrading
    request-driven work and blocking must-complete work."""

    def _patch_bound(self, size):
        return mock.patch.multiple(
            gcs,
            _duckdb_semaphore=threading.BoundedSemaphore(size),
            _MAX_CONCURRENT_DUCKDB=size,
            _DUCKDB_SLOT_TIMEOUT=0.05,
        )

    def test_degrades_when_saturated(self):
        with self._patch_bound(1):
            gcs._duckdb_semaphore.acquire()
            try:
                with gcs.duckdb_slot() as slot:
                    self.assertFalse(slot)
            finally:
                gcs._duckdb_semaphore.release()
            with gcs.duckdb_slot() as slot:
                self.assertTrue(slot)

    def test_slot_released_on_exit(self):
        with self._patch_bound(1):
            for _ in range(3):
                with gcs.duckdb_slot() as slot:
                    self.assertTrue(slot)
            self.assertTrue(gcs._duckdb_semaphore.acquire(timeout=0.05))
            gcs._duckdb_semaphore.release()

    def test_block_waits_for_slot(self):
        with self._patch_bound(1):
            gcs._duckdb_semaphore.acquire()
            acquired: list = []
            started = threading.Event()

            def worker():
                started.set()
                with gcs.duckdb_slot(block=True) as slot:
                    acquired.append(slot)

            t = threading.Thread(target=worker)
            t.start()
            try:
                started.wait(1)
                time.sleep(0.2)
                self.assertEqual(acquired, [])
                gcs._duckdb_semaphore.release()
                t.join(2)
                self.assertEqual(acquired, [True])
            finally:
                t.join(2)


class DeletionTestCase(ColdStorageTestMixin, TestCase):
    """Org/project deletion must purge the hour-tiered raw subtree AND the
    rollup tree (self-hosters without S3 lifecycle rules rely on this)."""

    def setUp(self):
        super().setUp()
        self.project = baker.make(
            "projects.Project", organization__scrub_ip_addresses=False
        )
        self.org = self.project.organization
        # delete_org_cold_storage / rewrite resolve the backend from
        # glitchtip.cold_storage directly — patch there too.
        self._p1 = mock.patch(
            "glitchtip.cold_storage.get_cold_storage_backend",
            return_value=self.storage,
        )
        self._p2 = mock.patch(
            "glitchtip.cold_storage.is_duckdb_available", return_value=True
        )
        self._p1.start()
        self._p2.start()
        self.hour = datetime(2026, 5, 10, 2, 0, 0, tzinfo=timezone.utc)
        self.open_hour = datetime(2026, 5, 10, 5, 0, 0, tzinfo=timezone.utc)

    def tearDown(self):
        self._p1.stop()
        self._p2.stop()
        super().tearDown()

    def test_delete_org_purges_raw_and_rollup_trees(self):
        from glitchtip.cold_storage import delete_org_cold_storage

        other = baker.make("projects.Project")
        # Sealed hour (+rollup) and an untouched open chunk (depth-3 path).
        self._write_chunk(
            self.org.id, self.hour, [_row(self.org.id, self.project.id, self.hour)]
        )
        with freeze_time("2026-05-10T04:00:00Z"):
            compact_span_chunks()
        self._write_chunk(
            self.org.id,
            self.open_hour,
            [_row(self.org.id, self.project.id, self.open_hour)],
        )
        # Control org must survive.
        self._write_chunk(
            other.organization_id,
            self.hour,
            [_row(other.organization_id, other.id, self.hour)],
        )

        deleted = delete_org_cold_storage(self.org.id, STORAGE_PREFIX)
        delete_org_cold_storage(self.org.id, ROLLUP_STORAGE_PREFIX)

        self.assertGreater(deleted, 0)
        self.assertFalse(self._exists(f"{RAW}/org_{self.org.id}"))
        self.assertFalse(self._exists(f"{ROLLUP}/org_{self.org.id}"))
        self.assertTrue(self._exists(f"{RAW}/org_{other.organization_id}"))

    def test_project_rewrite_drops_project_from_raw_and_rollup(self):
        from glitchtip.cold_storage import rewrite_parquet_excluding_project

        p2 = baker.make("projects.Project", organization=self.org)
        self._write_chunk(
            self.org.id,
            self.hour,
            [
                _row(self.org.id, self.project.id, self.hour, span_id="p1a"),
                _row(self.org.id, self.project.id, self.hour, span_id="p1b"),
                _row(self.org.id, p2.id, self.hour, span_id="p2a"),
            ],
        )
        with freeze_time("2026-05-10T04:00:00Z"):
            compact_span_chunks()  # sealed hour + rollup carry project_id

        rewrite_parquet_excluding_project(
            org_id=self.org.id,
            project_id=self.project.id,
            storage_prefix=STORAGE_PREFIX,
        )
        rewrite_parquet_excluding_project(
            org_id=self.org.id,
            project_id=self.project.id,
            storage_prefix=ROLLUP_STORAGE_PREFIX,
        )

        raw = get_duckdb_parquet_path(
            self.storage, f"{RAW}/org_{self.org.id}/20260510/02.parquet"
        )
        rollup = get_duckdb_parquet_path(
            self.storage, f"{ROLLUP}/org_{self.org.id}/20260510/02.parquet"
        )
        conn = get_duckdb_connection(self.storage)
        try:
            raw_projects = [
                r[0]
                for r in conn.execute(
                    f"SELECT DISTINCT project_id FROM read_parquet('{raw}')"
                ).fetchall()
            ]
            rollup_projects = [
                r[0]
                for r in conn.execute(
                    f"SELECT DISTINCT project_id FROM read_parquet('{rollup}')"
                ).fetchall()
            ]
        finally:
            conn.close()
        self.assertEqual(raw_projects, [p2.id])
        self.assertEqual(rollup_projects, [p2.id])
