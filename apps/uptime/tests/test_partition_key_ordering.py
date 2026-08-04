"""MonitorCheck reads are keyed on the RANGE partition key (``id``), not ``start_check``.

``uptime_monitorcheck`` is RANGE partitioned on ``id`` (a UUIDv7) and HASH
sub-partitioned on ``organization_id``. Ordering by ``start_check`` -- which is
not the partition key -- forces a Merge Append that opens an index scan on every
partition just to find out which one holds the newest row. Ordering by ``id``
lets Postgres walk partitions newest-first and stop at LIMIT.

These tests pin both halves of that: that the two orderings return the same
rows (so the swap is invisible to callers), and that the query actually stops
early (so a revert to ``start_check`` fails here rather than silently costing a
full scan per page load).

The fixture deliberately spans many daily partitions with monitors of differing
check rates, including one that stopped reporting weeks ago -- the shape a
recent-activity window gets wrong but partition-key ordering handles by
construction.
"""

from datetime import timedelta

from django.db import connection
from django.urls import reverse
from django.utils import timezone

from glitchtip.partition_manager import PartitionManager, UUID7Helper
from glitchtip.test_utils.test_case import GlitchTestCase

from ..models import Monitor, MonitorCheck

# Days of daily partitions the fixture writes into. Each day is its own RANGE
# partition (times PARTITION_HASH_BUCKETS leaves), so this is also roughly the
# number of partitions an unbounded Merge Append has to open.
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
    """Build a plausible check sequence: mostly up, with a couple of outages.

    ``id`` is minted from the check's own timestamp so rows land in the daily
    partition they belong to, mirroring production where the UUIDv7 is
    generated at insert time.
    """
    checks = []
    previous_up = True
    for index, when in enumerate(timestamps):
        # Two short outages per sequence, at deterministic offsets.
        is_up = not (7 <= index % 40 <= 9)
        checks.append(
            MonitorCheck(
                id=UUID7Helper.from_datetime(when),
                monitor=monitor,
                organization=organization,
                start_check=when,
                is_up=is_up,
                is_change=is_up != previous_up,
                response_time=120 + (index % 80) if is_up else None,
                reason=None if is_up else 2,
            )
        )
        previous_up = is_up
    return checks


def build_fixture(organization):
    """Three monitors with materially different check rates.

    Returns (frequent, sparse, paused).
    """
    now = timezone.now()

    def make_monitor(name, interval):
        return Monitor.objects.create(
            organization=organization,
            name=name,
            url="http://example.com",
            interval=interval,
            monitor_type="GET",
        )

    # Reporting every 5 minutes for the last day and a bit: 60 checks fit inside
    # the newest partition, so the read should never leave it.
    frequent = make_monitor("frequent", 60)
    frequent_times = [now - timedelta(minutes=5 * i) for i in range(1, 400)]

    # One check every 6 hours across the whole window: 60 checks span ~15 days,
    # so the read has to walk back through many partitions.
    sparse = make_monitor("sparse", 3600)
    sparse_times = [now - timedelta(hours=6 * i) for i in range(1, 116)]

    # Stopped reporting 20 days ago. Every one of its checks sits outside any
    # recent-activity window, but they are still the newest rows it has.
    paused = make_monitor("paused", 60)
    paused_start = now - timedelta(days=20)
    paused_times = [paused_start - timedelta(minutes=5 * i) for i in range(1, 200)]

    MonitorCheck.objects.bulk_create(
        _checks_for(frequent, organization, frequent_times)
        + _checks_for(sparse, organization, sparse_times)
        + _checks_for(paused, organization, paused_times)
    )
    return frequent, sparse, paused


