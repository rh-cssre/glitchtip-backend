import json
from django.shortcuts import reverse
from rest_framework.test import APITestCase
from model_bakery import baker
from glitchtip import test_utils  # pylint: disable=unused-import
from issues.models import Issue, Event, EventStatus
from ..test_data.csp import mdn_sample_csp


class EventStoreTestCase(APITestCase):
    def setUp(self):
        self.project = baker.make("projects.Project")
        self.projectkey = self.project.projectkey_set.first()
        self.params = f"?sentry_key={self.projectkey.public_key}"
        self.url = reverse("event_store", args=[self.project.id]) + self.params

    def test_store_api(self):
        with open("event_store/test_data/py_hi_event.json") as json_file:
            data = json.load(json_file)
        res = self.client.post(self.url, data, format="json")
        self.assertEqual(res.status_code, 200)

    def test_store_api_auth_failure(self):
        url = "/api/1/store/"
        with open("event_store/test_data/py_hi_event.json") as json_file:
            data = json.load(json_file)
        res = self.client.post(url, data, format="json")
        self.assertEqual(res.status_code, 403)

    def test_error_event(self):
        with open("event_store/test_data/py_error.json") as json_file:
            data = json.load(json_file)
        res = self.client.post(self.url, data, format="json")
        self.assertEqual(res.status_code, 200)

    def test_csp_event(self):
        url = reverse("csp_store", args=[self.project.id]) + self.params
        data = mdn_sample_csp
        res = self.client.post(url, data, format="json")
        self.assertEqual(res.status_code, 200)
        expected_title = "Blocked 'style' from 'example.com'"
        issue = Issue.objects.get(title=expected_title)
        event = Event.objects.get()
        self.assertEqual(event.data["csp"]["effective_directive"], "style-src")
        self.assertTrue(issue)

    def test_reopen_resolved_issue(self):
        with open("event_store/test_data/py_hi_event.json") as json_file:
            data = json.load(json_file)
        self.client.post(self.url, data, format="json")
        issue = Issue.objects.all().first()
        issue.status = EventStatus.RESOLVED
        issue.save()
        data["event_id"] = "6600a066e64b4caf8ed7ec5af64ac4ba"
        self.client.post(self.url, data, format="json")
        issue.refresh_from_db()
        self.assertEqual(issue.status, EventStatus.UNRESOLVED)

    def test_performance(self):
        with open("event_store/test_data/py_hi_event.json") as json_file:
            data = json.load(json_file)
        with self.assertNumQueries(9):
            res = self.client.post(self.url, data, format="json")
        self.assertEqual(res.status_code, 200)

        # Second event should have less queries
        data["event_id"] = "6600a066e64b4caf8ed7ec5af64ac4bb"
        with self.assertNumQueries(5):
            res = self.client.post(self.url, data, format="json")
        self.assertEqual(res.status_code, 200)

    def test_throttle_organization(self):
        organization = self.project.organization
        organization.is_accepting_events = False
        organization.save()
        with open("event_store/test_data/py_hi_event.json") as json_file:
            data = json.load(json_file)
        res = self.client.post(self.url, data, format="json")
        self.assertEqual(res.status_code, 429)

    def test_project_first_event(self):
        with open("event_store/test_data/py_error.json") as json_file:
            data = json.load(json_file)
        self.assertFalse(self.project.first_event)
        self.client.post(self.url, data, format="json")
        self.project.refresh_from_db()
        self.assertTrue(self.project.first_event)

    def test_null_character_event(self):
        """
        Unicode null characters \u0000 are not compatible with Postgres JSON data types.
        They should be filtered out
        """
        with open("event_store/test_data/py_error.json") as json_file:
            data = json.load(json_file)
        data["exception"]["values"][0]["stacktrace"]["frames"][0][
            "function"
        ] = "a\u0000a"
        res = self.client.post(self.url, data, format="json")
        self.assertEqual(res.status_code, 200)

    def test_header_value_array(self):
        """
        Request Header values are both strings and arrays (sentry-php uses arrays)
        """
        with open("event_store/test_data/py_error.json") as json_file:
            data = json.load(json_file)
        data["request"]["headers"]["Content-Type"] = ["text/plain"]
        res = self.client.post(self.url, data, format="json")
        self.assertEqual(res.status_code, 200)
        event = Event.objects.first()
        header = next(
            x for x in event.data["request"]["headers"] if x[0] == "Content-Type"
        )
        self.assertTrue(isinstance(header[1], str))

    def test_anonymize_ip(self):
        """ ip address should get masked because default project settings are to scrub ip address """
        with open("event_store/test_data/py_hi_event.json") as json_file:
            data = json.load(json_file)
        test_ip = "123.168.29.14"
        res = self.client.post(self.url, data, format="json", REMOTE_ADDR=test_ip)
        self.assertEqual(res.status_code, 200)
        event = Event.objects.first()
        self.assertNotEqual(event.data["user"]["ip_address"], test_ip)

    def test_csp_event_anonymize_ip(self):
        url = reverse("csp_store", args=[self.project.id]) + self.params
        test_ip = "123.168.29.14"
        data = mdn_sample_csp
        res = self.client.post(url, data, format="json", REMOTE_ADDR=test_ip)
        self.assertEqual(res.status_code, 200)
        event = Event.objects.first()
        self.assertNotEqual(event.data["user"]["ip_address"], test_ip)
