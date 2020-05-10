from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APITestCase
from freezegun import freeze_time
from model_bakery import baker
from organizations_ext.models import OrganizationUserRole


class FilterTestCase(APITestCase):
    def setUp(self):
        self.user = baker.make("users.user")
        self.organization = baker.make("organizations_ext.Organization")
        self.organization.add_user(self.user, OrganizationUserRole.ADMIN)
        self.team = baker.make("teams.Team", organization=self.organization)
        self.team.members.add(self.user)
        self.project = baker.make("projects.Project", organization=self.organization)
        self.project.team_set.add(self.team)
        self.client.force_login(self.user)
        self.url = reverse("issue-list")

    def test_filter_by_date(self):
        """ A user should be able to filter by start and end datetimes. """
        with freeze_time(timezone.datetime(1999, 1, 1)):
            event1 = baker.make("issues.Event", issue__project=self.project)
        with freeze_time(timezone.datetime(2010, 1, 1)):
            event2 = baker.make("issues.Event", issue__project=self.project)
        with freeze_time(timezone.datetime(2020, 1, 1)):
            event3 = baker.make("issues.Event", issue__project=self.project)
        res = self.client.get(
            self.url + "?start=2000-01-01T05:00:00.000Z&end=2019-01-01T05:00:00.000Z"
        )
        self.assertContains(res, event2.issue.title)
        self.assertNotContains(res, event1.issue.title)
        self.assertNotContains(res, event3.issue.title)


class SearchTestCase(APITestCase):
    def setUp(self):
        self.user = baker.make("users.user")
        self.organization = baker.make("organizations_ext.Organization")
        self.organization.add_user(self.user, OrganizationUserRole.ADMIN)
        self.team = baker.make("teams.Team", organization=self.organization)
        self.team.members.add(self.user)
        self.project = baker.make("projects.Project", organization=self.organization)
        self.project.team_set.add(self.team)
        self.client.force_login(self.user)
        self.url = reverse("issue-list")

    def test_search(self):
        event = baker.make(
            "issues.Event", issue__project=self.project, data={"name": "apple sauce"}
        )
        other_event = baker.make("issues.Event", issue__project=self.project)
        res = self.client.get(self.url + "?query=is:unresolved apple sauce")
        self.assertContains(res, event.issue.title)
        self.assertNotContains(res, other_event.issue.title)
