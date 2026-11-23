from django.test import TestCase

from apps.performance.histogram import (
    BUCKET_BOUNDARIES,
    get_bucket_index,
    merge_durations,
    new_histogram,
    percentile_from_histogram,
)


class BucketIndexTestCase(TestCase):
    def test_minimum_value(self):
        self.assertEqual(get_bucket_index(0.5), 0)

    def test_one_ms(self):
        self.assertEqual(get_bucket_index(1.0), 0)

    def test_maximum_value(self):
        idx = get_bucket_index(60_000.0)
        self.assertEqual(idx, 49)

    def test_above_maximum(self):
        idx = get_bucket_index(100_000.0)
        self.assertEqual(idx, 49)

    def test_mid_range(self):
        idx = get_bucket_index(100.0)
        self.assertGreater(idx, 0)
        self.assertLess(idx, 49)
        self.assertGreaterEqual(100.0, BUCKET_BOUNDARIES[idx])
        self.assertLess(100.0, BUCKET_BOUNDARIES[idx + 1])


class MergeDurationsTestCase(TestCase):
    def test_empty_histogram(self):
        hist = new_histogram()
        result = merge_durations(hist, [100.0, 200.0, 100.0])
        self.assertEqual(sum(result), 3)

    def test_merge_into_existing(self):
        hist = new_histogram()
        hist[5] = 10
        result = merge_durations(hist, [])
        self.assertEqual(result[5], 10)

    def test_accumulates(self):
        hist = new_histogram()
        merge_durations(hist, [100.0])
        merge_durations(hist, [100.0])
        self.assertEqual(sum(hist), 2)


class PercentileTestCase(TestCase):
    def test_empty_histogram(self):
        self.assertIsNone(percentile_from_histogram(new_histogram(), 0, 50))

    def test_single_value(self):
        hist = new_histogram()
        merge_durations(hist, [100.0])
        result = percentile_from_histogram(hist, 1, 50)
        self.assertIsNotNone(result)
        # Should be near 100ms (within bucket width)
        self.assertGreater(result, 50)
        self.assertLess(result, 200)

    def test_p50_p95(self):
        hist = new_histogram()
        # Create a distribution: many fast, few slow
        durations = [10.0] * 90 + [5000.0] * 10
        merge_durations(hist, durations)

        p50 = percentile_from_histogram(hist, 100, 50)
        p95 = percentile_from_histogram(hist, 100, 95)

        self.assertIsNotNone(p50)
        self.assertIsNotNone(p95)
        # p50 should be near 10ms
        self.assertLess(p50, 50)
        # p95 should be near 5000ms
        self.assertGreater(p95, 1000)
        # p95 > p50
        self.assertGreater(p95, p50)

    def test_p100(self):
        hist = new_histogram()
        merge_durations(hist, [1.0, 100.0, 10000.0])
        result = percentile_from_histogram(hist, 3, 100)
        self.assertIsNotNone(result)
        # Should be in the highest bucket used
        self.assertGreater(result, 1000)
