from asgiref.sync import sync_to_async
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from model_bakery import baker

from apps.organizations_ext.constants import OrganizationUserRole

from ..models import Project, ProjectKey


class ProjectsAPITestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = baker.make("users.user")
        cls.organization = baker.make("organizations_ext.Organization")
        cls.org_user = cls.organization.add_user(
            cls.user, role=OrganizationUserRole.OWNER
        )
        cls.project = baker.make(
            "projects.Project",
            organization=cls.organization,
            name="Alpha",
            first_event=timezone.now(),
        )
        cls.team = baker.make(
            "teams.Team",
            organization=cls.organization,
            members=[cls.org_user],
            projects=[cls.project],
        )

        cls.url = reverse("api:list_projects")
        cls.detail_url = reverse(
            "api:get_project", args=[cls.organization.slug, cls.project.slug]
        )
        cls.update_url = reverse(
            "api:update_project", args=[cls.organization.slug, cls.project.slug]
        )

    def setUp(self):
        self.client.force_login(self.user)
        self.async_client.force_login(self.user)

    async def test_projects_api_list(self):
        # Ensure project annotate_is_member works with two teams on one project
        await baker.amake(
            "teams.Team",
            organization=self.organization,
            members=[self.org_user],
            projects=[self.project],
        )

        res = await self.async_client.get(self.url)
        self.assertContains(res, self.organization.name)
        data = res.json()[0]
        self.assertIsInstance(data["id"], str)
        self.assertEqual(data["name"], self.project.name)
        self.assertTrue(data["isMember"])
        data_keys = res.json()[0].keys()
        self.assertNotIn("keys", data_keys, "Project keys shouldn't be in list")
        self.assertNotIn("teams", data_keys, "Teams shouldn't be in list")

        # When an org is soft deleted, that org's projects should not show up in list,
        # even if they haven't been soft deleted yet
        self.organization.is_deleted = True
        await self.organization.asave()
        res = await self.async_client.get(self.url)
        self.assertEqual(res.json(), [])

    async def test_default_ordering(self):
        projectA = self.project
        projectZ = await baker.amake(
            "projects.Project", organization=self.organization, name="Z Proj"
        )
        await baker.amake(
            "projects.Project", organization=self.organization, name="B Proj"
        )
        res = await self.async_client.get(self.url)
        data = res.json()
        self.assertEqual(data[0]["name"], projectA.name)
        self.assertEqual(data[2]["name"], projectZ.name)

    async def test_projects_api_retrieve(self):
        res = await self.async_client.get(self.detail_url)
        self.assertTrue(res.json()["firstEvent"])

    async def test_projects_api_update(self):
        self.assertEqual(self.project.event_throttle_rate, 0)
        self.assertEqual(self.project.platform, None)
        res = await self.async_client.put(
            self.update_url,
            {
                "name": "New Name",
                "eventThrottleRate": 50,
                "platform": "python",
            },
            content_type="application/json",
        )
        self.assertEqual(res.status_code, 200)
        await self.project.arefresh_from_db()
        self.assertEqual(self.project.name, "New Name")
        self.assertEqual(self.project.event_throttle_rate, 50)
        self.assertEqual(self.project.platform, "python")

    async def test_projects_pagination(self):
        """
        Test link header pagination
        """
        page_size = 50
        firstProject = self.project
        await baker.amake(
            "projects.Project",
            organization=self.organization,
            name="B",
            _quantity=page_size,
        )
        lastProject = await baker.amake(
            "projects.Project",
            organization=self.organization,
            name="Last Alphabetically",
        )
        res = await self.async_client.get(self.url)
        self.assertNotContains(res, lastProject.name)
        self.assertContains(res, firstProject.name)
        link_header = res.get("Link")
        self.assertIn('results="true"', link_header)

    async def test_project_isolation(self):
        """Users should only access projects in their organization"""
        user2 = await baker.amake("users.user")
        org2 = await baker.amake("organizations_ext.Organization")
        await sync_to_async(org2.add_user)(user2)
        project1 = self.project
        project2 = await baker.amake("projects.Project", organization=org2)

        res = await self.async_client.get(self.url)
        self.assertContains(res, project1.name)
        self.assertNotContains(res, project2.name)

    async def test_project_delete(self):
        """Projects should get soft deleted"""
        project = await baker.amake(
            "projects.Project",
            organization=self.organization,
            name="To Delete",
            first_event=timezone.now(),
        )

        url = reverse("api:delete_project", args=[self.organization.slug, project.slug])
        res = await self.async_client.delete(url)
        self.assertEqual(res.status_code, 204)
        with self.assertRaises(Project.DoesNotExist):
            await project.arefresh_from_db()

    async def test_project_invalid_delete(self):
        """Cannot delete projects that are not in the organization the user is an admin of"""
        organization = await baker.amake("organizations_ext.Organization")
        await sync_to_async(organization.add_user)(
            self.user, OrganizationUserRole.ADMIN
        )
        project = await baker.amake("projects.Project")
        url = reverse("api:delete_project", args=[organization.slug, project.slug])
        res = await self.async_client.delete(url)
        self.assertEqual(res.status_code, 404)

    async def test_project_delete_requires_admin_role(self):
        """A non-admin member cannot delete a project, even when the org has an
        admin. Regression: the role check matched any admin in the org rather
        than the requesting user, so a plain member could delete via a session.
        """
        member = await baker.amake("users.user")
        await sync_to_async(self.organization.add_user)(member, role=OrganizationUserRole.MEMBER)
        project = await baker.amake(
            "projects.Project",
            organization=self.organization,
            name="Member Protected",
            first_event=timezone.now(),
        )
        await self.async_client.aforce_login(member)
        url = reverse("api:delete_project", args=[self.organization.slug, project.slug])
        res = await self.async_client.delete(url)
        self.assertEqual(res.status_code, 404)
        await project.arefresh_from_db() 

    async def test_project_key_delete_requires_admin_role(self):
        """A non-admin member cannot delete a project key, even when the org has
        an admin (same role-check regression as project deletion)."""
        member = await baker.amake("users.user")
        await sync_to_async(self.organization.add_user)(member, role=OrganizationUserRole.MEMBER)
        key = await baker.amake("projects.ProjectKey", project=self.project)
        await self.async_client.aforce_login(member)
        url = reverse(
            "api:delete_project_key",
            args=[self.organization.slug, self.project.slug, key.public_key],
        )
        res = await self.async_client.delete(url)
        self.assertEqual(res.status_code, 404)
        self.assertTrue(await ProjectKey.objects.filter(id=key.id).aexists())


