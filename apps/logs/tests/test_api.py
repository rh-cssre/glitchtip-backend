"""
Tests for logs API endpoints.
"""

from datetime import timedelta

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from model_bakery import baker

from apps.organizations_ext.constants import OrganizationUserRole
from glitchtip.partition_manager import UUID7Helper
from glitchtip.test_utils.test_case import GlitchTipTestCaseMixin

from ..constants import LogLevel
from ..models import LogEvent


class LogsAPITestCase(GlitchTipTestCaseMixin, TestCase):
    """Test logs API endpoints"""

    def setUp(self):
        self.create_logged_in_user()

    def create_log(self, **kwargs):
        """Helper to create a log event"""
        now = timezone.now()
        # If id is provided, use it; otherwise generate from current time
        if "id" not in kwargs:
            kwargs["id"] = UUID7Helper.from_datetime(now)
        defaults = {
            "organization": self.organization,
            "project": self.project,
            "level": LogLevel.INFO,
            "body": "Test log message",
            "service": "web",
            "environment": "prod",
            "host": "host-1",
        }
        defaults.update(kwargs)
        return LogEvent.objects.create(**defaults)

    def test_list_logs(self):
        """Test listing logs for an organization"""
        self.create_log(body="Log 1")
        self.create_log(body="Log 2")
        self.create_log(body="Log 3")

        url = reverse(
            "api:list_logs", kwargs={"organization_slug": self.organization.slug}
        )
        res = self.client.get(url)

        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(len(data), 3)
        self.assertEqual(data[0]["service"], "web")
        self.assertEqual(data[0]["environment"], "prod")
        self.assertEqual(data[0]["host"], "host-1")

    def test_list_logs_filter_by_environment(self):
        """Test filtering logs by environment"""
        self.create_log(environment="prod", body="Prod log")
        self.create_log(environment="staging", body="Staging log")

        url = reverse(
            "api:list_logs", kwargs={"organization_slug": self.organization.slug}
        )
        res = self.client.get(url, {"environment": "prod"})

        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["environment"], "prod")

    def test_list_logs_filter_by_host(self):
        """Test filtering logs by host"""
        self.create_log(host="host-1", body="Host 1 log")
        self.create_log(host="host-2", body="Host 2 log")

        url = reverse(
            "api:list_logs", kwargs={"organization_slug": self.organization.slug}
        )
        res = self.client.get(url, {"host": "host-1"})

        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["host"], "host-1")

    def test_list_logs_ordered_by_id_desc(self):
        """Test that logs are ordered by id descending (newest first)"""
        # Create logs in the past so they fall within the API's default time range
        # API defaults to last 7 days, so we create logs 1 hour ago
        base_time = timezone.now() - timedelta(hours=1)

        # Create UUIDs with explicit time offsets to ensure proper ordering
        uuids = []
        for i in range(3):
            t = base_time + timedelta(seconds=i * 10)  # 10 seconds apart
            uuid = UUID7Helper.from_datetime(t)
            uuids.append(uuid)

        # Verify UUIDs are properly ordered (later time = larger UUID)
        self.assertLess(uuids[0], uuids[1])
        self.assertLess(uuids[1], uuids[2])

        # Create logs with these UUIDs
        for i, uuid in enumerate(uuids):
            self.create_log(
                id=uuid,
                body=f"Log {i}",
            )

        url = reverse(
            "api:list_logs", kwargs={"organization_slug": self.organization.slug}
        )
        res = self.client.get(url)

        self.assertEqual(res.status_code, 200)
        data = res.json()
        # Newest (Log 2) should be first (highest UUID)
        self.assertEqual(data[0]["body"], "Log 2")
        self.assertEqual(data[1]["body"], "Log 1")
        self.assertEqual(data[2]["body"], "Log 0")

    def test_list_logs_filter_by_project(self):
        """Test filtering logs by project"""
        project2 = baker.make("projects.Project", organization=self.organization)
        project2.teams.add(self.team)

        self.create_log(project=self.project, body="Project 1 log")
        self.create_log(project=project2, body="Project 2 log")

        url = reverse(
            "api:list_logs", kwargs={"organization_slug": self.organization.slug}
        )
        res = self.client.get(url, {"project": self.project.id})

        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["body"], "Project 1 log")

    def test_list_logs_filter_by_level(self):
        """Test filtering logs by level"""
        self.create_log(level=LogLevel.INFO, body="Info log")
        self.create_log(level=LogLevel.WARN, body="Warning log")
        self.create_log(level=LogLevel.ERROR, body="Error log")

        url = reverse(
            "api:list_logs", kwargs={"organization_slug": self.organization.slug}
        )

        # Filter by error level
        res = self.client.get(url, {"level": "error"})
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["level"], "error")

        # Filter by multiple levels
        res = self.client.get(url, {"level": ["warn", "error"]})
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(len(data), 2)

    def test_list_logs_filter_by_query(self):
        """Test filtering logs by body text search"""
        self.create_log(body="User login successful")
        self.create_log(body="Payment processed")
        self.create_log(body="User logout")

        url = reverse(
            "api:list_logs", kwargs={"organization_slug": self.organization.slug}
        )
        res = self.client.get(url, {"query": "User"})

        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(len(data), 2)

    def test_list_logs_filter_by_trace_id(self):
        """Test filtering logs by trace ID"""
        trace_id = UUID7Helper.from_datetime()
        self.create_log(trace_id=trace_id, body="Traced log")
        self.create_log(body="Untraced log")

        url = reverse(
            "api:list_logs", kwargs={"organization_slug": self.organization.slug}
        )
        res = self.client.get(url, {"traceId": str(trace_id)})

        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["body"], "Traced log")

    def test_list_logs_filter_by_time_range(self):
        """Test filtering logs by time range"""
        base_time = timezone.now().replace(hour=12, minute=0, second=0, microsecond=0)

        # Create logs at different times
        for i in range(5):
            t = base_time + timedelta(hours=i)
            self.create_log(
                id=UUID7Helper.from_datetime(t),
                body=f"Log hour {i}",
            )

        url = reverse(
            "api:list_logs", kwargs={"organization_slug": self.organization.slug}
        )

        # Filter for hours 1-3
        start = (base_time + timedelta(hours=1)).isoformat()
        end = (base_time + timedelta(hours=4)).isoformat()
        res = self.client.get(url, {"start": start, "end": end})

        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(len(data), 3)

    def test_get_single_log(self):
        """Test getting a single log by ID"""
        log = self.create_log(body="Specific log")

        url = reverse(
            "api:get_log",
            kwargs={
                "organization_slug": self.organization.slug,
                "log_id": str(log.id),
            },
        )
        res = self.client.get(url)

        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["body"], "Specific log")
        self.assertEqual(data["id"], str(log.id))

    def test_get_log_not_found(self):
        """Test 404 for non-existent log"""
        fake_id = UUID7Helper.from_datetime()

        url = reverse(
            "api:get_log",
            kwargs={
                "organization_slug": self.organization.slug,
                "log_id": str(fake_id),
            },
        )
        res = self.client.get(url)

        self.assertEqual(res.status_code, 404)

    def test_list_logs_unauthorized(self):
        """Test that logs require authentication"""
        self.client.logout()

        url = reverse(
            "api:list_logs", kwargs={"organization_slug": self.organization.slug}
        )
        res = self.client.get(url)

        self.assertEqual(res.status_code, 401)

    def test_list_logs_wrong_organization(self):
        """Test that users can't access logs from other organizations"""
        other_org = baker.make("organizations_ext.Organization")
        other_project = baker.make("projects.Project", organization=other_org)
        now = timezone.now()

        LogEvent.objects.create(
            id=UUID7Helper.from_datetime(now),
            organization=other_org,
            project=other_project,
            level=LogLevel.INFO,
            body="Secret log",
        )

        url = reverse("api:list_logs", kwargs={"organization_slug": other_org.slug})
        res = self.client.get(url)

        # Should get 404 as user doesn't belong to this org
        self.assertEqual(res.status_code, 404)


