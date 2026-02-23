import contextlib
import json
import tempfile
import zipfile
from hashlib import sha1
from unittest.mock import MagicMock, patch

from django.core.files import File as DjangoFile
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from model_bakery import baker

from apps.difs.stacktrace_processor import (
    digest_symbol,
    extract_source_from_bundle,
    find_source_bundle,
)
from apps.difs.tasks import ChecksumMismatched, difs_create_file_from_chunks
from apps.files.models import File
from glitchtip.test_utils import generators  # noqa: F401
from glitchtip.test_utils.test_case import GlitchTestCase


class DebugInformationFileModelTestCase(GlitchTestCase):
    def test_is_proguard(self):
        dif = baker.make("difs.DebugInformationFile")

        self.assertEqual(dif.is_proguard_mapping(), False)

        dif = baker.make("difs.DebugInformationFile", data={"symbol_type": "proguard"})
        self.assertEqual(dif.is_proguard_mapping(), True)


class DifsAssembleAPITestCase(GlitchTestCase):
    @classmethod
    def setUpTestData(cls):
        cls.create_user()
        cls.url = reverse(
            "api:difs_assemble_api", args=[cls.organization.slug, cls.project.slug]
        )
        cls.checksum = "0892b6a9469438d9e5ffbf2807759cd689996271"
        cls.chunks = [
            "efa73a85c44d64e995ade0cc3286ea47cfc49c36",
            "966e44663054d6c1f38d04c6ff4af83467659bd7",
        ]
        cls.data = {
            cls.checksum: {
                "name": "test",
                "debug_id": "a959d2e6-e4e5-303e-b508-670eb84b392c",
                "chunks": cls.chunks,
            }
        }

    def setUp(self):
        self.client.force_login(self.user)

    def test_difs_assemble_with_dif_existed(self):
        file = baker.make("files.File", checksum=self.checksum)
        baker.make(
            "difs.DebugInformationFile",
            project=self.project,
            file=file,
        )

        expected_response = {self.checksum: {"state": "ok", "missingChunks": []}}

        response = self.client.post(
            self.url, self.data, content_type="application/json"
        )
        self.assertEqual(response.json(), expected_response)

    def test_difs_assemble_with_missing_chunks(self):
        baker.make("files.FileBlob", checksum=self.chunks[0])

        data = {
            self.checksum: {
                "name": "test",
                "debug_id": "a959d2e6-e4e5-303e-b508-670eb84b392c",
                "chunks": self.chunks,
            }
        }

        expected_response = {
            self.checksum: {"state": "not_found", "missingChunks": [self.chunks[1]]}
        }

        response = self.client.post(self.url, data, content_type="application/json")
        self.assertEqual(response.json(), expected_response)

    def test_difs_assemble_without_missing_chunks(self):
        for chunk in self.chunks:
            baker.make("files.FileBlob", checksum=chunk)

        expected_response = {self.checksum: {"state": "created", "missingChunks": []}}

        response = self.client.post(
            self.url, self.data, content_type="application/json"
        )
        self.assertEqual(response.json(), expected_response)


