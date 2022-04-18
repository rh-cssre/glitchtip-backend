import shutil
from django.shortcuts import reverse
from rest_framework.test import APITestCase
from model_bakery import baker
from glitchtip import test_utils  # pylint: disable=unused-import
from ..models import Event


sample_event = {
    "event_id": "cf536c31b68a473f97e579507ce155e3",
    "platform": "javascript",
}


sample_event = {
    "exception": {
        "values": [
            {
                "type": "Error",
                "value": "The error",
                "stacktrace": {
                    "frames": [
                        {
                            "filename": "http://localhost:8080/dist/bundle.js",
                            "function": "?",
                            "in_app": True,
                            "lineno": 2,
                            "colno": 74016,
                        },
                        {
                            "filename": "http://localhost:8080/dist/bundle.js",
                            "function": "?",
                            "in_app": True,
                            "lineno": 2,
                            "colno": 74012,
                        },
                        {
                            "filename": "http://localhost:8080/dist/bundle.js",
                            "function": "?",
                            "in_app": True,
                            "lineno": 2,
                            "colno": 73992,
                        },
                    ]
                },
                "mechanism": {"type": "onerror", "handled": False},
            }
        ]
    },
    "level": "error",
    "platform": "javascript",
    "event_id": "0691751a89db419994efac8ac9b00a5d",
    "timestamp": 1648414309.82,
    "environment": "production",
    "request": {
        "url": "http://localhost:8080/",
        "headers": {
            "User-Agent": "Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:98.0) Gecko/20100101 Firefox/98.0"
        },
    },
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
        blob_bundle = baker.make("files.FileBlob", blob="uploads/file_blobs/bundle.js")
        blob_bundle_map = baker.make(
            "files.FileBlob", blob="uploads/file_blobs/bundle.js.map"
        )
        release_file_bundle = baker.make(
            "releases.ReleaseFile",
            release=self.release,
            file__name="bundle.js",
            file__blob=blob_bundle,
        )
        release_file_bundle_map = baker.make(
            "releases.ReleaseFile",
            release=self.release,
            file__name="bundle.js.map",
            file__blob=blob_bundle_map,
        )
        shutil.copyfile(
            "./events/tests/test_data/bundle.js", "./uploads/file_blobs/bundle.js"
        )
        shutil.copyfile(
            "./events/tests/test_data/bundle.js.map",
            "./uploads/file_blobs/bundle.js.map",
        )
        data = sample_event | {"release": self.release.version}

        res = self.client.post(self.url, data, format="json")
        self.assertTrue(Event.objects.filter(release=self.release).exists())
