from django.shortcuts import reverse
from rest_framework.test import APITestCase
from organizations_ext.models import OrganizationUser
from model_bakery import baker
from glitchtip import test_utils  # pylint: disable=unused-import


class OrganizationsAPITestCase(APITestCase):
    def setUp(self):
        self.user = baker.make("users.user")
        self.organization = baker.make("organizations_ext.Organization")
        self.organization.add_user(self.user)
        self.client.force_login(self.user)
        self.url = reverse("organization-list")

    def test_organizations_list(self):
        not_my_organization = baker.make("organizations_ext.Organization")
        res = self.client.get(self.url)
        self.assertContains(res, self.organization.slug)
        self.assertNotContains(res, not_my_organization.slug)

    def test_organizations_retrieve(self):
        project = baker.make("projects.Project", organization=self.organization)
        url = reverse("organization-detail", args=[self.organization.slug])
        res = self.client.get(url)
        self.assertContains(res, self.organization.name)
        self.assertContains(res, project.name)

    def test_organizations_create(self):
        data = {"name": "test"}
        res = self.client.post(self.url, data)
        self.assertContains(res, data["name"], status_code=201)
        self.assertEqual(
            OrganizationUser.objects.filter(organization__name=data["name"]).count(), 1
        )
