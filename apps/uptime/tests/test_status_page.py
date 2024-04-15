from django.urls import reverse
from model_bakery import baker

from glitchtip.test_utils.test_case import GlitchTipTestCase


class StatusPageTestCase(GlitchTipTestCase):
    def setUp(self):
        self.create_user_and_project()

    def test_status_page(self):
        status_page = baker.make(
            "uptime.StatusPage", organization=self.organization, is_public=False
        )
        url = status_page.get_absolute_url()
        res = self.client.get(url)
        self.assertContains(res, status_page.name)

        self.client.logout()
        res = self.client.get(url)
        self.assertEqual(res.status_code, 404)

        status_page.is_public = True
        status_page.save()
        res = self.client.get(url)
        self.assertContains(res, status_page.name)

    def test_status_page_api(self):
        status_page = baker.make("uptime.StatusPage", organization=self.organization)
        other_status_page = baker.make("uptime.StatusPage")
        url = reverse("organization-status-pages-list", args=(self.organization.slug,))
        res = self.client.get(url)
        self.assertContains(res, status_page.name)
        self.assertNotContains(res, other_status_page.name)

    def test_status_page_api_create(self):
        url = reverse("organization-status-pages-list", args=(self.organization.slug,))
        data = {"name": "foo"}
        res = self.client.post(url, data)
        self.assertContains(res, data["name"], status_code=201)
