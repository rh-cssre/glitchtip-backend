import gzip
import uuid

from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.tasks import task_backends
from django.test import TestCase
from django.test.client import BOUNDARY, MULTIPART_CONTENT, encode_multipart
from django.urls import reverse

from apps.event_ingest.minidump_event import (
    minidump_to_event,
    parse_cv_record_debug_id,
)
from apps.issue_events.models import IssueEvent
from apps.releases.models import Release

from .utils import EventIngestTestCase


class ParseCvRecordDebugIdTest(TestCase):
    def test_valid_rsds_record(self):
        """Parse a valid PDB70 (RSDS) CodeView record."""
        import struct

        cv = b"RSDS"
        cv += struct.pack("<I", 0x01020304)
        cv += struct.pack("<H", 0x0506)
        cv += struct.pack("<H", 0x0708)
        cv += bytes([0x09, 0x0A, 0x0B, 0x0C, 0x0D, 0x0E, 0x0F, 0x10])
        cv += struct.pack("<I", 1)  # age
        cv += b"test.pdb\x00"

        result = parse_cv_record_debug_id(cv)
        self.assertIsNotNone(result)
        self.assertEqual(result, "01020304-0506-0708-090a-0b0c0d0e0f10-1")

    def test_invalid_signature(self):
        """Non-RSDS records return None."""
        self.assertIsNone(parse_cv_record_debug_id(b"NB10" + b"\x00" * 20))

    def test_too_short(self):
        """Records shorter than 24 bytes return None."""
        self.assertIsNone(parse_cv_record_debug_id(b"RSDS" + b"\x00" * 10))


class MinidumpToEventTest(TestCase):
    """Unit tests for the minidump-to-event conversion."""

    def _load_fixture(self):
        with open(
            "apps/event_ingest/tests/test_data/minidump_linux_x86_64.dmp", "rb"
        ) as f:
            return f.read()

    def test_basic_event_structure(self):
        data = self._load_fixture()
        event = minidump_to_event(data)

        self.assertEqual(event["platform"], "native")
        self.assertEqual(event["level"], "fatal")
        self.assertIn("event_id", event)
        self.assertIn("exception", event)
        self.assertIn("threads", event)
        self.assertIn("debug_meta", event)

    def test_system_info(self):
        data = self._load_fixture()
        event = minidump_to_event(data)

        self.assertEqual(event["contexts"]["os"]["name"], "Linux")
        self.assertEqual(event["contexts"]["device"]["arch"], "x86_64")

    def test_exception(self):
        data = self._load_fixture()
        event = minidump_to_event(data)

        exc = event["exception"]["values"][0]
        self.assertEqual(exc["type"], "SIGSEGV")
        self.assertIn("SIGSEGV", exc["value"])
        self.assertEqual(exc["mechanism"]["type"], "minidump")
        self.assertFalse(exc["mechanism"]["handled"])

    def test_crashing_frame(self):
        data = self._load_fixture()
        event = minidump_to_event(data)

        exc = event["exception"]["values"][0]
        frame = exc["stacktrace"]["frames"][0]
        self.assertEqual(frame["instruction_addr"], "0x400100")
        self.assertEqual(frame["image_addr"], "0x400000")
        self.assertEqual(frame["package"], "/usr/bin/test_app")

    def test_threads(self):
        data = self._load_fixture()
        event = minidump_to_event(data)

        threads = event["threads"]["values"]
        self.assertEqual(len(threads), 2)

        crashed = [t for t in threads if t["crashed"]]
        self.assertEqual(len(crashed), 1)
        self.assertEqual(crashed[0]["id"], "100")

    def test_debug_images(self):
        data = self._load_fixture()
        event = minidump_to_event(data)

        images = event["debug_meta"]["images"]
        self.assertEqual(len(images), 1)
        self.assertEqual(images[0]["type"], "elf")
        self.assertEqual(images[0]["image_addr"], "0x400000")
        self.assertEqual(images[0]["image_size"], 0x10000)
        self.assertEqual(
            images[0]["debug_id"], "01020304-0506-0708-090a-0b0c0d0e0f10-1"
        )

    def test_sentry_metadata_merged(self):
        data = self._load_fixture()
        event = minidump_to_event(
            data, {"release": "2.0.0", "environment": "staging", "tags": {"a": "b"}}
        )

        self.assertEqual(event["release"], "2.0.0")
        self.assertEqual(event["environment"], "staging")
        self.assertEqual(event["tags"], {"a": "b"})

    def test_no_sentry_metadata(self):
        data = self._load_fixture()
        event = minidump_to_event(data)

        self.assertNotIn("release", event)
        self.assertNotIn("environment", event)


