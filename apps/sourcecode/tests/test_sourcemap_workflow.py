import gzip
import hashlib
import io
import json
import uuid
import zipfile
from unittest.mock import patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.http.response import HttpResponse
from django.tasks import task_backends
from django.urls import reverse

from apps.event_ingest.tests.utils import generate_event, list_to_envelope
from apps.files.models import File, FileBlob
from apps.issue_events.models import Issue
from glitchtip.test_utils.test_case import GlitchTipTransactionTestCase

debug_id = str(uuid.uuid4())
minified_js = "function a(n,t){return n+t}"
minified_js_map = f"""{{
    "version": 3,
    "file": "minified.js",
    "sourceRoot": "",
    "sources": ["original.js"],
    "names": ["calculateSum", "firstNumber", "secondNumber", "a", "n", "t"],
    "mappings": "AAAA,QAASA,CAAT,CAAeC,CAAf,EAAkBC,CAAlB,EAAqB,OAAOD,CAAP,GAAUC,CAAV,GAAaC,CAAd",
    "sourcesContent": ["function calculateSum(firstNumber, secondNumber) {{\\n    return firstNumber + secondNumber;\\n}}"],
    "debugId": "{debug_id}"
}}"""
original_js = """function calculateSum(firstNumber, secondNumber) {
  return firstNumber + secondNumber;
}"""


