from datetime import timedelta
from unittest.mock import patch

from asgiref.sync import async_to_sync
from django.test import TestCase
from django.utils import timezone
from mcp.server.auth.provider import AccessToken
from model_bakery import baker

from apps.issue_events.constants import EventStatus
from apps.mcp.auth import validate_token
from apps.mcp.data import (
    get_alerts,
    get_event,
    get_issue,
    get_issues,
    get_latest_event,
    get_monitors,
    get_organizations,
    get_projects,
    update_issue,
)
from apps.mcp.serializers import (
    serialize_alert,
    serialize_event,
    serialize_issue,
    serialize_monitor,
    serialize_organization,
    serialize_project,
)
from apps.mcp.server import _check_scopes, _parse_datetime
from glitchtip.test_utils.issue import make_issue


class ValidateTokenTest(TestCase):
    def test_valid_token(self):
        user = baker.make("users.user", is_active=True)
        token_obj = baker.make("api_tokens.APIToken", user=user)
        token_obj.add_permission("project:read")

        user_id, scopes = async_to_sync(validate_token)(token_obj.token)
        self.assertEqual(user_id, user.id)
        self.assertIn("project:read", scopes)

    def test_invalid_token(self):
        with self.assertRaises(ValueError):
            async_to_sync(validate_token)("invalid_token_value")

    def test_inactive_user_token(self):
        user = baker.make("users.user", is_active=False)
        token_obj = baker.make("api_tokens.APIToken", user=user)

        with self.assertRaises(ValueError):
            async_to_sync(validate_token)(token_obj.token)


class CheckScopesTest(TestCase):
    def _mock_access_token(self, user_id, scopes):
        return AccessToken(
            token="test-token",
            client_id=str(user_id),
            scopes=scopes,
        )

    def test_scope_enforcement(self):
        """Token without required scope should be rejected."""
        user = baker.make("users.user", is_active=True)
        token = self._mock_access_token(user.id, ["project:read"])

        with patch("apps.mcp.server.get_access_token", return_value=token):
            # Should pass with matching scope
            user_id = _check_scopes(["project:read"])
            self.assertEqual(user_id, user.id)

            # Should pass with broader scope list (OR logic)
            user_id = _check_scopes(["project:read", "project:write"])
            self.assertEqual(user_id, user.id)

            # Should fail when token lacks all required scopes
            with self.assertRaises(ValueError):
                _check_scopes(["event:read"])

    def test_no_scopes_rejected(self):
        """Token with no scopes should be rejected."""
        user = baker.make("users.user", is_active=True)
        token = self._mock_access_token(user.id, [])

        with patch("apps.mcp.server.get_access_token", return_value=token):
            with self.assertRaises(ValueError):
                _check_scopes(["project:read"])

    def test_not_authenticated(self):
        """Missing auth context should raise ValueError."""
        with patch("apps.mcp.server.get_access_token", return_value=None):
            with self.assertRaises(ValueError):
                _check_scopes(["project:read"])