class TeamProjectsAPITestCase(TestCase):
    def setUp(self):
        self.user = baker.make("users.user")
        self.organization = baker.make("organizations_ext.Organization")
        self.organization.add_user(self.user, OrganizationUserRole.ADMIN)
        self.team = baker.make("teams.Team", organization=self.organization)
        self.client.force_login(self.user)
        self.async_client.force_login(self.user)
        self.url = reverse(
            "api:list_team_projects", args=[self.organization.slug, self.team.slug]
        )

    async def test_list(self):
        project = await baker.amake(
            "projects.Project", organization=self.organization
        )
        await project.teams.aadd(self.team)
        not_my_project = await baker.amake("projects.Project")
        res = await self.async_client.get(self.url)
        self.assertContains(res, project.name)
        self.assertNotContains(res, not_my_project.name)

        # If a user is in multiple orgs, that user will have multiple org users.
        # Make sure endpoint doesn't show projects from other orgs
        second_org = await baker.amake("organizations_ext.Organization")
        await sync_to_async(second_org.add_user)(self.user, OrganizationUserRole.ADMIN)
        project_in_second_org = await baker.amake(
            "projects.Project", organization=second_org
        )
        res = await self.async_client.get(self.url)
        self.assertNotContains(res, project_in_second_org.name)

        # Only show projects that are associated with the team in the URL.
        # If a project is on another team in the same org, it should not show
        project_teamless = await baker.amake(
            "projects.Project", organization=self.organization
        )
        res = await self.async_client.get(self.url)
        self.assertNotContains(res, project_teamless)

    async def test_create(self):
        data = {"name": "test-team"}
        res = await self.async_client.post(self.url, data, content_type="application/json")
        res = self.assertContains(res, data["name"], status_code=201)

        res = await self.async_client.get(self.url)
        self.assertContains(res, data["name"])
        self.assertEqual(await ProjectKey.objects.acount(), 1)

    async def test_projects_api_create_unique_slug(self):
        name = "test project"
        data = {"name": name}
        res = await self.async_client.post(self.url, data, content_type="application/json")
        first_project = await Project.objects.aget()
        res = await self.async_client.post(self.url, data, content_type="application/json")
        self.assertContains(res, name, status_code=201)
        projects = [p async for p in Project.objects.all()]
        self.assertNotEqual(projects[0].slug, projects[1].slug)
        self.assertEqual(await ProjectKey.objects.acount(), 2)

        org2 = await baker.amake("organizations_ext.Organization")
        org2_project = await Project.objects.acreate(name=name, organization=org2)
        # The same slug can exist between multiple organizations
        self.assertEqual(first_project.slug, org2_project.slug)

    async def test_projects_api_project_has_team(self):
        """
        The frontend UI requires you to assign a new project to a team, so make sure
        that the new project has a team associated with it
        """
        name = "test project"
        data = {"name": name}
        await self.async_client.post(self.url, data, content_type="application/json")
        project = await Project.objects.afirst()
        self.assertEqual(await project.teams.acount(), 1)

    async def test_project_reserved_words(self):
        data = {"name": "new"}
        res = await self.async_client.post(self.url, data, content_type="application/json")
        self.assertContains(res, "new-1", status_code=201)
        await self.async_client.post(self.url, data)
        self.assertFalse(await Project.objects.filter(slug="new").aexists())
