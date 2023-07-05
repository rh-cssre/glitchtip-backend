import uuid

from django.shortcuts import reverse
from model_bakery import baker

from glitchtip.test_utils.test_case import GlitchTipTestCase

from ..models import Issue


class IssueGroupingTestCase(GlitchTipTestCase):
    def setUp(self):
        self.project = baker.make("projects.Project")
        self.projectkey = self.project.projectkey_set.first()
        self.params = f"?sentry_key={self.projectkey.public_key}"
        self.url = reverse("event_store", args=[self.project.id]) + self.params

    def test_fingerprint(self):
        data = {
            "exception": [
                {
                    "type": "a",
                    "value": "a",
                }
            ],
            "event_id": uuid.uuid4(),
            "fingerprint": ["foo"],
        }
        self.client.post(self.url, data, format="json")
        data["exception"][0]["type"] = "lol"
        data["event_id"] = uuid.uuid4()
        self.client.post(self.url, data, format="json")
        self.assertEqual(Issue.objects.count(), 1)
