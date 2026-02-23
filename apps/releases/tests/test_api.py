from django.urls import reverse
from model_bakery import baker

from apps.organizations_ext.constants import OrganizationUserRole
from glitchtip.test_utils.test_case import GlitchTestCase

from ..models import Deploy, Release


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

    def test_create_deploy(self):
        release = baker.make("releases.Release", organization=self.organization)
        url = reverse(
            "api:create_deploy",
            kwargs={
                "organization_slug": self.organization.slug,
                "version": release.version,
            },
        )
        data = {"environment": "production", "url": "https://example.com"}
        res = self.client.post(url, data, content_type="application/json")
        self.assertEqual(res.status_code, 201)
        self.assertEqual(res.json()["environment"], "production")
        release.refresh_from_db()
        self.assertEqual(release.deploy_count, 1)
        self.assertEqual(Deploy.objects.filter(release=release).count(), 1)

    def test_create_deploy_without_trailing_slash(self):
        release = baker.make("releases.Release", organization=self.organization)
        url = reverse(
            "api:create_deploy",
            kwargs={
                "organization_slug": self.organization.slug,
                "version": release.version,
            },
        ).rstrip("/")
        data = {"environment": "staging"}
        res = self.client.post(url, data, content_type="application/json")
        self.assertEqual(res.status_code, 201)

    def test_list_deploys(self):
        release = baker.make("releases.Release", organization=self.organization)
        deploy = baker.make(
            "releases.Deploy", release=release, environment="production"
        )
        baker.make("releases.Deploy")  # unrelated deploy
        url = reverse(
            "api:list_deploys",
            kwargs={
                "organization_slug": self.organization.slug,
                "version": release.version,
            },
        )
        res = self.client.get(url)
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["environment"], deploy.environment)
        self.assertIn("dateCreated", data[0])

    def test_create_commits(self):
        release = baker.make("releases.Release", organization=self.organization)
        url = reverse(
            "api:create_commits",
            kwargs={
                "organization_slug": self.organization.slug,
                "version": release.version,
            },
        )
        commits = [
            {
                "id": "abc123",
                "message": "fix bug",
                "authorName": "Test",
                "authorEmail": "t@t.com",
            },
            {"id": "def456", "message": "add feature"},
        ]
        res = self.client.post(url, commits, content_type="application/json")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["commitCount"], 2)
        release.refresh_from_db()
        self.assertEqual(release.commit_count, 2)
        self.assertEqual(len(release.data["commits"]), 2)

    def test_create_commits_truncates_at_1000(self):
        release = baker.make("releases.Release", organization=self.organization)
        url = reverse(
            "api:create_commits",
            kwargs={
                "organization_slug": self.organization.slug,
                "version": release.version,
            },
        )
        commits = [{"id": f"commit-{i}"} for i in range(1100)]
        res = self.client.post(url, commits, content_type="application/json")
        self.assertEqual(res.status_code, 200)
        release.refresh_from_db()
        self.assertEqual(release.commit_count, 1100)
        self.assertEqual(len(release.data["commits"]), 1000)

    def test_list_commits(self):
        release = baker.make(
            "releases.Release",
            organization=self.organization,
            data={"commits": [{"id": "abc123", "message": "fix"}]},
        )
        url = reverse(
            "api:list_commits",
            kwargs={
                "organization_slug": self.organization.slug,
                "version": release.version,
            },
        )
        res = self.client.get(url)
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["id"], "abc123")

    def test_list_commits_empty(self):
        release = baker.make("releases.Release", organization=self.organization)
        url = reverse(
            "api:list_commits",
            kwargs={
                "organization_slug": self.organization.slug,
                "version": release.version,
            },
        )
        res = self.client.get(url)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json(), [])

    def test_release_files_include_size(self):
        release = baker.make(
            "releases.Release",
            organization=self.organization,
            projects=[self.project],
        )
        file = baker.make("files.File", size=12345)
        baker.make(
            "sourcecode.DebugSymbolBundle",
            release=release,
            organization=self.organization,
            file=file,
        )
        url = reverse(
            "api:list_release_files",
            kwargs={
                "organization_slug": self.organization.slug,
                "version": release.version,
            },
        )
        res = self.client.get(url)
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["size"], 12345)

    def test_release_includes_commit_count(self):
        release = baker.make(
            "releases.Release",
            organization=self.organization,
            commit_count=5,
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
        self.assertEqual(res.json()["commitCount"], 5)