class DataLayerTest(TestCase):
    def setUp(self):
        self.user = baker.make("users.user")
        self.project = baker.make("projects.Project")
        self.organization = self.project.organization
        self.org_user = self.organization.add_user(self.user)
        self.team = baker.make("teams.Team", organization=self.organization)
        self.team.members.add(self.org_user)
        self.project.teams.add(self.team)

    def test_get_organizations(self):
        baker.make("projects.Project")

        orgs = async_to_sync(get_organizations)(self.user.id)
        self.assertEqual(len(orgs), 1)
        self.assertEqual(orgs[0].id, self.organization.id)

    def test_get_projects(self):
        baker.make("projects.Project")

        projects = async_to_sync(get_projects)(self.user.id, self.organization.slug)
        self.assertEqual(len(projects), 1)
        self.assertEqual(projects[0].id, self.project.id)

    def test_get_issues(self):
        issue = make_issue(
            project=self.project,
            title="Test Issue",
        )
        baker.make("issue_events.Issue")

        issues = async_to_sync(get_issues)(self.user.id, self.organization.slug)
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0].id, issue.id)

    def test_get_issues_with_project_filter(self):
        make_issue(
            project=self.project,
            title="Test Issue",
        )
        project2 = baker.make("projects.Project", organization=self.organization)
        project2.teams.add(self.team)
        baker.make("issue_events.Issue", project=project2, title="Other Issue")

        issues = async_to_sync(get_issues)(
            self.user.id, self.organization.slug, project_slug=self.project.slug
        )
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0].title, "Test Issue")

    def test_get_issue(self):
        issue = baker.make("issue_events.Issue", project=self.project)
        result = async_to_sync(get_issue)(self.user.id, issue.id)
        self.assertIsNotNone(result)
        self.assertEqual(result.id, issue.id)

    def test_get_issue_access_control(self):
        """User should not see issues from other organizations."""
        other_issue = baker.make("issue_events.Issue")
        result = async_to_sync(get_issue)(self.user.id, other_issue.id)
        self.assertIsNone(result)

    def test_get_latest_event(self):
        issue = baker.make("issue_events.Issue", project=self.project)
        event = baker.make(
            "issue_events.IssueEvent",
            issue=issue,
            organization=self.organization,
            data={},
            tags={},
        )

        result = async_to_sync(get_latest_event)(self.user.id, issue.id)
        self.assertIsNotNone(result)
        self.assertEqual(result.id, event.id)

    def test_get_latest_event_access_control(self):
        """User should not see events from other organizations."""
        other_issue = baker.make("issue_events.Issue")
        baker.make(
            "issue_events.IssueEvent",
            issue=other_issue,
            organization=other_issue.project.organization,
            data={},
            tags={},
        )

        result = async_to_sync(get_latest_event)(self.user.id, other_issue.id)
        self.assertIsNone(result)

    def test_get_event_by_id(self):
        """Look up event by server-generated UUIDv7 id."""
        issue = baker.make("issue_events.Issue", project=self.project)
        event = baker.make(
            "issue_events.IssueEvent",
            issue=issue,
            organization=self.organization,
            data={},
            tags={},
        )

        result = async_to_sync(get_event)(self.user.id, str(event.id))
        self.assertIsNotNone(result)
        self.assertEqual(result.id, event.id)
        # Verify issue is prefetched
        self.assertEqual(result.issue.id, issue.id)

    def test_get_event_by_event_id(self):
        """Look up event by client-provided Sentry SDK event_id."""
        import uuid

        issue = baker.make("issue_events.Issue", project=self.project)
        sdk_event_id = uuid.uuid4()
        event = baker.make(
            "issue_events.IssueEvent",
            issue=issue,
            organization=self.organization,
            event_id=sdk_event_id,
            data={},
            tags={},
        )

        result = async_to_sync(get_event)(self.user.id, str(sdk_event_id))
        self.assertIsNotNone(result)
        self.assertEqual(result.id, event.id)

    def test_get_event_access_control(self):
        """User should not see events from other organizations."""
        other_issue = baker.make("issue_events.Issue")
        event = baker.make(
            "issue_events.IssueEvent",
            issue=other_issue,
            organization=other_issue.project.organization,
            data={},
            tags={},
        )

        result = async_to_sync(get_event)(self.user.id, str(event.id))
        self.assertIsNone(result)

    def test_get_event_invalid_uuid(self):
        result = async_to_sync(get_event)(self.user.id, "not-a-uuid")
        self.assertIsNone(result)

    def test_get_issues_limit_cap(self):
        """Limit should be capped at MAX_ISSUE_LIMIT."""
        for _ in range(3):
            baker.make("issue_events.Issue", project=self.project)

        # Even with a huge limit, function should work (cap applied internally)
        issues = async_to_sync(get_issues)(
            self.user.id, self.organization.slug, limit=999999
        )
        self.assertEqual(len(issues), 3)

    def test_get_alerts(self):
        alert = baker.make(
            "alerts.ProjectAlert",
            project=self.project,
            timespan_minutes=60,
        )
        baker.make("alerts.ProjectAlert", timespan_minutes=60)

        alerts = async_to_sync(get_alerts)(
            self.user.id, self.organization.slug, project_slug=self.project.slug
        )
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0].id, alert.id)

    def test_get_alerts_org_wide(self):
        baker.make(
            "alerts.ProjectAlert",
            project=self.project,
            timespan_minutes=60,
        )
        alerts = async_to_sync(get_alerts)(self.user.id, self.organization.slug)
        self.assertEqual(len(alerts), 1)

    def test_get_monitors(self):
        monitor = baker.make(
            "uptime.Monitor",
            organization=self.organization,
            name="Test Monitor",
        )
        baker.make("uptime.Monitor")

        monitors = async_to_sync(get_monitors)(self.user.id, self.organization.slug)
        self.assertEqual(len(monitors), 1)
        self.assertEqual(monitors[0].id, monitor.id)

    def test_update_issue_resolve(self):
        issue = make_issue(
            project=self.project,
            status=EventStatus.UNRESOLVED,
        )
        result = async_to_sync(update_issue)(self.user.id, issue.id, "resolved")
        self.assertIsNotNone(result)
        self.assertEqual(result.status, EventStatus.RESOLVED)

    def test_update_issue_unresolve(self):
        issue = make_issue(
            project=self.project,
            status=EventStatus.RESOLVED,
        )
        result = async_to_sync(update_issue)(self.user.id, issue.id, "unresolved")
        self.assertIsNotNone(result)
        self.assertEqual(result.status, EventStatus.UNRESOLVED)
        self.assertIsNone(result.resolved_in_release)

    def test_update_issue_ignore(self):
        issue = make_issue(
            project=self.project,
            status=EventStatus.UNRESOLVED,
        )
        result = async_to_sync(update_issue)(self.user.id, issue.id, "ignored")
        self.assertIsNotNone(result)
        self.assertEqual(result.status, EventStatus.IGNORED)

    def test_update_issue_resolve_in_next_release(self):
        release = baker.make(
            "releases.Release",
            organization=self.organization,
            version="1.0.0",
        )
        release.projects.add(self.project)
        issue = make_issue(
            project=self.project,
            status=EventStatus.UNRESOLVED,
        )
        result = async_to_sync(update_issue)(
            self.user.id, issue.id, "resolved", in_next_release=True
        )
        self.assertIsNotNone(result)
        self.assertEqual(result.status, EventStatus.RESOLVED)
        self.assertEqual(result.resolved_in_release, release)

    def test_update_issue_resolve_in_release(self):
        release = baker.make(
            "releases.Release",
            organization=self.organization,
            version="2.0.0",
        )
        issue = make_issue(
            project=self.project,
            status=EventStatus.UNRESOLVED,
        )
        result = async_to_sync(update_issue)(
            self.user.id, issue.id, "resolved", in_release="2.0.0"
        )
        self.assertIsNotNone(result)
        self.assertEqual(result.status, EventStatus.RESOLVED)
        self.assertEqual(result.resolved_in_release, release)

    def test_update_issue_access_control(self):
        """User should not be able to update other org's issues."""
        other_issue = baker.make("issue_events.Issue")
        result = async_to_sync(update_issue)(self.user.id, other_issue.id, "resolved")
        self.assertIsNone(result)

    def test_update_issue_not_found(self):
        result = async_to_sync(update_issue)(self.user.id, 999999, "resolved")
        self.assertIsNone(result)

    @patch("apps.issue_events.cold_storage.is_duckdb_available", return_value=True)
    @patch("apps.issue_events.cold_storage.query_cold_events")
    def test_get_latest_event_cold_storage_fallback(
        self, mock_query_cold, _mock_duckdb
    ):
        """When Postgres has no events, fall back to cold storage."""
        issue = baker.make("issue_events.Issue", project=self.project)
        cold_event = baker.prepare(
            "issue_events.IssueEvent",
            issue=issue,
            organization=self.organization,
            data={},
            tags={},
        )
        mock_query_cold.return_value = [cold_event]

        result = async_to_sync(get_latest_event)(self.user.id, issue.id)
        self.assertIsNotNone(result)
        self.assertEqual(result.issue, issue)
        mock_query_cold.assert_called_once()

    @patch("apps.issue_events.cold_storage.is_duckdb_available", return_value=False)
    def test_get_latest_event_no_duckdb(self, _mock_duckdb):
        """When DuckDB is not available, return None."""
        issue = baker.make("issue_events.Issue", project=self.project)

        result = async_to_sync(get_latest_event)(self.user.id, issue.id)
        self.assertIsNone(result)

    @patch("apps.issue_events.cold_storage.is_duckdb_available", return_value=True)
    @patch("apps.issue_events.cold_storage.get_event_from_cold")
    def test_get_event_cold_storage_uuid7_fallback(self, mock_get_cold, _mock_duckdb):
        """UUIDv7 cold fallback uses get_event_from_cold with extracted timestamp."""
        from glitchtip.partition_manager import UUID7Helper

        issue = baker.make("issue_events.Issue", project=self.project)
        event_uuid = UUID7Helper._uuid7_for_timestamp(timezone.now())
        cold_event = baker.prepare(
            "issue_events.IssueEvent",
            id=event_uuid,
            issue=issue,
            organization=self.organization,
            data={},
            tags={},
        )
        mock_get_cold.return_value = cold_event

        result = async_to_sync(get_event)(self.user.id, str(event_uuid))
        self.assertIsNotNone(result)
        self.assertEqual(result.issue, issue)
        mock_get_cold.assert_called_once()

    @patch("apps.issue_events.cold_storage.is_duckdb_available", return_value=True)
    @patch("apps.issue_events.cold_storage.query_cold_events")
    def test_get_event_cold_storage_uuid4_fallback(self, mock_query_cold, _mock_duckdb):
        """UUIDv4 cold fallback scans recent cold storage by event_id."""
        import uuid as uuid_mod

        issue = baker.make("issue_events.Issue", project=self.project)
        sdk_event_id = uuid_mod.uuid4()
        cold_event = baker.prepare(
            "issue_events.IssueEvent",
            issue=issue,
            organization=self.organization,
            event_id=sdk_event_id,
            data={},
            tags={},
        )
        mock_query_cold.return_value = [cold_event]

        result = async_to_sync(get_event)(self.user.id, str(sdk_event_id))
        self.assertIsNotNone(result)
        self.assertEqual(result.issue, issue)
        mock_query_cold.assert_called_once()
        # Verify event_id was passed to cold storage query
        call_kwargs = mock_query_cold.call_args[1]
        self.assertEqual(call_kwargs["event_id"], sdk_event_id)

    @patch("apps.issue_events.cold_storage.is_duckdb_available", return_value=True)
    @patch("apps.issue_events.cold_storage.get_event_from_cold")
    def test_get_event_cold_storage_with_org_slug(self, mock_get_cold, _mock_duckdb):
        """Providing organization_slug scopes the cold storage search."""
        from glitchtip.partition_manager import UUID7Helper

        issue = baker.make("issue_events.Issue", project=self.project)
        event_uuid = UUID7Helper._uuid7_for_timestamp(timezone.now())
        cold_event = baker.prepare(
            "issue_events.IssueEvent",
            id=event_uuid,
            issue=issue,
            organization=self.organization,
            data={},
            tags={},
        )
        mock_get_cold.return_value = cold_event

        result = async_to_sync(get_event)(
            self.user.id, str(event_uuid), self.organization.slug
        )
        self.assertIsNotNone(result)
        self.assertEqual(result.issue, issue)

    @patch("apps.issue_events.cold_storage.is_duckdb_available", return_value=False)
    def test_get_event_no_duckdb(self, _mock_duckdb):
        """When DuckDB is not available, return None for missing events."""
        import uuid as uuid_mod

        result = async_to_sync(get_event)(self.user.id, str(uuid_mod.uuid4()))
        self.assertIsNone(result)


