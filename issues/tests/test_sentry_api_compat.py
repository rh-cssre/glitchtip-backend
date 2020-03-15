import json
from typing import List, Dict
from django.urls import reverse
from rest_framework.test import APITestCase
from model_bakery import baker
from event_store.test_data.django_error_factory import template_error, message
from event_store.test_data.js_error_factory import throw_error
from event_store.test_data.csp import mdn_sample_csp
from organizations_ext.models import OrganizationUserRole
from issues.models import Event


class SentryAPICompatTestCase(APITestCase):
    def setUp(self):
        self.user = baker.make("users.user")
        self.organization = baker.make("organizations_ext.Organization")
        self.organization.add_user(self.user, OrganizationUserRole.ADMIN)
        self.team = baker.make("teams.Team", organization=self.organization)
        self.team.members.add(self.user)
        self.project = baker.make("projects.Project", organization=self.organization)
        self.project.team_set.add(self.team)
        self.client.force_login(self.user)
        key = self.project.projectkey_set.first().public_key
        self.event_store_url = (
            reverse("event_store", args=[self.project.id]) + "?sentry_key=" + key.hex
        )
        self.csp_store_url = (
            reverse("csp_store", args=[self.project.id]) + "?sentry_key=" + key.hex
        )

    def assertCompareData(self, data1: Dict, data2: Dict, fields: List[str]):
        """ Compare data of two dict objects. Compare only provided fields list """
        for field in fields:
            self.assertEqual(
                data1.get(field), data2.get(field), f"Failed for field '{field}'",
            )

    def get_json_data(self, path: str):
        with open(path) as json_file:
            return json.load(json_file)

    def get_project_events_detail(self, event_id):
        return reverse(
            "project-events-detail",
            kwargs={
                "project_pk": f"{self.project.organization.slug}/{self.project.slug}",
                "pk": event_id,
            },
        )

    def test_template_error(self):
        res = self.client.post(self.event_store_url, template_error, format="json")
        self.assertEqual(res.status_code, 200)

        event_id = res.data["id"]
        url = self.get_project_events_detail(event_id)
        res = self.client.get(url)
        self.assertEqual(res.status_code, 200)
        issue = Event.objects.get(event_id=event_id).issue

        data = self.get_json_data(
            "event_store/test_data/django_template_error_event.json"
        )
        self.assertCompareData(res.data, data, ["culprit", "title", "metadata"])
        res_frames = res.data["entries"][0]["data"]["values"][0]["stacktrace"]["frames"]
        frames = data["entries"][0]["data"]["values"][0]["stacktrace"]["frames"]

        for i in range(9):
            # absPath don't always match - needs fixed
            self.assertCompareData(res_frames[i], frames[i], ["absPath"])
        for res_frame, frame in zip(res_frames, frames):
            self.assertCompareData(
                res_frame,
                frame,
                ["lineNo", "function", "filename", "module", "context"],
            )
            if frame.get("vars"):
                self.assertCompareData(
                    res_frame["vars"], frame["vars"], ["exc", "request"]
                )
                if frame["vars"].get("get_response"):
                    # Memory address is different, truncate it
                    self.assertEqual(
                        res_frame["vars"]["get_response"][:-16],
                        frame["vars"]["get_response"][:-16],
                    )

        self.assertCompareData(
            res.data["entries"][1]["data"],
            data["entries"][1]["data"],
            ["env", "headers", "url", "method", "inferredContentType"],
        )

        url = reverse("issue-detail", kwargs={"pk": issue.pk})
        res = self.client.get(url)
        self.assertEqual(res.status_code, 200)

        data = self.get_json_data(
            "event_store/test_data/django_template_error_issue.json"
        )
        self.assertCompareData(res.data, data, ["title", "metadata"])

    def test_throw_error(self):
        res = self.client.post(self.event_store_url, throw_error, format="json")
        self.assertEqual(res.status_code, 200)

        event_id = res.data["id"]
        url = self.get_project_events_detail(event_id)
        res = self.client.get(url)
        self.assertEqual(res.status_code, 200)
        issue = Event.objects.get(event_id=event_id).issue

        data = self.get_json_data("event_store/test_data/js_throw_error_event.json")
        self.assertCompareData(res.data, data, ["title"])
        self.assertEqual(
            res.data["culprit"],
            "viewWrappedDebugError(http://localhost:4200/vendor.js)",
            "Not perfect match, should really be viewWrappedDebugError(vendor)",
        )
        self.assertEqual(res.data["metadata"]["function"], data["metadata"]["function"])

        url = reverse("issue-detail", kwargs={"pk": issue.pk})
        res = self.client.get(url)
        self.assertEqual(res.status_code, 200)

        data = self.get_json_data("event_store/test_data/js_throw_error_issue.json")
        self.assertCompareData(res.data, data, ["title"])
        self.assertEqual(res.data["metadata"]["function"], data["metadata"]["function"])

    def test_csp_event(self):
        data = mdn_sample_csp
        res = self.client.post(self.csp_store_url, data, format="json")
        self.assertEqual(res.status_code, 200)

        event_id = res.data["id"]
        url = self.get_project_events_detail(event_id)
        res = self.client.get(url)
        self.assertEqual(res.status_code, 200)
        data = self.get_json_data("event_store/test_data/csp_event.json")
        self.assertCompareData(res.data, data, ["title", "culprit", "type", "metadata"])
        self.assertEqual(res.data["entries"][0], data["entries"][0])
        self.assertEqual(res.data["entries"][1], data["entries"][1])

    def test_message_event(self):
        """ A generic message made with the Sentry SDK. Generally has less data than exceptions. """
        res = self.client.post(self.event_store_url, message, format="json")
        self.assertEqual(res.status_code, 200)

        event_id = res.data["id"]
        url = self.get_project_events_detail(event_id)
        res = self.client.get(url)
        self.assertEqual(res.status_code, 200)
        data = self.get_json_data("event_store/test_data/django_message_event.json")
        self.assertCompareData(
            res.data,
            data,
            ["title", "culprit", "type", "metadata", "platform", "packages"],
        )
