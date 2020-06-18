from django.shortcuts import reverse
from rest_framework.test import APITestCase
from model_bakery import baker
from glitchtip import test_utils  # pylint: disable=unused-import


class OrgTeamTestCase(APITestCase):
    """ Tests nested under /organizations/ """

    def setUp(self):
        self.user = baker.make("users.user")
        self.organization = baker.make("organizations_ext.Organization")
        self.organization.add_user(self.user)
        self.client.force_login(self.user)
        self.url = reverse("organization-teams-list", args=[self.organization.slug])

    def test_list(self):
        team = baker.make("teams.Team", organization=self.organization)
        other_organization = baker.make("organizations_ext.Organization")
        other_organization.add_user(self.user)
        other_team = baker.make("teams.Team", organization=other_organization)
        res = self.client.get(self.url)
        self.assertContains(res, team.slug)
        self.assertNotContains(res, other_team.slug)

    def test_create(self):
        data = {"slug": "team"}
        res = self.client.post(self.url, data)
        self.assertContains(res, data["slug"], status_code=201)

    def test_unauthorized_create(self):
        """ Only admins can create teams for that org """
        data = {"slug": "team"}
        organization = baker.make("organizations_ext.Organization")
        url = reverse("organization-teams-list", args=[organization.slug])
        res = self.client.post(url, data)
        # Not even in this org
        self.assertEqual(res.status_code, 400)

        admin_user = baker.make("users.user")
        organization.add_user(admin_user)  # First user is always admin
        organization.add_user(self.user)
        res = self.client.post(url, data)
        # Not an admin
        self.assertEqual(res.status_code, 400)

    def test_invalid_create(self):
        url = reverse("organization-teams-list", args=["haha"])
        data = {"slug": "team"}
        res = self.client.post(url, data)
        self.assertEqual(res.status_code, 400)


class TeamTestCase(APITestCase):
    def setUp(self):
        self.user = baker.make("users.user")
        self.organization = baker.make("organizations_ext.Organization")
        self.org_user = self.organization.add_user(self.user)
        self.client.force_login(self.user)
        self.url = reverse("team-list")

    def test_list(self):
        team = baker.make("teams.Team", organization=self.organization)
        other_team = baker.make("teams.Team")
        res = self.client.get(self.url)
        self.assertContains(res, team.slug)
        self.assertNotContains(res, other_team.slug)

    def test_retrieve(self):
        team = baker.make("teams.Team", organization=self.organization)
        team.members.add(self.org_user)
        url = reverse(
            "team-detail", kwargs={"pk": f"{self.organization.slug}/{team.slug}",},
        )
        res = self.client.get(url)
        self.assertContains(res, team.slug)
        self.assertTrue(res.data["isMember"])

    def test_invalid_retrieve(self):
        team = baker.make("teams.Team")
        url = reverse(
            "team-detail", kwargs={"pk": f"{self.organization.slug}/{team.slug}",},
        )
        res = self.client.get(url)
        self.assertEqual(res.status_code, 404)
