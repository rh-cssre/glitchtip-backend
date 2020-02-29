from django.conf import settings
from rest_framework.test import APITestCase
from model_bakery import baker
from glitchtip import test_utils  # pylint: disable=unused-import
from organizations_ext.models import OrganizationUserRole
from .models import ProjectKey, Project


class ProjectsAPITestCase(APITestCase):
    def setUp(self):
        self.user = baker.make("users.user")
        self.client.force_login(self.user)
        self.url = "/api/0/projects/"

    def test_projects_api_create(self):
        name = "test project"
        data = {"name": name}
        res = self.client.post(self.url, data)
        self.assertContains(res, name, status_code=201)
        self.assertEqual(ProjectKey.objects.all().count(), 1)

    def test_projects_api_list(self):
        organization = baker.make("organizations_ext.Organization")
        organization.add_user(self.user, role=OrganizationUserRole.OWNER)
        project = baker.make("projects.Project", organization=organization)
        res = self.client.get(self.url)
        self.assertContains(res, project.name)

    def test_projects_api_create_unique_slug(self):
        name = "test project"
        data = {"name": name}
        res = self.client.post(self.url, data)
        res = self.client.post(self.url, data)
        self.assertContains(res, name, status_code=201)
        projects = Project.objects.all()
        self.assertNotEqual(projects[0].slug, projects[1].slug)
        self.assertEqual(ProjectKey.objects.all().count(), 2)

        org2 = baker.make("organizations_ext.Organization")
        org2_project = Project.objects.create(name=name, organization=org2)
        # The same slug can exist between multiple organizations
        self.assertEqual(projects[0].slug, org2_project.slug)

    def test_projects_pagination(self):
        """
        Test link header pagination
        """
        page_size = settings.REST_FRAMEWORK.get("PAGE_SIZE")
        organization = baker.make("organizations_ext.Organization")
        organization.add_user(self.user, role=OrganizationUserRole.OWNER)
        projects = baker.make(
            "projects.Project", organization=organization, _quantity=page_size + 1
        )
        res = self.client.get(self.url)
        self.assertNotContains(res, projects[0].name)
        self.assertContains(res, projects[-1].name)
        link_header = res.get("Link")
        self.assertIn('results="true"', link_header)

    def test_project_isolation(self):
        """ Users should only access projects in their organization """
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

        # self.assertEqual(self.client.get(self.url + project1.slug), 404)
