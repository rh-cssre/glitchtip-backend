from collections.abc import Iterable, Mapping
from typing import Optional

from django.test import TestCase
from django.urls import reverse
from model_bakery import baker
from prometheus_client import Metric
from prometheus_client.parser import text_string_to_metric_families

from glitchtip.test_utils import generators  # noqa: F401

from .metrics import organizations_metric, projects_metric
from .utils import clear_metrics_cache


def get_sample_value(
    metric_families: Iterable[Metric],
    metric_name: str,
    metric_type: str,
    labels: Mapping[str, str],
) -> Optional[float]:
    for metric_family in metric_families:
        if metric_family.name != metric_name or metric_family.type != metric_type:
            continue
        for metric in metric_family.samples:
            if metric[1] != labels:
                continue
            return metric.value
    return None


def parse_prometheus_text(text: str) -> list[Metric]:
    parser = text_string_to_metric_families(text)
    return list(parser)


class ObservabilityAPITestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = baker.make("users.user", is_staff=True)
        cls.url = reverse("api:django_prometheus_metrics")

    def setUp(self):
        self.client.force_login(self.user)

    def _get_metrics(self) -> list[Metric]:
        resp = self.client.get(self.url)
        return parse_prometheus_text(resp.content.decode("utf-8"))

    def test_get_metrics_and_cache(self):
        clear_metrics_cache()
        with self.assertNumQueries(2):
            resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, 200)

        with self.assertNumQueries(1):
            resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, 200)

    def test_org_metric(self):
        before_orgs_metric = get_sample_value(
            self._get_metrics(),
            organizations_metric._name,
            organizations_metric._type,
            {},
        )

        # create new org; must invalidate the cache
        org = baker.make("organizations_ext.Organization")
        metrics = self._get_metrics()
        orgs_metric = get_sample_value(
            metrics, organizations_metric._name, organizations_metric._type, {}
        )
        self.assertEqual(orgs_metric, before_orgs_metric + 1)

        # delete org and test again
        org.delete()
        metrics = self._get_metrics()
        orgs_metric = get_sample_value(
            metrics, organizations_metric._name, organizations_metric._type, {}
        )
        self.assertEqual(orgs_metric, before_orgs_metric)

    def test_project_metric(self):
        # create new org
        org = baker.make("organizations_ext.Organization")

        # no projects yet
        metrics = self._get_metrics()
        projs_metric = get_sample_value(
            metrics,
            projects_metric._name,
            projects_metric._type,
            {"organization": org.slug},
        )
        self.assertEqual(projs_metric, 0)

        # create new project
        proj = baker.make("projects.Project", organization=org)
        # test
        metrics = self._get_metrics()
        projs_metric = get_sample_value(
            metrics,
            projects_metric._name,
            projects_metric._type,
            {"organization": org.slug},
        )
        self.assertEqual(projs_metric, 1)

        # delete project
        proj.force_delete()

        # test
        metrics = self._get_metrics()
        projs_metric = get_sample_value(
            metrics,
            projects_metric._name,
            projects_metric._type,
            {"organization": org.slug},
        )
        self.assertEqual(projs_metric, 0)
