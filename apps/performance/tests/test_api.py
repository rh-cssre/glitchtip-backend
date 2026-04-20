from datetime import timedelta

from django.utils import timezone

from apps.performance.models import TransactionGroup
from glitchtip.test_utils.test_case import GlitchTestCase


class TransactionGroupAPITestCase(GlitchTestCase):
    @classmethod
    def setUpTestData(cls):
        cls.create_user()
        cls.list_url = (
            f"/api/0/organizations/{cls.organization.slug}/transaction-groups/"
        )

    def setUp(self):
        self.client.force_login(self.user)

    def create_group(self, **kwargs):
        now = timezone.now()
        defaults = {
            "project": self.project,
            "organization": self.organization,
            "transaction": "/api/test/",
            "op": "http.server",
            "method": "GET",
            "first_seen": now - timedelta(days=1),
            "last_seen": now,
            "avg_duration": 100.0,
            "count": 10,
        }
        defaults.update(kwargs)
        return TransactionGroup.objects.create(**defaults)

    def test_list(self):
        group = self.create_group()
        res = self.client.get(self.list_url)
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["id"], group.id)
        self.assertEqual(data[0]["transaction"], "/api/test/")
        self.assertEqual(data[0]["count"], 10)
        self.assertIn("avgDuration", data[0])
        self.assertIn("errorRate", data[0])
        self.assertIn("throughput", data[0])

    def test_list_empty(self):
        res = self.client.get(self.list_url)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json(), [])

    def test_list_sort_by_count(self):
        self.create_group(transaction="/slow/", count=5)
        self.create_group(transaction="/fast/", count=50)
        res = self.client.get(self.list_url + "?sort=-count")
        data = res.json()
        self.assertEqual(data[0]["transaction"], "/fast/")
        self.assertEqual(data[1]["transaction"], "/slow/")

    def test_list_filter_by_project(self):
        self.create_group(transaction="/proj1/")
        res = self.client.get(self.list_url + f"?project={self.project.id}")
        self.assertEqual(len(res.json()), 1)

    def test_list_query_filter(self):
        self.create_group(transaction="/api/users/")
        self.create_group(transaction="/api/projects/")
        res = self.client.get(self.list_url + "?query=users")
        data = res.json()
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["transaction"], "/api/users/")

    def test_list_time_range_filter(self):
        now = timezone.now()
        self.create_group(
            transaction="/old/",
            last_seen=now - timedelta(days=30),
            first_seen=now - timedelta(days=60),
        )
        self.create_group(
            transaction="/recent/",
            last_seen=now,
            first_seen=now - timedelta(days=1),
        )
        res = self.client.get(self.list_url + "?start=now-7d")
        data = res.json()
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["transaction"], "/recent/")

    def test_list_relative_parsing(self):
        res = self.client.get(self.list_url, {"start": "now-1h "})
        self.assertEqual(res.status_code, 200)
        res = self.client.get(self.list_url, {"start": "now - 1h"})
        self.assertEqual(res.status_code, 200)
        res = self.client.get(self.list_url, {"start": "now-1"})
        self.assertEqual(res.status_code, 422)
        res = self.client.get(self.list_url, {"start": "now-1minute"})
        self.assertEqual(res.status_code, 422)
        res = self.client.get(self.list_url, {"start": "won-1m"})
        self.assertEqual(res.status_code, 422)
        res = self.client.get(self.list_url, {"start": "now+1m"})
        self.assertEqual(res.status_code, 422)
        res = self.client.get(self.list_url, {"start": "now 1m"})
        self.assertEqual(res.status_code, 422)

    def test_error_rate_and_throughput(self):
        now = timezone.now()
        group = self.create_group(
            count=100,
            error_count=25,
            first_seen=now - timedelta(hours=1),
            last_seen=now,
        )
        url = f"/api/0/organizations/{self.organization.slug}/transaction-groups/{group.id}/"
        res = self.client.get(url)
        data = res.json()
        self.assertEqual(data["errorRate"], 25.0)
        self.assertIsNotNone(data["throughput"])
        # 100 txns over 3600 seconds = ~1.67/min
        self.assertAlmostEqual(data["throughput"], 1.67, places=2)

    def test_error_rate_zero_count(self):
        group = self.create_group(count=0, error_count=0)
        url = f"/api/0/organizations/{self.organization.slug}/transaction-groups/{group.id}/"
        res = self.client.get(url)
        data = res.json()
        self.assertEqual(data["errorRate"], 0.0)

    def test_detail(self):
        group = self.create_group()
        url = f"/api/0/organizations/{self.organization.slug}/transaction-groups/{group.id}/"
        res = self.client.get(url)
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["id"], group.id)
        self.assertEqual(data["transaction"], "/api/test/")

    def test_detail_not_found(self):
        url = f"/api/0/organizations/{self.organization.slug}/transaction-groups/99999/"
        res = self.client.get(url)
        self.assertEqual(res.status_code, 404)

    def test_spans_endpoint_empty(self):
        group = self.create_group()
        url = f"/api/0/organizations/{self.organization.slug}/transaction-groups/{group.id}/spans/"
        res = self.client.get(url)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json(), [])

    def test_span_groups_endpoint_empty(self):
        url = f"/api/0/organizations/{self.organization.slug}/span-groups/"
        res = self.client.get(url)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json(), [])

    def test_n_plus_one_endpoint_empty(self):
        url = f"/api/0/organizations/{self.organization.slug}/n-plus-one/"
        res = self.client.get(url)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json(), [])

    def test_trend_endpoint_empty(self):
        group = self.create_group()
        url = f"/api/0/organizations/{self.organization.slug}/transaction-groups/{group.id}/trend/"
        res = self.client.get(url)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json(), [])

    def test_trend_endpoint_not_found(self):
        url = f"/api/0/organizations/{self.organization.slug}/transaction-groups/99999/trend/"
        res = self.client.get(url)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json(), [])

    def test_cross_org_isolation(self):
        """A user in org B cannot see transaction groups from org A."""
        group = self.create_group()

        # Create a second user in a different organization
        from model_bakery import baker

        user_b = baker.make("users.user")
        org_b = baker.make("organizations_ext.Organization")
        org_b.add_user(user_b)

        self.client.force_login(user_b)

        # List endpoint — should return empty
        list_url = f"/api/0/organizations/{self.organization.slug}/transaction-groups/"
        res = self.client.get(list_url)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json(), [])

        # Detail endpoint — should return 404
        detail_url = f"/api/0/organizations/{self.organization.slug}/transaction-groups/{group.id}/"
        res = self.client.get(detail_url)
        self.assertEqual(res.status_code, 404)

        # Spans endpoint — should return empty
        spans_url = f"/api/0/organizations/{self.organization.slug}/transaction-groups/{group.id}/spans/"
        res = self.client.get(spans_url)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json(), [])

        # Trend endpoint — should return empty
        trend_url = f"/api/0/organizations/{self.organization.slug}/transaction-groups/{group.id}/trend/"
        res = self.client.get(trend_url)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json(), [])
