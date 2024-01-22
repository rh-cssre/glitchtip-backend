from django.conf import settings
from django.urls import reverse
from django.utils import timezone
from model_bakery import baker
from rest_framework.test import APITestCase

from glitchtip.test_utils import generators  # pylint: disable=unused-import
from organizations_ext.models import OrganizationUserRole

from ..models import Project, ProjectKey
from ..views import ProjectViewSet


class ProjectsAPITestCase(APITestCase):
    def setUp(self):
        self.user = baker.make("users.user")
        self.client.force_login(self.user)
        self.url = reverse("project-list")

    def test_projects_api_create(self):
        """This endpoint can't be used to create"""
        data = {"name": "test"}
        res = self.client.post(self.url, data)
        # Must specify organization and team
        self.assertEqual(res.status_code, 405)

    def test_projects_api_list(self):
        organization = baker.make("organizations_ext.Organization")
        organization.add_user(self.user, role=OrganizationUserRole.OWNER)
        project = baker.make("projects.Project", organization=organization)
        res = self.client.get(self.url)
        self.assertContains(res, project.name)

    def test_default_ordering(self):
        organization = baker.make("organizations_ext.Organization")
        organization.add_user(self.user, role=OrganizationUserRole.OWNER)
        projectA = baker.make(
            "projects.Project", organization=organization, name="A Project"
        )
        projectZ = baker.make(
            "projects.Project", organization=organization, name="Z Project"
        )
        projectB = baker.make(
            "projects.Project", organization=organization, name="B Project"
        )
        res = self.client.get(self.url)
        self.assertEqual(res.data[0]["name"], projectA.name)
        self.assertEqual(res.data[2]["name"], projectZ.name)

    def test_projects_api_retrieve(self):
        organization = baker.make("organizations_ext.Organization")
        organization.add_user(self.user, role=OrganizationUserRole.OWNER)
        project = baker.make(
            "projects.Project", organization=organization, first_event=timezone.now()
        )
        res = self.client.get(
            reverse(
                "project-detail", kwargs={"pk": organization.slug + "/" + project.slug}
            )
        )
        self.assertTrue(res.data["firstEvent"])

    def test_projects_pagination(self):
        """
        Test link header pagination
        """
        page_size = settings.REST_FRAMEWORK.get("PAGE_SIZE")
        organization = baker.make("organizations_ext.Organization")
        organization.add_user(self.user, role=OrganizationUserRole.OWNER)
        firstProject = projects = baker.make(
            "projects.Project", organization=organization, name="Alphabetically First"
        )
        baker.make(
            "projects.Project", organization=organization, name="B", _quantity=page_size
        )
        lastProject = projects = baker.make(
            "projects.Project", organization=organization, name="Last Alphabetically"
        )
        res = self.client.get(self.url)
        self.assertNotContains(res, lastProject.name)
        self.assertContains(res, firstProject.name)
        link_header = res.get("Link")
        self.assertIn('results="true"', link_header)

    def test_project_isolation(self):
        """Users should only access projects in their organization"""
        user1 = self.user
        user2 = baker.make("users.user")
        org1 = baker.make("organizations_ext.Organization")
        org2 = baker.make("organizations_ext.Organization")
        org1.add_user(user1)
        org2.add_user(user2)
        project1 = baker.make("projects.Project", organization=org1)
        project2 = baker.make("projects.Project", organization=org2)

        res = self.client.get(self.url)
        self.assertContains(res, project1.name)
        self.assertNotContains(res, project2.name)

    def test_project_delete(self):
        organization = baker.make("organizations_ext.Organization")
        organization.add_user(self.user, OrganizationUserRole.ADMIN)
        team = baker.make("teams.Team", organization=organization)
        project = baker.make("projects.Project", organization=organization, team=team)

        url = reverse(
            "project-detail", kwargs={"pk": f"{organization.slug}/{project.slug}"}
        )
        res = self.client.delete(url)
        self.assertEqual(res.status_code, 204)
        self.assertEqual(ProjectViewSet.queryset.count(), 0)

    def test_project_invalid_delete(self):
        """Cannot delete projects that are not in the organization the user is an admin of"""
        organization = baker.make("organizations_ext.Organization")
        organization.add_user(self.user, OrganizationUserRole.ADMIN)
        project = baker.make("projects.Project")
        url = reverse(
            "project-detail", kwargs={"pk": f"{organization.slug}/{project.slug}"}
        )
        res = self.client.delete(url)
        self.assertEqual(res.status_code, 404)


class TeamProjectsAPITestCase(APITestCase):
    def setUp(self):
        self.user = baker.make("users.user")
        self.organization = baker.make("organizations_ext.Organization")
        self.organization.add_user(self.user, OrganizationUserRole.ADMIN)
        self.team = baker.make("teams.Team", organization=self.organization)
        self.client.force_login(self.user)
        self.url = reverse(
            "team-projects-list",
            kwargs={"team_pk": f"{self.organization.slug}/{self.team.slug}"},
        )

    def test_list(self):
        project = baker.make("projects.Project", organization=self.organization)
        project.team_set.add(self.team)
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
        res = self.client.post(self.url, data)
        self.assertContains(res, data["name"], status_code=201)

        res = self.client.get(self.url)
        self.assertContains(res, data["name"])
        self.assertEqual(ProjectKey.objects.all().count(), 1)

    def test_projects_api_create_unique_slug(self):
        name = "test project"
        data = {"name": name}
        res = self.client.post(self.url, data)
        first_project = Project.objects.get()
        res = self.client.post(self.url, data)
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
        self.client.post(self.url, data)
        project = Project.objects.first()
        self.assertEqual(project.team_set.all().count(), 1)

    def test_project_reserved_words(self):
        data = {"name": "new"}
        res = self.client.post(self.url, data)
        self.assertContains(res, "new-1", status_code=201)
        self.client.post(self.url, data)
        self.assertFalse(Project.objects.filter(slug="new").exists())
