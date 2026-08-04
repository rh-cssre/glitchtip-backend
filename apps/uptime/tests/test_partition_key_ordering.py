"""The UUIDv7 ``id`` is MonitorCheck's only timestamp, and its partition key.

Two properties have to hold together, and each is worthless without the other:

* **Writers mint ``id`` from the moment the check started.** If a writer lets
  ``id`` default to insert time, the recorded time silently becomes completion
  time and a slow probe sorts ahead of a later one. There is no second column
  to cross-check against any more, so this is asserted directly against the
  real writers rather than against a fixture built to agree with itself.
* **Reads order by ``id``**, which is what lets Postgres walk partitions
  newest-first and stop at LIMIT instead of opening an index scan on every
  partition to find the newest row.

The fixture spans many daily partitions with monitors of differing check rates,
including one that stopped reporting weeks ago -- the shape a recent-activity
window gets wrong but partition-key ordering handles by construction.
"""

import re
from datetime import timedelta
from unittest import mock

from django.db import connection
from django.urls import reverse
from django.utils import timezone

from glitchtip.partition_manager import PartitionManager, UUID7Helper
from glitchtip.test_utils.test_case import GlitchTestCase

from ..constants import MonitorType
from ..models import Monitor, MonitorCheck
from ..tasks import save_monitor_checks

# Days of daily partitions the fixture writes into. Each is its own RANGE
# partition (times PARTITION_HASH_BUCKETS leaves), so this is also roughly how
# many partitions an unordered read would have to open.
FIXTURE_DAYS = 30

# Matches the API's per-monitor check limit.
CHART_LIMIT = 60


def create_fixture_partitions():
    """Daily partitions covering the backdated fixture range.

    The test database only carries partitions from today forward, so backdated
    rows need their own. Bounds land on day boundaries so they do not overlap
    the partition already covering today.
    """
    today = timezone.now().replace(hour=0, minute=0, second=0, microsecond=0)
    PartitionManager().create_partitions_for_date_range(
        parent_table="uptime_monitorcheck",
        start_date=today - timedelta(days=FIXTURE_DAYS),
        end_date=today,
        partition_interval="DAY",
        hash_buckets=None,
        hash_column="organization_id",
        key_type="uuid7",
    )


def _checks_for(monitor, organization, timestamps):
    """A plausible check sequence: mostly up, with a couple of outages."""
    checks = []
    previous_up = True
    for index, when in enumerate(timestamps):
        is_up = not (7 <= index % 40 <= 9)
        checks.append(
            MonitorCheck(
                id=UUID7Helper.from_datetime(when),
                monitor=monitor,
                organization=organization,
                is_up=is_up,
                is_change=is_up != previous_up,
                response_time=120 + (index % 80) if is_up else None,
                reason=None if is_up else 2,
            )
        )
        previous_up = is_up
    return checks


def build_fixture(organization):
    """Three monitors with materially different check rates."""
    now = timezone.now()

    def make_monitor(name, interval):
        return Monitor.objects.create(
            organization=organization,
            name=name,
            url="http://example.com",
            interval=interval,
            monitor_type="GET",
        )

    # Every 5 minutes: 60 checks fit inside the newest partition.
    frequent = make_monitor("frequent", 60)
    frequent_times = [now - timedelta(minutes=5 * i) for i in range(1, 400)]

    # Every 6 hours: 60 checks span ~15 days, so the read must walk back.
    sparse = make_monitor("sparse", 3600)
    sparse_times = [now - timedelta(hours=6 * i) for i in range(1, 116)]

    # Stopped reporting 20 days ago; nothing it has is recent.
    paused = make_monitor("paused", 60)
    paused_start = now - timedelta(days=20)
    paused_times = [paused_start - timedelta(minutes=5 * i) for i in range(1, 200)]

    MonitorCheck.objects.bulk_create(
        _checks_for(frequent, organization, frequent_times)
        + _checks_for(sparse, organization, sparse_times)
        + _checks_for(paused, organization, paused_times)
    )
    return frequent, sparse, paused


