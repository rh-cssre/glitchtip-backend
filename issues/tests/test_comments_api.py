from django.shortcuts import reverse
from model_bakery import baker

from glitchtip.test_utils.test_case import GlitchTipTestCase


# Create your tests here.
class CommentsApiTestCase(GlitchTipTestCase):
    def setUp(self):
        self.create_user_and_project()
        self.issue = baker.make("issues.Issue", project=self.project)
        self.url = reverse(
            "issue-comments-list",
            kwargs={"issue_pk": self.issue.pk},
        )

    def test_comment_creation(self):
        data = {"data": {"text": "Test"}}
        not_my_issue = baker.make("issues.Issue")

        res = self.client.post(self.url, data, format="json")

        self.assertEqual(res.status_code, 201)
        self.assertEqual(res.data["data"]["text"], "Test")

        url = reverse(
            "issue-comments-list",
            kwargs={"issue_pk": not_my_issue.pk},
        )
        res = self.client.post(url, data, format="json")
        self.assertEqual(res.status_code, 400)

    def test_comments_list(self):
        comments = baker.make(
            "issues.Comment", issue=self.issue, _fill_optional=["text"], _quantity=3
        )
        not_my_issue = baker.make("issues.Issue")
        not_my_comment = baker.make(
            "issues.Comment", issue=not_my_issue, _fill_optional=["text"]
        )

        res = self.client.get(self.url)
        self.assertContains(res, comments[2].text)

        url = reverse(
            "issue-comments-list",
            kwargs={"issue_pk": not_my_issue.pk},
        )
        res = self.client.get(url)
        self.assertEqual(len(res.data), 0)

    def test_comment_update(self):
        comment = baker.make(
            "issues.Comment",
            issue=self.issue,
            user=self.user,
            _fill_optional=["text"],
        )
        url = reverse(
            "issue-comments-detail",
            kwargs={"issue_pk": self.issue.pk, "pk": comment.pk},
        )
        data = {"data": {"text": "Test"}}

        res = self.client.put(url, data, format="json")
        self.assertEqual(res.data["data"]["text"], "Test")

    def test_comment_delete(self):
        comment = baker.make(
            "issues.Comment",
            issue=self.issue,
            user=self.user,
            _fill_optional=["text"],
        )
        url = reverse(
            "issue-comments-detail",
            kwargs={"issue_pk": self.issue.pk, "pk": comment.pk},
        )
        self.client.delete(url)
        res = self.client.get(self.url)
        self.assertEqual(len(res.data), 0)
