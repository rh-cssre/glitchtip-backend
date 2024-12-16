from django.urls import reverse
from model_bakery import baker

from apps.organizations_ext.constants import OrganizationUserRole
from glitchtip.test_utils.test_case import GlitchTestCase

from ..models import Release


class ReleaseAPITestCase(GlitchTestCase):
    @classmethod
    def setUpTestData(cls):
        cls.create_user()

    def setUp(self):
        self.client.force_login(self.user)

    def test_create(self):
        url = reverse("api:create_release", args=[self.organization.slug])
        data = {"version": "1.0", "projects": [self.project.slug]}
        res = self.client.post(url, data, content_type="application/json")
        self.assertContains(res, data["version"], status_code=201)
        self.assertTrue(Release.objects.filter(version=data["version"]).exists())

    def test_list(self):
        url = reverse(
            "api:list_releases",
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
        release = baker.make(
            "releases.Release", organization=self.organization, version="@1.1.1"
        )
        url = reverse(
            "api:get_release",
            kwargs={
                "organization_slug": self.organization.slug,
                "version": release.version,
            },
        )
        res = self.client.get(url)
        self.assertContains(res, release.version)

    def test_finalize(self):
        release = baker.make("releases.Release", organization=self.organization)
        url = reverse(
            "api:update_release",
            kwargs={
                "organization_slug": release.organization.slug,
                "version": release.version,
            },
        )
        data = {"dateReleased": "2021-09-04T14:08:57.388525996Z"}
        res = self.client.put(url, data, content_type="application/json")
        self.assertContains(res, data["dateReleased"][:14])

    def test_destroy_org_release(self):
        release1 = baker.make(
            "releases.Release", organization=self.organization, version="@1.1.1"
        )
        url = reverse(
            "api:delete_organization_release",
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
            "api:delete_organization_release",
            kwargs={
                "organization_slug": release2.organization.slug,
                "version": release2.version,
            },
        )
        res = self.client.delete(url)
        self.assertEqual(res.status_code, 404)
        self.assertEqual(Release.objects.all().count(), 1)

    def test_project_list(self):
        url = reverse(
            "api:list_project_releases",
            kwargs={
                "organization_slug": self.organization.slug,
                "project_slug": self.project.slug,
            },
        )
        project2 = baker.make("projects.Project", organization=self.organization)
        release1 = baker.make(
            "releases.Release",
            organization=self.organization,
            projects=[self.project, project2],
        )
        release2 = baker.make("releases.Release", organization=self.organization)
        res = self.client.get(url)
        self.assertContains(res, release1.version)
        self.assertNotContains(res, release2.version)  # User not in project
        self.assertEqual(len(res.json()), 1)

    def test_finalize_project_release(self):
        release = baker.make(
            "releases.Release", organization=self.organization, projects=[self.project]
        )
        url = reverse(
            "api:update_project_release",
            kwargs={
                "organization_slug": release.organization.slug,
                "project_slug": self.project.slug,
                "version": release.version,
            },
        )
        data = {"dateReleased": "2021-09-04T14:08:57.388525996Z"}
        res = self.client.put(url, data, content_type="application/json")
        self.assertContains(res, data["dateReleased"][:14])

    def test_destroy_project_release(self):
        release = baker.make(
            "releases.Release",
            organization=self.organization,
            projects=[self.project],
            version="@1.1.1",
        )
        other_project = baker.make("projects.Project", organization=self.organization)
        url = reverse(
            "api:delete_project_release",
            kwargs={
                "organization_slug": release.organization.slug,
                "project_slug": other_project.slug,
                "version": release.version,
            },
        )
        res = self.client.delete(url)
        self.assertEqual(res.status_code, 404)
        self.assertEqual(Release.objects.all().count(), 1)

        url = reverse(
            "api:delete_project_release",
            kwargs={
                "organization_slug": release.organization.slug,
                "project_slug": self.project.slug,
                "version": release.version,
            },
        )
        res = self.client.delete(url)
        self.assertEqual(res.status_code, 204)
        self.assertEqual(Release.objects.all().count(), 0)

    def test_assemble(self):
        version = "app@v1"
        baker.make("releases.Release", version=version, organization=self.organization)
        url = reverse("api:assemble_release", args=[self.organization.slug, version])
        data = {
            "checksum": "94bc085fe32db9b4b1b82236214d65eeeeeeeeee",
            "chunks": ["94bc085fe32db9b4b1b82236214d65eeeeeeeeee"],
        }
        res = self.client.post(url, data, content_type="application/json")
        self.assertEqual(res.status_code, 200)
