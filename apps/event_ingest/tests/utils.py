import json
from typing import Union

from django.test import TestCase
from model_bakery import baker

from apps.organizations_ext.models import OrganizationUserRole
from glitchtip.test_utils.test_case import GlitchTipTestCaseMixin

from ..process_event import process_issue_events
from ..schema import (
    InterchangeIssueEvent,
    IssueEventSchema,
)


class EventIngestTestCase(GlitchTipTestCaseMixin, TestCase):
    """
    Base class for event ingest tests with helper functions
    """

    def setUp(self):
        self.create_project()
        self.params = f"?sentry_key={self.projectkey.public_key}"

    def get_json_data(self, filename: str):
        with open(filename) as json_file:
            return json.load(json_file)

    def create_logged_in_user(self):
        self.user = baker.make("users.user")
        self.client.force_login(self.user)
        self.org_user = self.organization.add_user(
            self.user, OrganizationUserRole.ADMIN
        )
        self.team = baker.make("teams.Team", organization=self.organization)
        self.team.members.add(self.org_user)
        self.project = baker.make("projects.Project", organization=self.organization)
        self.project.teams.add(self.team)

    def process_events(self, data: Union[dict, list[dict]]) -> list:
        if isinstance(data, dict):
            data = [data]

        events = [
            InterchangeIssueEvent(
                project_id=self.project.id,
                organization_id=self.organization.id if self.organization else None,
                payload=IssueEventSchema(**dat),
            )
            for dat in data
        ]
        process_issue_events(events)
        return events