class LogsAPIPermissionTestCase(TestCase):
    """Test logs API permissions"""

    def setUp(self):
        self.user = baker.make("users.user")
        self.organization = baker.make("organizations_ext.Organization")
        self.org_user = self.organization.add_user(
            self.user, OrganizationUserRole.ADMIN
        )
        self.project = baker.make("projects.Project", organization=self.organization)
        self.auth_token = baker.make("api_tokens.APIToken", user=self.user)
        self.auth_token.add_permission("event:read")

    def get_headers(self):
        return {"HTTP_AUTHORIZATION": f"Bearer {self.auth_token.token}"}

    def test_list_logs_with_event_read_scope(self):
        """Test that event:read scope allows listing logs"""
        now = timezone.now()
        LogEvent.objects.create(
            id=UUID7Helper.from_datetime(now),
            organization=self.organization,
            project=self.project,
            level=LogLevel.INFO,
            body="Test log",
        )

        url = reverse(
            "api:list_logs", kwargs={"organization_slug": self.organization.slug}
        )
        res = self.client.get(url, **self.get_headers())

        self.assertEqual(res.status_code, 200)

    def test_list_logs_without_permission(self):
        """Test that missing scope denies access"""
        # Create a new token with only project:read
        self.auth_token = baker.make("api_tokens.APIToken", user=self.user)
        self.auth_token.add_permission("project:read")

        url = reverse(
            "api:list_logs", kwargs={"organization_slug": self.organization.slug}
        )
        res = self.client.get(url, **self.get_headers())

        self.assertEqual(res.status_code, 403)


