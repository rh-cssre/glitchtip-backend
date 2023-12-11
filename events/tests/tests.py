import json
import random
from collections.abc import Iterable, Mapping
from typing import Optional
from unittest.mock import patch

from django.shortcuts import reverse
from django.test import override_settings
from model_bakery import baker
from prometheus_client import Metric
from prometheus_client.parser import text_string_to_metric_families
from rest_framework.test import APITestCase

from environments.models import Environment, EnvironmentProject
from glitchtip.test_utils import generators  # pylint: disable=unused-import
from glitchtip.test_utils.test_case import GlitchTipTestCase
from issues.models import EventStatus, Issue
from observability.metrics import events_counter, issues_counter
from releases.models import Release

from ..models import Event, LogLevel
from ..test_data.csp import mdn_sample_csp


class EventStoreTestCase(APITestCase):
    def setUp(self):
        self.project = baker.make("projects.Project")
        self.projectkey = self.project.projectkey_set.first()
        self.params = f"?sentry_key={self.projectkey.public_key}"
        self.url = reverse("event_store", args=[self.project.id]) + self.params

    def test_store_api(self):
        with open("events/test_data/py_hi_event.json") as json_file:
            data = json.load(json_file)
        res = self.client.post(self.url, data, format="json")
        self.assertEqual(res.status_code, 200)

    def test_maintenance_freeze(self):
        with open("events/test_data/py_hi_event.json") as json_file:
            data = json.load(json_file)
        with override_settings(MAINTENANCE_EVENT_FREEZE=True):
            res = self.client.post(self.url, data, format="json")
        self.assertEqual(res.status_code, 503)

    def test_store_duplicate(self):
        with open("events/test_data/py_hi_event.json") as json_file:
            data = json.load(json_file)
        self.client.post(self.url, data, format="json")
        res = self.client.post(self.url, data, format="json")
        self.assertContains(res, "ID already exist", status_code=403)

    def test_store_invalid_key(self):
        with open("events/test_data/py_hi_event.json") as json_file:
            data = json.load(json_file)
        self.client.post(self.url, data, format="json")
        res = self.client.post(self.url, data, format="json")
        self.assertContains(res, "ID already exist", status_code=403)

    def test_store_api_auth_failure(self):
        url = "/api/1/store/"
        with open("events/test_data/py_hi_event.json") as json_file:
            data = json.load(json_file)
        params = "?sentry_key=aaa"
        url = reverse("event_store", args=[self.project.id]) + params
        res = self.client.post(url, data, format="json")
        self.assertEqual(res.status_code, 401)

        params = "?sentry_key=238df2aac6331578a16c14bcb3db5259"
        url = reverse("event_store", args=[self.project.id]) + params
        res = self.client.post(url, data, format="json")
        self.assertContains(res, "Invalid api key", status_code=401)

        url = reverse("event_store", args=[10000]) + self.params
        res = self.client.post(url, data, format="json")
        self.assertContains(res, "Invalid project_id", status_code=400)

    def test_error_event(self):
        with open("events/test_data/py_error.json") as json_file:
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
        with open("events/test_data/py_hi_event.json") as json_file:
            data = json.load(json_file)
        self.client.post(self.url, data, format="json")
        issue = Issue.objects.all().first()
        issue.status = EventStatus.RESOLVED
        issue.save()
        data["event_id"] = "6600a066e64b4caf8ed7ec5af64ac4ba"
        self.client.post(self.url, data, format="json")
        issue.refresh_from_db()
        self.assertEqual(issue.status, EventStatus.UNRESOLVED)

    # TODO Issue Ingest tests, got to here

    def test_issue_count(self):
        with open("events/test_data/py_hi_event.json") as json_file:
            data = json.load(json_file)
        self.client.post(self.url, data, format="json")
        issue = Issue.objects.first()
        self.assertEqual(issue.count, 1)
        data["event_id"] = "6600a066e64b4caf8ed7ec5af64ac4ba"
        self.client.post(self.url, data, format="json")
        issue.refresh_from_db()
        self.assertEqual(issue.count, 2)

    def test_performance(self):
        with open("events/test_data/py_hi_event.json") as json_file:
            data = json.load(json_file)
        with self.assertNumQueries(18):
            res = self.client.post(self.url, data, format="json")
        self.assertEqual(res.status_code, 200)

        # Second event should have less queries
        data["event_id"] = "6600a066e64b4caf8ed7ec5af64ac4bb"
        with self.assertNumQueries(10):
            res = self.client.post(self.url, data, format="json")
        self.assertEqual(res.status_code, 200)

    def test_throttle_organization(self):
        organization = self.project.organization
        organization.is_accepting_events = False
        organization.save()
        with open("events/test_data/py_hi_event.json") as json_file:
            data = json.load(json_file)
        res = self.client.post(self.url, data, format="json")
        self.assertEqual(res.status_code, 429)

    def test_throttle_project(self):
        self.project.event_throttle_rate = 100
        self.project.save()
        with open("events/test_data/py_hi_event.json") as json_file:
            data = json.load(json_file)
        # throttled
        res = self.client.post(self.url, data, format="json")
        self.assertEqual(res.status_code, 429)

    def test_project_first_event(self):
        with open("events/test_data/py_error.json") as json_file:
            data = json.load(json_file)
        self.assertFalse(self.project.first_event)
        self.client.post(self.url, data, format="json")
        self.project.refresh_from_db()
        self.assertTrue(self.project.first_event)

    def test_null_character_event(self):
        """
        Unicode null characters \u0000 are not supported by Postgres JSONB
        NUL \x00 characters are not supported by Postgres string types
        They should be filtered out
        """
        with open("events/test_data/py_error.json") as json_file:
            data = json.load(json_file)
        data["exception"]["values"][0]["stacktrace"]["frames"][0][
            "function"
        ] = "a\u0000a"
        data["exception"]["values"][0]["value"] = "\x00\u0000"
        res = self.client.post(self.url, data, format="json")
        self.assertEqual(res.status_code, 200)

    def test_header_value_array(self):
        """
        Request Header values are both strings and arrays (sentry-php uses arrays)
        """
        with open("events/test_data/py_error.json") as json_file:
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
        """ip address should get masked because default project settings are to scrub ip address"""
        with open("events/test_data/py_hi_event.json") as json_file:
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

    def test_store_very_large_data(self):
        """
        This test is expected to exceed the 1mb limit of a postgres tsvector
        """
        with open("events/test_data/py_hi_event.json") as json_file:
            data = json.load(json_file)

        data["platform"] = " ".join([str(random.random()) for _ in range(50000)])
        res = self.client.post(self.url, data, format="json")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(
            Issue.objects.first().search_vector,
            None,
            "No tsvector is expected as it would exceed the Postgres limit",
        )
        data["event_id"] = "6600a066e64b4caf8ed7ec5af64ac4be"
        res = self.client.post(self.url, data, format="json")
        self.assertEqual(res.status_code, 200)

    def test_store_somewhat_large_data(self):
        """
        This test is expected to exceed the 1mb limit of a postgres tsvector
        only when two events exist for 1 issue.
        """
        with open("events/test_data/py_hi_event.json") as json_file:
            data = json.load(json_file)

        data["platform"] = " ".join([str(random.random()) for _ in range(30000)])
        res = self.client.post(self.url, data, format="json")
        self.assertEqual(res.status_code, 200)
        data["event_id"] = "6600a066e64b4caf8ed7ec5af64ac4be"
        data["platform"] = " ".join([str(random.random()) for _ in range(30000)])
        res = self.client.post(self.url, data, format="json")
        self.assertEqual(res.status_code, 200)
        self.assertTrue(
            Issue.objects.first().search_vector,
            "tsvector is expected",
        )

    @patch("events.views.logger")
    def test_invalid_event(self, mock_logger):
        with open("events/test_data/py_hi_event.json") as json_file:
            data = json.load(json_file)

        data["transaction"] = True
        res = self.client.post(self.url, data, format="json")
        self.assertEqual(res.status_code, 200)
        mock_logger.warning.assert_called()

    def test_breadcrumbs_object(self):
        """Event breadcrumbs may be sent as an array or a object."""
        with open("events/test_data/py_hi_event.json") as json_file:
            data = json.load(json_file)

        data["breadcrumbs"] = {
            "values": [
                {
                    "timestamp": "2020-01-20T20:00:00.000Z",
                    "message": "Something",
                    "category": "log",
                    "data": {"foo": "bar"},
                },
            ]
        }
        res = self.client.post(self.url, data, format="json")
        self.assertEqual(res.status_code, 200)
        self.assertTrue(Issue.objects.exists())

    def test_event_release(self):
        with open("events/test_data/py_hi_event.json") as json_file:
            data = json.load(json_file)

        baker.make("releases.Release", version=data.get("release"))

        self.client.post(self.url, data, format="json")
        event = Event.objects.first()
        event_json = event.event_json()
        self.assertTrue(event.release)
        self.assertEqual(event_json.get("release"), event.release.version)
        self.assertIn(
            event.release.version,
            dict(event_json.get("tags")).values(),
        )
        self.assertTrue(
            Release.objects.filter(
                version=data.get("release"), projects=self.project
            ).exists()
        )

    def test_event_release_blank(self):
        """In the SDK, it's possible to set a release to a blank string"""
        with open("events/test_data/py_hi_event.json") as json_file:
            data = json.load(json_file)
        data["release"] = ""
        res = self.client.post(self.url, data, format="json")
        self.assertEqual(res.status_code, 200)
        self.assertTrue(Event.objects.first())

    def test_client_tags(self):
        with open("events/test_data/py_hi_event.json") as json_file:
            data = json.load(json_file)
        data["tags"] = {"test_tag": "the value"}
        self.client.post(self.url, data, format="json")
        event = Event.objects.first()
        event_json = event.event_json()
        self.assertIn(
            "the value",
            tuple(event_json.get("tags"))[1],
        )

    def test_client_tags_invalid(self):
        """Invalid tags should not be saved. But should not error."""
        with open("events/test_data/py_hi_event.json") as json_file:
            data = json.load(json_file)
        data["tags"] = {
            "value": "valid value",
            "my invalid tag key": {"oh": "this is invalid"},
        }
        res = self.client.post(self.url, data, format="json")
        event = Event.objects.first()
        self.assertEqual(res.status_code, 200)
        self.assertTrue(event)
        event_json = event.event_json()
        tags = tuple(event_json.get("tags"))
        self.assertIn(
            "valid value",
            tags[0],
        )
        for tag in tags:
            self.assertNotIn("this is invalid", tag)
        self.assertEqual(len(event_json.get("errors")), 1)

    def test_malformed_exception_value(self):
        """Malformed exception values aren't 100% supported, but should stored anyway"""
        with open("events/test_data/py_error.json") as json_file:
            data = json.load(json_file)
        data["exception"]["values"][0]["value"] = {"why is this": "any object?"}
        res = self.client.post(self.url, data, format="json")
        self.assertEqual(res.status_code, 200)

    def test_no_sdk(self):
        data = {
            "exception": [
                {
                    "type": "Plug.Parsers.ParseError",
                    "value": "malformed request",
                    "module": None,
                }
            ],
            "culprit": "Plug.Parsers.JSON.decode",
            "extra": {},
            "event_id": "11111111111111111111111111111111",
            "breadcrumbs": [],
            "level": "error",
            "modules": {
                "cowboy": "2.8.0",
            },
            "fingerprint": ["{{ default }}"],
            "message": "(Plug.Parsers.ParseError) malformed",
        }
        res = self.client.post(self.url, data, format="json")
        self.assertEqual(res.status_code, 200)
        self.assertTrue(Event.objects.exists())

    def test_invalid_level(self):
        data = {
            "exception": [
                {
                    "type": "a",
                    "value": "a",
                    "module": None,
                }
            ],
            "culprit": "a",
            "extra": {},
            "event_id": "11111111111111111111111111111111",
            "breadcrumbs": [],
            "level": "haha",
            "message": "a",
        }

        res = self.client.post(self.url, data, format="json")
        self.assertEqual(res.status_code, 200)
        self.assertTrue(Event.objects.filter(level=LogLevel.ERROR).exists())

    def test_null_release(self):
        data = {
            "exception": [
                {
                    "type": "a",
                    "value": "a",
                    "module": None,
                }
            ],
            "culprit": "a",
            "extra": {},
            "event_id": "11111111111111111111111111111111",
            "breadcrumbs": [],
            "level": "haha",
            "message": "",
            "release": None,
            "environment": None,
            "request": {"env": {"FOO": None}},
        }

        res = self.client.post(self.url, data, format="json")
        self.assertEqual(res.status_code, 200)
        self.assertTrue(Event.objects.filter().exists())

    def test_formatted_message(self):
        data = {
            "exception": [
                {
                    "type": "a",
                    "value": "a",
                    "module": None,
                }
            ],
            "event_id": "11111111111111111111111111111111",
            "message": {"formatted": "Hello"},
        }

        res = self.client.post(self.url, data, format="json")
        self.assertTrue(Event.objects.filter(data__message="Hello").exists())

    def test_invalid_message(self):
        # It's actually accepted as is. Considered to be message: ""
        data = {
            "exception": [
                {
                    "type": "a",
                    "value": "a",
                    "module": None,
                }
            ],
            "event_id": "11111111111111111111111111111111",
            "message": {},
        }

        res = self.client.post(self.url, data, format="json")
        self.assertTrue(Event.objects.filter(data__message="").exists())

    def test_null_message(self):
        data = {
            "exception": [{}],
            "event_id": "11111111111111111111111111111111",
            "message": None,
        }

        res = self.client.post(self.url, data, format="json")
        self.assertTrue(Event.objects.filter(data__message=None).exists())

    def test_long_environment(self):
        data = {
            "exception": [
                {
                    "type": "a",
                    "value": "a",
                    "module": None,
                }
            ],
            "event_id": "11111111111111111111111111111111",
            "environment": "a" * 257,
        }

        res = self.client.post(self.url, data, format="json")
        self.assertTrue(Event.objects.filter().exists())

    def test_repeat_environment(self):
        existing_environment = baker.make("environments.Environment", name="staging")
        data = {
            "exception": [
                {
                    "type": "a",
                    "value": "a",
                    "module": None,
                }
            ],
            "event_id": "11111111111111111111111111111111",
            "environment": existing_environment.name,
        }

        res = self.client.post(self.url, data, format="json")
        self.assertTrue(
            EnvironmentProject.objects.filter(
                environment__name=existing_environment.name, project=self.project
            ).exists()
        )

    def test_invalid_environment(self):
        data = {
            "exception": [
                {
                    "type": "a",
                    "value": "a",
                    "module": None,
                }
            ],
            "event_id": "11111111111111111111111111111111",
            "environment": "a/a",
        }

        res = self.client.post(self.url, data, format="json")
        self.assertTrue(Event.objects.filter().exists())
        self.assertFalse(Environment.objects.exists())

    def test_query_string_formats(self):
        data = {
            "event_id": "11111111111111111111111111111111",
            "exception": [
                {
                    "type": "a",
                    "value": "a",
                    "module": None,
                }
            ],
            "request": {
                "method": "GET",
                "query_string": {"search": "foo"},
            },
        }
        self.client.post(self.url, data, format="json")
        data = {
            "event_id": "11111111111111111111111111111112",
            "exception": [
                {
                    "type": "a",
                    "value": "a",
                    "module": None,
                }
            ],
            "request": {
                "query_string": "search=foo",
            },
        }
        self.client.post(self.url, data, format="json")
        data = {
            "event_id": "11111111111111111111111111111113",
            "exception": [
                {
                    "type": "a",
                    "value": "a",
                    "module": None,
                }
            ],
            "request": {"query_string": [["search", "foo"]]},
        }
        self.client.post(self.url, data, format="json")
        self.assertEqual(
            Event.objects.filter(
                data__request__query_string=[["search", "foo"]]
            ).count(),
            3,
        )


