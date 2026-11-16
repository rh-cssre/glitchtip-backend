"""Behaviour of the issue routes under the folded gate.

master answered a non-member of the path's organization with 404 on these
routes, from a membership lookup ahead of the data query. With the gate
folded into the data query a non-member's query simply finds nothing, so
each route that used to 404 must still ask the gate why it found nothing.
"""

import json
import uuid
from unittest.mock import AsyncMock, patch

from django.urls import reverse
from model_bakery import baker

from glitchtip.test_utils.issue import amake_issue
from glitchtip.test_utils.test_case import GlitchTestCase

from ..models import Issue


class OrgScopedIssueRouteTests(GlitchTestCase):
    @classmethod
    def setUpTestData(cls):
        cls.create_user()

    def setUp(self):
        self.async_client.force_login(self.user)

    async def request(self, method, name, args, data=None, query=""):
        return await self.async_client.generic(
            method,
            reverse(f"api:{name}", args=args) + query,
            "" if data is None else json.dumps(data),
            content_type="application/json",
        )

    def routes(self, org, issue_id):
        project = self.project.slug
        return (
            ("GET", "list_issues", [org], None, ""),
            ("GET", "list_project_issues", [org, project], None, ""),
            ("PUT", "update_issues", [org], {"status": "resolved"}, ""),
            ("DELETE", "delete_issues", [org], None, ""),
            ("GET", "issue_stats", [org], None, f"?groups={issue_id}"),
            ("GET", "list_issue_hashes", [org, issue_id], None, ""),
            ("DELETE", "delete_hash", [org, issue_id], None, f"?id={uuid.uuid4()}"),
            ("GET", "list_organization_issue_event", [org, issue_id], None, ""),
        )

    async def test_non_member_gets_404(self):
        issue = await amake_issue(project=self.project)
        outsider = await baker.amake("users.user")
        await self.async_client.aforce_login(outsider)
        for method, name, args, data, query in self.routes(
            self.organization.slug, issue.id
        ):
            with self.subTest(name):
                res = await self.request(method, name, args, data, query)
                self.assertEqual(res.status_code, 404)
        await issue.arefresh_from_db()
        self.assertFalse(issue.is_deleted)

    async def test_member_with_nothing_matching_is_not_a_404(self):
        # No issue 0, so every route finds nothing; a member is not refused.
        for method, name, args, data, query in self.routes(self.organization.id, 0):
            with self.subTest(name):
                res = await self.request(method, name, args, data, query)
                self.assertIn(res.status_code, (200, 202))

    async def test_project_event_cold_fallback_needs_membership(self):
        outsider = await baker.amake("users.user")
        await self.async_client.aforce_login(outsider)
        with patch("apps.issue_events.api.events._get_event_from_cold") as cold:
            res = await self.request(
                "GET",
                "get_project_issue_event",
                [self.organization.slug, self.project.slug, uuid.uuid4()],
            )
        self.assertEqual(res.status_code, 404)
        cold.assert_not_called()

    @patch("apps.issue_events.api.issues.delete_issue_task")
    async def test_bulk_delete_deletes_and_enqueues(self, task):
        task.aenqueue = AsyncMock()
        issues = [await amake_issue(project=self.project) for _ in range(2)]
        res = await self.request("DELETE", "delete_issues", [self.organization.id])
        self.assertEqual(res.status_code, 200)
        ids = [issue.id for issue in issues]
        self.assertEqual(
            await Issue.objects.filter(id__in=ids, is_deleted=True).acount(), 2
        )
        self.assertCountEqual(task.aenqueue.await_args.args[0], ids)

    @patch("apps.issue_events.api.issues.update_issues_task")
    async def test_bulk_update_task_gets_the_slug(self, task):
        """The task re-resolves the organization by slug, so a path that names
        the organization by id must still hand it the slug."""
        task.aenqueue = AsyncMock()
        for _ in range(51):  # one past the batch handled inline
            await amake_issue(project=self.project)
        res = await self.request(
            "PUT", "update_issues", [self.organization.id], {"status": "resolved"}
        )
        self.assertEqual(res.status_code, 200)
        self.assertEqual(
            task.aenqueue.await_args.kwargs["organization_slug"],
            self.organization.slug,
        )
