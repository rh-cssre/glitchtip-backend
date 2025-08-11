import re

from django.test import TestCase
from django.urls import reverse
from model_bakery import baker

from glitchtip.test_utils.test_case import APIPermissionTestCase, GlitchTipTestCaseMixin


def get_list_issue_event_url(issue_id: int) -> str:
    return reverse("api:list_issue_event", args=[issue_id])


def get_issue_event_url(issue_id: int, event_id: str) -> str:
    return reverse(
        "api:get_issue_event",
        kwargs={"issue_id": issue_id, "event_id": event_id},
    )


def get_latest_issue_event_url(issue_id: int) -> str:
    return reverse("api:get_latest_issue_event", kwargs={"issue_id": issue_id})


def get_event_json_url(organization_slug: str, issue_id: int, event_id: str) -> str:
    return reverse(
        "api:get_event_json",
        kwargs={
            "organization_slug": organization_slug,
            "issue_id": issue_id,
            "event_id": event_id,
        },
    )


class IssueEventAPITestCase(GlitchTipTestCaseMixin, TestCase):
    def setUp(self):
        super().create_logged_in_user()

    def test_multi_page_list(self):
        first_event = baker.make("issue_events.IssueEvent", issue__project=self.project)
        baker.make(
            "issue_events.IssueEvent",
            issue__project=self.project,
            issue_id=first_event.issue_id,
            _quantity=50,
        )
        last_event = baker.make(
            "issue_events.IssueEvent",
            issue__project=self.project,
            issue_id=first_event.issue_id,
        )
        url = get_list_issue_event_url(first_event.issue_id)

        with self.assertNumQueries(2):
            res = self.client.get(url)

        self.assertEqual(res.headers.get("X-Hits"), "52")
        self.assertIn('rel="previous"; results="false"', res.headers.get("Link"))

        self.assertEqual(res.json()[0]["id"], last_event.pk.hex)
        self.assertNotContains(res, first_event.pk.hex)

        pattern = r"(?<=\<).+?(?=\>)"
        links = re.findall(pattern, res.headers.get("Link"))

        res = self.client.get(links[1])

        self.assertEqual(res.headers.get("X-Hits"), "52")
        self.assertIn('rel="previous"; results="true"', res.headers.get("Link"))
        self.assertIn('rel="next"; results="false"', res.headers.get("Link"))

        self.assertEqual(res.json()[-1]["id"], first_event.pk.hex)
        self.assertNotContains(res, last_event.pk.hex)

    def test_single_page_list(self):
        """
        Single page query should not hit DB for count
        """
        first_event = baker.make("issue_events.IssueEvent", issue__project=self.project)
        last_event = baker.make(
            "issue_events.IssueEvent",
            issue__project=self.project,
            issue_id=first_event.issue_id,
        )
        url = get_list_issue_event_url(first_event.issue_id)

        with self.assertNumQueries(1):
            res = self.client.get(url)

        self.assertEqual(res.headers.get("X-Hits"), "2")
        self.assertContains(res, last_event.pk.hex)
        self.assertContains(res, first_event.pk.hex)

    def test_retrieve(self):
        issue = baker.make("issue_events.issue", project=self.project)
        baker.make("issue_events.IssueEvent", issue=issue, _quantity=10)
        previous_event = baker.make("issue_events.IssueEvent", issue=issue)
        latest_event = baker.make("issue_events.IssueEvent", issue=issue)
        url = get_issue_event_url(issue.id, "a" * 32)
        res = self.client.get(url)
        self.assertEqual(res.status_code, 404)

        url = get_issue_event_url(issue.id, latest_event.id)
        res = self.client.get(url)
        self.assertContains(res, latest_event.pk.hex)

        url = get_latest_issue_event_url(issue.id)
        res = self.client.get(url)
        event_details = res.json()
        self.assertEqual(event_details["id"], latest_event.pk.hex)
        self.assertEqual(event_details["previousEventID"], previous_event.pk.hex)

    def test_relative_event_ordering(self):
        issue = baker.make("issue_events.issue", project=self.project)
        baker.make("issue_events.IssueEvent", issue=issue)
        event1 = baker.make("issue_events.IssueEvent", issue=issue)
        event2 = baker.make("issue_events.IssueEvent", issue=issue)
        event3 = baker.make("issue_events.IssueEvent", issue=issue)
        baker.make("issue_events.IssueEvent", issue=issue)
        url = get_issue_event_url(issue.id, event2.id)
        res = self.client.get(url)
        event_details = res.json()
        self.assertEqual(event_details["nextEventID"], event3.pk.hex)
        self.assertEqual(event_details["previousEventID"], event1.pk.hex)

    def test_authentication(self):
        url = get_list_issue_event_url(1)
        self.client.logout()
        res = self.client.get(url)
        self.assertEqual(res.status_code, 401)


