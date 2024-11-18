from django.shortcuts import reverse
from model_bakery import baker

from glitchtip.test_utils.test_case import GlitchTipTestCase


# Create your tests here.
class CommentsApiTestCase(GlitchTipTestCase):
    def setUp(self):
        self.create_user_and_project()
        self.issue = baker.make("issue_events.Issue", project=self.project)
        self.url = reverse("api:list_comments", kwargs={"issue_id": self.issue.id})

    def test_comment_creation(self):
        data = {"data": {"text": "Test"}}
        not_my_issue = baker.make("issue_events.Issue")

        res = self.client.post(self.url, data, content_type="application/json")

        self.assertEqual(res.status_code, 201)
        self.assertEqual(res.json()["data"]["text"], "Test")

        url = reverse(
            "api:list_comments",
            kwargs={"issue_id": not_my_issue.id},
        )
        res = self.client.post(url, data, content_type="application/json")
        self.assertEqual(res.status_code, 400)

    def test_comments_list(self):
        comments = baker.make(
            "issue_events.Comment",
            issue=self.issue,
            user=self.user,
            _fill_optional=["text"],
            _quantity=3,
        )
        not_my_issue = baker.make("issue_events.Issue")
        baker.make("issue_events.Comment", issue=not_my_issue, _fill_optional=["text"])
        res = self.client.get(self.url)
        self.assertContains(res, comments[2].text)

        url = reverse("api:list_comments", kwargs={"issue_id": not_my_issue.id})
        res = self.client.get(url)
        self.assertEqual(len(res.json()), 0)

    def test_comments_list_deleted_user(self):
        user2 = baker.make(
            "users.User"
        )
        self.organization.add_user(user2)
        comment = baker.make(
            "issue_events.Comment",
            issue=self.issue,
            user=user2,
            _fill_optional=["text"],
        )
        user2.delete()
        res = self.client.get(self.url)
        self.assertContains(res, comment.text)

    def test_comment_update(self):
        comment = baker.make(
            "issue_events.Comment",
            issue=self.issue,
            user=self.user,
            _fill_optional=["text"],
        )
        url = reverse(
            "api:update_comment",
            kwargs={"issue_id": self.issue.id, "comment_id": comment.id},
        )
        data = {"data": {"text": "Test"}}

        res = self.client.put(url, data, content_type="application/json")
        self.assertEqual(res.json()["data"]["text"], "Test")

    def test_comment_delete(self):
        comment = baker.make(
            "issue_events.Comment",
            issue=self.issue,
            user=self.user,
            _fill_optional=["text"],
        )
        url = reverse(
            "api:delete_comment",
            kwargs={"issue_id": self.issue.id, "comment_id": comment.id},
        )
        self.client.delete(url)
        res = self.client.get(self.url)
        self.assertEqual(len(res.json()), 0)
