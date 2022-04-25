import contextlib
import tempfile
from hashlib import sha1
from unittest.mock import MagicMock, patch

from django.core.files import File as DjangoFile
from django.core.files.uploadedfile import SimpleUploadedFile
from model_bakery import baker

from difs.tasks import ChecksumMismatched, difs_create_file_from_chunks
from files.models import File
from glitchtip.test_utils.test_case import GlitchTipTestCase


class DebugInformationFileModelTestCase(GlitchTipTestCase):
    def test_is_proguard(self):
        dif = baker.make("difs.DebugInformationFile")

        self.assertEqual(dif.is_proguard_mapping(), False)

        dif = baker.make("difs.DebugInformationFile", data={"symbol_type": "proguard"})
        self.assertEqual(dif.is_proguard_mapping(), True)


class DifsAssembleAPITestCase(GlitchTipTestCase):
    def setUp(self):
        self.create_user_and_project()
        self.url = f"/api/0/projects/{self.organization.slug}/{self.project.slug}/files/difs/assemble/"  # noqa
        self.checksum = "0892b6a9469438d9e5ffbf2807759cd689996271"
        self.chunks = [
            "efa73a85c44d64e995ade0cc3286ea47cfc49c36",
            "966e44663054d6c1f38d04c6ff4af83467659bd7",
        ]
        self.data = {
            self.checksum: {
                "name": "test",
                "debug_id": "a959d2e6-e4e5-303e-b508-670eb84b392c",
                "chunks": self.chunks,
            }
        }

    def test_difs_assemble_with_dif_existed(self):
        file = baker.make("files.File", checksum=self.checksum)
        baker.make(
            "difs.DebugInformationFile",
            project=self.project,
            file=file,
        )

        expected_response = {self.checksum: {"state": "ok", "missingChunks": []}}

        response = self.client.post(self.url, self.data, format="json")
        self.assertEqual(response.data, expected_response)

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

        response = self.client.post(self.url, data, format="json")
        self.assertEqual(response.data, expected_response)

    def test_difs_assemble_without_missing_chunks(self):
        for chunk in self.chunks:
            baker.make("files.FileBlob", checksum=chunk)

        expected_response = {self.checksum: {"state": "created", "missingChunks": []}}

        response = self.client.post(self.url, self.data, format="json")
        self.assertEqual(response.data, expected_response)


class DsymsAPIViewTestCase(GlitchTipTestCase):
    def setUp(self):
        self.create_user_and_project()
        self.url = f"/api/0/projects/{self.organization.slug}/{self.project.slug}/files/dsyms/"  # noqa
        self.uuid = "afb116cf-efec-49af-a7fe-281ac680d8a0"
        self.checksum = "da39a3ee5e6b4b0d3255bfef95601890afd80709"

    @contextlib.contextmanager
    def patch(self):
        proguard_file = MagicMock()
        proguard_file.read.return_value = b""

        uploaded_zip_file = MagicMock()
        uploaded_zip_file.namelist.return_value = iter([f"proguard/{self.uuid}.txt"])
        uploaded_zip_file.open.return_value.__enter__.return_value = (
            proguard_file  # noqa
        )

        with patch("zipfile.is_zipfile", return_value=True), patch(
            "zipfile.ZipFile"
        ) as ZipFile:

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
                "id": response.data[0]["id"],
                "debugId": self.uuid,
                "cpuName": "any",
                "objectName": "proguard-mapping",
                "symbolType": "proguard",
                "headers": {"Content-Type": "text/x-proguard+plain"},
                "size": 0,
                "sha1": self.checksum,
                "dateCreated": response.data[0]["dateCreated"],
                "data": {"features": ["mapping"]},
            }
        ]

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data, expected_response)

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
                "dateCreated": response.data[0]["dateCreated"],
                "data": {"features": ["mapping"]},
            }
        ]

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data, expected_response)

    def test_post_invalid_zip_file(self):
        upload_file = SimpleUploadedFile(
            "example.zip", b"random_content", content_type="multipart/form-data"
        )
        data = {"file": upload_file}
        response = self.client.post(self.url, data)

        expected_response = {"error": "Invalid file type uploaded"}

        self.assertEqual(response.data, expected_response)
        self.assertEqual(response.status_code, 400)


class DifsTasksTestCase(GlitchTipTestCase):
    def setUp(self):
        self.create_user_and_project()

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
