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

        res = self.client.post(self.url, data, format="json")

        self.assertEqual(res.status_code, 201)
        self.assertEqual(res.data["data"]["text"], "Test")

    def test_comments_list(self):
        comments = baker.make(
            "issues.Comment", issue=self.issue, _fill_optional=["text"], _quantity=3
        )
        other_issue = baker.make("issues.Issue")
        other_comment = baker.make(
            "issues.Comment", issue=other_issue, _fill_optional=["text"]
        )

        res = self.client.get(self.url)
        self.assertContains(res, comments[2].text)
        self.assertNotContains(res, other_comment.text)

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
