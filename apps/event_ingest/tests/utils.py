import json

from django.test import TestCase
from model_bakery import baker


class EventIngestTestCase(TestCase):
    """
    Base class for event ingest tests with helper functions
    """

    def setUp(self):
        self.project = baker.make("projects.Project")
        self.projectkey = self.project.projectkey_set.first()
        self.params = f"?sentry_key={self.projectkey.public_key}"

    def get_event_json(self, filename: str):
        with open(filename) as json_file:
            return json.load(json_file)
