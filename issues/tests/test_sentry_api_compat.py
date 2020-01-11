from django.urls import reverse
from rest_framework.test import APITestCase
from model_bakery import baker
from event_store.test_data.django_error_factory import template_error
from issues.models import Event


class SentryAPICompatTestCase(APITestCase):
    def test_template_error(self):
        project = baker.make("projects.Project")
        key = project.projectkey_set.first().public_key
        url = reverse("event_store", args=[project.id]) + "?sentry_key=" + key.hex
        res = self.client.post(url, template_error, format="json")
        event = Event.objects.get(event_id=res.data["id"])
        self.assertEqual(event.culprit, "/template-error/")