class SerializerTest(TestCase):
    def setUp(self):
        self.project = baker.make("projects.Project")
        self.organization = self.project.organization

    def test_serialize_organization(self):
        result = serialize_organization(self.organization)
        self.assertEqual(result["id"], str(self.organization.id))
        self.assertEqual(result["name"], self.organization.name)
        self.assertEqual(result["slug"], self.organization.slug)
        self.assertIn("dateCreated", result)

    def test_serialize_project(self):
        result = serialize_project(self.project)
        self.assertEqual(result["id"], str(self.project.id))
        self.assertEqual(result["name"], self.project.name)
        self.assertIn("slug", result)

    def test_serialize_issue(self):
        now = timezone.now()
        issue = make_issue(
            project=self.project,
            title="Test Error",
            count=5,
            first_seen=now,
            last_seen=now,
            metadata={"type": "Error"},
        )
        result = serialize_issue(issue)
        self.assertEqual(result["id"], str(issue.id))
        self.assertEqual(result["title"], "Test Error")
        self.assertEqual(result["count"], 5)
        self.assertIn("level", result)
        self.assertIn("status", result)
        self.assertIn("firstSeen", result)
        self.assertIn("lastSeen", result)

    def test_serialize_event(self):
        issue = baker.make("issue_events.Issue", project=self.project)
        event = baker.make(
            "issue_events.IssueEvent",
            issue=issue,
            organization=self.organization,
            title="Event Title",
            data={"contexts": {"os": {"name": "Linux"}}},
            tags={"browser": "Chrome"},
        )
        result = serialize_event(event)
        self.assertEqual(result["title"], "Event Title")
        self.assertIn("eventId", result)
        self.assertIn("tags", result)
        self.assertEqual(result["tags"], [{"key": "browser", "value": "Chrome"}])
        self.assertEqual(result["contexts"], {"os": {"name": "Linux"}})

    def test_serialize_alert(self):
        alert = baker.make(
            "alerts.ProjectAlert",
            project=self.project,
            name="My Alert",
            timespan_minutes=30,
            quantity=5,
        )
        result = serialize_alert(alert)
        self.assertEqual(result["name"], "My Alert")
        self.assertEqual(result["timespanMinutes"], 30)
        self.assertEqual(result["quantity"], 5)

    def test_serialize_monitor(self):
        monitor = baker.make(
            "uptime.Monitor",
            organization=self.organization,
            name="Uptime Check",
            url="https://example.com",
            interval=60,
        )
        result = serialize_monitor(monitor)
        self.assertEqual(result["name"], "Uptime Check")
        self.assertEqual(result["url"], "https://example.com")
        self.assertEqual(result["interval"], 60)