class RealBreakpadMinidumpTest(TestCase):
    """Tests using a real minidump from Google Breakpad's test suite.

    This validates our parser against a minidump produced by a real crash
    (null pointer dereference on Linux x86_64), not a synthetic fixture.
    """

    def _load_fixture(self):
        with open(
            "apps/event_ingest/tests/test_data/breakpad_linux_null_deref.dmp", "rb"
        ) as f:
            return f.read()

    def test_parses_without_error(self):
        """Real Breakpad minidump parses successfully."""
        data = self._load_fixture()
        event = minidump_to_event(data)
        self.assertEqual(event["platform"], "native")
        self.assertEqual(event["level"], "fatal")

    def test_exception_is_sigsegv(self):
        data = self._load_fixture()
        event = minidump_to_event(data)

        exc = event["exception"]["values"][0]
        self.assertEqual(exc["type"], "SIGSEGV")
        self.assertIn("0x0", exc["value"])
        self.assertEqual(exc["mechanism"]["type"], "minidump")
        self.assertFalse(exc["mechanism"]["handled"])

    def test_crashing_frame_at_address_zero(self):
        """Crash IP of 0x0 (null pointer jump) still produces a frame."""
        data = self._load_fixture()
        event = minidump_to_event(data)

        exc = event["exception"]["values"][0]
        self.assertIn("stacktrace", exc)
        frame = exc["stacktrace"]["frames"][0]
        self.assertEqual(frame["instruction_addr"], "0x0")

    def test_system_info(self):
        data = self._load_fixture()
        event = minidump_to_event(data)

        self.assertEqual(event["contexts"]["os"]["name"], "Linux")
        self.assertEqual(event["contexts"]["device"]["arch"], "x86_64")

    def test_debug_images_have_debug_ids(self):
        """Real modules produce debug images with debug_ids."""
        data = self._load_fixture()
        event = minidump_to_event(data)

        images = event["debug_meta"]["images"]
        self.assertGreater(len(images), 0)
        # All real modules should have ELF type on Linux
        for img in images:
            self.assertEqual(img["type"], "elf")
            self.assertIn("image_addr", img)
            self.assertIn("image_size", img)
            self.assertIn("code_file", img)
        # At least some should have debug_ids from CvRecords
        with_debug_id = [img for img in images if "debug_id" in img]
        self.assertGreater(len(with_debug_id), 0)

    def test_thread_marked_as_crashed(self):
        data = self._load_fixture()
        event = minidump_to_event(data)

        threads = event["threads"]["values"]
        crashed = [t for t in threads if t["crashed"]]
        self.assertEqual(len(crashed), 1)


class MinidumpViewTest(EventIngestTestCase):
    """Integration tests for the minidump HTTP endpoint."""

    def setUp(self):
        super().setUp()
        cache.clear()
        self.url = reverse("minidump", args=[self.project.id]) + self.params
        with open(
            "apps/event_ingest/tests/test_data/minidump_linux_x86_64.dmp", "rb"
        ) as f:
            self.minidump_data = f.read()

    def test_upload_minidump(self):
        """Successful minidump upload creates an event."""
        from django.core.files.uploadedfile import SimpleUploadedFile

        uploaded = SimpleUploadedFile(
            "crash.dmp", self.minidump_data, content_type="application/octet-stream"
        )
        res = self.client.post(
            self.url,
            {"upload_file_minidump": uploaded, "sentry": '{"release":"1.0.0"}'},
        )
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertIn("id", data)
        # Verify it's a valid hex UUID
        uuid.UUID(data["id"])

        task_backends["default"].flush_batches()

        event = IssueEvent.objects.first()
        self.assertIsNotNone(event)
        self.assertEqual(event.data["platform"], "native")
        exc = event.data["exception"]["values"][0]
        self.assertEqual(exc["type"], "SIGSEGV")
        # Release is stored as a FK, not in event.data
        self.assertTrue(Release.objects.filter(version="1.0.0").exists())

    def test_upload_minidump_gzipped(self):
        """A gzip Content-Encoded multipart upload is decompressed in Rust.

        With DecompressBodyMiddleware gone, the view decompresses the body via
        gt_rust before Django parses request.FILES.
        """
        uploaded = SimpleUploadedFile(
            "crash.dmp", self.minidump_data, content_type="application/octet-stream"
        )
        body = encode_multipart(
            BOUNDARY,
            {"upload_file_minidump": uploaded, "sentry": '{"release":"1.0.0"}'},
        )
        res = self.client.generic(
            "POST",
            self.url,
            data=gzip.compress(body),
            content_type=MULTIPART_CONTENT,
            HTTP_CONTENT_ENCODING="gzip",
        )
        self.assertEqual(res.status_code, 200)
        uuid.UUID(res.json()["id"])
        task_backends["default"].flush_batches()
        event = IssueEvent.objects.first()
        self.assertIsNotNone(event)
        self.assertEqual(event.data["platform"], "native")

    def test_missing_file(self):
        """Request without upload_file_minidump returns 400."""
        res = self.client.post(self.url, {"sentry": "{}"})
        self.assertEqual(res.status_code, 400)
        self.assertIn("Missing", res.json()["detail"])

    def test_invalid_magic(self):
        """Non-minidump file returns 400."""
        from django.core.files.uploadedfile import SimpleUploadedFile

        uploaded = SimpleUploadedFile(
            "crash.dmp", b"NOT_A_MINIDUMP_FILE", content_type="application/octet-stream"
        )
        res = self.client.post(self.url, {"upload_file_minidump": uploaded})
        self.assertEqual(res.status_code, 400)
        self.assertIn("Invalid minidump", res.json()["detail"])

    def test_bad_sentry_key(self):
        """Invalid sentry_key returns 403."""
        url = (
            reverse("minidump", args=[self.project.id])
            + "?sentry_key=00000000000000000000000000000000"
        )
        from django.core.files.uploadedfile import SimpleUploadedFile

        uploaded = SimpleUploadedFile(
            "crash.dmp", self.minidump_data, content_type="application/octet-stream"
        )
        res = self.client.post(url, {"upload_file_minidump": uploaded})
        self.assertEqual(res.status_code, 403)

    def test_method_not_allowed(self):
        """GET returns 405."""
        res = self.client.get(self.url)
        self.assertEqual(res.status_code, 405)
