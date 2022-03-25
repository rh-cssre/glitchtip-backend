from django.shortcuts import reverse
from rest_framework.test import APITestCase
from model_bakery import baker
from glitchtip import test_utils  # pylint: disable=unused-import
from ..models import Event


sample_event = {
    "event_id": "cf536c31b68a473f97e579507ce155e3",
    "platform": "javascript",
}


class JavaScriptProcessorTestCase(APITestCase):
    def setUp(self):
        self.project = baker.make("projects.Project")
        self.organization = self.project.organization
        self.release = baker.make("releases.Release", organization=self.organization)
        self.release.projects.add(self.project)
        key = self.project.projectkey_set.first().public_key
        self.url = (
            reverse("event_store", args=[self.project.id]) + "?sentry_key=" + key.hex
        )

    def test_process_sourcemap(self):
        release_file = baker.make("releases.ReleaseFile", release=self.release)
        data = sample_event | {"release": self.release.version}
        res = self.client.post(self.url, data, format="json")
        self.assertTrue(Event.objects.filter(release=self.release).exists())
