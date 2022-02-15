from django.shortcuts import reverse
from model_bakery import baker
from glitchtip import test_utils  # pylint: disable=unused-import
from glitchtip.test_utils.test_case import GlitchTipTestCase
from organizations_ext.models import Organization
from ..models import ProjectAlert


class AlertAPITestCase(GlitchTipTestCase):
    def setUp(self):
        self.create_user_and_project()

    def test_retrieve_with_second_team(self):
        alert = baker.make(
            "alerts.ProjectAlert", project=self.project, timespan_minutes=60
        )
        url = reverse(
            "project-alerts-list",
            kwargs={"project_pk": f"{self.organization.slug}/{self.project.slug}",},
        )

        team2 = baker.make("teams.Team", organization=self.organization)
        team2.members.add(self.org_user)
        self.project.team_set.add(team2)
        res = self.client.get(url)
        self.assertEqual(len(res.json()), 1)

    def test_delete_with_second_team(self):
        alert = baker.make(
            "alerts.ProjectAlert", project=self.project, timespan_minutes=60
        )
        url = reverse(
            "project-alerts-detail",
            kwargs={
                "project_pk": f"{self.organization.slug}/{self.project.slug}",
                "pk": alert.pk,
            },
        )
        team2 = baker.make("teams.Team", organization=self.organization)
        team2.members.add(self.org_user)
        self.project.team_set.add(team2)
        res = self.client.delete(url)
        self.assertEqual(res.status_code, 204)
        self.assertEqual(ProjectAlert.objects.count(), 0)

    def test_create_with_second_team(self):
        team2 = baker.make("teams.Team", organization=self.organization)
        team2.members.add(self.org_user)
        self.project.team_set.add(team2)

        url = reverse(
            "project-alerts-list",
            kwargs={"project_pk": f"{self.organization.slug}/{self.project.slug}",},
        )
        data = {
            "name": "foo",
            "timespan_minutes": 60,
            "quantity": 2,
            "uptime": True,
            "alertRecipients": [{"recipientType": "email", "url": "example.com"}],
        }
        res = self.client.post(url, data)
        self.assertEqual(res.status_code, 201)
        project_alert = ProjectAlert.objects.filter(name="foo", uptime=True).first()
        self.assertEqual(project_alert.timespan_minutes, data["timespan_minutes"])
        self.assertEqual(project_alert.project, self.project)

