from django.shortcuts import reverse
from rest_framework.test import APITestCase
from model_bakery import baker
from glitchtip import test_utils  # pylint: disable=unused-import


class OrganizationsAPITestCase(APITestCase):
    def setUp(self):
        self.user = baker.make("users.user")
        self.organization = baker.make("organizations.Organization")
        self.client.force_login(self.user)

    def test_organizations_retrieve(self):
        project = baker.make("projects.Project", organization=self.organization)
        url = reverse("organization-detail", args=[self.organization.slug])
        res = self.client.get(url)
        self.assertContains(res, self.organization.name)
        self.assertContains(res, project.name)
