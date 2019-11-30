from rest_framework.test import APITestCase
from model_bakery import baker
from .models import ProjectKey


class ProjectsAPITestCase(APITestCase):
    def test_projects_api_create(self):
        user = baker.make("users.user")
        self.client.force_login(user)
        url = "/api/projects/"
        name = "test project"
        data = {"name": name}
        res = self.client.post(url, data)
        self.assertContains(res, name, status_code=201)
        self.assertEqual(ProjectKey.objects.all().count(), 1)

    def test_projects_api_list(self):
        user = baker.make("users.user")
        self.client.force_login(user)
        project = baker.make("projects.Project")
        url = "/api/projects/"
        res = self.client.get(url)
        self.assertContains(res, project.name)