class IssueEventAPIPermissionTestCase(APIPermissionTestCase):
    def setUp(self):
        self.create_user_org()
        self.set_client_credentials(self.auth_token.token)
        self.team = baker.make("teams.Team", organization=self.organization)
        self.team.members.add(self.org_user)
        self.project = baker.make("projects.Project", organization=self.organization)
        self.project.teams.add(self.team)
        self.event = baker.make("issue_events.IssueEvent", issue__project=self.project)

        self.list_url = get_list_issue_event_url(self.event.issue_id)
        self.project_list_url = reverse(
            "api:list_project_issue_event",
            kwargs={
                "organization_slug": self.organization.slug,
                "project_slug": self.project.slug,
            },
        )
        self.detail_url = get_issue_event_url(self.event.issue_id, self.event.pk)
        self.project_detail_url = reverse(
            "api:get_project_issue_event",
            kwargs={
                "organization_slug": self.organization.slug,
                "project_slug": self.project.slug,
                "event_id": self.event.pk.hex,
            },
        )
        self.latest_detail_url = get_latest_issue_event_url(self.event.issue_id)
        self.event_json_url = get_event_json_url(
            self.organization.slug, self.event.issue_id, self.event.pk
        )

    def test_list(self):
        self.assertGetReqStatusCode(self.list_url, 403)
        self.assertGetReqStatusCode(self.project_list_url, 403)
        self.auth_token.add_permission("event:read")
        self.assertGetReqStatusCode(self.list_url, 200)
        self.assertGetReqStatusCode(self.project_list_url, 200)

    def test_retrieve(self):
        self.assertGetReqStatusCode(self.detail_url, 403)
        self.assertGetReqStatusCode(self.project_detail_url, 403)
        self.assertGetReqStatusCode(self.latest_detail_url, 403)
        self.auth_token.add_permission("event:read")
        self.assertGetReqStatusCode(self.detail_url, 200)
        self.assertGetReqStatusCode(self.project_detail_url, 200)
        self.assertGetReqStatusCode(self.latest_detail_url, 200)

    def test_event_json_view(self):
        url = self.event_json_url
        self.assertGetReqStatusCode(url, 403)
        self.auth_token.add_permission("event:read")
        self.assertGetReqStatusCode(url, 200)


class CommentsAPIPermissionTests(APIPermissionTestCase):
    def setUp(self):
        self.create_org_team_project()
        self.set_client_credentials(self.auth_token.token)
        self.project = baker.make("projects.Project", organization=self.organization)
        self.issue = baker.make("issue_events.Issue", project=self.project)
        self.comment = baker.make(
            "issue_events.Comment", issue=self.issue, user=self.user, text="text"
        )

        self.list_url = reverse(
            "api:list_comments",
            kwargs={"issue_id": self.issue.id},
        )

    def test_list(self):
        self.assertGetReqStatusCode(self.list_url, 403)

        self.auth_token.add_permission("event:read")
        self.assertGetReqStatusCode(self.list_url, 200)

    def test_create(self):
        self.auth_token.add_permission("event:read")
        data = {"data": {"text": "Test"}}
        url = reverse(
            "api:add_comment",
            kwargs={"issue_id": self.issue.id},
        )
        self.assertPostReqStatusCode(url, data, 403)

        self.auth_token.add_permission("event:write")
        self.assertPostReqStatusCode(url, data, 201)

    def test_destroy(self):
        self.auth_token.add_permissions(["event:read", "event:write"])
        url = reverse(
            "api:delete_comment",
            kwargs={"issue_id": self.issue.id, "comment_id": self.comment.id},
        )
        self.assertDeleteReqStatusCode(url, 403)

        self.auth_token.add_permission("event:admin")
        self.assertDeleteReqStatusCode(url, 204)

    def test_update(self):
        self.auth_token.add_permission("event:read")
        data = {"data": {"text": "Test"}}
        url = reverse(
            "api:update_comment",
            kwargs={"issue_id": self.issue.id, "comment_id": self.comment.id},
        )
        self.assertPutReqStatusCode(url, data, 403)

        self.auth_token.add_permission("event:write")
        self.assertPutReqStatusCode(url, data, 200)
