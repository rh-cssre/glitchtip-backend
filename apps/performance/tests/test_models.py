from datetime import timedelta

from django.test import TestCase
from django.utils import timezone
from model_bakery import baker

from apps.performance.models import TransactionGroup


class TransactionGroupModelTestCase(TestCase):
    def test_create_group(self):
        now = timezone.now()
        project = baker.make("projects.Project")
        group = TransactionGroup.objects.create(
            project=project,
            organization=project.organization,
            transaction="/api/test/",
            op="http.server",
            method="GET",
            first_seen=now,
            last_seen=now,
            avg_duration=150.5,
            count=42,
            error_count=3,
        )
        self.assertEqual(str(group), "/api/test/")
        self.assertEqual(group.count, 42)
        self.assertEqual(group.error_count, 3)
        self.assertAlmostEqual(group.avg_duration, 150.5)

    def test_default_values(self):
        now = timezone.now()
        project = baker.make("projects.Project")
        group = TransactionGroup.objects.create(
            project=project,
            organization=project.organization,
            transaction="/test/",
            op="http.server",
            first_seen=now,
            last_seen=now,
        )
        self.assertEqual(group.count, 0)
        self.assertEqual(group.error_count, 0)
        self.assertEqual(group.avg_duration, 0)
        self.assertIsNone(group.p50)
        self.assertIsNone(group.p95)
        self.assertEqual(group.duration_histogram, [0] * 50)

    def test_unique_constraint(self):
        from django.db import IntegrityError

        now = timezone.now()
        project = baker.make("projects.Project")
        TransactionGroup.objects.create(
            project=project,
            organization=project.organization,
            transaction="/api/test/",
            op="http.server",
            method="GET",
            first_seen=now,
            last_seen=now,
        )
        with self.assertRaises(IntegrityError):
            TransactionGroup.objects.create(
                project=project,
                organization=project.organization,
                transaction="/api/test/",
                op="http.server",
                method="GET",
                first_seen=now,
                last_seen=now,
            )

    def test_histogram_array_field(self):
        now = timezone.now()
        project = baker.make("projects.Project")
        hist = [0] * 50
        hist[0] = 5
        hist[10] = 3
        hist[20] = 1
        group = TransactionGroup.objects.create(
            project=project,
            organization=project.organization,
            transaction="/test/",
            op="http.server",
            first_seen=now,
            last_seen=now,
            duration_histogram=hist,
        )
        group.refresh_from_db()
        self.assertEqual(group.duration_histogram[0], 5)
        self.assertEqual(group.duration_histogram[10], 3)


class ErrorRatePropertyTestCase(TestCase):
    def test_error_rate_with_errors(self):
        now = timezone.now()
        project = baker.make("projects.Project")
        group = TransactionGroup(
            project=project,
            organization=project.organization,
            transaction="/test/",
            op="http.server",
            first_seen=now,
            last_seen=now,
            count=200,
            error_count=50,
        )
        self.assertEqual(group.error_rate, 25.0)

    def test_error_rate_zero_count(self):
        now = timezone.now()
        project = baker.make("projects.Project")
        group = TransactionGroup(
            project=project,
            organization=project.organization,
            transaction="/test/",
            op="http.server",
            first_seen=now,
            last_seen=now,
            count=0,
            error_count=0,
        )
        self.assertEqual(group.error_rate, 0.0)

    def test_error_rate_no_errors(self):
        now = timezone.now()
        project = baker.make("projects.Project")
        group = TransactionGroup(
            project=project,
            organization=project.organization,
            transaction="/test/",
            op="http.server",
            first_seen=now,
            last_seen=now,
            count=100,
            error_count=0,
        )
        self.assertEqual(group.error_rate, 0.0)

    def test_error_rate_all_errors(self):
        now = timezone.now()
        project = baker.make("projects.Project")
        group = TransactionGroup(
            project=project,
            organization=project.organization,
            transaction="/test/",
            op="http.server",
            first_seen=now,
            last_seen=now,
            count=50,
            error_count=50,
        )
        self.assertEqual(group.error_rate, 100.0)

    def test_error_rate_rounds_to_two_decimals(self):
        now = timezone.now()
        project = baker.make("projects.Project")
        group = TransactionGroup(
            project=project,
            organization=project.organization,
            transaction="/test/",
            op="http.server",
            first_seen=now,
            last_seen=now,
            count=3,
            error_count=1,
        )
        # 1/3 * 100 = 33.333... → 33.33
        self.assertEqual(group.error_rate, 33.33)


class ThroughputPropertyTestCase(TestCase):
    def test_throughput_normal(self):
        now = timezone.now()
        project = baker.make("projects.Project")
        group = TransactionGroup(
            project=project,
            organization=project.organization,
            transaction="/test/",
            op="http.server",
            first_seen=now - timedelta(hours=1),
            last_seen=now,
            count=120,
        )
        # 120 over 3600s = 2.0/min
        self.assertAlmostEqual(group.throughput, 2.0, places=2)

    def test_throughput_same_time(self):
        """When first_seen == last_seen, time span is 0 → None."""
        now = timezone.now()
        project = baker.make("projects.Project")
        group = TransactionGroup(
            project=project,
            organization=project.organization,
            transaction="/test/",
            op="http.server",
            first_seen=now,
            last_seen=now,
            count=100,
        )
        self.assertIsNone(group.throughput)

    def test_throughput_none_timestamps(self):
        """Throughput is None when timestamps are missing."""
        project = baker.make("projects.Project")
        group = TransactionGroup(
            project=project,
            organization=project.organization,
            transaction="/test/",
            op="http.server",
            count=100,
        )
        # first_seen and last_seen are None (not saved)
        group.first_seen = None
        group.last_seen = None
        self.assertIsNone(group.throughput)


class BigIntegerFieldTestCase(TestCase):
    def test_count_exceeds_32bit(self):
        """Verify PositiveBigIntegerField stores values beyond 2^31."""
        now = timezone.now()
        project = baker.make("projects.Project")
        large_count = 3_000_000_000  # > 2^31 (2,147,483,647)
        group = TransactionGroup.objects.create(
            project=project,
            organization=project.organization,
            transaction="/big/",
            op="http.server",
            first_seen=now,
            last_seen=now,
            count=large_count,
            error_count=large_count,
        )
        group.refresh_from_db()
        self.assertEqual(group.count, large_count)
        self.assertEqual(group.error_count, large_count)