class CheckStartTimeTestCase(GlitchTestCase):
    """``id`` must carry the check's start time, set by the writers themselves."""

    @classmethod
    def setUpTestData(cls):
        cls.create_user()
        cls.monitor = Monitor.objects.create(
            organization=cls.organization,
            name="probe",
            url="http://example.com",
            interval=60,
            monitor_type="GET",
        )

    def setUp(self):
        self.async_client.force_login(self.user)

    def test_start_check_reads_back_out_of_the_id(self):
        when = timezone.now() - timedelta(days=3)
        check = MonitorCheck(id=UUID7Helper.from_datetime(when))
        # UUIDv7 encodes milliseconds, so equality holds to that resolution.
        self.assertLess(abs(check.start_check - when), timedelta(milliseconds=1))

    async def test_save_monitor_checks_mints_the_id_from_the_tick(self):
        """The regression guard for the writer.

        ``save_monitor_checks`` receives the tick that *started* the round of
        probes, and inserts after those probes return. If the id were left to
        default, its timestamp would be insert time and the recorded start
        would drift by the probe duration. Passing a tick well in the past
        makes any such drift enormous rather than subtle.
        """
        tick = timezone.now() - timedelta(days=2)
        await save_monitor_checks(
            [
                {
                    "id": self.monitor.id,
                    "organization_id": self.organization.id,
                    "is_up": True,
                    "latest_is_up": True,
                    "last_change": tick,
                    "cached_consecutive_down": 0,
                    "confirmation_threshold": 1,
                    "response_time": 42,
                }
            ],
            tick,
        )

        check = await MonitorCheck.objects.filter(monitor_id=self.monitor.id).afirst()
        self.assertIsNotNone(check)
        self.assertLess(
            abs(check.start_check - tick),
            timedelta(seconds=1),
            "id must encode the tick that started the check, not insert time",
        )

    @mock.patch("apps.uptime.tasks.perform_checks")
    async def test_heartbeat_records_arrival_time(self, _):
        """For a heartbeat the check time *is* the moment the ping arrived."""
        monitor = await Monitor.objects.acreate(
            organization=self.organization,
            name="beat",
            monitor_type=MonitorType.HEARTBEAT,
        )
        url = reverse(
            "api:heartbeat_check", args=[self.organization.slug, monitor.endpoint_id]
        )
        before = timezone.now()
        res = await self.async_client.post(url)
        self.assertEqual(res.status_code, 200)

        check = await MonitorCheck.objects.filter(monitor_id=monitor.id).afirst()
        self.assertGreaterEqual(check.start_check, before - timedelta(seconds=1))
        self.assertLessEqual(check.start_check, timezone.now() + timedelta(seconds=1))


