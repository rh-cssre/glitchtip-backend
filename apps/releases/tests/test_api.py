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
        self.async_client.force_login(self.user)

    async def test_create(self):
        url = reverse("api:create_release", args=[self.organization.slug])
        data = {"version": "1.0", "projects": [self.project.slug]}
        res = await self.async_client.post(url, data, content_type="application/json")
        self.assertContains(res, data["version"], status_code=201)
        self.assertTrue(await Release.objects.filter(version=data["version"]).aexists())

    async def test_create_duplicate(self):
        """Creating a release with the same version should be idempotent."""
        url = reverse("api:create_release", args=[self.organization.slug])
        data = {"version": "1.0", "projects": [self.project.slug]}
        res1 = await self.async_client.post(url, data, content_type="application/json")
        self.assertEqual(res1.status_code, 201)
        res2 = await self.async_client.post(url, data, content_type="application/json")
        self.assertEqual(res2.status_code, 201)
        self.assertEqual(await Release.objects.filter(version="1.0").acount(), 1)

    async def test_list(self):
        url = reverse(
            "api:list_releases",
            kwargs={"organization_slug": self.organization.slug},
        )
        release1 = await baker.amake("releases.Release", organization=self.organization)
        release2 = await baker.amake("releases.Release")
        organization2 = await baker.amake("organizations_ext.Organization")
        await organization2.aadd_user(self.user, OrganizationUserRole.ADMIN)
        release3 = await baker.amake("releases.Release", organization=organization2)
        res = await self.async_client.get(url)
        self.assertContains(res, release1.version)
        self.assertNotContains(res, release2.version)  # User not in org
        self.assertNotContains(res, release3.version)  # Filtered our by url

    async def test_retrieve(self):
        release = await baker.amake(
            "releases.Release", organization=self.organization, version="@1.1.1"
        )
        url = reverse(
            "api:get_release",
            kwargs={
                "organization_slug": self.organization.slug,
                "version": release.version,
            },
        )
        res = await self.async_client.get(url)
        self.assertContains(res, release.version)

    async def test_finalize(self):
        release = await baker.amake("releases.Release", organization=self.organization)
        url = reverse(
            "api:update_release",
            kwargs={
                "organization_slug": release.organization.slug,
                "version": release.version,
            },
        )
        data = {"dateReleased": "2021-09-04T14:08:57.388525996Z"}
        res = await self.async_client.put(url, data, content_type="application/json")
        self.assertContains(res, data["dateReleased"][:14])

    async def test_destroy_org_release(self):
        release1 = await baker.amake(
            "releases.Release", organization=self.organization, version="@1.1.1"
        )
        url = reverse(
            "api:delete_organization_release",
            kwargs={
                "organization_slug": release1.organization.slug,
                "version": release1.version,
            },
        )
        res = await self.async_client.delete(url)
        self.assertEqual(res.status_code, 204)
        self.assertEqual(await Release.objects.acount(), 0)

        release2 = await baker.amake("releases.Release")
        url = reverse(
            "api:delete_organization_release",
            kwargs={
                "organization_slug": release2.organization.slug,
                "version": release2.version,
            },
        )
        res = await self.async_client.delete(url)
        self.assertEqual(res.status_code, 404)
        self.assertEqual(await Release.objects.acount(), 1)

    async def test_project_list(self):
        url = reverse(
            "api:list_project_releases",
            kwargs={
                "organization_slug": self.organization.slug,
                "project_slug": self.project.slug,
            },
        )
        project2 = await baker.amake("projects.Project", organization=self.organization)
        release1 = await baker.amake(
            "releases.Release",
            organization=self.organization,
            projects=[self.project, project2],
        )
        release2 = await baker.amake("releases.Release", organization=self.organization)
        res = await self.async_client.get(url)
        self.assertContains(res, release1.version)
        self.assertNotContains(res, release2.version)  # User not in project
        self.assertEqual(len(res.json()), 1)

    async def test_finalize_project_release(self):
        release = await baker.amake(
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
        res = await self.async_client.put(url, data, content_type="application/json")
        self.assertContains(res, data["dateReleased"][:14])

    async def test_destroy_project_release(self):
        release = await baker.amake(
            "releases.Release",
            organization=self.organization,
            projects=[self.project],
            version="@1.1.1",
        )
        other_project = await baker.amake(
            "projects.Project", organization=self.organization
        )
        url = reverse(
            "api:delete_project_release",
            kwargs={
                "organization_slug": release.organization.slug,
                "project_slug": other_project.slug,
                "version": release.version,
            },
        )
        res = await self.async_client.delete(url)
        self.assertEqual(res.status_code, 404)
        self.assertEqual(await Release.objects.acount(), 1)

        url = reverse(
            "api:delete_project_release",
            kwargs={
                "organization_slug": release.organization.slug,
                "project_slug": self.project.slug,
                "version": release.version,
            },
        )
        res = await self.async_client.delete(url)
        self.assertEqual(res.status_code, 204)
        self.assertEqual(await Release.objects.acount(), 0)

    async def test_assemble(self):
        version = "app@v1"
        await baker.amake(
            "releases.Release", version=version, organization=self.organization
        )
        url = reverse("api:assemble_release", args=[self.organization.slug, version])
        data = {
            "checksum": "94bc085fe32db9b4b1b82236214d65eeeeeeeeee",
            "chunks": ["94bc085fe32db9b4b1b82236214d65eeeeeeeeee"],
        }
        res = await self.async_client.post(url, data, content_type="application/json")
        self.assertEqual(res.status_code, 200)

    async def test_create_deploy(self):
        release = await baker.amake("releases.Release", organization=self.organization)
        url = reverse(
            "api:create_deploy",
            kwargs={
                "organization_slug": self.organization.slug,
                "version": release.version,
            },
        )
        data = {"environment": "production", "url": "https://example.com"}
        res = await self.async_client.post(url, data, content_type="application/json")
        self.assertEqual(res.status_code, 201)
        self.assertEqual(res.json()["environment"], "production")
        await release.arefresh_from_db()
        self.assertEqual(release.deploy_count, 1)
        self.assertEqual(await Deploy.objects.filter(release=release).acount(), 1)

    async def test_create_deploy_without_trailing_slash(self):
        release = await baker.amake("releases.Release", organization=self.organization)
        url = reverse(
            "api:create_deploy",
            kwargs={
                "organization_slug": self.organization.slug,
                "version": release.version,
            },
        ).rstrip("/")
        data = {"environment": "staging"}
        res = await self.async_client.post(url, data, content_type="application/json")
        self.assertEqual(res.status_code, 201)

    async def test_list_deploys(self):
        release = await baker.amake("releases.Release", organization=self.organization)
        deploy = await baker.amake(
            "releases.Deploy", release=release, environment="production"
        )
        await baker.amake("releases.Deploy")  # unrelated deploy
        url = reverse(
            "api:list_deploys",
            kwargs={
                "organization_slug": self.organization.slug,
                "version": release.version,
            },
        )
        res = await self.async_client.get(url)
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["environment"], deploy.environment)
        self.assertIn("dateCreated", data[0])

    async def test_create_commits(self):
        release = await baker.amake("releases.Release", organization=self.organization)
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
        res = await self.async_client.post(
            url, commits, content_type="application/json"
        )
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["commitCount"], 2)
        await release.arefresh_from_db()
        self.assertEqual(release.commit_count, 2)
        self.assertEqual(len(release.data["commits"]), 2)

    async def test_create_commits_truncates_at_1000(self):
        release = await baker.amake("releases.Release", organization=self.organization)
        url = reverse(
            "api:create_commits",
            kwargs={
                "organization_slug": self.organization.slug,
                "version": release.version,
            },
        )
        commits = [{"id": f"commit-{i}"} for i in range(1100)]
        res = await self.async_client.post(
            url, commits, content_type="application/json"
        )
        self.assertEqual(res.status_code, 200)
        await release.arefresh_from_db()
        self.assertEqual(release.commit_count, 1100)
        self.assertEqual(len(release.data["commits"]), 1000)

    async def test_list_commits(self):
        release = await baker.amake(
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
        res = await self.async_client.get(url)
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["id"], "abc123")

    async def test_list_commits_empty(self):
        release = await baker.amake("releases.Release", organization=self.organization)
        url = reverse(
            "api:list_commits",
            kwargs={
                "organization_slug": self.organization.slug,
                "version": release.version,
            },
        )
        res = await self.async_client.get(url)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json(), [])

    async def test_release_files_include_size(self):
        release = await baker.amake(
            "releases.Release",
            organization=self.organization,
            projects=[self.project],
        )
        file = await baker.amake("files.File", size=12345)
        await baker.amake(
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
        res = await self.async_client.get(url)
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["size"], 12345)

    async def test_release_includes_commit_count(self):
        release = await baker.amake(
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
        res = await self.async_client.get(url)
        self.assertContains(res, release.version)
        self.assertEqual(res.json()["commitCount"], 5)
