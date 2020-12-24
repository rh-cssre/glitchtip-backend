from django.shortcuts import reverse
from model_bakery import baker
from organizations_ext.models import OrganizationUserRole
from glitchtip.test_utils.test_case import GlitchTipTestCase
from ..models import Release


class ReleaseAPITestCase(GlitchTipTestCase):
    def setUp(self):
        self.create_user_and_project()

    def test_list(self):
        url = reverse(
            "organization-releases-list",
            kwargs={"organization_slug": self.organization.slug},
        )
        release1 = baker.make("releases.Release", organization=self.organization)
        release2 = baker.make("releases.Release")
        organization2 = baker.make("organizations_ext.Organization")
        organization2.add_user(self.user, OrganizationUserRole.ADMIN)
        release3 = baker.make("releases.Release", organization=organization2)
        res = self.client.get(url)
        self.assertContains(res, release1.version)
        self.assertNotContains(res, release2.version)  # User not in org
        self.assertNotContains(res, release3.version)  # Filtered our by url

    def test_retrieve(self):
        release = baker.make("releases.Release", organization=self.organization)
        url = reverse(
            "organization-releases-detail",
            kwargs={
                "organization_slug": self.organization.slug,
                "version": release.version,
            },
        )
        res = self.client.get(url)
        self.assertContains(res, release.version)

    def test_destroy(self):
        release1 = baker.make("releases.Release", organization=self.organization)
        url = reverse(
            "organization-releases-detail",
            kwargs={
                "organization_slug": release1.organization.slug,
                "version": release1.version,
            },
        )
        res = self.client.delete(url)
        self.assertEqual(res.status_code, 204)
        self.assertEqual(Release.objects.all().count(), 0)

        release2 = baker.make("releases.Release")
        url = reverse(
            "organization-releases-detail",
            kwargs={
                "organization_slug": release2.organization.slug,
                "version": release2.version,
            },
        )
        res = self.client.delete(url)
        self.assertEqual(res.status_code, 404)
        self.assertEqual(Release.objects.all().count(), 1)