class MonitorCheckOrderingTestCase(GlitchTestCase):
    """Reads return the newest checks, newest-first, across partitions."""

    @classmethod
    def setUpTestData(cls):
        cls.create_user()
        cls.list_url = reverse("api:list_monitors", args=[cls.organization.slug])
        create_fixture_partitions()
        cls.frequent, cls.sparse, cls.paused = build_fixture(cls.organization)

    def setUp(self):
        self.async_client.force_login(self.user)

    async def test_monitor_list_returns_newest_checks_per_monitor(self):
        res = await self.async_client.get(self.list_url)
        self.assertEqual(res.status_code, 200)

        by_name = {row["name"]: row for row in res.json()}
        self.assertEqual(set(by_name), {"frequent", "sparse", "paused"})
        for name, row in by_name.items():
            with self.subTest(monitor=name):
                self.assertEqual(len(row["checks"]), CHART_LIMIT)
                timestamps = [check["startCheck"] for check in row["checks"]]
                self.assertEqual(
                    timestamps,
                    sorted(timestamps, reverse=True),
                    "checks must be newest-first",
                )

    async def test_start_check_is_still_served_to_api_clients(self):
        """Dropping the column must not change the response shape."""
        res = await self.async_client.get(self.list_url)
        frequent = next(row for row in res.json() if row["name"] == "frequent")
        newest = frequent["checks"][0]

        self.assertIn("startCheck", newest)
        expected = await MonitorCheck.objects.filter(
            monitor_id=self.frequent.id
        ).afirst()
        self.assertEqual(
            newest["startCheck"][:23],
            expected.start_check.isoformat(timespec="milliseconds")[:23],
        )

    async def test_paused_monitor_still_returns_its_history(self):
        """A monitor silent for weeks must not render a blank chart.

        Its newest check is 20 days old, so any read bounded to a recent window
        finds nothing. Walking partitions newest-first has no such cliff.
        """
        res = await self.async_client.get(self.list_url)
        paused = next(row for row in res.json() if row["name"] == "paused")

        self.assertEqual(len(paused["checks"]), CHART_LIMIT)
        newest = paused["checks"][0]["startCheck"]
        self.assertLess(newest, (timezone.now() - timedelta(days=19)).isoformat())

    def test_checks_are_gathered_across_partition_boundaries(self):
        """The sparse monitor's 60 checks cannot come from one daily partition.

        At one check per 6 hours they span roughly 15 days, so early
        termination has to keep walking until LIMIT is satisfied rather than
        stopping at the newest partition.
        """
        checks = MonitorCheck.objects.filter(
            monitor_id=self.sparse.id, organization_id=self.organization.id
        ).order_by("-id")[:CHART_LIMIT]
        distinct_days = {check.start_check.date() for check in checks}
        self.assertGreater(len(distinct_days), 10)

    async def test_monitor_checks_endpoint_is_newest_first_and_pages_cleanly(self):
        url = reverse(
            "api:list_monitor_checks", args=[self.organization.slug, self.sparse.id]
        )
        res = await self.async_client.get(url + "?limit=25")
        self.assertEqual(res.status_code, 200)
        page = res.json()
        self.assertEqual(len(page), 25)

        timestamps = [row["startCheck"] for row in page]
        self.assertEqual(timestamps, sorted(timestamps, reverse=True))

        expected = [
            check.start_check.isoformat(timespec="milliseconds").replace("+00:00", "Z")
            async for check in MonitorCheck.objects.filter(
                monitor_id=self.sparse.id, organization_id=self.organization.id
            ).order_by("-id")[:25]
        ]
        self.assertEqual(timestamps, expected)

    async def test_monitor_checks_endpoint_filters_changes(self):
        url = reverse(
            "api:list_monitor_checks", args=[self.organization.slug, self.sparse.id]
        )
        res = await self.async_client.get(url + "?is_change=true")
        self.assertEqual(res.status_code, 200)
        returned = [row["startCheck"] for row in res.json()]
        self.assertTrue(returned, "fixture should contain up/down transitions")

        expected = [
            check.start_check.isoformat(timespec="milliseconds").replace("+00:00", "Z")
            async for check in MonitorCheck.objects.filter(
                monitor_id=self.sparse.id,
                organization_id=self.organization.id,
                is_change=True,
            ).order_by("-id")[: len(returned)]
        ]
        self.assertEqual(returned, expected)

    async def test_monitor_checks_endpoint_excludes_changes(self):
        """``is_change=False`` has no dedicated index; it must still be correct."""
        url = reverse(
            "api:list_monitor_checks", args=[self.organization.slug, self.sparse.id]
        )
        res = await self.async_client.get(url + "?is_change=false&limit=10")
        self.assertEqual(res.status_code, 200)

        change_times = {
            check.start_check.isoformat(timespec="milliseconds").replace("+00:00", "Z")
            async for check in MonitorCheck.objects.filter(
                monitor_id=self.sparse.id, is_change=True
            )
        }
        for row in res.json():
            self.assertNotIn(row["startCheck"], change_times)

    def test_default_ordering_follows_the_partition_key(self):
        self.assertEqual(MonitorCheck._meta.ordering, ("-id",))
        self.assertIn('"id" DESC', str(MonitorCheck.objects.all().query))