class ParseDatetimeTest(TestCase):
    def test_none_returns_none(self):
        self.assertIsNone(_parse_datetime(None))

    def test_empty_string_returns_none(self):
        self.assertIsNone(_parse_datetime(""))

    def test_iso8601(self):
        result = _parse_datetime("2025-01-15T10:00:00Z")
        self.assertEqual(result.year, 2025)
        self.assertEqual(result.month, 1)
        self.assertEqual(result.day, 15)

    def test_iso8601_naive_gets_utc(self):
        result = _parse_datetime("2025-01-15T10:00:00")
        self.assertIsNotNone(result.tzinfo)

    def test_relative_now(self):
        before = timezone.now()
        result = _parse_datetime("now")
        after = timezone.now()
        self.assertGreaterEqual(result, before)
        self.assertLessEqual(result, after)

    def test_relative_now_minus_10m(self):
        result = _parse_datetime("now-10m")
        expected = timezone.now() - timedelta(minutes=10)
        self.assertAlmostEqual(result.timestamp(), expected.timestamp(), delta=2)

    def test_relative_now_minus_1h(self):
        result = _parse_datetime("now-1h")
        expected = timezone.now() - timedelta(hours=1)
        self.assertAlmostEqual(result.timestamp(), expected.timestamp(), delta=2)

    def test_invalid_raises(self):
        with self.assertRaises(ValueError):
            _parse_datetime("not-a-date")


