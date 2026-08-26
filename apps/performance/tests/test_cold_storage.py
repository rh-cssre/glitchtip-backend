"""
Integration tests for the hour-tiered span cold-storage pipeline.

Covers: SpanStaging → hour-bucketed promotion → idempotent hourly seal →
lazy daily roll → trend rollups → split-retention cleanup → DuckDB reads,
plus the process-wide DuckDB concurrency bound.
"""

import logging
import os
import shutil
import tempfile
import threading
import time
from datetime import UTC, datetime, timedelta
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
    _duckdb_copy,
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
        timestamp = datetime.now(UTC) - timedelta(minutes=10)
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


class PhantomListingStorage(FileSystemStorage):
    """Storage whose ``listdir`` reports files that are not really there.

    Reproduces the production symptom: an object listing hands compaction an
    input key, and the subsequent read of that key 404s. ``exists()`` stays
    honest, so it is the phantom that a tolerant compaction must drop.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.phantom_files: dict[str, list[str]] = {}

    def listdir(self, path):
        dirs, files = super().listdir(path)
        extra = [f for f in self.phantom_files.get(path, []) if f not in files]
        return dirs, files + extra


class ColdStorageTestMixin:
    """Temp dir + cold-storage backend patched to it."""

    storage_class = FileSystemStorage

    def setUp(self):
        super().setUp()
        self.cold_dir = tempfile.mkdtemp()
        self.storage = self.storage_class(location=self.cold_dir)
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
        ts = datetime.now(UTC) - timedelta(minutes=10)
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
        recent = datetime(2026, 2, 23, 11, 59, 0, tzinfo=UTC)
        await SpanStaging.objects.abulk_create(
            [_make_span_staging_row(self.org.id, self.project.id, timestamp=recent)]
        )
        promoted, _ = await promote_spans()
        self.assertEqual(promoted, 0)
        self.assertEqual(await SpanStaging.objects.acount(), 1)

    async def test_promote_drops_and_consumes_garbage(self):
        """Far-future spans are dropped but still consumed from staging."""
        ts_ok = datetime.now(UTC) - timedelta(minutes=10)
        # Older than raw retention — selected by promotion (old UUID7 id),
        # then dropped by the garbage guard.
        ts_old = datetime.now(UTC) - timedelta(days=365)
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
        self.hour = datetime(2026, 5, 10, 2, 0, 0, tzinfo=UTC)

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

    def test_rollup_quantiles_are_exact(self):
        """p50/p95 are real observed durations, not sketch estimates.

        The rollup uses quantile_disc rather than approx_quantile: a T-Digest
        costs a fixed ~20 KiB of state per group regardless of how few values
        that group holds, and that state is not charged against DuckDB's
        memory_limit, so it grows without bound as description cardinality
        grows. Exactness is the observable half of that change — over
        durations 10.0..109.0 approx_quantile answers 59.5 and 104.49999...,
        neither of which any span actually took.
        """
        self._seed_hour(n_chunks=1, rows_per=100)
        with freeze_time("2026-05-10T04:00:00Z"):
            compact_span_chunks()
        rollup = get_duckdb_parquet_path(
            self.storage, f"{ROLLUP}/org_{self.org.id}/20260510/02.parquet"
        )
        conn = get_duckdb_connection(self.storage)
        try:
            count, p50, p95, lo, hi = conn.execute(
                f"SELECT count, p50, p95, min_duration, max_duration "
                f"FROM read_parquet('{rollup}')"
            ).fetchone()
        finally:
            conn.close()

        durations = [10.0 + j for j in range(100)]
        self.assertEqual(count, 100)
        self.assertEqual(lo, 10.0)
        self.assertEqual(hi, 109.0)
        # Assert membership, not the exact index convention: duckdb is pinned
        # only as >=1.2.0, and which element a discrete quantile picks is an
        # implementation detail that may shift on a minor bump. Membership is
        # the real invariant and still separates the three functions — over
        # this input approx_quantile answers 59.5 and 104.4999988079071 and
        # quantile_cont answers 59.5 and 104.05, none of which are members.
        self.assertIn(p50, durations)
        self.assertIn(p95, durations)
        self.assertLess(p50, p95)

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


class CompactionMissingInputTestCase(ColdStorageTestMixin, TestCase):
    """A cold-storage input that vanished must not wedge a date forever.

    Compaction builds its input list from a directory listing and only
    deletes the source directory *after* a successful copy. So if the
    listing names an object the read then 404s on, the copy fails, the
    source directory survives, and the next run lists the same missing key
    and fails identically — permanently, without ever making progress.
    """

    storage_class = PhantomListingStorage

    # Shaped like a real pre-hour-tiering chunk name, which lives directly in
    # the date directory rather than under an {HH}/ subdirectory.
    PHANTOM = "chunk_1777896579649605123_1062465.parquet"

    def setUp(self):
        super().setUp()
        self.project = baker.make(
            "projects.Project", organization__scrub_ip_addresses=False
        )
        self.org = self.project.organization
        self.date_path = f"{RAW}/org_{self.org.id}/20260510"
        self.day_file = f"{RAW}/org_{self.org.id}/20260510.parquet"

    def _seed_hour(self, hour, rows=2):
        dt = datetime(2026, 5, 10, hour, 0, 0, tzinfo=UTC)
        self._write_chunk(
            self.org.id,
            dt,
            [
                _row(
                    self.org.id,
                    self.project.id,
                    dt + timedelta(seconds=j),
                    span_id=f"s{hour}_{j}",
                )
                for j in range(rows)
            ],
        )

    def _add_phantom(self, directory, name):
        self.storage.phantom_files.setdefault(directory, []).append(name)

    def _strict_copy(self, calls=None):
        """Make DuckDB reads fail on a missing input the way S3 does.

        DuckDB's local-filesystem reader treats each input path as a glob and
        silently drops one that matches nothing, so a temp dir cannot
        reproduce the production failure on its own. Its httpfs reader
        instead raises ``HTTP 404`` on the individual key. This wrapper
        forces the S3 behaviour, and optionally records the input list of
        every copy attempt.
        """
        real = _duckdb_copy

        def strict(storage, inputs, output):
            if calls is not None:
                calls.append(list(inputs))
            for rel in inputs:
                if not os.path.exists(os.path.join(self.cold_dir, rel)):
                    raise OSError(
                        "HTTP Error: HTTP GET error reading "
                        f"'{rel}' in region '' (HTTP 404 Not Found)"
                    )
            return real(storage, inputs, output)

        return mock.patch("apps.performance.promotion._duckdb_copy", side_effect=strict)

    def _mkdir(self, rel):
        os.makedirs(os.path.join(self.cold_dir, rel), exist_ok=True)

    def _day_row_count(self):
        path = get_duckdb_parquet_path(self.storage, self.day_file)
        conn = get_duckdb_connection(self.storage)
        try:
            return conn.execute(
                f"SELECT count(*) FROM read_parquet('{path}')"
            ).fetchone()[0]
        finally:
            conn.close()

    def test_day_roll_skips_missing_input_and_keeps_the_rest(self):
        """One unreadable input must not cost the day its readable ones."""
        self._seed_hour(2, rows=2)
        self._seed_hour(3, rows=3)
        self._add_phantom(self.date_path, self.PHANTOM)

        # settings.py globally disables WARNING-and-below while testing;
        # lift it so the operator-facing skip warning is assertable.
        previous_disable = logging.root.manager.disable
        logging.disable(logging.NOTSET)
        self.addCleanup(logging.disable, previous_disable)

        with (
            self._strict_copy(),
            freeze_time("2026-05-11T01:00:00Z"),
            self.assertLogs("apps.performance.promotion", level="WARNING") as logs,
        ):
            compact_span_chunks()

        self.assertTrue(self._exists(self.day_file))
        self.assertEqual(self._day_row_count(), 5)
        # Cleaned up, so the missing key is never listed again.
        self.assertFalse(self._exists(self.date_path))
        self.assertTrue(
            any("1 of 3 input files are missing" in line for line in logs.output),
            logs.output,
        )

    def test_day_roll_with_every_input_missing_cleans_up(self):
        """Nothing left to compact is a cleanup, not a failure."""
        self._mkdir(self.date_path)
        self._add_phantom(self.date_path, self.PHANTOM)
        self._add_phantom(self.date_path, "chunk_1777896579649605124_1062465.parquet")

        with self._strict_copy(), freeze_time("2026-05-11T01:00:00Z"):
            compact_span_chunks()

        self.assertFalse(self._exists(self.day_file))
        self.assertFalse(self._exists(self.date_path))

    def test_second_run_does_not_retry_the_missing_input(self):
        """The actual bug: without cleanup the same missing key is retried
        every run, forever. Two passes over one broken fixture — the second
        must not touch the missing input at all."""
        self._seed_hour(2, rows=2)
        self._add_phantom(self.date_path, self.PHANTOM)

        calls: list[list[str]] = []

        with self._strict_copy(calls):
            with freeze_time("2026-05-11T01:00:00Z"):
                compact_span_chunks()
            first_pass = len(calls)
            self.assertTrue(
                any(self.PHANTOM in " ".join(c) for c in calls),
                "fixture never exercised the missing input",
            )
            with freeze_time("2026-05-12T01:00:00Z"):
                compact_span_chunks()

        retried = [c for c in calls[first_pass:] if any(self.PHANTOM in p for p in c)]
        self.assertEqual(retried, [], "second run re-attempted the missing input")
        self.assertTrue(self._exists(self.day_file))

    def test_hour_seal_skips_missing_chunk(self):
        """Same tolerance on the hour-seal path, which has the same
        delete-only-after-success shape."""
        self._seed_hour(2, rows=2)
        chunk_dir = f"{self.date_path}/02"
        self._add_phantom(chunk_dir, self.PHANTOM)

        with self._strict_copy(), freeze_time("2026-05-10T04:00:00Z"):
            compact_span_chunks()

        self.assertTrue(self._exists(f"{self.date_path}/02.parquet"))
        self.assertFalse(self._exists(chunk_dir))

    def test_hour_seal_with_every_chunk_missing_cleans_up(self):
        self._mkdir(f"{self.date_path}/02")
        self._add_phantom(f"{self.date_path}/02", self.PHANTOM)

        with self._strict_copy(), freeze_time("2026-05-10T04:00:00Z"):
            self.assertEqual(compact_span_chunks(), 0)

        self.assertFalse(self._exists(f"{self.date_path}/02.parquet"))
        self.assertFalse(self._exists(f"{self.date_path}/02"))

    def test_unreadable_input_that_still_exists_leaves_the_directory_intact(self):
        """A file that is present but unreadable is not data we may throw
        away, so the date is left for an operator rather than cleaned up."""
        self._seed_hour(2, rows=2)
        with freeze_time("2026-05-11T01:00:00Z"):
            compact_span_chunks()  # seals hour 02 into 02.parquet, rolls the day
        # Corrupt the rolled day file's source state: re-create the date dir
        # with a non-Parquet file that exists but cannot be read.
        self._mkdir(self.date_path)
        os.remove(os.path.join(self.cold_dir, self.day_file))
        with open(os.path.join(self.cold_dir, self.date_path, "05.parquet"), "w") as f:
            f.write("not parquet")

        with freeze_time("2026-05-12T01:00:00Z"):
            compact_span_chunks()

        self.assertFalse(self._exists(self.day_file))
        self.assertTrue(self._exists(f"{self.date_path}/05.parquet"))

    def test_legacy_chunk_in_date_dir_is_rolled_in_not_discarded(self):
        """Pins the legacy-layout decision. A ``chunk_*.parquet`` sitting
        directly in the date directory is pre-hour-tiering span data, not an
        hour file — but it is readable, so the day roll folds it in. Only
        inputs that are actually gone get skipped."""
        self._seed_hour(2, rows=2)
        self._seed_hour(3, rows=3)
        # Relocate hour 03's chunk to the legacy flat layout.
        hour_dir = os.path.join(self.cold_dir, self.date_path, "03")
        (name,) = os.listdir(hour_dir)
        os.rename(
            os.path.join(hour_dir, name),
            os.path.join(self.cold_dir, self.date_path, f"legacy_{name}"),
        )
        os.rmdir(hour_dir)
        self._add_phantom(self.date_path, self.PHANTOM)

        with self._strict_copy(), freeze_time("2026-05-11T01:00:00Z"):
            compact_span_chunks()

        # 2 sealed-hour rows + 3 legacy-chunk rows; the phantom contributes
        # nothing and costs nothing.
        self.assertEqual(self._day_row_count(), 5)
        self.assertFalse(self._exists(self.date_path))


class EnumerateSpanFilesTestCase(ColdStorageTestMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.project = baker.make(
            "projects.Project", organization__scrub_ip_addresses=False
        )
        self.org = self.project.organization
        self.hour = datetime(2026, 5, 10, 2, 0, 0, tzinfo=UTC)
        self.start = datetime(2026, 5, 10, tzinfo=UTC)
        self.end = datetime(2026, 5, 11, tzinfo=UTC)

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
        self.ts = datetime(2026, 2, 20, 12, 0, 0, tzinfo=UTC)

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
        start = datetime(2026, 2, 20, tzinfo=UTC)
        end = datetime(2026, 2, 21, tzinfo=UTC)
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
        start = datetime(2026, 2, 20, tzinfo=UTC)
        end = datetime(2026, 2, 21, tzinfo=UTC)
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
        start = datetime(2026, 2, 20, tzinfo=UTC)
        end = datetime(2026, 2, 21, tzinfo=UTC)
        results = query_n_plus_one_patterns(
            self.org.id, None, start, end, threshold=5.0
        )
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["total_spans"], 20)
        self.assertEqual(results[0]["transaction_count"], 2)

    def test_query_transaction_trend_reads_rollup(self):
        """Trend reads the rollup tier produced by compaction."""
        day1 = datetime(2026, 5, 1, 12, 0, 0, tzinfo=UTC)
        day2 = datetime(2026, 5, 2, 12, 0, 0, tzinfo=UTC)
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
            datetime(2026, 5, 1, tzinfo=UTC),
            datetime(2026, 5, 3, tzinfo=UTC),
        )
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0]["count"], 3)
        self.assertAlmostEqual(results[0]["avg_duration"], 10.0, places=0)
        self.assertEqual(results[1]["count"], 2)
        self.assertAlmostEqual(results[1]["avg_duration"], 20.0, places=0)

    def test_query_empty_org(self):
        start = datetime(2026, 2, 20, tzinfo=UTC)
        end = datetime(2026, 2, 21, tzinfo=UTC)
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
        self.hour = datetime(2026, 5, 10, 2, 0, 0, tzinfo=UTC)
        self.open_hour = datetime(2026, 5, 10, 5, 0, 0, tzinfo=UTC)

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


class PromotePagingTestCase(ColdStorageTestMixin, TestCase):
    """Promotion walks an org's backlog in bounded keyset pages.

    DISABLE_SERVER_SIDE_CURSORS is set globally for PgBouncer, so an iterator
    would not bound the fetch — the page LIMIT is what keeps peak memory flat.
    """

    def setUp(self):
        super().setUp()
        self.project = baker.make(
            "projects.Project", organization__scrub_ip_addresses=False
        )
        self.org = self.project.organization

    async def _seed(self, n, ts=None):
        ts = ts or datetime.now(UTC) - timedelta(minutes=10)
        await SpanStaging.objects.abulk_create(
            [
                _make_span_staging_row(
                    self.org.id,
                    self.project.id,
                    timestamp=ts,
                    span_id=f"s{i}",
                    transaction_id=f"t{i}",
                )
                for i in range(n)
            ]
        )
        return ts

    async def test_pages_through_backlog(self):
        """Every row is promoted even when the backlog spans many pages."""
        ts = await self._seed(7)
        with mock.patch("apps.performance.promotion.PROMOTE_PAGE_ROWS", 2):
            promoted, truncated = await promote_spans()

        self.assertEqual(promoted, 7)
        self.assertFalse(truncated)
        self.assertEqual(await SpanStaging.objects.acount(), 0)

        hour_dir = os.path.join(
            self.cold_dir,
            f"{RAW}/org_{self.org.id}/{ts.strftime('%Y%m%d')}/{ts.strftime('%H')}",
        )
        # 7 rows at 2 per page -> 4 chunk files, one per page.
        self.assertEqual(len(os.listdir(hour_dir)), 4)

    async def test_page_boundary_exactly_divides(self):
        """A backlog that is a whole number of pages still terminates."""
        await self._seed(4)
        with mock.patch("apps.performance.promotion.PROMOTE_PAGE_ROWS", 2):
            promoted, truncated = await promote_spans()
        self.assertEqual(promoted, 4)
        self.assertFalse(truncated)
        self.assertEqual(await SpanStaging.objects.acount(), 0)

    async def test_batch_limit_truncates(self):
        """The per-org cap still bounds one invocation and reports truncation."""
        await self._seed(6)
        with (
            mock.patch("apps.performance.promotion.PROMOTE_PAGE_ROWS", 2),
            mock.patch("apps.performance.promotion.BATCH_LIMIT_PER_ORG", 4),
        ):
            promoted, truncated = await promote_spans()
        self.assertEqual(promoted, 4)
        self.assertTrue(truncated)
        self.assertEqual(await SpanStaging.objects.acount(), 2)

    async def test_delete_failure_removes_chunk(self):
        """A chunk written but not consumed is rolled back.

        This is the only failure mode here that corrupts data rather than
        retrying: the rows stay in staging, so if the chunk survived, the next
        run would promote them a second time and double-count the spans.
        Paging makes it likelier, not less — N pages per org means N times as
        many write/delete pairs that can half-succeed.
        """
        ts = await self._seed(4)
        with (
            mock.patch("apps.performance.promotion.PROMOTE_PAGE_ROWS", 2),
            mock.patch("apps.performance.promotion.BATCH_LIMIT_PER_ORG", 4),
            mock.patch(
                "apps.performance.promotion._delete_promoted_rows",
                side_effect=OSError("db gone"),
            ),
        ):
            promoted, _ = await promote_spans()

        self.assertEqual(promoted, 0)
        # Rows survive for the next run...
        self.assertEqual(await SpanStaging.objects.acount(), 4)
        # ...and no chunk is left behind to duplicate them.
        hour_dir = os.path.join(
            self.cold_dir,
            f"{RAW}/org_{self.org.id}/{ts.strftime('%Y%m%d')}/{ts.strftime('%H')}",
        )
        self.assertEqual(os.listdir(hour_dir) if os.path.isdir(hour_dir) else [], [])

    async def test_delete_and_cleanup_both_failing_is_logged(self):
        """If the compensating delete also fails, say so loudly.

        The chunk is then orphaned and its rows are still in staging, so the
        next run duplicates them. Nothing can fix that in-process; it has to be
        attributable in the logs.
        """
        await self._seed(2)
        storage = mock.Mock(wraps=self.storage)
        storage.delete.side_effect = OSError("storage gone")
        with (
            mock.patch("apps.performance.promotion.PROMOTE_PAGE_ROWS", 2),
            mock.patch("apps.performance.promotion.BATCH_LIMIT_PER_ORG", 2),
            mock.patch(
                "apps.performance.promotion.get_cold_storage_backend",
                return_value=storage,
            ),
            mock.patch(
                "apps.performance.promotion._delete_promoted_rows",
                side_effect=OSError("db gone"),
            ),
            self.assertLogs("apps.performance.promotion", level="ERROR") as logs,
        ):
            promoted, _ = await promote_spans()

        self.assertEqual(promoted, 0)
        self.assertTrue(
            any("duplicates may" in line for line in logs.output),
            f"expected an orphaned-chunk error, got: {logs.output}",
        )

    async def test_page_of_only_garbage_still_advances(self):
        """A page whose rows are all dropped consumes them and moves on.

        Garbage rows are deleted without being promoted, so the cursor has to
        advance on the drop path too — otherwise an all-garbage page would
        re-select the same rows until the per-org cap.
        """
        # Older than raw retention: selected by promotion, then dropped.
        old = datetime.now(UTC) - timedelta(days=365)
        await SpanStaging.objects.abulk_create(
            [
                _make_span_staging_row(
                    self.org.id,
                    self.project.id,
                    timestamp=old,
                    span_id=f"g{i}",
                    transaction_id=f"g{i}",
                )
                for i in range(4)
            ]
        )
        with mock.patch("apps.performance.promotion.PROMOTE_PAGE_ROWS", 2):
            promoted, truncated = await promote_spans()

        self.assertEqual(promoted, 0)
        self.assertFalse(truncated)
        # All consumed, so they cannot reaccumulate and re-loop next run.
        self.assertEqual(await SpanStaging.objects.acount(), 0)

    async def test_page_spanning_two_hours_writes_both(self):
        """One page split across an hour boundary writes a chunk per hour."""
        base = datetime.now(UTC).replace(minute=30, second=0, microsecond=0)
        first = base - timedelta(hours=2)
        second = first + timedelta(hours=1)
        await SpanStaging.objects.abulk_create(
            [
                _make_span_staging_row(
                    self.org.id,
                    self.project.id,
                    timestamp=ts,
                    span_id=f"h{i}",
                    transaction_id=f"h{i}",
                )
                for i, ts in enumerate([first, first, second, second])
            ]
        )
        with mock.patch("apps.performance.promotion.PROMOTE_PAGE_ROWS", 4):
            promoted, _ = await promote_spans()

        self.assertEqual(promoted, 4)
        self.assertEqual(await SpanStaging.objects.acount(), 0)
        for ts in (first, second):
            hour_dir = os.path.join(
                self.cold_dir,
                f"{RAW}/org_{self.org.id}/{ts.strftime('%Y%m%d')}/{ts.strftime('%H')}",
            )
            self.assertTrue(os.path.isdir(hour_dir), f"missing {ts:%Y%m%d/%H}")
            self.assertEqual(len(os.listdir(hour_dir)), 1)

    async def test_failed_page_does_not_loop_forever(self):
        """A page that fails to write is skipped, not retried in place.

        Its rows stay in staging for the next run, but the keyset cursor has
        already advanced past them, so the loop makes forward progress instead
        of re-selecting the same rows until the batch limit is hit.
        """
        await self._seed(4)
        with (
            mock.patch("apps.performance.promotion.PROMOTE_PAGE_ROWS", 2),
            # Cap the batch too: if the cursor ever stopped advancing this
            # would grind through BATCH_LIMIT_PER_ORG/2 = 10,000 round trips
            # before reaching the assertion, stalling CI instead of failing.
            mock.patch("apps.performance.promotion.BATCH_LIMIT_PER_ORG", 6),
            mock.patch(
                "apps.performance.promotion._write_chunk_parquet",
                side_effect=OSError("cold storage down"),
            ) as write,
        ):
            promoted, _ = await promote_spans()

        self.assertEqual(promoted, 0)
        # Two pages attempted, then the backlog ran out — not an endless retry.
        self.assertEqual(write.call_count, 2)
        self.assertEqual(await SpanStaging.objects.acount(), 4)