class DsymsAPIViewTestCase(GlitchTestCase):
    @classmethod
    def setUpTestData(cls):
        cls.create_user()
        cls.url = (
            f"/api/0/projects/{cls.organization.slug}/{cls.project.slug}/files/dsyms/"  # noqa
        )
        cls.uuid = "afb116cf-efec-49af-a7fe-281ac680d8a0"
        cls.checksum = "da39a3ee5e6b4b0d3255bfef95601890afd80709"

    def setUp(self):
        self.client.force_login(self.user)

    @contextlib.contextmanager
    def patch(self):
        proguard_file = MagicMock()
        proguard_file.read.return_value = b""

        uploaded_zip_file = MagicMock()
        uploaded_zip_file.namelist.return_value = iter([f"proguard/{self.uuid}.txt"])
        uploaded_zip_file.open.return_value.__enter__.return_value = proguard_file  # noqa

        with (
            patch("zipfile.is_zipfile", return_value=True),
            patch("zipfile.ZipFile") as ZipFile,
        ):
            ZipFile.return_value.__enter__.return_value = uploaded_zip_file
            yield

    def test_post(self):
        """
        It should return the expected response
        """
        upload_file = SimpleUploadedFile(
            "example.zip", b"random_content", content_type="multipart/form-data"
        )
        data = {"file": upload_file}

        with self.patch():
            response = self.client.post(self.url, data)

        expected_response = [
            {
                "id": response.json()[0]["id"],
                "debugId": self.uuid,
                "cpuName": "any",
                "objectName": "proguard-mapping",
                "symbolType": "proguard",
                "headers": {"Content-Type": "text/x-proguard+plain"},
                "size": 0,
                "sha1": self.checksum,
                "dateCreated": response.json()[0]["dateCreated"],
                "data": {"features": ["mapping"]},
            }
        ]

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()), 1)
        self.assertEqual(response.json(), expected_response)

    def test_post_existing_file(self):
        """
        It should success and return the expected response
        """

        baker.make("files.FileBlob", checksum=self.checksum)

        fileobj = baker.make("files.File", checksum=self.checksum)

        dif = baker.make(
            "difs.DebugInformationFile", file=fileobj, project=self.project
        )

        upload_file = SimpleUploadedFile(
            "example.zip", b"random_content", content_type="multipart/form-data"
        )
        data = {"file": upload_file}

        with self.patch():
            response = self.client.post(self.url, data)

        expected_response = [
            {
                "id": dif.id,
                "debugId": self.uuid,
                "cpuName": "any",
                "objectName": "proguard-mapping",
                "symbolType": "proguard",
                "headers": {"Content-Type": "text/x-proguard+plain"},
                "size": 0,
                "sha1": "da39a3ee5e6b4b0d3255bfef95601890afd80709",
                "dateCreated": response.json()[0]["dateCreated"],
                "data": {"features": ["mapping"]},
            }
        ]

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()), 1)
        self.assertEqual(response.json(), expected_response)

    def test_post_invalid_zip_file(self):
        upload_file = SimpleUploadedFile(
            "example.zip", b"random_content", content_type="multipart/form-data"
        )
        data = {"file": upload_file}
        response = self.client.post(self.url, data)

        expected_response = {"detail": "Invalid file type uploaded"}

        self.assertEqual(response.json(), expected_response)
        self.assertEqual(response.status_code, 400)


class DifsTasksTestCase(GlitchTestCase):
    @classmethod
    def setUpTestData(cls):
        cls.create_user()

    def setUp(self):
        self.client.force_login(self.user)

    def create_file_blob(self, name, content):
        bin = content.encode("utf-8")
        tmp = tempfile.NamedTemporaryFile()
        tmp.write(bin)
        tmp.flush()

        checksum = sha1(bin).hexdigest()
        fileblob = baker.make("files.FileBlob", checksum=checksum)
        fileblob.blob.save(name, DjangoFile(tmp))
        tmp.close()

        return fileblob

    def test_difs_create_file_from_chunks(self):
        fileblob1 = self.create_file_blob("1", "1")
        fileblob2 = self.create_file_blob("2", "2")
        checksum = sha1(b"12").hexdigest()
        chunks = [fileblob1.checksum, fileblob2.checksum]
        difs_create_file_from_chunks("12", checksum, chunks)
        file = File.objects.filter(checksum=checksum).first()
        self.assertEqual(file.checksum, checksum)

    def test_difs_create_file_from_chunks_with_mismatched_checksum(self):
        fileblob1 = self.create_file_blob("1", "1")
        fileblob2 = self.create_file_blob("2", "2")
        checksum = sha1(b"123").hexdigest()
        chunks = [fileblob1.checksum, fileblob2.checksum]
        with self.assertRaises(ChecksumMismatched):
            difs_create_file_from_chunks("123", checksum, chunks)


