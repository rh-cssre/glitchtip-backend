from django.shortcuts import reverse
from model_bakery import baker

from glitchtip.test_utils.test_case import GlitchTipTestCase


# Create your tests here.
class CommentsApiTestCase(GlitchTipTestCase):
    def setUp(self):
        self.create_user_and_project()
        self.async_client.force_login(self.user)
        self.issue = baker.make("issue_events.Issue", project=self.project)
        self.url = reverse("api:list_comments", kwargs={"issue_id": self.issue.id})

    async def test_comment_creation(self):
        data = {"data": {"text": "Test"}}
        not_my_issue = await baker.amake("issue_events.Issue")

        res = await self.async_client.post(
            self.url, data, content_type="application/json"
        )

        self.assertEqual(res.status_code, 201)
        self.assertEqual(res.json()["data"]["text"], "Test")

        url = reverse(
            "api:list_comments",
            kwargs={"issue_id": not_my_issue.id},
        )
        res = await self.async_client.post(url, data, content_type="application/json")
        self.assertEqual(res.status_code, 400)

    async def test_comments_list(self):
        comments = await baker.amake(
            "issue_events.Comment",
            issue=self.issue,
            user=self.user,
            _fill_optional=["text"],
            _quantity=3,
        )
        not_my_issue = await baker.amake("issue_events.Issue")
        await baker.amake(
            "issue_events.Comment", issue=not_my_issue, _fill_optional=["text"]
        )
        res = await self.async_client.get(self.url)
        self.assertContains(res, comments[2].text)

        url = reverse("api:list_comments", kwargs={"issue_id": not_my_issue.id})
        res = await self.async_client.get(url)
        self.assertEqual(len(res.json()), 0)

    async def test_comments_list_deleted_user(self):
        user2 = await baker.amake("users.User")
        await self.organization.aadd_user(user2)
        comment = await baker.amake(
            "issue_events.Comment",
            issue=self.issue,
            user=user2,
            _fill_optional=["text"],
        )
        await user2.adelete()
        res = await self.async_client.get(self.url)
        self.assertContains(res, comment.text)

    async def test_comment_update(self):
        comment = await baker.amake(
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

        res = await self.async_client.put(url, data, content_type="application/json")
        self.assertEqual(res.json()["data"]["text"], "Test")

    async def test_comment_delete(self):
        comment = await baker.amake(
            "issue_events.Comment",
            issue=self.issue,
            user=self.user,
            _fill_optional=["text"],
        )
        url = reverse(
            "api:delete_comment",
            kwargs={"issue_id": self.issue.id, "comment_id": comment.id},
        )
        await self.async_client.delete(url)
        res = await self.async_client.get(self.url)
        self.assertEqual(len(res.json()), 0)
