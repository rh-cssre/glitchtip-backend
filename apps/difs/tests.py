import codecs
import contextlib
import gzip
import json
import tempfile
import zipfile
from hashlib import sha1
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from asgiref.sync import async_to_sync
from django.core.cache import cache
from django.core.files import File as DjangoFile
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from gt_rust.symbolic import SymCache
from model_bakery import baker
from pydantic import ValidationError

from apps.difs.api import _PROGUARD_SNIFF_BYTES, extract_proguard_metadata
from apps.difs.models import DebugInformationFile
from apps.difs.stacktrace_processor import (
    MAX_BASE_PROBES,
    MAX_SCORED_ADDRS,
    StacktraceProcessor,
    _estimate_image_base,
    adjusted_frame_indexes,
    bundle_member_name,
    digest_symbol,
    find_source_bundle,
    jvm_module_to_path,
    read_source_files,
)
from apps.difs.tasks import (
    ChecksumMismatched,
    difs_create_file_from_chunks,
    resolve_event_stacktraces,
)
from apps.event_ingest.schema import ErrorIssueEventSchema, NativeDebugImage
from apps.event_ingest.tests.symbolication import (
    ELF_DEBUG_ID,
    IN_MEMORY_STORAGE,
    NATIVE_FIXTURES,
    PROGUARD_FIXTURES,
    store_blob,
)
from apps.files.models import File, FileBlob
from apps.symbolication.models import CacheKind
from glitchtip.test_utils import generators  # noqa: F401
from glitchtip.test_utils.test_case import GlitchTestCase, GlitchTipTransactionTestCase


class DebugInformationFileModelTestCase(GlitchTestCase):
    def test_is_proguard(self):
        dif = baker.make("difs.DebugInformationFile")

        self.assertEqual(dif.is_proguard_mapping(), False)

        dif = baker.make("difs.DebugInformationFile", data={"symbol_type": "proguard"})
        self.assertEqual(dif.is_proguard_mapping(), True)


class ExtractProguardMetadataTestCase(GlitchTestCase):
    def test_accepts_utf8_mapping_content(self):
        metadata = extract_proguard_metadata(b"com.example.Foo -> a.b.c:\n")
        self.assertEqual(metadata, {"arch": "any", "feature": "mapping"})

    def test_accepts_mapping_with_header_comments(self):
        content = (
            b"# compiler: R8\n"
            b"# compiler_version: 8.0.40\n"
            b"# min_api: 21\n"
            b"com.example.Foo -> a.b.c:\n"
            b"    int field -> a\n"
        )
        metadata = extract_proguard_metadata(content)
        self.assertEqual(metadata, {"arch": "any", "feature": "mapping"})

    def test_accepts_empty_content(self):
        # Nothing to reject: an empty mapping also builds an empty
        # ProguardCache, it just remaps nothing.
        metadata = extract_proguard_metadata(b"")
        self.assertEqual(metadata, {"arch": "any", "feature": "mapping"})

    def test_accepts_only_header_comments(self):
        # No class records at all -- same "nothing to reject" situation as
        # empty content.
        metadata = extract_proguard_metadata(b"# compiler: R8\n# min_api: 21\n")
        self.assertEqual(metadata, {"arch": "any", "feature": "mapping"})

    def test_rejects_non_utf8_binary(self):
        metadata = extract_proguard_metadata(b"\xff\xfe\x00\x01")
        self.assertIsNone(metadata)

    def test_rejects_plain_text(self):
        # The mapping parser would accept this too (it skips any line it
        # cannot parse as a mapping record rather than erroring), so this is
        # a deliberate tightening at upload.
        metadata = extract_proguard_metadata(
            b"hello world, this is not a proguard mapping at all\njust some prose.\n"
        )
        self.assertIsNone(metadata)

    def test_rejects_csv_like_content(self):
        metadata = extract_proguard_metadata(b"a,b,c\n1,2,3\n")
        self.assertIsNone(metadata)

    def test_rejects_member_line_without_class_header(self):
        # A member record can never legally appear before its class record.
        metadata = extract_proguard_metadata(b"    int field -> a\n")
        self.assertIsNone(metadata)

    def test_accepts_content_with_bom(self):
        content = codecs.BOM_UTF8 + b"com.example.Foo -> a.b.c:\n"
        metadata = extract_proguard_metadata(content)
        self.assertEqual(metadata, {"arch": "any", "feature": "mapping"})

    def test_ignores_non_utf8_bytes_past_the_sniff_window(self):
        # Only the first _PROGUARD_SNIFF_BYTES are inspected -- a valid class
        # record within that window is enough, regardless of what a huge real
        # mapping file contains further down (checked in full when its
        # ProguardCache is built, on first use).
        content = b"com.example.Foo -> a.b.c:\n" + b"\xff" * (2 * _PROGUARD_SNIFF_BYTES)
        metadata = extract_proguard_metadata(content)
        self.assertEqual(metadata, {"arch": "any", "feature": "mapping"})


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
        # Assemble outcomes are remembered in the cache, per checksum.
        cache.clear()
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
            f"/api/0/projects/{cls.organization.slug}/{cls.project.slug}/files/dsyms/"
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
        uploaded_zip_file.open.return_value.__enter__.return_value = proguard_file

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

    async def test_post_invalid_proguard_content(self):
        """Non-UTF8 binary content inside proguard/<uuid>.txt should be rejected."""
        proguard_file = MagicMock()
        proguard_file.read.return_value = b"\xff\xfe\x00\x01"

        uploaded_zip_file = MagicMock()
        uploaded_zip_file.namelist.return_value = iter([f"proguard/{self.uuid}.txt"])
        uploaded_zip_file.open.return_value.__enter__.return_value = proguard_file

        upload_file = SimpleUploadedFile(
            "example.zip", b"random_content", content_type="multipart/form-data"
        )
        data = {"file": upload_file}

        with (
            patch("zipfile.is_zipfile", return_value=True),
            patch("zipfile.ZipFile") as ZipFile,
        ):
            ZipFile.return_value.__enter__.return_value = uploaded_zip_file
            response = await self.async_client.post(self.url, data)

        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            response.json(), {"detail": "Invalid proguard mapping file uploaded"}
        )

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


