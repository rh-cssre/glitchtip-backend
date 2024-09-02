from django.test import TestCase
from django.urls import reverse
from model_bakery import baker

from apps.organizations_ext.models import OrganizationUser


class OrganizationsAPITestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = baker.make("users.user")
        cls.organization = baker.make("organizations_ext.Organization")
        cls.organization.add_user(cls.user)
        cls.url = reverse("api:list_organizations")

    def setUp(self):
        self.client.force_login(self.user)

    def test_organizations_list(self):
        not_my_organization = baker.make("organizations_ext.Organization")
        res = self.client.get(self.url)
        self.assertContains(res, self.organization.slug)
        self.assertNotContains(res, not_my_organization.slug)
        self.assertFalse(
            "teams" in res.json()[0].keys(), "List view shouldn't contain teams"
        )

    def test_organizations_retrieve(self):
        project = baker.make("projects.Project", organization=self.organization)
        team = baker.make("teams.Team", organization=self.organization)
        url = reverse("api:get_organization", args=[self.organization.slug])
        res = self.client.get(url)
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

    def test_organizations_create(self):
        data = {"name": "test"}
        res = self.client.post(self.url, data, content_type="application/json")
        self.assertContains(res, data["name"], status_code=201)
        self.assertEqual(
            OrganizationUser.objects.filter(organization__name=data["name"]).count(), 1
        )

    def test_organizations_create_closed_registration_superuser(self):
        data = {"name": "test"}

        with self.settings(ENABLE_ORGANIZATION_CREATION=False):
            res = self.client.post(self.url, data, content_type="application/json")
        self.assertEqual(res.status_code, 403)

        self.user.is_superuser = True
        self.user.save()

        with self.settings(ENABLE_ORGANIZATION_CREATION=False):
            with self.assertNumQueries(9):
                res = self.client.post(self.url, data, content_type="application/json")
        self.assertEqual(res.status_code, 201)

    def test_organizations_update(self):
        data = {"name": "edit"}
        url = reverse("api:get_organization", args=[self.organization.slug])
        res = self.client.put(url, data, content_type="application/json")
        self.assertContains(res, data["name"])
        self.assertTrue(
            OrganizationUser.objects.filter(organization__name=data["name"]).exists()
        )
