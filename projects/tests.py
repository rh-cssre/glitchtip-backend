from django.conf import settings
from rest_framework.test import APITestCase
from model_bakery import baker
from glitchtip import test_utils  # pylint: disable=unused-import
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
        project = baker.make("projects.Project")
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

        org2 = baker.make("organizations.Organization")
        org2_project = Project.objects.create(name=name, organization=org2)
        # The same slug can exist between multiple organizations
        self.assertEqual(projects[0].slug, org2_project.slug)

    def test_projects_pagination(self):
        """
        Test link header pagination
        """
        page_size = settings.REST_FRAMEWORK.get("PAGE_SIZE")
        projects = baker.make("projects.Project", _quantity=page_size + 1)
        res = self.client.get(self.url)
        self.assertNotContains(res, projects[0].name)
        self.assertContains(res, projects[-1].name)
        link_header = res.get("Link")
        self.assertIn('results="true"', link_header)