class IOSSymbolicationTestCase(GlitchTipTransactionTestCase):
    def setUp(self):
        self.create_logged_in_user()

    def create_source_bundle(
        self, debug_id, source_code, file_path="/Users/test/ContentView.swift"
    ):
        manifest = {
            "files": {
                bundle_member_name(file_path): {
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
            zipf.writestr(bundle_member_name(file_path), source_code)

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

        found_bundle = async_to_sync(find_source_bundle)(self.project.id, debug_id)

        self.assertIsNotNone(found_bundle)
        self.assertEqual(found_bundle.id, dif.id)
        self.assertEqual(found_bundle.data["kind"], "src")
        self.assertEqual(found_bundle.data["debug_id"], debug_id)

    def test_find_source_bundle_not_found(self):
        debug_id = "00000000-0000-0000-0000-000000000000"

        found_bundle = async_to_sync(find_source_bundle)(self.project.id, debug_id)

        self.assertIsNone(found_bundle)

    def test_find_source_bundle_wrong_project(self):
        debug_id = "93ec5160-1d69-3227-8410-c2687fce4ea2"
        other_project = baker.make("projects.Project", organization=self.organization)

        self.create_source_bundle(debug_id, "test code")

        found_bundle = async_to_sync(find_source_bundle)(other_project.id, debug_id)

        self.assertIsNone(found_bundle)

    def test_read_source_files_from_bundle(self):
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

        lines = async_to_sync(read_source_files)(dif, [file_path]).get(file_path)

        self.assertIsNotNone(lines)
        self.assertEqual(len(lines), 7)
        self.assertEqual(lines[0], "import SwiftUI")
        self.assertEqual(lines[2], "struct ContentView: View {")
        self.assertEqual(lines[4], '        Text("Hello")')

    def test_read_source_files_finds_a_windows_path(self):
        """A PDB records C:\\ paths; the bundle writer stores them with the
        drive colon and backslashes folded into "/"."""
        path = "C:\\src\\app\\crash.cpp"
        dif = self.create_source_bundle(
            "93ec5160-1d69-3227-8410-c2687fce4ea2",
            "int main() {\n  crash();\n}\n",
            path,
        )

        lines = async_to_sync(read_source_files)(dif, [path]).get(path)

        self.assertEqual(lines, ["int main() {", "  crash();", "}"])

    def test_read_source_files_skips_a_missing_file(self):
        debug_id = "93ec5160-1d69-3227-8410-c2687fce4ea2"
        dif = self.create_source_bundle(debug_id, "test code")

        lines = async_to_sync(read_source_files)(dif, ["/NonExistent.swift"]).get(
            "/NonExistent.swift"
        )

        self.assertIsNone(lines)


class BundleMemberNameTestCase(GlitchTestCase):
    def test_names_files_as_the_bundle_writer_does(self):
        """The examples symbolic's own SourceBundleWriter tests pin."""
        for path, member in [
            ("foo", "files/foo"),
            ("foo/bar", "files/foo/bar"),
            ("/foo/bar", "files/foo/bar"),
            ("C:/foo/bar", "files/C/foo/bar"),
            ("\\foo\\bar", "files/foo/bar"),
            ("\\\\UNC\\foo\\bar", "files/UNC/foo/bar"),
            ("/_/_/com/example/MyClass.jvm", "files/_/_/com/example/MyClass.jvm"),
        ]:
            with self.subTest(path=path):
                self.assertEqual(bundle_member_name(path), member)


class JvmSourceContextTestCase(GlitchTipTransactionTestCase):
    def setUp(self):
        self.create_logged_in_user()

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
        found = async_to_sync(find_source_bundle)(self.project.id, debug_id)
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

        result = async_to_sync(StacktraceProcessor.resolve_jvm_source_context)(
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
        result = async_to_sync(StacktraceProcessor.resolve_jvm_source_context)(
            event_json, self.project.id, [debug_id]
        )
        self.assertFalse(result)


class FakeSymCache:
    """Stand-in for symbolic.SymCache with the same lookup() contract.

    lookup() returns a plain list of matches - empty when nothing resolves -
    and the underlying FFI takes an unsigned address, so a negative offset
    raises rather than returning no match.
    """

    def __init__(self, valid_offsets, span=1):
        # Each offset resolves within ``span`` bytes either side, so a caller
        # frame's return address minus one still lands on it.
        self.valid_offsets = set(valid_offsets)
        self.span = span
        self.lookups = []

    def lookup(self, addr):
        self.lookups.append(addr)
        if not 0 <= addr <= 0xFFFFFFFFFFFFFFFF:
            raise OverflowError("can't convert negative number to unsigned")
        hits = [o for o in self.valid_offsets if o - self.span < addr < o + self.span]
        if not hits:
            return []
        location = MagicMock()
        # A distinct function per offset, as in a real symbol table.
        location.symbol = f"function_{hits[0]:x}"
        location.full_path = "/lib/main.dart"
        location.line = 68
        location.lang = "unknown"
        return [location]


class EstimateImageBaseTestCase(GlitchTestCase):
    """Test image base estimation for frames without image_addr."""

    def test_estimates_base_from_absolute_addresses(self):
        sym_cache = FakeSymCache([0x2000, 0x4000, 0x6000])

        base = _estimate_image_base(sym_cache, [0x402000, 0x404000, 0x406000])

        self.assertEqual(base, 0x400000)

    def test_skips_addresses_below_the_candidate_base(self):
        """A frame below the probe must not be looked up at a negative offset."""
        sym_cache = FakeSymCache([0x1000, 0x5000])

        _estimate_image_base(sym_cache, [0x105000, 0x101000])

        self.assertTrue(all(addr >= 0 for addr in sym_cache.lookups))

    def test_scores_a_bounded_sample_of_addresses(self):
        """The event payload decides the frame count, so scoring is bounded."""
        sym_cache = FakeSymCache([0x1000])
        addrs = [0x101000 + (i * 0x10000) for i in range(500)]

        _estimate_image_base(sym_cache, addrs)

        # Per probe, one lookup per candidate base plus one per scored
        # address; never a walk over all 500 frames.
        candidate_bases = 0x500000 // 0x1000
        self.assertLessEqual(
            len(sym_cache.lookups),
            MAX_BASE_PROBES * (candidate_bases + MAX_SCORED_ADDRS),
        )


class EstimateImageBaseAnchorTestCase(GlitchTestCase):
    def test_base_is_page_aligned_when_frames_are_not(self):
        """Frames sit mid-page; the base found is the page boundary below."""
        sym_cache = FakeSymCache([0x1165, 0x118C, 0x11A6])

        base = _estimate_image_base(sym_cache, [0x5555555551A6, 0x55555555518C])

        self.assertEqual(base, 0x555555554000)

    def test_a_foreign_first_frame_does_not_anchor_the_search(self):
        sym_cache = FakeSymCache([0x1165, 0x118C, 0x11A6])

        base = _estimate_image_base(
            sym_cache, [0x7FFFF7A21B40, 0x5555555551A6, 0x55555555518C]
        )

        self.assertEqual(base, 0x555555554000)

    def test_no_base_when_nothing_fits(self):
        """Better no symbols than wrong ones."""
        sym_cache = FakeSymCache([0x1000])

        self.assertIsNone(_estimate_image_base(sym_cache, [0x7F0000, 0x9F0000]))
        self.assertIsNone(_estimate_image_base(sym_cache, [0x101000]))


class EstimateImageBaseAmbiguityTestCase(GlitchTestCase):
    def test_refuses_when_bases_tie(self):
        """In a dense symbol table many bases resolve every frame to some
        function: with no clear best there is no answer."""

        class Dense(FakeSymCache):
            def lookup(self, addr):
                self.lookups.append(addr)
                location = MagicMock()
                location.symbol = f"function_{addr // 0x10:x}"
                return [location]

        base = _estimate_image_base(Dense([]), [0x402468, 0x40ABCD, 0x413579])

        self.assertIsNone(base)

    def test_frames_on_one_function_are_not_enough(self):
        """Below an image, every address can resolve to its last symbol."""

        class LastSymbolForever(FakeSymCache):
            def lookup(self, addr):
                self.lookups.append(addr)
                location = MagicMock()
                location.symbol = "_fini"
                return [location]

        base = _estimate_image_base(
            LastSymbolForever([]), [0x5555555551A6, 0x55555555518C]
        )

        self.assertIsNone(base)

    def test_stops_after_the_first_anchor_that_fits(self):
        sym_cache = FakeSymCache([0x1165, 0x118C, 0x11A6])
        addrs = [0x5555555551A6, 0x55555555518C, 0x555555555165, 0x7FFFF7A21B40]

        self.assertEqual(_estimate_image_base(sym_cache, addrs), 0x555555554000)

        # One anchor's candidates, not all four anchors'.
        self.assertLess(len(sym_cache.lookups), 2 * (0x500000 // 0x1000))


class EstimateImageBaseFlutterTestCase(GlitchTestCase):
    """A real Flutter (Dart AOT) crash, sent without debug_meta."""

    FIXTURES = (
        Path(__file__).parent.parent
        / "event_ingest/tests/test_data/symbolication/flutter"
    )
    TRUE_BASE = 0x75FE1EC1A000

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        with tempfile.NamedTemporaryFile(suffix=".symbols") as symbols:
            symbols.write(
                gzip.decompress(
                    (cls.FIXTURES / "app.android-x64.symbols.gz").read_bytes()
                )
            )
            symbols.flush()
            cls.sym_cache = SymCache.load(SymCache.build(symbols.name), verify=True)
        cls.addrs = json.loads((cls.FIXTURES / "stack_addrs.json").read_text())

    def test_a_dense_symbol_table_gives_no_estimate(self):
        """Dart AOT symbols are so dense that the neighbouring page-aligned
        bases resolve all eleven frames as well as the true one does: there
        is no telling them apart, so no guess."""
        self.assertIsNone(_estimate_image_base(self.sym_cache, self.addrs))
        # The true base is among the equally good ones.
        at_true_base = [self.sym_cache.lookup(a - self.TRUE_BASE) for a in self.addrs]
        self.assertTrue(all(at_true_base))

    def test_the_frames_resolve_with_the_sdk_base(self):
        """With image_addr from debug_meta, as the SDK sends it when it can,
        the frames resolve."""
        stacktrace = {"frames": [{"instruction_addr": hex(a)} for a in self.addrs]}
        result = StacktraceProcessor.resolve_native_stacktrace(
            stacktrace,
            self.sym_cache,
            arch="x86_64",
            image_addr_from_meta=hex(self.TRUE_BASE),
        )
        self.assertEqual(result.score, len(self.addrs))
        self.assertIn(
            "_Timer._handleMessage", [frame["function"] for frame in result.frames]
        )


class NativeSymbolicationTestCase(GlitchTestCase):
    """Test native stacktrace symbolication via StacktraceProcessor."""

    def test_resolve_frames_without_image_addr(self):
        """Flutter on Android sends instruction_addr with no image_addr/debug_meta."""
        sym_cache = FakeSymCache([0x2000, 0x4000], span=0x10)

        stacktrace = {
            "frames": [
                {"instruction_addr": "0x402000", "function": "bK"},
                {"instruction_addr": "0x404000", "function": "bL"},
            ]
        }

        sym_cache.arch = "arm64"

        result = StacktraceProcessor.resolve_native_stacktrace(
            stacktrace, sym_cache, arch="arm64"
        )

        self.assertIsNotNone(result)
        self.assertEqual(result.score, 2)
        self.assertTrue(all(frame["resolved"] for frame in result.frames))
        self.assertEqual(result.frames[0]["filename"], "/lib/main.dart")

    def test_frame_outside_the_image_does_not_drop_the_stacktrace(self):
        """One frame below the base must not cost every other frame its symbol."""
        sym_cache = FakeSymCache([0x2000, 0x4000], span=0x10)

        stacktrace = {
            "frames": [
                {"instruction_addr": "0x402000"},
                {"instruction_addr": "0x404000"},
                # Below the estimated base, so instruction_addr - base is
                # negative.
                {"instruction_addr": "0x1000"},
                # No instruction_addr at all: parse_addr reports 0.
                {"function": "<unknown>"},
            ]
        }

        sym_cache.arch = "arm64"

        result = StacktraceProcessor.resolve_native_stacktrace(
            stacktrace, sym_cache, arch="arm64"
        )

        self.assertIsNotNone(result)
        self.assertEqual(result.score, 2)
        self.assertEqual(len(result.frames), 4)
        self.assertNotIn("resolved", result.frames[2])
        self.assertNotIn("resolved", result.frames[3])

    def test_resolve_with_mismatched_function_names(self):
        """Obfuscated function names (e.g. Flutter --obfuscate) should still resolve."""
        mock_symbol = MagicMock()
        mock_symbol.symbol = "_MyHomePageState._incrementCounter"
        mock_symbol.full_path = "/lib/main.dart"
        mock_symbol.line = 68
        mock_symbol.lang = "unknown"

        mock_sym_cache = MagicMock()
        mock_sym_cache.lookup.return_value = [mock_symbol]

        stacktrace = {
            "frames": [
                {
                    "instruction_addr": "0x20d9a0",
                    "image_addr": "0x0",
                    "function": "bK",  # obfuscated name
                }
            ]
        }

        mock_sym_cache.arch = "x86_64"

        result = StacktraceProcessor.resolve_native_stacktrace(
            stacktrace, mock_sym_cache, arch="x86_64"
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

    def test_debug_meta_image_addr_used_as_base(self):
        """image_addr from debug_meta is used as the base, not a guess."""
        correct_symbol = MagicMock()
        correct_symbol.symbol = "NetworkManager.handleError"
        correct_symbol.full_path = "/lib/network.dart"
        correct_symbol.line = 156
        correct_symbol.lang = "dart"

        def lookup(addr):
            # base is 0x100000 (from debug_meta), so 0x100ABC - 0x100000 = 0xABC
            return [correct_symbol] if addr == 0xABC else []

        mock_sym_cache = MagicMock()
        mock_sym_cache.lookup.side_effect = lookup

        event = {
            "contexts": {"os": {"name": "Android"}, "device": {"arch": "x86_64"}},
            "debug_meta": {
                "images": [
                    {
                        "type": "elf",
                        "debug_id": "DF398B02-1681-3B54-8FA5-B205E1ECFD7E",
                        "image_addr": "0x100000",
                    }
                ]
            },
            "exception": {
                "values": [
                    {
                        "type": "Exception",
                        "value": "network error",
                        "stacktrace": {
                            "frames": [
                                {
                                    "instruction_addr": "0x100abc",
                                    "function": "bK",  # obfuscated
                                }
                            ]
                        },
                    }
                ]
            },
        }

        mock_sym_cache.arch = "x86_64"

        result = StacktraceProcessor.resolve_stacktrace(
            event,
            mock_sym_cache,
            debug_id="df398b02-1681-3b54-8fa5-b205e1ecfd7e",
        )

        self.assertIsNotNone(result)
        self.assertEqual(result.score, 1)
        self.assertTrue(result.frames[0]["resolved"])
        self.assertEqual(result.frames[0]["function"], "NetworkManager.handleError")
        self.assertEqual(result.frames[0]["filename"], "/lib/network.dart")
        self.assertEqual(result.frames[0]["lineno"], 156)

    def test_debug_meta_image_addr_ignored_for_mismatched_debug_id(self):
        """image_addr is only used from the image matching this DIF's debug_id."""
        raw_symbol = MagicMock()
        raw_symbol.symbol = "raw_function"
        raw_symbol.full_path = "/lib/raw.dart"
        raw_symbol.line = 1
        raw_symbol.lang = "dart"

        mock_sym_cache = MagicMock()
        # only resolves at 0x100ABC, i.e. when base stayed 0 (meta was ignored)
        mock_sym_cache.lookup.side_effect = lambda addr: (
            [raw_symbol] if addr == 0x100ABC else []
        )

        event = {
            "contexts": {"os": {"name": "Android"}, "device": {"arch": "x86_64"}},
            "debug_meta": {
                "images": [
                    {
                        "type": "elf",
                        # different debug_id than the DIF we're resolving
                        "debug_id": "AAAAAAAA-BBBB-CCCC-DDDD-EEEEEEEEEEEE",
                        "image_addr": "0x100000",
                    }
                ]
            },
            "exception": {
                "values": [
                    {"stacktrace": {"frames": [{"instruction_addr": "0x100abc"}]}}
                ]
            },
        }

        mock_sym_cache.arch = "x86_64"

        result = StacktraceProcessor.resolve_stacktrace(
            event,
            mock_sym_cache,
            debug_id="df398b02-1681-3b54-8fa5-b205e1ecfd7e",
        )

        # The other image's base (0x100000) was not applied: no lookup at
        # 0xABC. With one frame there is too little to estimate a base from,
        # so the frame stays unresolved rather than guessed.
        looked_up = [call.args[0] for call in mock_sym_cache.lookup.call_args_list]
        self.assertNotIn(0xABC, looked_up)
        self.assertIsNotNone(result)
        self.assertEqual(result.score, 0)

    def test_unparseable_debug_meta_image_addr_falls_back_to_estimation(self):
        """A garbage image_addr must not pin the base at 0 and skip estimation."""
        sym_cache = FakeSymCache([0x2000, 0x4000], span=0x10)

        event = {
            "contexts": {"os": {"name": "Android"}, "device": {"arch": "x86_64"}},
            "debug_meta": {
                "images": [
                    {
                        "type": "elf",
                        "debug_id": "DF398B02-1681-3B54-8FA5-B205E1ECFD7E",
                        "image_addr": "garbage",
                    }
                ]
            },
            "exception": {
                "values": [
                    {
                        "stacktrace": {
                            "frames": [
                                {"instruction_addr": "0x402000"},
                                {"instruction_addr": "0x404000"},
                            ]
                        }
                    }
                ]
            },
        }

        sym_cache.arch = "x86_64"

        result = StacktraceProcessor.resolve_stacktrace(
            event,
            sym_cache,
            debug_id="df398b02-1681-3b54-8fa5-b205e1ecfd7e",
        )

        # base 0x400000 recovered by estimation, both frames resolve
        self.assertIsNotNone(result)
        self.assertEqual(result.score, 2)


class ReturnAddressTestCase(GlitchTestCase):
    def test_caller_frames_are_looked_up_one_byte_back(self):
        """Callers report the address after their call; the innermost frame
        (the last) reports the faulting instruction itself."""
        sym_cache = FakeSymCache([0x1FFF, 0x3000])
        sym_cache.arch = "x86_64"
        stacktrace = {
            "frames": [
                {"instruction_addr": "0x402000", "image_addr": "0x400000"},
                {"instruction_addr": "0x403000", "image_addr": "0x400000"},
            ]
        }

        result = StacktraceProcessor.resolve_native_stacktrace(
            stacktrace, sym_cache, arch="x86_64"
        )

        self.assertEqual(sym_cache.lookups, [0x1FFF, 0x3000])
        self.assertEqual(result.score, 2)


class InstructionAddrAdjustmentTestCase(GlitchTestCase):
    """instruction_addr_adjustment decides which frames are looked up one byte
    back. Frames are oldest first, so the crashing frame is the last."""

    FRAMES = [
        {"instruction_addr": "0x401000"},
        {"instruction_addr": "0x402000"},
        {"instruction_addr": "0x403000"},
    ]

    def adjusted(self, stacktrace, exception=None):
        return adjusted_frame_indexes({"frames": self.FRAMES, **stacktrace}, exception)

    def signal_exception(self):
        return {
            "mechanism": {"type": "signalhandler", "meta": {"signal": {"number": 11}}}
        }

    def test_all(self):
        self.assertEqual(
            self.adjusted({"instruction_addr_adjustment": "all"}), {0, 1, 2}
        )

    def test_all_but_first_leaves_the_crashing_frame(self):
        self.assertEqual(
            self.adjusted({"instruction_addr_adjustment": "all_but_first"}), {0, 1}
        )

    def test_none_as_libunwindstack_sends(self):
        self.assertEqual(self.adjusted({"instruction_addr_adjustment": "none"}), set())

    def test_absent_or_auto_is_all_but_first(self):
        self.assertEqual(self.adjusted({}), {0, 1})
        self.assertEqual(self.adjusted({"instruction_addr_adjustment": "auto"}), {0, 1})

    def test_auto_with_a_signal_and_a_mismatched_pc_is_all(self):
        """The crashing frame came from unwinding, not from the registers, so
        it is a return address too."""
        stacktrace = {"registers": {"pc": "0x409999"}}
        self.assertEqual(self.adjusted(stacktrace, self.signal_exception()), {0, 1, 2})
        self.assertEqual(
            self.adjusted({"registers": {"rip": "0x409999"}}, self.signal_exception()),
            {0, 1, 2},
        )

    def test_auto_with_a_signal_and_a_matching_pc_is_all_but_first(self):
        stacktrace = {"registers": {"pc": "0x403000"}}
        self.assertEqual(self.adjusted(stacktrace, self.signal_exception()), {0, 1})

    def test_auto_needs_both_a_signal_and_registers(self):
        self.assertEqual(
            self.adjusted(
                {"registers": {"pc": "0x409999"}}, {"mechanism": {"type": "x"}}
            ),
            {0, 1},
        )
        self.assertEqual(self.adjusted({}, self.signal_exception()), {0, 1})

    def test_resolution_follows_the_mode(self):
        sym_cache = FakeSymCache([0x1000, 0x2000, 0x3000])
        sym_cache.arch = "x86_64"
        frames = [{**frame, "image_addr": "0x400000"} for frame in self.FRAMES]
        StacktraceProcessor.resolve_native_stacktrace(
            {"frames": frames, "instruction_addr_adjustment": "none"},
            sym_cache,
            arch="x86_64",
        )
        self.assertEqual(sym_cache.lookups, [0x1000, 0x2000, 0x3000])
        sym_cache.lookups.clear()
        StacktraceProcessor.resolve_native_stacktrace(
            {"frames": frames, "instruction_addr_adjustment": "all"},
            sym_cache,
            arch="x86_64",
        )
        self.assertEqual(sym_cache.lookups, [0xFFF, 0x1FFF, 0x2FFF])


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
            StacktraceProcessor.resolve_stacktrace(event, MagicMock())
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
            StacktraceProcessor.resolve_stacktrace(event, MagicMock())
            mock_proguard.assert_called_once()
            mock_native.assert_not_called()


class DifTypeFilteringTestCase(GlitchTipTransactionTestCase):
    """Test that resolve_event_stacktraces filters DIFs by type at the DB level."""

    def setUp(self):
        self.create_logged_in_user()

    def test_source_bundles_excluded_from_native_loop(self):
        """Source bundles (kind='src'/'sources') should never enter the native/proguard loop."""
        # Create a source bundle DIF — should be excluded
        baker.make(
            "difs.DebugInformationFile",
            project=self.project,
            file__blob=baker.make("files.FileBlob"),
            data={"kind": "sources", "debug_id": "aaa"},
        )
        # Create a native DIF — should be excluded for Android events
        baker.make(
            "difs.DebugInformationFile",
            project=self.project,
            file__blob=baker.make("files.FileBlob"),
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

        with patch(
            "apps.difs.tasks.get_cache", new=AsyncMock(return_value=None)
        ) as mock_get_cache:
            async_to_sync(resolve_event_stacktraces)(event, self.project.id)
            # Android event should only try proguard DIFs — neither the source
            # bundle nor the native DIF should cause a cache lookup.
            mock_get_cache.assert_not_called()

    def test_proguard_excluded_for_non_android(self):
        """Proguard DIFs should be excluded for non-Android events."""
        baker.make(
            "difs.DebugInformationFile",
            project=self.project,
            file__blob=baker.make("files.FileBlob"),
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

        with patch(
            "apps.difs.tasks.get_cache", new=AsyncMock(return_value=None)
        ) as mock_get_cache:
            async_to_sync(resolve_event_stacktraces)(event, self.project.id)
            # Non-Android event should exclude proguard DIFs — no cache lookup.
            mock_get_cache.assert_not_called()

    def test_native_difs_filtered_by_debug_id(self):
        """When event has native debug images, only matching DIFs should be tried."""
        matching_id = "df398b02-1681-3b54-8fa5-b205e1ecfd7e"
        non_matching_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"

        baker.make(
            "difs.DebugInformationFile",
            project=self.project,
            file__blob=baker.make("files.FileBlob"),
            data={
                "kind": "debug",
                "symbol_type": "native",
                "debug_id": matching_id,
            },
        )
        baker.make(
            "difs.DebugInformationFile",
            project=self.project,
            file__blob=baker.make("files.FileBlob"),
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

        with patch(
            "apps.difs.tasks.get_cache", new=AsyncMock(return_value=None)
        ) as mock_get_cache:
            async_to_sync(resolve_event_stacktraces)(event, self.project.id)
            # Should only try the matching DIF (1 call), not both
            self.assertEqual(mock_get_cache.call_count, 1)

    def test_android_native_frames_query_native_difs(self):
        """Android events with native frames should query native DIFs, not proguard."""
        # Create both proguard and native DIFs
        baker.make(
            "difs.DebugInformationFile",
            project=self.project,
            file__blob=baker.make("files.FileBlob"),
            data={"kind": "debug", "symbol_type": "proguard", "debug_id": "aaa"},
        )
        native = baker.make(
            "difs.DebugInformationFile",
            project=self.project,
            file__blob=baker.make("files.FileBlob"),
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

        with patch(
            "apps.difs.tasks.get_cache", new=AsyncMock(return_value=None)
        ) as mock_get_cache:
            async_to_sync(resolve_event_stacktraces)(event, self.project.id)
            # Only the native DIF, as a SymCache: not the ProGuard mapping.
            [call] = mock_get_cache.call_args_list
            source = call.args[0]
            self.assertEqual(source.kind, CacheKind.SYMCACHE)
            self.assertEqual(source.source_blob_id, native.file.blob_id)


class _LocatingSymCache:
    """A SymCache stand-in resolving every address to one function, with or
    without a source file."""

    arch = "x86_64"

    def __init__(self, full_path):
        self.full_path = full_path

    def lookup(self, addr):
        location = MagicMock()
        location.symbol = "crash_here"
        location.full_path = self.full_path
        location.line = 7 if self.full_path else 0
        return [location]


class ResultRankingTestCase(GlitchTipTransactionTestCase):
    def setUp(self):
        self.create_logged_in_user()

    def test_equal_scores_go_to_the_result_with_source_files(self):
        """Two debug files resolve every frame; the one that also names
        source files wins, though the other (newer) is tried first."""
        with_sources = baker.make(
            "difs.DebugInformationFile",
            project=self.project,
            file__blob=baker.make("files.FileBlob"),
            data={"kind": "dbg", "symbol_type": "native", "debug_id": "a" * 32},
        )
        symbols_only = baker.make(
            "difs.DebugInformationFile",
            project=self.project,
            file__blob=baker.make("files.FileBlob"),
            data={"kind": "lib", "symbol_type": "native", "debug_id": "b" * 32},
        )
        caches = {
            with_sources.file.blob_id: _LocatingSymCache("/src/crash.cpp"),
            symbols_only.file.blob_id: _LocatingSymCache(None),
        }
        event = ErrorIssueEventSchema(
            platform="native",
            exception={
                "values": [
                    {
                        "type": "SIGSEGV",
                        "value": "Segfault",
                        "stacktrace": {
                            "frames": [
                                {"instruction_addr": "0x1165", "image_addr": "0x0"},
                                {"instruction_addr": "0x118c", "image_addr": "0x0"},
                            ]
                        },
                    }
                ]
            },
            contexts={"os": {"name": "Linux"}, "device": {"arch": "x86_64"}},
        )

        with patch(
            "apps.difs.tasks.get_cache",
            new=AsyncMock(
                side_effect=lambda source, context: caches[source.source_blob_id]
            ),
        ) as mock_get_cache:
            async_to_sync(resolve_event_stacktraces)(event, self.project.id)

        tried = [call.args[0].source_blob_id for call in mock_get_cache.call_args_list]
        self.assertEqual(tried, [symbols_only.file.blob_id, with_sources.file.blob_id])
        frames = event.exception.values[0].stacktrace.frames
        self.assertEqual([frame.filename for frame in frames], ["/src/crash.cpp"] * 2)


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
        and keep a non-zero appendix: for a PDB it is the age, part of the
        identity that uploaded debug files are stored under."""
        base = "114f8cb943bc5bf52c409cabe92c0924"
        dashed = "114f8cb9-43bc-5bf5-2c40-9cabe92c0924"
        for appendix, expected in [
            ("0", dashed),
            ("1", f"{dashed}-1"),
            ("9", f"{dashed}-9"),
            ("a", f"{dashed}-a"),
            ("f", f"{dashed}-f"),
        ]:
            image = NativeDebugImage(type="wasm", debug_id=base + appendix)
            self.assertEqual(image.debug_id, expected)

    def test_a_malformed_known_image_is_not_reported_as_unknown(self):
        from apps.event_ingest.schema import DebugMeta

        with patch("apps.event_ingest.schema.logger") as logger:
            DebugMeta.model_validate(
                {"images": [{"type": "elf", "debug_id": "garbage"}, {"type": "new"}]}
            )
        self.assertEqual(logger.warning.call_count, 1)
        self.assertEqual(logger.warning.call_args.args[1], "new")

    def test_native_debug_image_still_rejects_garbage(self):
        """Genuinely invalid debug_id should still fail validation as before."""
        with self.assertRaises(ValidationError):
            NativeDebugImage(type="wasm", debug_id="not-a-debug-id-at-all")


@IN_MEMORY_STORAGE
class ChunkedDifAssembleTestCase(GlitchTipTransactionTestCase):
    """Upload tools send ProGuard mappings, like native debug files, as
    chunks followed by an assemble request."""

    MAPPING_UUID = "20fce343-c2d0-3343-9688-e32c516c7297"

    def setUp(self):
        cache.clear()
        self.create_logged_in_user()
        self.url = reverse(
            "api:difs_assemble_api", args=[self.organization.slug, self.project.slug]
        )

    def assemble(self, content: bytes, name: str, debug_id=None):
        blob = store_blob(name, content)
        entry = {"name": name, "chunks": [blob.checksum]}
        if debug_id:
            entry["debug_id"] = debug_id
        response = self.client.post(
            self.url, {blob.checksum: entry}, content_type="application/json"
        )
        self.assertEqual(response.status_code, 200)
        return blob.checksum, response.json()[blob.checksum]

    def test_a_proguard_mapping_is_stored_like_a_dsyms_upload(self):
        mapping = (PROGUARD_FIXTURES / "mapping.txt").read_bytes()
        _, first = self.assemble(mapping, "mapping.txt", self.MAPPING_UUID)
        self.assertEqual(first["state"], "created")
        dif = DebugInformationFile.objects.get(project=self.project)
        self.assertEqual(
            dif.data,
            {
                "arch": "any",
                "debug_id": self.MAPPING_UUID,
                "symbol_type": "proguard",
                "features": ["mapping"],
            },
        )
        # The upload tool's next poll sees it done.
        _, again = self.assemble(mapping, "mapping.txt", self.MAPPING_UUID)
        self.assertEqual(again["state"], "ok")

    def test_the_uuid_can_come_from_the_file_name(self):
        mapping = (PROGUARD_FIXTURES / "mapping.txt").read_bytes()
        self.assemble(mapping, f"proguard/{self.MAPPING_UUID}.txt")
        dif = DebugInformationFile.objects.get(project=self.project)
        self.assertEqual(dif.data["debug_id"], self.MAPPING_UUID)

    def test_an_unsupported_file_is_reported_as_an_error(self):
        """Not a debug file nor a mapping: the next poll reports an error,
        so the upload tool does not claim success."""
        content = b"not a debug file at all"
        _, first = self.assemble(content, "junk.bin", self.MAPPING_UUID)
        self.assertEqual(first["state"], "created")
        self.assertFalse(DebugInformationFile.objects.exists())
        _, again = self.assemble(content, "junk.bin", self.MAPPING_UUID)
        self.assertEqual(again["state"], "error")
        self.assertIn("not a supported debug file", again["detail"])

    def test_a_mapping_without_a_uuid_is_an_error(self):
        mapping = (PROGUARD_FIXTURES / "mapping.txt").read_bytes()
        self.assemble(mapping, "mapping.txt")
        _, again = self.assemble(mapping, "mapping.txt")
        self.assertEqual(again["state"], "error")

    def test_native_debug_files_still_assemble(self):
        elf = (NATIVE_FIXTURES / "crash.elf").read_bytes()
        _, first = self.assemble(elf, "crash.elf", ELF_DEBUG_ID)
        self.assertEqual(first["state"], "created")
        dif = DebugInformationFile.objects.get(project=self.project)
        self.assertEqual(dif.data["symbol_type"], "native")
        self.assertEqual(dif.data["debug_id"], ELF_DEBUG_ID)