class SourceCodeTestCase(GlitchTipTransactionTestCase):
    def setUp(self):
        # ``create_project`` sets self.project + self.projectkey on the same
        # Project (matters for the envelope URL below). The default
        # ``create_logged_in_user`` then creates a second project, which
        # would mismatch self.projectkey — bypass it.
        self.create_project()
        from model_bakery import baker

        self.user = baker.make("users.user")
        self.organization.add_user(self.user)
        self.client.force_login(self.user)

    def upload_chunk(
        self,
        url: str,
    ) -> tuple[HttpResponse, str]:
        # First construct zip file containing source, map, and manifest files.
        manifest = {
            "files": {
                f"files/_/_/{debug_id}-0.js": {
                    "type": "minified_source",
                    "url": f"~/{debug_id}-0.js",
                    "headers": {"debug-id": debug_id, "sourcemap": "minified.js.map"},
                },
                f"files/_/_/{debug_id}-0.js.map": {
                    "type": "source_map",
                    "url": f"~/{debug_id}-0.js.map",
                    "headers": {"debug-id": debug_id},
                },
            },
            "debug_id": str(uuid.uuid4()),
            "org": self.organization.slug,
            "project": self.project.slug,
        }
        in_memory_buffer = io.BytesIO()
        with zipfile.ZipFile(in_memory_buffer, mode="w") as zipf:
            zipf.writestr("manifest.json", json.dumps(manifest))
            zipf.writestr(f"files/_/_/{debug_id}-0.js", minified_js)
            zipf.writestr(f"files/_/_/{debug_id}-0.js.map", minified_js_map)
        in_memory_buffer.seek(0)

        # Calculate the SHA1 checksum first
        checksum = hashlib.sha1(in_memory_buffer.read()).hexdigest()
        in_memory_buffer.seek(0)

        file = SimpleUploadedFile(
            checksum,  # Use checksum as filename
            gzip.compress(in_memory_buffer.read()),
        )

        response = self.client.post(
            url,
            {"file_gzip": [file]},
        )

        return response, checksum

    def test_sourcemap_integrated(self):
        """Test full workflow of uploading sourcemaps to unminifying event code"""
        chunk_upload_url = reverse(
            "api:get_chunk_upload_info", args=[self.organization.slug]
        )
        assemble_url = reverse(
            "api:artifact_bundle_assemble", args=[self.organization.slug]
        )
        envelope_url = (
            reverse("event_envelope", args=[self.project.id])
            + f"?sentry_key={self.projectkey.public_key}"
        )

        res = self.client.get(chunk_upload_url)
        self.assertContains(res, "artifact_bundles")  # sentry sdk requires this set

        # Upload source code "chunks"
        res, checksum = self.upload_chunk(chunk_upload_url)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(FileBlob.objects.count(), 1)

        # Call assemble
        res = self.client.post(
            assemble_url,
            {
                "checksum": checksum,
                "chunks": [checksum],
                "projects": [self.project.slug],
            },
            content_type="application/json",
        )
        self.assertContains(res, "created")
        self.assertEqual(FileBlob.objects.count(), 3)
        self.assertEqual(File.objects.count(), 2)

        # Submit event
        data = generate_event(
            event_type="error",
            platform="javascript",
            event={
                "exception": {
                    "values": [
                        {
                            "type": "Error",
                            "value": "err",
                            "stacktrace": {
                                "frames": [
                                    {
                                        "filename": "http://127.0.0.1:8080/assets/minified.js",
                                        "function": "?",
                                        "in_app": True,
                                        "lineno": 1,
                                        "colno": 4,
                                    },
                                ]
                            },
                        }
                    ]
                },
                "debug_meta": {
                    "images": [
                        {
                            "type": "sourcemap",
                            "code_file": "http://127.0.0.1:8080/assets/minified.js",
                            "debug_id": debug_id,
                        }
                    ]
                },
            },
            envelope=True,
        )
        res = self.client.post(
            envelope_url, list_to_envelope(data), content_type="application/json"
        )
        task_backends["default"].flush_batches()
        self.assertContains(res, data[0]["event_id"][:8])
        self.assertEqual(Issue.objects.count(), 1)
        issue = Issue.objects.get()
        event = issue.issueevent_set.first()
        self.assertIn(
            "firstNumber",
            event.data["exception"]["values"][0]["stacktrace"]["frames"][0][
                "context_line"
            ],
        )

    def test_sourcemap_parsed_once_per_event(self):
        """A bundle is parsed once per event, not once per stack frame.

        Rebuilding SourceMapCache per frame made ingest of a single event with
        a large sourcemap and many frames take minutes and peg the worker at
        100% CPU. All frames sharing a bundle must share one parsed cache.
        """
        from apps.event_ingest import javascript_event_processor as jsproc

        chunk_upload_url = reverse(
            "api:get_chunk_upload_info", args=[self.organization.slug]
        )
        assemble_url = reverse(
            "api:artifact_bundle_assemble", args=[self.organization.slug]
        )
        envelope_url = (
            reverse("event_envelope", args=[self.project.id])
            + f"?sentry_key={self.projectkey.public_key}"
        )

        _res, checksum = self.upload_chunk(chunk_upload_url)
        self.client.post(
            assemble_url,
            {
                "checksum": checksum,
                "chunks": [checksum],
                "projects": [self.project.slug],
            },
            content_type="application/json",
        )

        # Three frames resolving against the same minified file.
        frame = {
            "filename": "http://127.0.0.1:8080/assets/minified.js",
            "function": "?",
            "in_app": True,
            "lineno": 1,
        }
        data = generate_event(
            event_type="error",
            platform="javascript",
            event={
                "exception": {
                    "values": [
                        {
                            "type": "Error",
                            "value": "err",
                            "stacktrace": {
                                "frames": [
                                    {**frame, "colno": 4},
                                    {**frame, "colno": 16},
                                    {**frame, "colno": 24},
                                ]
                            },
                        }
                    ]
                },
                "debug_meta": {
                    "images": [
                        {
                            "type": "sourcemap",
                            "code_file": "http://127.0.0.1:8080/assets/minified.js",
                            "debug_id": debug_id,
                        }
                    ]
                },
            },
            envelope=True,
        )

        with patch.object(
            jsproc.SourceMapCache,
            "from_bytes",
            wraps=jsproc.SourceMapCache.from_bytes,
        ) as mock_from_bytes:
            self.client.post(
                envelope_url, list_to_envelope(data), content_type="application/json"
            )
            task_backends["default"].flush_batches()

        # Parsed once for the file pair, not once per frame.
        self.assertEqual(mock_from_bytes.call_count, 1)

        # Symbolication still works: every frame is remapped from the cache.
        event = Issue.objects.get().issueevent_set.first()
        self.assertIn(
            "firstNumber",
            event.data["exception"]["values"][0]["stacktrace"]["frames"][0][
                "context_line"
            ],
        )
