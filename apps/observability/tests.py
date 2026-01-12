from asgiref.sync import async_to_sync
from django.test import TestCase
from model_bakery import baker
from prometheus_client import REGISTRY, Metric

from glitchtip.test_utils import generators  # noqa: F401

from .metrics import update_metrics
from .utils import clear_metrics_cache


def get_sample_value(
    metric_families: list[Metric],
    metric_name: str,
    metric_type: str,
    labels: dict[str, str],
) -> float | None:
    for metric_family in metric_families:
        if metric_family.name != metric_name or metric_family.type != metric_type:
            continue
        for metric in metric_family.samples:
            if metric[1] != labels:
                continue
            return metric.value
    return None


class ObservabilityTestCase(TestCase):
    def _get_metrics(self) -> list[Metric]:
        async_to_sync(update_metrics)()
        return list(REGISTRY.collect())

    def test_get_metrics_and_cache(self):
        clear_metrics_cache()
        # Note: assertNumQueries might capture queries from async_to_sync
        # depending on DB connection handling.
        # With async_to_sync, it runs in a loop.
        with self.assertNumQueries(1):
            async_to_sync(update_metrics)()

        with self.assertNumQueries(0):  # Should hit cache
            async_to_sync(update_metrics)()

    def test_org_metric(self):
        before_orgs_metric = get_sample_value(
            self._get_metrics(),
            "glitchtip_organizations",
            "gauge",
            {},
        )
        before_orgs_metric = before_orgs_metric or 0.0

        # create new org; must invalidate the cache
        clear_metrics_cache()
        org = baker.make("organizations_ext.Organization")
        metrics = self._get_metrics()
        orgs_metric = get_sample_value(metrics, "glitchtip_organizations", "gauge", {})
        self.assertEqual(orgs_metric, before_orgs_metric + 1)

        # delete org and test again
        org.delete()
        clear_metrics_cache()
        metrics = self._get_metrics()
        orgs_metric = get_sample_value(metrics, "glitchtip_organizations", "gauge", {})
        self.assertEqual(orgs_metric, before_orgs_metric)

    def test_project_metric(self):
        clear_metrics_cache()
        # create new org
        org = baker.make("organizations_ext.Organization")

        # no projects yet
        metrics = self._get_metrics()
        projs_metric = get_sample_value(
            metrics,
            "glitchtip_projects",
            "gauge",
            {"organization": org.slug},
        )
        self.assertEqual(projs_metric or 0, 0)

        # create new project
        clear_metrics_cache()
        baker.make("projects.Project", organization=org)
        # test
        metrics = self._get_metrics()
        projs_metric = get_sample_value(
            metrics,
            "glitchtip_projects",
            "gauge",
            {"organization": org.slug},
        )
        self.assertEqual(projs_metric, 1)
