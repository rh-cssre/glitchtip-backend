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
from pydantic import ValidationError

from apps.difs.stacktrace_processor import (
    StacktraceProcessor,
    digest_symbol,
    extract_source_from_bundle,
    find_source_bundle,
    jvm_module_to_path,
)
from apps.difs.tasks import ChecksumMismatched, difs_create_file_from_chunks
from apps.event_ingest.schema import NativeDebugImage
from apps.files.models import File, FileBlob
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
        self.async_client.force_login(self.user)

    async def test_difs_assemble_with_dif_existed(self):
        file = await baker.amake("files.File", checksum=self.checksum)
        await baker.amake(
            "difs.DebugInformationFile",
            project=self.project,
            file=file,
        )

        expected_response = {self.checksum: {"state": "ok", "missingChunks": []}}

        response = await self.async_client.post(
            self.url, self.data, content_type="application/json"
        )
        self.assertEqual(response.json(), expected_response)

    async def test_difs_assemble_with_missing_chunks(self):
        await baker.amake("files.FileBlob", checksum=self.chunks[0])

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

        response = await self.async_client.post(
            self.url, data, content_type="application/json"
        )
        self.assertEqual(response.json(), expected_response)

    async def test_difs_assemble_without_missing_chunks(self):
        for chunk in self.chunks:
            await baker.amake("files.FileBlob", checksum=chunk)

        expected_response = {self.checksum: {"state": "created", "missingChunks": []}}

        response = await self.async_client.post(
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
        self.async_client.force_login(self.user)

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

    async def test_post(self):
        """
        It should return the expected response
        """
        upload_file = SimpleUploadedFile(
            "example.zip", b"random_content", content_type="multipart/form-data"
        )
        data = {"file": upload_file}

        with self.patch():
            response = await self.async_client.post(self.url, data)

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

    async def test_post_existing_file(self):
        """
        It should success and return the expected response
        """

        await baker.amake("files.FileBlob", checksum=self.checksum)

        fileobj = await baker.amake("files.File", checksum=self.checksum)

        dif = await baker.amake(
            "difs.DebugInformationFile", file=fileobj, project=self.project
        )

        upload_file = SimpleUploadedFile(
            "example.zip", b"random_content", content_type="multipart/form-data"
        )
        data = {"file": upload_file}

        with self.patch():
            response = await self.async_client.post(self.url, data)

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

    async def test_post_invalid_zip_file(self):
        upload_file = SimpleUploadedFile(
            "example.zip", b"random_content", content_type="multipart/form-data"
        )
        data = {"file": upload_file}
        response = await self.async_client.post(self.url, data)

        expected_response = {"detail": "Invalid file type uploaded"}

        self.assertEqual(response.json(), expected_response)
        self.assertEqual(response.status_code, 400)

    async def test_get(self):
        """
        It should return a list of debug information files
        """
        fileobj = await baker.amake("files.File", checksum=self.checksum, size=1234)
        dif = await baker.amake(
            "difs.DebugInformationFile",
            project=self.project,
            file=fileobj,
            name="mapping.txt",
            data={
                "debug_id": self.uuid,
                "symbol_type": "proguard",
                "arch": "any",
                "features": ["mapping"],
            },
        )

        response = await self.async_client.get(self.url)
        self.assertEqual(response.status_code, 200)

        res_data = response.json()
        self.assertEqual(len(res_data), 1)
        self.assertEqual(res_data[0]["id"], str(dif.id))
        self.assertEqual(res_data[0]["uuid"], self.uuid)
        self.assertEqual(res_data[0]["debugId"], self.uuid)
        self.assertEqual(res_data[0]["cpuName"], "any")
        self.assertEqual(res_data[0]["objectName"], "mapping.txt")
        self.assertEqual(res_data[0]["symbolType"], "proguard")
        self.assertEqual(res_data[0]["size"], 1234)
        self.assertEqual(res_data[0]["sha1"], self.checksum)
        self.assertEqual(res_data[0]["data"], {"features": ["mapping"]})
        self.assertEqual(res_data[0]["headers"], {})
        # Verify it's a valid ISO date
        self.assertTrue(res_data[0]["dateCreated"].endswith("Z"))


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

    def test_difs_create_file_from_chunks_assembles_content(self):
        # A multi-chunk file is concatenated into a single combined blob whose
        # content is the whole file, not just the first chunk.
        fileblob1 = self.create_file_blob("1", "aaa")
        fileblob2 = self.create_file_blob("2", "bbb")
        checksum = sha1(b"aaabbb").hexdigest()
        chunks = [fileblob1.checksum, fileblob2.checksum]
        difs_create_file_from_chunks("ab", checksum, chunks)
        file = File.objects.filter(checksum=checksum).first()
        self.assertEqual(file.checksum, checksum)
        self.assertEqual(file.size, 6)
        with file.blob.blob.open("rb") as f:
            self.assertEqual(f.read(), b"aaabbb")

    def test_difs_create_file_from_chunks_respects_chunk_order(self):
        # Reassembly must follow the request's chunk order, not the arbitrary
        # order FileBlob.objects.filter(checksum__in=...) returns.
        fileblob1 = self.create_file_blob("1", "aaa")
        fileblob2 = self.create_file_blob("2", "bbb")
        checksum = sha1(b"bbbaaa").hexdigest()
        chunks = [fileblob2.checksum, fileblob1.checksum]
        difs_create_file_from_chunks("ba", checksum, chunks)
        file = File.objects.filter(checksum=checksum).first()
        with file.blob.blob.open("rb") as f:
            self.assertEqual(f.read(), b"bbbaaa")

    def test_difs_create_file_from_chunks_single_chunk_reuses_blob(self):
        # A single-chunk file reuses the uploaded blob without storing a copy.
        fileblob = self.create_file_blob("1", "solo")
        checksum = fileblob.checksum
        difs_create_file_from_chunks("solo", checksum, [fileblob.checksum])
        file = File.objects.filter(checksum=checksum).first()
        self.assertEqual(file.blob_id, fileblob.id)
        self.assertEqual(FileBlob.objects.filter(checksum=checksum).count(), 1)

    def test_difs_create_file_from_chunks_missing_chunk(self):
        fileblob = self.create_file_blob("1", "aaa")
        checksum = sha1(b"aaabbb").hexdigest()
        chunks = [fileblob.checksum, sha1(b"bbb").hexdigest()]
        with self.assertRaises(ChecksumMismatched):
            difs_create_file_from_chunks("ab", checksum, chunks)

    def test_difs_create_file_from_chunks_duplicate_chunk(self):
        # A file whose two chunks have identical content references the same
        # blob twice. The blob must be emitted once per chunk-list entry (so the
        # content is duplicated), not once per distinct checksum.
        fileblob = self.create_file_blob("1", "aa")
        checksum = sha1(b"aaaa").hexdigest()
        chunks = [fileblob.checksum, fileblob.checksum]
        difs_create_file_from_chunks("aa", checksum, chunks)
        file = File.objects.filter(checksum=checksum).first()
        self.assertEqual(file.size, 4)
        with file.blob.blob.open("rb") as f:
            self.assertEqual(f.read(), b"aaaa")

    def test_difs_create_file_from_chunks_is_idempotent(self):
        # Assembling the same multi-chunk file twice must not create a second
        # combined blob; the get_or_create keyed on the whole-file checksum
        # collapses the repeat onto the existing blob.
        fileblob1 = self.create_file_blob("1", "aaa")
        fileblob2 = self.create_file_blob("2", "bbb")
        checksum = sha1(b"aaabbb").hexdigest()
        chunks = [fileblob1.checksum, fileblob2.checksum]
        difs_create_file_from_chunks("ab", checksum, chunks)
        difs_create_file_from_chunks("ab", checksum, chunks)
        self.assertEqual(FileBlob.objects.filter(checksum=checksum).count(), 1)


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


class JvmSourceContextTestCase(GlitchTestCase):
    @classmethod
    def setUpTestData(cls):
        cls.create_user()

    def test_jvm_module_to_path_standard(self):
        result = jvm_module_to_path("com.example.MyClass", "MyClass.java")
        self.assertEqual(result, "/_/_/com/example/MyClass.jvm")

    def test_jvm_module_to_path_inner_class(self):
        result = jvm_module_to_path("com.example.MyClass$Inner", "MyClass.java")
        self.assertEqual(result, "/_/_/com/example/MyClass.jvm")

    def test_jvm_module_to_path_kotlin(self):
        result = jvm_module_to_path("com.example.MainKt", "Main.kt")
        self.assertEqual(result, "/_/_/com/example/Main.jvm")

    def test_jvm_module_to_path_no_package(self):
        result = jvm_module_to_path("Main", "Main.java")
        self.assertEqual(result, "/_/_/Main.jvm")

    def test_jvm_module_to_path_missing_module(self):
        result = jvm_module_to_path(None, "Main.java")
        self.assertIsNone(result)

    def test_jvm_module_to_path_missing_filename(self):
        result = jvm_module_to_path("com.example.MyClass", None)
        self.assertIsNone(result)

    def test_find_source_bundle_kind_sources(self):
        """Verify find_source_bundle finds DIFs with kind='sources' (real uploads)."""
        debug_id = "a959d2e6-e4e5-303e-b508-670eb84b392c"
        dif = baker.make(
            "difs.DebugInformationFile",
            project=self.project,
            data={"kind": "sources", "debug_id": debug_id},
        )
        found = find_source_bundle(self.project.id, debug_id)
        self.assertIsNotNone(found)
        self.assertEqual(found.id, dif.id)

    def create_jvm_source_bundle(self, debug_id, source_code, file_path):
        """Create a source bundle ZIP with a Java/Kotlin source file."""
        manifest = {
            "files": {
                f"files{file_path}": {
                    "type": "source",
                    "path": file_path,
                }
            },
            "debug_id": debug_id,
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
        fileblob.blob.save("jvm_source_bundle.zip", DjangoFile(in_memory_buffer))
        in_memory_buffer.close()

        file = baker.make("files.File", checksum=checksum, blob=fileblob)
        return baker.make(
            "difs.DebugInformationFile",
            project=self.project,
            file=file,
            data={
                "kind": "sources",
                "debug_id": debug_id,
            },
        )

    def test_resolve_jvm_source_context(self):
        debug_id = "b1c2d3e4-f5a6-7890-abcd-ef1234567890"
        source_code = (
            "package com.example;\n"
            "\n"
            "public class MyClass {\n"
            "    public void doSomething() {\n"
            '        throw new RuntimeException("test");\n'
            "    }\n"
            "}\n"
        )
        # Use the real sentry-cli bundle path format: _/_/ prefix, .jvm extension
        self.create_jvm_source_bundle(
            debug_id, source_code, "/_/_/com/example/MyClass.jvm"
        )

        event_json = {
            "exception": {
                "values": [
                    {
                        "type": "RuntimeException",
                        "value": "test",
                        "stacktrace": {
                            "frames": [
                                {
                                    "module": "com.example.MyClass",
                                    "filename": "MyClass.java",
                                    "function": "doSomething",
                                    "lineno": 5,
                                    "in_app": True,
                                }
                            ]
                        },
                    }
                ]
            }
        }

        result = StacktraceProcessor.resolve_jvm_source_context(
            event_json, self.project.id, [debug_id]
        )

        self.assertTrue(result)
        frame = event_json["exception"]["values"][0]["stacktrace"]["frames"][0]
        self.assertEqual(
            frame["context_line"],
            '        throw new RuntimeException("test");',
        )
        self.assertEqual(len(frame["pre_context"]), 4)
        self.assertEqual(frame["pre_context"][0], "package com.example;")
        self.assertEqual(len(frame["post_context"]), 2)

    def test_resolve_jvm_source_context_skips_existing(self):
        """Frames with context_line already set should be skipped."""
        debug_id = "b1c2d3e4-f5a6-7890-abcd-ef1234567890"
        self.create_jvm_source_bundle(
            debug_id, "line1\nline2\n", "/_/_/com/example/MyClass.jvm"
        )
        event_json = {
            "exception": {
                "values": [
                    {
                        "stacktrace": {
                            "frames": [
                                {
                                    "module": "com.example.MyClass",
                                    "filename": "MyClass.java",
                                    "lineno": 1,
                                    "context_line": "already set",
                                }
                            ]
                        }
                    }
                ]
            }
        }
        result = StacktraceProcessor.resolve_jvm_source_context(
            event_json, self.project.id, [debug_id]
        )
        self.assertFalse(result)


class NativeSymbolicationTestCase(GlitchTestCase):
    """Test native stacktrace symbolication via StacktraceProcessor."""

    def test_resolve_with_mismatched_function_names(self):
        """Obfuscated function names (e.g. Flutter --obfuscate) should still resolve."""
        mock_symbol = MagicMock()
        mock_symbol.symbol = "_MyHomePageState._incrementCounter"
        mock_symbol.full_path = "/lib/main.dart"
        mock_symbol.line = 68
        mock_symbol.lang = "unknown"

        mock_sym_cache = MagicMock()
        mock_sym_cache.lookup.return_value = [mock_symbol]

        mock_obj = MagicMock()
        mock_obj.arch = "x86_64"

        mock_archive = MagicMock()
        mock_archive.get_object.return_value = mock_obj

        stacktrace = {
            "frames": [
                {
                    "instruction_addr": "0x20d9a0",
                    "image_addr": "0x0",
                    "function": "bK",  # obfuscated name
                }
            ]
        }

        with (
            patch("apps.difs.stacktrace_processor.Archive") as MockArchive,
            patch("apps.difs.stacktrace_processor.SymCache") as MockSymCache,
        ):
            MockArchive.open.return_value = mock_archive
            MockSymCache.from_object.return_value = mock_sym_cache

            result = StacktraceProcessor.resolve_native_stacktrace(
                stacktrace, "/fake/symbols.elf", arch="x86_64"
            )

        self.assertIsNotNone(result)
        self.assertEqual(result.score, 1)
        self.assertEqual(
            result.frames[0]["function"],
            "_MyHomePageState._incrementCounter",
        )
        self.assertEqual(result.frames[0]["filename"], "/lib/main.dart")
        self.assertEqual(result.frames[0]["lineno"], 68)
        self.assertTrue(result.frames[0]["resolved"])


class HasNativeFramesTestCase(GlitchTestCase):
    """Test frame-based detection of native vs proguard events."""

    def test_native_frames_detected(self):
        event = {
            "exception": {
                "values": [
                    {
                        "stacktrace": {
                            "frames": [
                                {"instruction_addr": "0x20d9a0", "image_addr": "0x0"}
                            ]
                        }
                    }
                ]
            }
        }
        self.assertTrue(StacktraceProcessor.has_native_frames(event))

    def test_jvm_frames_not_native(self):
        event = {
            "exception": {
                "values": [
                    {
                        "stacktrace": {
                            "frames": [
                                {
                                    "module": "com.example.Foo",
                                    "function": "bar",
                                    "lineno": 1,
                                }
                            ]
                        }
                    }
                ]
            }
        }
        self.assertFalse(StacktraceProcessor.has_native_frames(event))

    def test_empty_event(self):
        self.assertFalse(StacktraceProcessor.has_native_frames({}))
        self.assertFalse(StacktraceProcessor.has_native_frames({"exception": None}))

    def test_mixed_frames_detected(self):
        """If even one frame has instruction_addr, treat as native."""
        event = {
            "exception": {
                "values": [
                    {
                        "stacktrace": {
                            "frames": [
                                {"module": "com.example.Foo", "function": "bar"},
                                {"instruction_addr": "0x20d9a0"},
                            ]
                        }
                    }
                ]
            }
        }
        self.assertTrue(StacktraceProcessor.has_native_frames(event))


class ResolverRoutingTestCase(GlitchTestCase):
    """Test that resolve_stacktrace routes to the correct resolver."""

    def test_android_native_frames_use_native_resolver(self):
        """Android events with instruction_addr frames use native symbolication."""
        event = {
            "contexts": {"os": {"name": "Android"}, "device": {"arch": "x86_64"}},
            "exception": {
                "values": [
                    {
                        "stacktrace": {
                            "frames": [
                                {"instruction_addr": "0x20d9a0", "image_addr": "0x0"}
                            ]
                        }
                    }
                ]
            },
        }
        with (
            patch.object(
                StacktraceProcessor, "resolve_native_stacktrace"
            ) as mock_native,
            patch.object(
                StacktraceProcessor, "resolve_proguard_stacktrace"
            ) as mock_proguard,
        ):
            StacktraceProcessor.resolve_stacktrace(event, "/fake/symbol.elf")
            mock_native.assert_called_once()
            mock_proguard.assert_not_called()

    def test_android_jvm_frames_use_proguard_resolver(self):
        """Android events with JVM-style frames use proguard symbolication."""
        event = {
            "contexts": {"os": {"name": "Android"}, "device": {"arch": "arm64"}},
            "exception": {
                "values": [
                    {
                        "stacktrace": {
                            "frames": [
                                {
                                    "module": "com.example.Foo",
                                    "function": "bar",
                                    "lineno": 1,
                                }
                            ]
                        }
                    }
                ]
            },
        }
        with (
            patch.object(
                StacktraceProcessor, "resolve_native_stacktrace"
            ) as mock_native,
            patch.object(
                StacktraceProcessor, "resolve_proguard_stacktrace"
            ) as mock_proguard,
        ):
            StacktraceProcessor.resolve_stacktrace(event, "/fake/mapping.txt")
            mock_proguard.assert_called_once()
            mock_native.assert_not_called()


class DifTypeFilteringTestCase(GlitchTestCase):
    """Test that event_difs_resolve_stacktrace filters DIFs by type at the DB level."""

    @classmethod
    def setUpTestData(cls):
        cls.create_user()

    def test_source_bundles_excluded_from_native_loop(self):
        """Source bundles (kind='src'/'sources') should never enter the native/proguard loop."""
        from apps.difs.tasks import event_difs_resolve_stacktrace
        from apps.event_ingest.schema import ErrorIssueEventSchema

        # Create a source bundle DIF — should be excluded
        baker.make(
            "difs.DebugInformationFile",
            project=self.project,
            data={"kind": "sources", "debug_id": "aaa"},
        )
        # Create a native DIF — should be excluded for Android events
        baker.make(
            "difs.DebugInformationFile",
            project=self.project,
            data={"kind": "debug", "symbol_type": "native"},
        )

        event = ErrorIssueEventSchema(
            platform="java",
            exception={
                "values": [
                    {
                        "type": "RuntimeException",
                        "value": "test",
                        "stacktrace": {
                            "frames": [
                                {
                                    "module": "com.example.Foo",
                                    "filename": "Foo.java",
                                    "function": "bar",
                                    "lineno": 1,
                                    "in_app": True,
                                }
                            ]
                        },
                    }
                ]
            },
            contexts={"os": {"name": "Android"}},
        )

        with patch("apps.difs.tasks.difs_concat_file_blobs_to_disk") as mock_concat:
            event_difs_resolve_stacktrace(event, self.project.id)
            # Android event should only try proguard DIFs — neither the source
            # bundle nor the native DIF should cause a blob download.
            mock_concat.assert_not_called()

    def test_proguard_excluded_for_non_android(self):
        """Proguard DIFs should be excluded for non-Android events."""
        from apps.difs.tasks import event_difs_resolve_stacktrace
        from apps.event_ingest.schema import ErrorIssueEventSchema

        baker.make(
            "difs.DebugInformationFile",
            project=self.project,
            data={"symbol_type": "proguard", "debug_id": "bbb"},
        )

        event = ErrorIssueEventSchema(
            platform="cocoa",
            exception={
                "values": [
                    {
                        "type": "NSError",
                        "value": "test",
                        "stacktrace": {
                            "frames": [
                                {
                                    "function": "foo",
                                    "filename": "bar.m",
                                    "lineno": 1,
                                    "in_app": True,
                                    "image_addr": "0x0",
                                    "instruction_addr": "0x0",
                                }
                            ]
                        },
                    }
                ]
            },
            contexts={"os": {"name": "iOS"}, "device": {"arch": "arm64"}},
        )

        with patch("apps.difs.tasks.difs_concat_file_blobs_to_disk") as mock_concat:
            event_difs_resolve_stacktrace(event, self.project.id)
            # Non-Android event should exclude proguard DIFs — no blob download.
            mock_concat.assert_not_called()

    def test_native_difs_filtered_by_debug_id(self):
        """When event has native debug images, only matching DIFs should be tried."""
        from apps.difs.tasks import event_difs_resolve_stacktrace
        from apps.event_ingest.schema import ErrorIssueEventSchema

        matching_id = "df398b02-1681-3b54-8fa5-b205e1ecfd7e"
        non_matching_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"

        baker.make(
            "difs.DebugInformationFile",
            project=self.project,
            data={
                "kind": "debug",
                "symbol_type": "native",
                "debug_id": matching_id,
            },
        )
        baker.make(
            "difs.DebugInformationFile",
            project=self.project,
            data={
                "kind": "debug",
                "symbol_type": "native",
                "debug_id": non_matching_id,
            },
        )

        event = ErrorIssueEventSchema(
            platform="cocoa",
            exception={
                "values": [
                    {
                        "type": "NSError",
                        "value": "test",
                        "stacktrace": {
                            "frames": [
                                {
                                    "function": "foo",
                                    "filename": "bar.m",
                                    "lineno": 1,
                                    "in_app": True,
                                    "image_addr": "0x0",
                                    "instruction_addr": "0x0",
                                }
                            ]
                        },
                    }
                ]
            },
            contexts={"os": {"name": "iOS"}, "device": {"arch": "arm64"}},
            debug_meta={
                "images": [
                    {
                        "type": "macho",
                        "debug_id": "DF398B02-1681-3B54-8FA5-B205E1ECFD7E",
                        "image_addr": "0x100000",
                    }
                ]
            },
        )

        with patch("apps.difs.tasks.difs_concat_file_blobs_to_disk") as mock_concat:
            event_difs_resolve_stacktrace(event, self.project.id)
            # Should only try the matching DIF (1 call), not both
            self.assertEqual(mock_concat.call_count, 1)

    def test_android_native_frames_query_native_difs(self):
        """Android events with native frames should query native DIFs, not proguard."""
        from apps.difs.tasks import event_difs_resolve_stacktrace
        from apps.event_ingest.schema import ErrorIssueEventSchema

        # Create both proguard and native DIFs
        baker.make(
            "difs.DebugInformationFile",
            project=self.project,
            data={"kind": "debug", "symbol_type": "proguard", "debug_id": "aaa"},
        )
        baker.make(
            "difs.DebugInformationFile",
            project=self.project,
            data={"kind": "debug", "symbol_type": "native", "debug_id": "bbb"},
        )

        event = ErrorIssueEventSchema(
            platform="dart",
            exception={
                "values": [
                    {
                        "type": "Exception",
                        "value": "test",
                        "stacktrace": {
                            "frames": [
                                {
                                    "instruction_addr": "0x20d9a0",
                                    "image_addr": "0x0",
                                    "function": "bK",
                                    "in_app": True,
                                }
                            ]
                        },
                    }
                ]
            },
            contexts={
                "os": {"name": "Android"},
                "device": {"arch": "x86_64"},
            },
        )

        with patch("apps.difs.tasks.difs_concat_file_blobs_to_disk") as mock_concat:
            event_difs_resolve_stacktrace(event, self.project.id)
            # Should try the native DIF (1 call), not the proguard one
            self.assertEqual(mock_concat.call_count, 1)


class NormalizeDebugIdTestCase(GlitchTestCase):
    """Test that normalize_debug_id is applied at all boundaries."""

    @classmethod
    def setUpTestData(cls):
        cls.create_user()

    def test_extract_jvm_debug_ids_normalizes(self):
        from apps.difs.tasks import _extract_jvm_debug_ids
        from apps.event_ingest.schema import ErrorIssueEventSchema

        event = ErrorIssueEventSchema(
            platform="java",
            exception={
                "values": [
                    {
                        "type": "RuntimeException",
                        "value": "test",
                        "stacktrace": {"frames": [{"lineno": 1}]},
                    }
                ]
            },
            debug_meta={
                "images": [
                    {
                        "type": "jvm",
                        "debug_id": "DF398B02-1681-3B54-8FA5-B205E1ECFD7E",
                    }
                ]
            },
        )
        ids = _extract_jvm_debug_ids(event)
        self.assertEqual(ids, ["df398b02-1681-3b54-8fa5-b205e1ecfd7e"])

    def test_extract_native_debug_ids_normalizes(self):
        from apps.difs.tasks import _extract_native_debug_ids
        from apps.event_ingest.schema import ErrorIssueEventSchema

        event = ErrorIssueEventSchema(
            platform="cocoa",
            exception={
                "values": [
                    {
                        "type": "NSError",
                        "value": "test",
                        "stacktrace": {"frames": [{"lineno": 1}]},
                    }
                ]
            },
            debug_meta={
                "images": [
                    {
                        "type": "macho",
                        "debug_id": "DF398B02-1681-3B54-8FA5-B205E1ECFD7E",
                        "image_addr": "0x100000",
                    }
                ]
            },
        )
        ids = _extract_native_debug_ids(event)
        self.assertEqual(ids, ["df398b02-1681-3b54-8fa5-b205e1ecfd7e"])

    def test_native_debug_image_schema(self):
        """NativeDebugImage should parse macho/elf/pe/wasm types with fields."""
        from apps.event_ingest.schema import DebugMeta

        meta = DebugMeta.model_validate(
            {
                "images": [
                    {
                        "type": "macho",
                        "debug_id": "df398b02-1681-3b54-8fa5-b205e1ecfd7e",
                        "image_addr": "0x100000",
                        "image_size": 4096,
                        "code_file": "/usr/lib/libfoo.dylib",
                    },
                    {
                        "type": "elf",
                        "debug_id": "abcdef01-2345-6789-abcd-ef0123456789",
                    },
                    {"type": "unknown_type"},
                ]
            }
        )
        from apps.event_ingest.schema import NativeDebugImage, OtherDebugImage

        self.assertIsInstance(meta.images[0], NativeDebugImage)
        self.assertEqual(meta.images[0].image_addr, "0x100000")
        self.assertIsInstance(meta.images[1], NativeDebugImage)
        self.assertIsInstance(meta.images[2], OtherDebugImage)

    def test_native_debug_image_accepts_breakpad_format(self):
        """33-char Breakpad-style debug_id (32 hex + appendix) should validate,
        regardless of the appendix/age value."""
        base = "114f8cb943bc5bf52c409cabe92c0924"
        for appendix in ["0", "1", "9", "a", "f"]:
            breakpad_id = base + appendix
            image = NativeDebugImage(type="wasm", debug_id=breakpad_id)
            self.assertIsNotNone(image.debug_id)
            self.assertEqual(len(str(image.debug_id).replace("-", "")), 32)

    def test_native_debug_image_still_rejects_garbage(self):
        """Genuinely invalid debug_id should still fail validation as before."""
        with self.assertRaises(ValidationError):
            NativeDebugImage(type="wasm", debug_id="not-a-debug-id-at-all")
