"""Span cold storage against a live S3-compatible server.

Every other cold-storage test patches the storage backend to a local
``FileSystemStorage``, which cannot catch a failure that only appears against
a real object store: enumeration depends on ``ListObjectsV2`` returning
``CommonPrefixes`` for the "directories" in our layout, and DuckDB reads the
Parquet we wrote back over ``s3://`` with ranged GETs. A filesystem stands in
for neither, so nothing else in the suite covers that seam.

These run wherever ``AWS_S3_ENDPOINT_URL`` points -- Garage in CI and under
``compose.s3.yml`` -- and skip when no endpoint is configured, leaving the
S3-less lanes alone.

The bucket is shared with file uploads and with whatever else runs against the
same server, so each test works under a randomised org id and removes its own
tree afterwards.
"""

import random
from datetime import UTC, datetime, timedelta
from unittest import skipUnless

from asgiref.sync import async_to_sync
from django.conf import settings
from django.test import TestCase

from apps.performance.cold_storage import (
    STORAGE_PREFIX,
    enumerate_span_files,
    query_span_groups,
)
from apps.performance.promotion import _write_chunk_parquet
from glitchtip.cold_storage import (
    COLD_STORAGE_PREFIX,
    _purge_tree,
    get_cold_storage_backend,
    is_duckdb_available,
)

S3_CONFIGURED = bool(
    getattr(settings, "AWS_S3_ENDPOINT_URL", None)
    and getattr(settings, "GLITCHTIP_COLD_STORAGE_BUCKET", None)
)

SPAN_TS = datetime(2026, 1, 2, 12, 30, tzinfo=UTC)
OP = "db.query"
DESCRIPTION = "SELECT 1"


@skipUnless(S3_CONFIGURED, "requires AWS_S3_ENDPOINT_URL and a cold storage bucket")
@skipUnless(is_duckdb_available(), "requires DuckDB")
class SpanColdStorageS3TestCase(TestCase):
    """Write, enumerate and read spans back through a real S3 endpoint."""

    def setUp(self):
        super().setUp()
        self.storage = get_cold_storage_backend()
        self.assertIsNotNone(
            self.storage, "cold storage backend should resolve when S3 is configured"
        )
        # The bucket is shared, so keep each test in its own org tree.
        self.org_id = random.randrange(10**8, 10**9)
        self.date_str = SPAN_TS.strftime("%Y%m%d")
        self.hour_str = SPAN_TS.strftime("%H")
        self.start_dt = SPAN_TS - timedelta(days=1)
        self.end_dt = SPAN_TS + timedelta(days=1)

    def tearDown(self):
        _purge_tree(
            self.storage,
            f"{COLD_STORAGE_PREFIX}/{STORAGE_PREFIX}/org_{self.org_id}",
        )
        super().tearDown()

    def _write_chunk(self, count: int, *, project_id: int = 7) -> str:
        """Write one chunk through the real promotion write path."""
        rows = [
            (
                None,  # id, not written to Parquet
                self.org_id,
                project_id,
                f"txn-{i}",
                f"span{i:016x}",
                f"txn{i:016x}",
                OP,
                DESCRIPTION,
                float(i),
                SPAN_TS,
            )
            for i in range(count)
        ]
        return async_to_sync(_write_chunk_parquet)(
            self.storage, self.org_id, self.date_str, self.hour_str, rows
        )

    def _groups(self, project_ids=None):
        return query_span_groups(
            org_id=self.org_id,
            project_ids=project_ids,
            start_dt=self.start_dt,
            end_dt=self.end_dt,
        )

    def test_written_chunks_are_enumerated_and_readable(self):
        """The round trip the mocked tests cannot make: S3 write, list, read."""
        first = self._write_chunk(3)
        second = self._write_chunk(2)

        found = enumerate_span_files(
            self.storage, self.org_id, self.start_dt, self.end_dt
        )
        self.assertCountEqual(
            found,
            [first, second],
            "both chunks should come back from ListObjectsV2",
        )

        groups = self._groups()
        self.assertEqual(len(groups), 1, groups)
        self.assertEqual(groups[0]["op"], OP)
        self.assertEqual(groups[0]["description"], DESCRIPTION)
        self.assertEqual(groups[0]["count"], 5)

    def test_sealed_hour_shadows_its_chunks(self):
        """A sealed hour file must win over the chunks it replaced.

        Compaction seals an hour by writing ``{date}/{HH}.parquet`` beside the
        chunk directory. If enumeration returned both, every span in that hour
        would be counted twice.
        """
        chunk_path = self._write_chunk(2)
        sealed_path = (
            f"{COLD_STORAGE_PREFIX}/{STORAGE_PREFIX}/org_{self.org_id}"
            f"/{self.date_str}/{self.hour_str}.parquet"
        )
        with self.storage.open(chunk_path) as fh:
            self.storage.save(sealed_path, fh)

        found = enumerate_span_files(
            self.storage, self.org_id, self.start_dt, self.end_dt
        )
        self.assertEqual(
            found,
            [sealed_path],
            "sealed hour should shadow the open chunk directory",
        )
        self.assertEqual(self._groups()[0]["count"], 2)

    def test_project_filter_applies_to_remote_parquet(self):
        """Filtering runs in DuckDB against the file it fetched over s3://."""
        self._write_chunk(3, project_id=11)
        self._write_chunk(2, project_id=22)

        self.assertEqual(self._groups()[0]["count"], 5)
        self.assertEqual(self._groups(project_ids=[22])[0]["count"], 2)