class MonitorCheckOrderingTestCase(GlitchTestCase):
    """The sort key swap must be invisible to every caller."""

    @classmethod
    def setUpTestData(cls):
        cls.create_user()
        cls.list_url = reverse("api:list_monitors", args=[cls.organization.slug])
        create_fixture_partitions()
        cls.frequent, cls.sparse, cls.paused = build_fixture(cls.organization)

    def setUp(self):
        self.async_client.force_login(self.user)

    def test_id_ordering_selects_the_same_rows_as_start_check_ordering(self):
        """Swapping the sort key must not change which checks a caller sees.

        Every writer records a ``start_check`` no later than the insert that
        mints the ``id``, so per monitor the two sequences agree. This asserts
        that rather than trusting it.
        """
        for monitor in (self.frequent, self.sparse, self.paused):
            with self.subTest(monitor=monitor.name):
                base = MonitorCheck.objects.filter(
                    monitor_id=monitor.id, organization_id=self.organization.id
                ).values_list("start_check", flat=True)
                by_id = list(base.order_by("-id")[:CHART_LIMIT])
                by_start_check = list(base.order_by("-start_check")[:CHART_LIMIT])
                self.assertEqual(len(by_id), CHART_LIMIT)
                self.assertEqual(by_id, by_start_check)

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

    async def test_paused_monitor_still_returns_its_history(self):
        """A monitor silent for weeks must not render a blank chart.

        Its newest check is 20 days old, so any read that bounds itself to a
        recent window finds nothing. Walking partitions newest-first has no such
        cliff -- it keeps going until it has enough rows.
        """
        res = await self.async_client.get(self.list_url)
        paused = next(row for row in res.json() if row["name"] == "paused")

        self.assertEqual(len(paused["checks"]), CHART_LIMIT)
        newest = paused["checks"][0]["startCheck"]
        self.assertLess(newest, (timezone.now() - timedelta(days=19)).isoformat())

    def test_checks_are_gathered_across_partition_boundaries(self):
        """The sparse monitor's 60 checks cannot come from one daily partition.

        At one check per 6 hours, 60 checks span roughly 15 days. This is the
        case that proves early termination is not just "read the newest
        partition and stop" -- it has to keep walking until LIMIT is satisfied.
        """
        timestamps = list(
            MonitorCheck.objects.filter(
                monitor_id=self.sparse.id, organization_id=self.organization.id
            )
            .order_by("-id")
            .values_list("start_check", flat=True)[:CHART_LIMIT]
        )
        distinct_days = {timestamp.date() for timestamp in timestamps}
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

        # The API serialises to millisecond precision; compare like for like.
        expected = [
            timestamp.isoformat(timespec="milliseconds").replace("+00:00", "Z")
            async for timestamp in MonitorCheck.objects.filter(
                monitor_id=self.sparse.id, organization_id=self.organization.id
            )
            .order_by("-id")
            .values_list("start_check", flat=True)[:25]
        ]
        self.assertEqual(timestamps, expected)

    async def test_monitor_checks_endpoint_filters_changes(self):
        url = reverse(
            "api:list_monitor_checks", args=[self.organization.slug, self.sparse.id]
        )
        res = await self.async_client.get(url + "?is_change=true")
        self.assertEqual(res.status_code, 200)
        rows = res.json()

        self.assertTrue(rows, "fixture should contain up/down transitions")
        self.assertTrue(all(row["isUp"] is not None for row in rows))
        change_count = await MonitorCheck.objects.filter(
            monitor_id=self.sparse.id,
            organization_id=self.organization.id,
            is_change=True,
        ).acount()
        self.assertLessEqual(len(rows), change_count)

    def test_default_ordering_follows_the_partition_key(self):
        self.assertEqual(MonitorCheck._meta.ordering, ("-id",))
        self.assertIn('"id" DESC', str(MonitorCheck.objects.all().query))


class MonitorCheckPlanShapeTestCase(GlitchTestCase):
    """The read must stop early instead of opening every partition.

    Two things about the test database differ from production and both have to
    be neutralised for the plan to be representative:

    * The test runner attaches a DEFAULT partition to every partitioned table
      (see ``glitchtip.test_runner``). A DEFAULT partition may hold any ``id``,
      so Postgres cannot order the Append and the optimisation under test is
      structurally unavailable. Dropped here; the class-level transaction rolls
      it back afterwards, so other test classes are unaffected.
    * The fixture is far smaller than production, so a cost-based planner would
      pick a bitmap or sequential scan regardless of sort key. Those are
      disabled for the duration so the assertion is about plan *structure* --
      can Postgres walk partitions in key order and stop -- rather than about
      which plan is cheapest at fixture scale.
    """

    @classmethod
    def setUpTestData(cls):
        cls.create_user()
        create_fixture_partitions()
        cls.frequent, cls.sparse, cls.paused = build_fixture(cls.organization)
        with connection.cursor() as cursor:
            cursor.execute("DROP TABLE IF EXISTS uptime_monitorcheck_default")
            cursor.execute("ANALYZE uptime_monitorcheck")

    def _explain(self, ordering, **filters):
        queryset = MonitorCheck.objects.filter(
            monitor_id=self.frequent.id,
            organization_id=self.organization.id,
            **filters,
        ).order_by(ordering)[:CHART_LIMIT]
        with connection.cursor() as cursor:
            # SET LOCAL: scoped to the test's transaction, reverted on rollback.
            cursor.execute("SET LOCAL enable_seqscan = off")
            cursor.execute("SET LOCAL enable_bitmapscan = off")
            return queryset.explain(analyze=True, verbose=True)

    def test_read_stops_early_instead_of_opening_every_partition(self):
        """The regression guard: reverting to ``start_check`` must fail here.

        With the sort key equal to the partition key, Postgres walks partitions
        newest-first and leaves the rest "(never executed)". Ordering by
        ``start_check`` instead makes it seed a Merge Append from every
        partition, so nothing is ever skipped.
        """
        by_id = self._explain("-id")
        by_start_check = self._explain("-start_check")

        skipped_by_id = by_id.count("never executed")
        skipped_by_start_check = by_start_check.count("never executed")

        self.assertGreater(
            skipped_by_id,
            skipped_by_start_check,
            "ordering by the partition key must skip partitions that ordering "
            f"by start_check cannot.\n\nBY ID:\n{by_id}\n\n"
            f"BY START_CHECK:\n{by_start_check}",
        )
        # The frequent monitor's newest 60 checks all live in the current day,
        # so the great majority of partitions should go untouched.
        self.assertGreater(skipped_by_id, FIXTURE_DAYS // 2)

    def test_chart_query_uses_the_partition_key_index(self):
        plan = self._explain("-id")
        self.assertIn(
            "_mid", plan, f"expected the (monitor_id, id DESC) index:\n{plan}"
        )

    def test_is_change_filter_uses_the_partial_index(self):
        """``is_change=True`` is rare, so it gets its own partial index.

        ``is_change=False`` deliberately does not -- 99%+ of rows match, so the
        main index plus a filter is the better plan and avoids carrying a second
        full-size index.
        """
        plan = self._explain("-id", is_change=True)
        self.assertIn("_cid", plan, f"expected the partial is_change index:\n{plan}")

    def test_partitioned_indexes_are_valid(self):
        """An index left invalid by a half-finished build is ignored by the planner."""
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
