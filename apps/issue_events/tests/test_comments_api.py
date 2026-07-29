from django.shortcuts import reverse
from model_bakery import baker

from apps.organizations_ext.constants import OrganizationUserRole
from glitchtip.test_utils.test_case import GlitchTipTestCase

from ..models import Comment


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


class OrganizationScopedCommentsApiTestCase(GlitchTipTestCase):
    """Org-scoped twins of the bare ``/issues/{id}/comments/`` routes.

    ``other_organization`` is one the user *is* a member of, so a comment
    reachable through it proves the slug is being ignored rather than merely
    proving that membership filtering works.
    """

    def setUp(self):
        self.create_user_and_project()
        self.async_client.force_login(self.user)
        self.issue = baker.make("issue_events.Issue", project=self.project)
        self.other_organization = baker.make("organizations_ext.Organization")
        self.other_organization.add_user(self.user, OrganizationUserRole.ADMIN)

    def list_url(self, organization_slug: str, issue_id: int) -> str:
        return reverse(
            "api:organization_list_comments",
            kwargs={"organization_slug": organization_slug, "issue_id": issue_id},
        )

    def add_url(self, organization_slug: str, issue_id: int) -> str:
        return reverse(
            "api:add_organization_comment",
            kwargs={"organization_slug": organization_slug, "issue_id": issue_id},
        )

    def update_url(self, organization_slug: str, issue_id: int, comment_id: int) -> str:
        return reverse(
            "api:update_organization_comment",
            kwargs={
                "organization_slug": organization_slug,
                "issue_id": issue_id,
                "comment_id": comment_id,
            },
        )

    def delete_url(self, organization_slug: str, issue_id: int, comment_id: int) -> str:
        return reverse(
            "api:delete_organization_comment",
            kwargs={
                "organization_slug": organization_slug,
                "issue_id": issue_id,
                "comment_id": comment_id,
            },
        )

    async def amake_comment(self):
        return await baker.amake(
            "issue_events.Comment",
            issue=self.issue,
            user=self.user,
            _fill_optional=["text"],
        )

    async def test_list(self):
        comment = await self.amake_comment()
        res = await self.async_client.get(
            self.list_url(self.organization.slug, self.issue.id)
        )
        self.assertContains(res, comment.text)

    async def test_list_wrong_organization(self):
        await self.amake_comment()
        res = await self.async_client.get(
            self.list_url(self.other_organization.slug, self.issue.id)
        )
        self.assertEqual(res.status_code, 200)
        self.assertEqual(len(res.json()), 0)

    async def test_create(self):
        data = {"data": {"text": "Test"}}
        res = await self.async_client.post(
            self.add_url(self.organization.slug, self.issue.id),
            data,
            content_type="application/json",
        )
        self.assertEqual(res.status_code, 201)
        self.assertEqual(res.json()["data"]["text"], "Test")

    async def test_create_wrong_organization(self):
        """A mismatched slug must not create a comment on another org's issue."""
        data = {"data": {"text": "Test"}}
        res = await self.async_client.post(
            self.add_url(self.other_organization.slug, self.issue.id),
            data,
            content_type="application/json",
        )
        self.assertEqual(res.status_code, 404)
        self.assertEqual(await Comment.objects.acount(), 0)

    async def test_update(self):
        comment = await self.amake_comment()
        data = {"data": {"text": "Updated"}}
        res = await self.async_client.put(
            self.update_url(self.organization.slug, self.issue.id, comment.id),
            data,
            content_type="application/json",
        )
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["data"]["text"], "Updated")

    async def test_update_wrong_organization(self):
        comment = await self.amake_comment()
        data = {"data": {"text": "Updated"}}
        res = await self.async_client.put(
            self.update_url(self.other_organization.slug, self.issue.id, comment.id),
            data,
            content_type="application/json",
        )
        self.assertEqual(res.status_code, 404)
        await comment.arefresh_from_db()
        self.assertNotEqual(comment.text, "Updated")

    async def test_delete(self):
        comment = await self.amake_comment()
        res = await self.async_client.delete(
            self.delete_url(self.organization.slug, self.issue.id, comment.id)
        )
        self.assertEqual(res.status_code, 204)
        self.assertEqual(await Comment.objects.acount(), 0)

    async def test_delete_wrong_organization(self):
        comment = await self.amake_comment()
        res = await self.async_client.delete(
            self.delete_url(self.other_organization.slug, self.issue.id, comment.id)
        )
        self.assertEqual(res.status_code, 404)
        self.assertEqual(await Comment.objects.acount(), 1)
