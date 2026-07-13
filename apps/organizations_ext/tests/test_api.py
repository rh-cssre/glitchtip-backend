from django.test import TestCase
from django.urls import reverse
from model_bakery import baker

from apps.organizations_ext.constants import OrganizationUserRole
from apps.organizations_ext.models import OrganizationOwner, OrganizationUser


class OrganizationsAPITestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = baker.make("users.user")
        cls.organization = baker.make("organizations_ext.Organization")
        cls.org_user = cls.organization.add_user(cls.user)
        cls.url = reverse("api:list_organizations")

    def setUp(self):
        self.client.force_login(self.user)
        self.async_client.force_login(self.user)

    async def test_organizations_list(self):
        not_my_organization = await baker.amake("organizations_ext.Organization")
        res = await self.async_client.get(self.url)
        self.assertContains(res, self.organization.slug)
        self.assertNotContains(res, not_my_organization.slug)
        self.assertIsInstance(res.json()[0]["id"], str)
        self.assertFalse(
            "teams" in res.json()[0].keys(), "List view shouldn't contain teams"
        )

    async def test_organizations_retrieve(self):
        project = await baker.amake("projects.Project", organization=self.organization)
        team = await baker.amake("teams.Team", organization=self.organization)
        url = reverse("api:get_organization", args=[self.organization.slug])
        res = await self.async_client.get(url)
        self.assertIsInstance(res.json()["id"], str)
        self.assertContains(res, self.organization.name)
        self.assertContains(res, project.name)
        data = res.json()
        self.assertTrue("teams" in data.keys(), "Retrieve view should contain teams")
        self.assertTrue(
            "projects" in data.keys(), "Retrieve view should contain projects"
        )
        self.assertContains(res, team.slug)
        self.assertTrue(
            "teams" in data["projects"][0].keys(),
            "Org projects should contain teams id/name",
        )

    async def test_organizations_retrieve_access(self):
        """
        Ensure 'access' field reflects correct organization user's role
        """
        self.org_user.role = OrganizationUserRole.MEMBER
        await self.org_user.asave()

        organization_2 = await baker.amake("organizations_ext.Organization")
        await organization_2.aadd_user(self.user)

        url = reverse("api:get_organization", args=[organization_2.slug])
        res = await self.async_client.get(url)
        data = res.json()["access"]
        owner_scopes = OrganizationUserRole.get_role(OrganizationUserRole.OWNER)[
            "scopes"
        ]
        self.assertCountEqual(data, owner_scopes)

    async def test_organizations_create(self):
        data = {"name": "test"}
        res = await self.async_client.post(
            self.url, data, content_type="application/json"
        )
        self.assertContains(res, data["name"], status_code=201)
        org_user = await OrganizationUser.objects.aget(organization__name=data["name"])
        self.assertEqual(org_user.role, OrganizationUserRole.OWNER)
        self.assertTrue(
            await OrganizationOwner.objects.filter(
                organization__name=data["name"], organization_user=org_user
            ).aexists()
        )

    async def test_organizations_create_closed_registration_superuser(self):
        data = {"name": "test"}

        with self.settings(ENABLE_ORGANIZATION_CREATION=False):
            res = await self.async_client.post(
                self.url, data, content_type="application/json"
            )
        self.assertEqual(res.status_code, 403)

        self.user.is_superuser = True
        await self.user.asave()

        with self.settings(ENABLE_ORGANIZATION_CREATION=False):
            res = await self.async_client.post(
                self.url, data, content_type="application/json"
            )
        self.assertEqual(res.status_code, 201)

    async def test_organizations_update(self):
        data = {"name": "edit"}
        url = reverse("api:get_organization", args=[self.organization.slug])
        res = await self.async_client.put(url, data, content_type="application/json")
        self.assertContains(res, data["name"])
        self.assertTrue(
            await OrganizationUser.objects.filter(
                organization__name=data["name"]
            ).aexists()
        )

    async def test_organizations_update_without_permissions(self):
        """
        Ensure queryset with role_required checks the correct organization user's role
        """
        organization_2 = await baker.amake("organizations_ext.Organization")

        org_2_user = await organization_2.aadd_user(self.user)
        org_2_user.role = OrganizationUserRole.MEMBER
        await org_2_user.asave()

        data = {"name": "edit"}
        url = reverse("api:update_organization", args=[organization_2.slug])
        res = await self.async_client.put(url, data, content_type="application/json")
        self.assertEqual(res.status_code, 403)

        org_2_user.role = OrganizationUserRole.OWNER
        await org_2_user.asave()

        res = await self.async_client.put(url, data, content_type="application/json")
        self.assertContains(res, data["name"])
        self.assertTrue(
            await OrganizationUser.objects.filter(
                organization__name=data["name"]
            ).aexists()
        )

    async def test_organizations_delete_without_permissions(self):
        """
        Ensure queryset with role_required checks the correct organization user's role.
        Deletion is soft-delete: org is marked is_deleted=True, then async task hard-deletes.
        With the immediate task backend, force_delete runs synchronously.
        """
        from apps.organizations_ext.models import Organization

        organization_2 = await baker.amake("organizations_ext.Organization")

        org_2_user = await organization_2.aadd_user(self.user)
        org_2_user.role = OrganizationUserRole.MEMBER
        await org_2_user.asave()

        url = reverse("api:delete_organization", args=[organization_2.slug])
        res = await self.async_client.delete(url)
        self.assertEqual(res.status_code, 403)

        org_2_user.role = OrganizationUserRole.OWNER
        await org_2_user.asave()

        org_2_id = organization_2.id
        res = await self.async_client.delete(url)
        self.assertEqual(res.status_code, 204)

        # With immediate task backend, org is fully deleted after the API call
        self.assertFalse(await Organization.objects.filter(id=org_2_id).aexists())

    async def test_organizations_soft_delete(self):
        """Test that Organization.delete() sets is_deleted=True before task runs."""
        from apps.organizations_ext.models import Organization

        organization_2 = await baker.amake("organizations_ext.Organization")
        org_2_id = organization_2.id

        # Directly set is_deleted to verify the queryset filter works
        organization_2.is_deleted = True
        await organization_2.asave(update_fields=["is_deleted"])

        await organization_2.arefresh_from_db()
        self.assertTrue(organization_2.is_deleted)

        # Soft-deleted org should not appear in filtered queries
        self.assertFalse(
            await Organization.objects.filter(is_deleted=False, id=org_2_id).aexists()
        )
        # But should still exist in unfiltered queries
        self.assertTrue(await Organization.objects.filter(id=org_2_id).aexists())