class MonitorCheckPlanShapeTestCase(GlitchTestCase):
    """The read must stop early instead of opening every partition.

    Two things about the test database differ from production and both must be
    neutralised for the plan to be representative:

    * The test runner attaches a DEFAULT partition to every partitioned table
      (see ``glitchtip.test_runner``). A DEFAULT partition may hold any ``id``,
      so Postgres cannot order the Append and the behaviour under test is
      structurally unavailable. It is dropped *before* the fixture is written,
      so a row that misses its intended partition raises loudly instead of
      being silently absorbed. The class-level transaction rolls the drop back,
      leaving other test classes untouched.
    * The fixture is far smaller than production, so a cost-based planner would
      choose a bitmap or sequential scan regardless of sort key. Those are
      disabled for the duration, making the assertion about plan *structure*
      rather than about which plan is cheapest at fixture scale.
    """

    @classmethod
    def setUpTestData(cls):
        cls.create_user()
        create_fixture_partitions()
        with connection.cursor() as cursor:
            cursor.execute("DROP TABLE IF EXISTS uptime_monitorcheck_default")
        cls.frequent, cls.sparse, cls.paused = build_fixture(cls.organization)
        with connection.cursor() as cursor:
            cursor.execute("ANALYZE uptime_monitorcheck")

    def _explain(self, **filters):
        queryset = MonitorCheck.objects.filter(
            monitor_id=self.frequent.id,
            organization_id=self.organization.id,
            **filters,
        ).order_by("-id")[:CHART_LIMIT]
        with connection.cursor() as cursor:
            # SET LOCAL: scoped to the test's transaction, reverted on rollback.
            cursor.execute("SET LOCAL enable_seqscan = off")
            cursor.execute("SET LOCAL enable_bitmapscan = off")
            return queryset.explain(analyze=True, verbose=True)

    def _executed_indexes(self, plan):
        """Index names actually scanned, ignoring "(never executed)" children."""
        names = []
        for line in plan.splitlines():
            match = re.search(r"Index Scan using (\S+)", line)
            if match and "never executed" not in line:
                names.append(match.group(1))
        return names

    def _index_ancestry(self, leaf_index_name):
        """Every index this leaf index belongs to, up to the partitioned root."""
        with connection.cursor() as cursor:
            cursor.execute(
                """
                WITH RECURSIVE up AS (
                    SELECT c.oid, c.relname
                    FROM pg_class c WHERE c.relname = %s
                    UNION ALL
                    SELECT p.oid, p.relname
                    FROM pg_inherits i
                    JOIN up ON up.oid = i.inhrelid
                    JOIN pg_class p ON p.oid = i.inhparent
                )
                SELECT relname FROM up
                """,
                [leaf_index_name],
            )
            return {row[0] for row in cursor.fetchall()}

    def test_read_stops_early_instead_of_opening_every_partition(self):
        """The regression guard.

        The frequent monitor's newest 60 checks all live in the current day, so
        the great majority of partitions must go untouched. Ordering by
        anything that is not the partition key cannot skip any of them.
        """
        plan = self._explain()
        executed = self._executed_indexes(plan)
        skipped = plan.count("never executed")

        # All 60 come from the current day, so the walk should reach data
        # almost immediately and stop. It is not 1: partition maintenance keeps
        # a week of empty partitions ahead of now, and an ordered Append starts
        # at the newest, so it descends through those before finding rows. That
        # is a handful of index descents, not one per partition in the window.
        self.assertLessEqual(
            len(executed),
            10,
            f"expected the read to stop soon after the newest populated "
            f"partition, opened {len(executed)}:\n{plan}",
        )
        # And the rest of the window must be left untouched rather than seeded.
        self.assertGreaterEqual(
            skipped,
            FIXTURE_DAYS - 2,
            f"expected ~{FIXTURE_DAYS} partitions unopened, saw {skipped}:\n{plan}",
        )

    def test_chart_query_uses_the_partition_key_index(self):
        """Assert on the index actually scanned, resolved by parentage.

        Not by name: partitions created after the migration get their indexes
        cloned by Postgres under names it chooses, so a name-suffix check would
        pass here and mean nothing on a real install.
        """
        executed = self._executed_indexes(self._explain())
        self.assertTrue(executed, "expected at least one executed index scan")
        for leaf_index in executed:
            with self.subTest(index=leaf_index):
                self.assertIn(
                    "uptime_check_monitor_id_idx", self._index_ancestry(leaf_index)
                )

    def test_is_change_filter_uses_the_partial_index(self):
        """``is_change=True`` is rare, so it gets its own partial index."""
        executed = self._executed_indexes(self._explain(is_change=True))
        self.assertTrue(executed, "expected at least one executed index scan")
        for leaf_index in executed:
            with self.subTest(index=leaf_index):
                self.assertIn(
                    "uptime_check_change_id_idx", self._index_ancestry(leaf_index)
                )

    def test_partitioned_indexes_are_valid(self):
        """An index left invalid by a half-finished build is ignored entirely."""
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT c.relname, i.indisvalid
                FROM pg_class c
                JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname IN (
                    'uptime_check_monitor_id_idx', 'uptime_check_change_id_idx'
                )
                """
            )
            state = dict(cursor.fetchall())
        self.assertEqual(
            state,
            {
                "uptime_check_monitor_id_idx": True,
                "uptime_check_change_id_idx": True,
            },
        )

    def test_start_check_column_is_gone(self):
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT count(*) FROM information_schema.columns
                WHERE table_name = 'uptime_monitorcheck'
                  AND column_name = 'start_check'
                """
            )
            self.assertEqual(cursor.fetchone()[0], 0)