class LogStatsAPITestCase(GlitchTipTestCaseMixin, TestCase):
    """Test log statistics API endpoint."""

    def setUp(self):
        self.create_logged_in_user()
        # Create stats data directly in the table
        from apps.projects.models import LogProjectHourlyStatistic

        from ..models import compute_hash_bucket

        now = timezone.now().replace(minute=0, second=0, microsecond=0)
        self.worker_bucket = compute_hash_bucket("worker")
        self.prod_bucket = compute_hash_bucket("prod")
        self.staging_bucket = compute_hash_bucket("staging")

        # Create hourly stats for different levels (default service_bucket=0)
        LogProjectHourlyStatistic.objects.create(
            project=self.project,
            organization=self.organization,
            date=now - timedelta(hours=2),
            level=LogLevel.INFO,
            service_bucket=0,
            environment_bucket=self.prod_bucket,
            count=10,
        )
        LogProjectHourlyStatistic.objects.create(
            project=self.project,
            organization=self.organization,
            date=now - timedelta(hours=2),
            level=LogLevel.ERROR,
            service_bucket=0,
            environment_bucket=self.prod_bucket,
            count=3,
        )
        LogProjectHourlyStatistic.objects.create(
            project=self.project,
            organization=self.organization,
            date=now - timedelta(hours=1),
            level=LogLevel.INFO,
            service_bucket=0,
            environment_bucket=self.prod_bucket,
            count=15,
        )
        LogProjectHourlyStatistic.objects.create(
            project=self.project,
            organization=self.organization,
            date=now - timedelta(hours=1),
            level=LogLevel.ERROR,
            service_bucket=0,
            environment_bucket=self.staging_bucket,
            count=5,
        )
        # Add stats for "worker" service
        LogProjectHourlyStatistic.objects.create(
            project=self.project,
            organization=self.organization,
            date=now - timedelta(hours=1),
            level=LogLevel.ERROR,
            service_bucket=self.worker_bucket,
            environment_bucket=self.prod_bucket,
            count=7,
        )

    def test_get_log_stats(self):
        """Test fetching log statistics."""
        url = reverse(
            "api:get_log_stats", kwargs={"organization_slug": self.organization.slug}
        )
        res = self.client.get(url)

        self.assertEqual(res.status_code, 200)
        data = res.json()

        # Should have intervals and series
        self.assertIn("intervals", data)
        self.assertIn("series", data)

        # Should have 2 intervals (2 hours of data)
        self.assertEqual(len(data["intervals"]), 2)

        # Should have series for error and info levels
        series_names = [s["name"] for s in data["series"]]
        self.assertIn("error", series_names)
        self.assertIn("info", series_names)

        # Check totals (includes all service buckets and environments)
        info_series = next(s for s in data["series"] if s["name"] == "info")
        error_series = next(s for s in data["series"] if s["name"] == "error")
        self.assertEqual(sum(info_series["data"]), 25)  # 10 + 15
        self.assertEqual(sum(error_series["data"]), 15)  # 3 + 5 + 7 (worker)

    def test_get_log_stats_filter_by_level(self):
        """Test filtering stats by level."""
        url = reverse(
            "api:get_log_stats", kwargs={"organization_slug": self.organization.slug}
        )
        res = self.client.get(url, {"level": ["error"]})

        self.assertEqual(res.status_code, 200)
        data = res.json()

        # Should only have error series
        series_names = [s["name"] for s in data["series"]]
        self.assertEqual(series_names, ["error"])

    def test_get_log_stats_filter_by_service(self):
        """Test filtering stats by service name (using hash bucket)."""
        url = reverse(
            "api:get_log_stats", kwargs={"organization_slug": self.organization.slug}
        )
        # Filter by "worker" service - should only get stats for that bucket
        res = self.client.get(url, {"service": ["worker"], "level": ["error"]})

        self.assertEqual(res.status_code, 200)
        data = res.json()

        # Should have error series with worker stats only
        error_series = next(s for s in data["series"] if s["name"] == "error")
        self.assertEqual(sum(error_series["data"]), 7)  # Only worker errors

    def test_get_log_stats_filter_by_environment(self):
        """Test filtering stats by environment."""
        url = reverse(
            "api:get_log_stats", kwargs={"organization_slug": self.organization.slug}
        )
        # Filter by "staging" environment
        res = self.client.get(url, {"environment": ["staging"], "level": ["error"]})

        self.assertEqual(res.status_code, 200)
        data = res.json()

        # Should have error series with staging stats only
        error_series = next(s for s in data["series"] if s["name"] == "error")
        self.assertEqual(sum(error_series["data"]), 5)  # Only staging errors

    def test_get_log_stats_empty(self):
        """Test stats for org with no data."""
        # Create new org with no stats
        new_org = baker.make("organizations_ext.Organization")
        new_org.add_user(self.user)

        url = reverse("api:get_log_stats", kwargs={"organization_slug": new_org.slug})
        res = self.client.get(url)

        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["intervals"], [])
        self.assertEqual(data["series"], [])