def get_sample_value(
    metric_families: Iterable[Metric],
    metric_name: str,
    metric_type: str,
    labels: Mapping[str, str],
) -> Optional[float]:
    for metric_family in metric_families:
        if metric_family.name != metric_name or metric_family.type != metric_type:
            continue
        for metric in metric_family.samples:
            if metric[1] != labels:
                continue
            return metric.value
    return None


def parse_prometheus_text(text: str) -> list[Metric]:
    parser = text_string_to_metric_families(text)
    return list(parser)


class EventMetricTestCase(GlitchTipTestCase):
    def setUp(self):
        self.create_user_and_project()
        self.user.is_staff = True
        self.user.save()

        self.metrics_url = reverse("prometheus-django-metrics")
        self.projectkey = self.project.projectkey_set.first()
        self.params = f"?sentry_key={self.projectkey.public_key}"
        self.events_url = reverse("event_store", args=[self.project.id]) + self.params

    def test_metrics(self):
        with open("events/test_data/py_hi_event.json") as json_file:
            data = json.load(json_file)
        event_metric_labels = {
            "project": self.project.slug,
            "organization": self.project.organization.slug,
            "issue": "hi",
        }
        issue_metric_labels = {
            "project": self.project.slug,
            "organization": self.project.organization.slug,
        }

        # get initial metrics
        metric_res = self.client.get(self.metrics_url)
        self.assertEqual(metric_res.status_code, 200)
        metrics = parse_prometheus_text(metric_res.content.decode("utf-8"))

        events_before = get_sample_value(
            metrics, events_counter._name, events_counter._type, event_metric_labels
        )
        # no events yet
        self.assertEqual(events_before, None)
        issues_before = get_sample_value(
            metrics, issues_counter._name, issues_counter._type, issue_metric_labels
        )
        # no issues yet
        self.assertEqual(issues_before, None)

        # send event
        res = self.client.post(self.events_url, data, format="json")
        self.assertEqual(res.status_code, 200)

        # get latest metrics
        metric_res = self.client.get(self.metrics_url)
        self.assertEqual(metric_res.status_code, 200)

        metrics = parse_prometheus_text(metric_res.content.decode("utf-8"))
        events_after = get_sample_value(
            metrics, events_counter._name, events_counter._type, event_metric_labels
        )
        self.assertEqual(events_after, 1)
        issues_after = get_sample_value(
            metrics, issues_counter._name, issues_counter._type, issue_metric_labels
        )
        self.assertEqual(issues_after, 1)

        # Second event should not increase the issue count
        data["event_id"] = "6600a066e64b4caf8ed7ec5af64ac4bb"
        res = self.client.post(self.events_url, data, format="json")
        self.assertEqual(res.status_code, 200)

        # get latest metrics
        metric_res = self.client.get(self.metrics_url)
        self.assertEqual(metric_res.status_code, 200)

        metrics = parse_prometheus_text(metric_res.content.decode("utf-8"))
        events_after = get_sample_value(
            metrics, events_counter._name, events_counter._type, event_metric_labels
        )
        # new event
        self.assertEqual(events_after, 2)
        issues_after = get_sample_value(
            metrics, issues_counter._name, issues_counter._type, issue_metric_labels
        )
        # but no new issue
        self.assertEqual(issues_after, 1)