class IOSSymbolicationTestCase(GlitchTestCase):
    @classmethod
    def setUpTestData(cls):
        cls.create_user()

    def setUp(self):
        self.client.force_login(self.user)

    def create_source_bundle(
        self, debug_id, source_code, file_path="/Users/test/ContentView.swift"
    ):
        manifest = {
            "files": {
                f"files{file_path}": {
                    "type": "source",
                    "path": file_path,
                }
            },
            "arch": "arm64",
            "code_id": "testcodeid123",
            "debug_id": debug_id,
            "object_name": "TestApp.debug.dylib",
        }

        in_memory_buffer = tempfile.NamedTemporaryFile(delete=False)
        with zipfile.ZipFile(in_memory_buffer, mode="w") as zipf:
            zipf.writestr("manifest.json", json.dumps(manifest))
            zipf.writestr(f"files{file_path}", source_code)

        in_memory_buffer.seek(0)
        content = in_memory_buffer.read()
        checksum = sha1(content).hexdigest()

        fileblob = baker.make("files.FileBlob", checksum=checksum)
        in_memory_buffer.seek(0)
        fileblob.blob.save("source_bundle.zip", DjangoFile(in_memory_buffer))
        in_memory_buffer.close()

        file = baker.make("files.File", checksum=checksum, blob=fileblob)

        dif = baker.make(
            "difs.DebugInformationFile",
            project=self.project,
            file=file,
            data={
                "kind": "src",
                "debug_id": debug_id,
                "arch": "arm64",
                "symbol_type": "native",
                "features": ["sources"],
            },
        )
        return dif

    def test_digest_symbol_accepts_lang_unknown(self):
        """Test that we do not reject symbols with lang = unkown"""
        mock_symbol = MagicMock()
        mock_symbol.lang = "unknown"
        mock_symbol.symbol = "+[SentrySDKInternal captureError:]"
        mock_symbol.full_path = None
        mock_symbol.line = 0

        result = digest_symbol([mock_symbol])

        self.assertIsNotNone(result)
        self.assertEqual(result.symbol, "+[SentrySDKInternal captureError:]")

    def test_find_source_bundle_by_debug_id(self):
        debug_id = "93ec5160-1d69-3227-8410-c2687fce4ea2"
        source_code = "import SwiftUI\n\nstruct ContentView: View {}\n"

        dif = self.create_source_bundle(debug_id, source_code)

        found_bundle = find_source_bundle(self.project.id, debug_id)

        self.assertIsNotNone(found_bundle)
        self.assertEqual(found_bundle.id, dif.id)
        self.assertEqual(found_bundle.data["kind"], "src")
        self.assertEqual(found_bundle.data["debug_id"], debug_id)

    def test_find_source_bundle_not_found(self):
        debug_id = "00000000-0000-0000-0000-000000000000"

        found_bundle = find_source_bundle(self.project.id, debug_id)

        self.assertIsNone(found_bundle)

    def test_find_source_bundle_wrong_project(self):
        debug_id = "93ec5160-1d69-3227-8410-c2687fce4ea2"
        other_project = baker.make("projects.Project", organization=self.organization)

        self.create_source_bundle(debug_id, "test code")

        found_bundle = find_source_bundle(other_project.id, debug_id)

        self.assertIsNone(found_bundle)

    def test_extract_source_from_bundle(self):
        debug_id = "93ec5160-1d69-3227-8410-c2687fce4ea2"
        source_code = """import SwiftUI

struct ContentView: View {
    var body: some View {
        Text("Hello")
    }
}
"""
        file_path = "/Users/test/ContentView.swift"
        dif = self.create_source_bundle(debug_id, source_code, file_path)

        lines = extract_source_from_bundle(dif, file_path)

        self.assertIsNotNone(lines)
        self.assertEqual(len(lines), 7)
        self.assertEqual(lines[0], "import SwiftUI")
        self.assertEqual(lines[2], "struct ContentView: View {")
        self.assertEqual(lines[4], '        Text("Hello")')

    def test_extract_source_from_bundle_nonexistent_file(self):
        debug_id = "93ec5160-1d69-3227-8410-c2687fce4ea2"
        dif = self.create_source_bundle(debug_id, "test code")

        lines = extract_source_from_bundle(dif, "/NonExistent.swift")

        self.assertIsNone(lines)
