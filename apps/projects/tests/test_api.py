from unittest import mock

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from model_bakery import baker

import apps.projects.tasks
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

    def test_projects_api_list(self):
        # Ensure project annotate_is_member works with two teams on one project
        baker.make(
            "teams.Team",
            organization=self.organization,
            members=[self.org_user],
            projects=[self.project],
        )

        res = self.client.get(self.url)
        self.assertContains(res, self.organization.name)
        data = res.json()[0]
        self.assertIsInstance(data["id"], str)
        self.assertEqual(data["name"], self.project.name)
        self.assertTrue(data["isMember"])
        data_keys = res.json()[0].keys()
        self.assertNotIn("keys", data_keys, "Project keys shouldn't be in list")
        self.assertNotIn("teams", data_keys, "Teams shouldn't be in list")

    def test_default_ordering(self):
        projectA = self.project
        projectZ = baker.make(
            "projects.Project", organization=self.organization, name="Z Proj"
        )
        baker.make("projects.Project", organization=self.organization, name="B Proj")
        res = self.client.get(self.url)
        data = res.json()
        self.assertEqual(data[0]["name"], projectA.name)
        self.assertEqual(data[2]["name"], projectZ.name)

    def test_projects_api_retrieve(self):
        res = self.client.get(self.detail_url)
        self.assertTrue(res.json()["firstEvent"])

    def test_projects_api_update(self):
        self.assertEqual(self.project.event_throttle_rate, 0)
        self.assertEqual(self.project.platform, None)
        res = self.client.put(
            self.update_url,
            {
                "name": "New Name",
                "eventThrottleRate": 50,
                "platform": "python",
            },
            content_type="application/json",
        )
        self.assertEqual(res.status_code, 200)
        self.project.refresh_from_db()
        self.assertEqual(self.project.name, "New Name")
        self.assertEqual(self.project.event_throttle_rate, 50)
        self.assertEqual(self.project.platform, "python")

    def test_projects_pagination(self):
        """
        Test link header pagination
        """
        page_size = 50
        firstProject = self.project
        baker.make(
            "projects.Project",
            organization=self.organization,
            name="B",
            _quantity=page_size,
        )
        lastProject = baker.make(
            "projects.Project",
            organization=self.organization,
            name="Last Alphabetically",
        )
        res = self.client.get(self.url)
        self.assertNotContains(res, lastProject.name)
        self.assertContains(res, firstProject.name)
        link_header = res.get("Link")
        self.assertIn('results="true"', link_header)

    def test_project_isolation(self):
        """Users should only access projects in their organization"""
        user2 = baker.make("users.user")
        org2 = baker.make("organizations_ext.Organization")
        org2.add_user(user2)
        project1 = self.project
        project2 = baker.make("projects.Project", organization=org2)

        res = self.client.get(self.url)
        self.assertContains(res, project1.name)
        self.assertNotContains(res, project2.name)

    def test_project_delete(self):
        """Projects should get soft deleted"""
        project = baker.make(
            "projects.Project",
            organization=self.organization,
            name="To Delete",
            first_event=timezone.now(),
        )

        url = reverse("api:delete_project", args=[self.organization.slug, project.slug])
        with mock.patch.object(
            apps.projects.tasks.delete_project, "delay"
        ) as delete_project_mock:
            res = self.client.delete(url)
        self.assertEqual(res.status_code, 204)
        project.refresh_from_db()
        self.assertTrue(project.is_deleted)
        self.assertEqual(delete_project_mock.call_args, mock.call(project.pk))

    def test_project_invalid_delete(self):
        """Cannot delete projects that are not in the organization the user is an admin of"""
        organization = baker.make("organizations_ext.Organization")
        organization.add_user(self.user, OrganizationUserRole.ADMIN)
        project = baker.make("projects.Project")
        url = reverse("api:delete_project", args=[organization.slug, project.slug])
        res = self.client.delete(url)
        self.assertEqual(res.status_code, 404)


class TeamProjectsAPITestCase(TestCase):
    def setUp(self):
        self.user = baker.make("users.user")
        self.organization = baker.make("organizations_ext.Organization")
        self.organization.add_user(self.user, OrganizationUserRole.ADMIN)
        self.team = baker.make("teams.Team", organization=self.organization)
        self.client.force_login(self.user)
        self.url = reverse(
            "api:list_team_projects", args=[self.organization.slug, self.team.slug]
        )

    def test_list(self):
        project = baker.make("projects.Project", organization=self.organization)
        project.teams.add(self.team)
        not_my_project = baker.make("projects.Project")
        res = self.client.get(self.url)
        self.assertContains(res, project.name)
        self.assertNotContains(res, not_my_project.name)

        # If a user is in multiple orgs, that user will have multiple org users.
        # Make sure endpoint doesn't show projects from other orgs
        second_org = baker.make("organizations_ext.Organization")
        second_org.add_user(self.user, OrganizationUserRole.ADMIN)
        project_in_second_org = baker.make("projects.Project", organization=second_org)
        res = self.client.get(self.url)
        self.assertNotContains(res, project_in_second_org.name)

        # Only show projects that are associated with the team in the URL.
        # If a project is on another team in the same org, it should not show
        project_teamless = baker.make(
            "projects.Project", organization=self.organization
        )
        res = self.client.get(self.url)
        self.assertNotContains(res, project_teamless)

    def test_create(self):
        data = {"name": "test-team"}
        res = self.client.post(self.url, data, content_type="application/json")
        res = self.assertContains(res, data["name"], status_code=201)

        res = self.client.get(self.url)
        self.assertContains(res, data["name"])
        self.assertEqual(ProjectKey.objects.all().count(), 1)

    def test_projects_api_create_unique_slug(self):
        name = "test project"
        data = {"name": name}
        res = self.client.post(self.url, data, content_type="application/json")
        first_project = Project.objects.get()
        res = self.client.post(self.url, data, content_type="application/json")
        self.assertContains(res, name, status_code=201)
        projects = Project.objects.all()
        self.assertNotEqual(projects[0].slug, projects[1].slug)
        self.assertEqual(ProjectKey.objects.all().count(), 2)

        org2 = baker.make("organizations_ext.Organization")
        org2_project = Project.objects.create(name=name, organization=org2)
        # The same slug can exist between multiple organizations
        self.assertEqual(first_project.slug, org2_project.slug)

    def test_projects_api_project_has_team(self):
        """
        The frontend UI requires you to assign a new project to a team, so make sure
        that the new project has a team associated with it
        """
        name = "test project"
        data = {"name": name}
        self.client.post(self.url, data, content_type="application/json")
        project = Project.objects.first()
        self.assertEqual(project.teams.all().count(), 1)

    def test_project_reserved_words(self):
        data = {"name": "new"}
        res = self.client.post(self.url, data, content_type="application/json")
        self.assertContains(res, "new-1", status_code=201)
        self.client.post(self.url, data)
        self.assertFalse(Project.objects.filter(slug="new").exists())
