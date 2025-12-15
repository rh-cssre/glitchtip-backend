import datetime
import json
import time
import uuid
from typing import Union

from django.test import TestCase
from django.utils import timezone
from model_bakery import baker

from apps.organizations_ext.constants import OrganizationUserRole
from glitchtip.test_utils.test_case import GlitchTipTestCaseMixin

from ..process_event import process_issue_events
from ..schema import (
    IssueEventSchema,
    IssueTaskMessage,
)


def list_to_envelope(data: list[dict]) -> str:
    result_lines = []
    for item in data:
        result_lines.append(json.dumps(item))
    return "\n".join(result_lines)


def generate_event(
    event_type="error",
    level="error",
    platform="python",
    release="default-release",
    environment="production",
    num_events=1,
    event=None,
    envelope=False,
):
    """
    Generates sentry compatible events for use in unit tests.

    Args:
      event_type (str): The type of event ('error', 'warning', 'transaction', etc.). Default is 'error'.
      level (str): The event level ('error', 'warning', 'info', etc.). Default is 'error'.
      platform (str): The platform the event originated from ('python', 'javascript', 'java', etc.). Default is 'python'.
      release (str): The release version. Default is 'default-release'.
      environment (str): The environment (e.g., 'production', 'staging'). Default is 'production'.
      num_events (int): The number of events to generate. Default is 1.
      event (dict): A dictionary of additional fields to override in the base event.
      envelope (bool): Whether to wrap the event(s) in an envelope list. Default is False.

    Returns:
      dict or list: A single event dictionary, a list of event dictionaries, or an envelope list containing event data.
    """

    base_event = {
        "event_id": str(uuid.uuid4()),
        "timestamp": time.time(),
        "sdk": {"name": "sentry.python", "version": "1.11.0"},
        "platform": platform,
        "level": level,
        "exception": {
            "values": [
                {
                    "type": "TypeError",
                    "value": "unsupported operand type(s) for +: 'int' and 'str'",
                    "stacktrace": {
                        "frames": [
                            {
                                "filename": "my_module.py",
                                "function": "my_function",
                                "in_app": True,
                                "lineno": 10,
                            }
                        ]
                    },
                }
            ]
        },
        "release": release,
        "environment": environment,
        "request": {
            "url": "http://example.com",
            "headers": {"User-Agent": "Test Agent"},
        },
    }

    if event:
        base_event.update(event)

    if num_events == 1:
        if envelope:
            return [
                {
                    "event_id": base_event["event_id"],
                    "sent_at": datetime.datetime.now().isoformat() + "Z",
                },
                {"type": "event"},
                base_event,
            ]
        else:
            return base_event
    else:
        events = []
        for _ in range(num_events):
            new_event = base_event.copy()
            new_event["event_id"] = str(uuid.uuid4())
            if envelope:
                events.append(
                    [
                        {
                            "event_id": new_event["event_id"],
                            "sent_at": datetime.datetime.now().isoformat() + "Z",
                        },
                        {"type": "event"},
                        new_event,
                    ]
                )
            else:
                events.append(new_event)
        return events


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
            IssueTaskMessage(
                project_id=self.project.id,
                organization_id=self.organization.id if self.organization else None,
                received=timezone.now(),
                payload=IssueEventSchema(**dat),
            )
            for dat in data
        ]
        process_issue_events(events)
        return events
