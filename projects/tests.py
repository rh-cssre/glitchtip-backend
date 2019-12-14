from django.conf import settings
from rest_framework.test import APITestCase
from model_bakery import baker
from glitchtip import test_utils  # pylint: disable=unused-import
from .models import ProjectKey


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
        # TODO test with different orgs
        name = "test project"
        data = {"name": name}
        res = self.client.post(self.url, data)
        res = self.client.post(self.url, data)
        self.assertContains(res, name, status_code=201)
        self.assertEqual(ProjectKey.objects.all().count(), 2)

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