class IssueFilterTest(TestCase):
    def setUp(self):
        self.user = baker.make("users.user")
        self.project = baker.make("projects.Project")
        self.organization = self.project.organization
        self.org_user = self.organization.add_user(self.user)
        self.team = baker.make("teams.Team", organization=self.organization)
        self.team.members.add(self.org_user)
        self.project.teams.add(self.team)

    def test_filter_by_start(self):
        now = timezone.now()
        old_issue = make_issue(
            project=self.project,
            first_seen=now - timedelta(hours=2),
        )
        new_issue = make_issue(
            project=self.project,
            first_seen=now - timedelta(minutes=5),
        )

        issues = async_to_sync(get_issues)(
            self.user.id,
            self.organization.slug,
            start=now - timedelta(minutes=10),
        )
        issue_ids = [i.id for i in issues]
        self.assertIn(new_issue.id, issue_ids)
        self.assertNotIn(old_issue.id, issue_ids)

    def test_filter_by_start_and_end(self):
        now = timezone.now()
        make_issue(
            project=self.project,
            first_seen=now - timedelta(hours=5),
        )
        target_issue = make_issue(
            project=self.project,
            first_seen=now - timedelta(hours=2),
        )
        make_issue(
            project=self.project,
            first_seen=now - timedelta(minutes=5),
        )

        issues = async_to_sync(get_issues)(
            self.user.id,
            self.organization.slug,
            start=now - timedelta(hours=3),
            end=now - timedelta(hours=1),
        )
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0].id, target_issue.id)

    def test_filter_by_environment(self):
        issue = make_issue(
            project=self.project,
        )
        tag_key = baker.make("issue_events.TagKey", key="environment")
        tag_value = baker.make("issue_events.TagValue", value="production")
        baker.make(
            "issue_events.IssueTag",
            issue=issue,
            tag_key=tag_key,
            tag_value=tag_value,
        )
        baker.make("issue_events.Issue", project=self.project)

        issues = async_to_sync(get_issues)(
            self.user.id,
            self.organization.slug,
            environment="production",
        )
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0].id, issue.id)