class LogResourcesAPITestCase(GlitchTipTestCaseMixin, TestCase):
    """Test log resources list API endpoint."""

    def setUp(self):
        self.create_logged_in_user()
        from ..models import LogResource

        # Create some resource entries
        LogResource.objects.create(
            organization=self.organization,
            name="api-gateway",
            type=LogResource.ResourceType.SERVICE,
        )
        LogResource.objects.create(
            organization=self.organization,
            name="worker",
            type=LogResource.ResourceType.SERVICE,
        )
        LogResource.objects.create(
            organization=self.organization,
            name="prod",
            type=LogResource.ResourceType.ENVIRONMENT,
        )
        LogResource.objects.create(
            organization=self.organization,
            name="host-1",
            type=LogResource.ResourceType.HOST,
        )

    def test_list_resources(self):
        """Test listing resources for an organization."""
        url = reverse(
            "api:list_log_resources",
            kwargs={"organization_slug": self.organization.slug},
        )
        res = self.client.get(url)

        self.assertEqual(res.status_code, 200)
        data = res.json()

        self.assertEqual(len(data), 4)
        resource_names = [r["name"] for r in data]
        self.assertIn("api-gateway", resource_names)
        self.assertIn("prod", resource_names)
        self.assertIn("host-1", resource_names)

    def test_list_resources_filter_by_type(self):
        """Test filtering resources by type."""
        url = reverse(
            "api:list_log_resources",
            kwargs={"organization_slug": self.organization.slug},
        )
        res = self.client.get(url, {"resource_type": "environment"})

        self.assertEqual(res.status_code, 200)
        data = res.json()

        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["name"], "prod")
        self.assertEqual(data[0]["type"], "environment")

    def test_list_resources_empty(self):
        """Test listing resources for org with no data."""
        new_org = baker.make("organizations_ext.Organization")
        new_org.add_user(self.user)

        url = reverse(
            "api:list_log_resources", kwargs={"organization_slug": new_org.slug}
        )
        res = self.client.get(url)

        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json(), [])
