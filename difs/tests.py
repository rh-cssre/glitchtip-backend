import tempfile
from django.core.files import File as DjangoFile
from django.conf import settings
from glitchtip.test_utils.test_case import GlitchTipTestCase
from difs.tasks import (
    difs_create_file_from_chunks,
    difs_get_file_from_chunks,
    difs_concat_file_blobs_to_disk,
    ChecksumMismatched
)
from files.models import File
from model_bakery import baker
from hashlib import sha1


class DifsAssembleAPITestCase(GlitchTipTestCase):
    def setUp(self):
        self.create_user_and_project()
        self.url = f"/api/0/projects/{self.organization.slug}/{self.project.slug}/files/difs/assemble/"  # noqa
        self.checksum = "0892b6a9469438d9e5ffbf2807759cd689996271"
        self.chunks = [
            "efa73a85c44d64e995ade0cc3286ea47cfc49c36",
            "966e44663054d6c1f38d04c6ff4af83467659bd7"
        ]
        self.data = {
            self.checksum: {
                "name": "test",
                "debug_id": "a959d2e6-e4e5-303e-b508-670eb84b392c",
                "chunks": self.chunks
            }
        }
        settings.GLITCHTIP_ENABLE_DIFS = True

    def tearDown(self):
        settings.GLITCHTIP_ENABLE_DIFS = False

    def test_difs_assemble_with_dif_existed(self):
        file = baker.make(
            "files.File",
            checksum=self.checksum
        )
        baker.make(
            "difs.DebugInformationFile",
            project=self.project,
            file=file,
        )

        expected_response = {
            self.checksum: {
                "state": "ok",
                "missingChunks": []
            }
        }

        response = self.client.post(self.url,
                                    self.data,
                                    format='json'
                                    )
        self.assertEqual(response.data, expected_response)

    def test_difs_assemble_with_missing_chunks(self):
        baker.make(
            "files.FileBlob",
            checksum=self.chunks[0]
        )

        data = {
            self.checksum: {
                "name": "test",
                "debug_id": "a959d2e6-e4e5-303e-b508-670eb84b392c",
                "chunks": self.chunks
            }
        }

        expected_response = {
            self.checksum: {
                "state": "not_found",
                "missingChunks": [self.chunks[1]]
            }
        }

        response = self.client.post(self.url,
                                    data,
                                    format='json'
                                    )
        self.assertEqual(response.data, expected_response)

    def test_difs_assemble_without_missing_chunks(self):
        for chunk in self.chunks:
            baker.make("files.FileBlob", checksum=chunk)

        expected_response = {
            self.checksum: {
                "state": "created",
                "missingChunks": []
            }
        }

        response = self.client.post(self.url,
                                    self.data,
                                    format='json'
                                    )
        self.assertEqual(response.data, expected_response)


class DifsTasksTestCase(GlitchTipTestCase):
    def setUp(self):
        self.create_user_and_project()

    def create_file_blob(self, name, content):
        bin = content.encode('utf-8')
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

    def test_difs_get_file_from_chunks(self):
        fileblob1 = self.create_file_blob("1", "1")
        fileblob2 = self.create_file_blob("2", "2")
        checksum = sha1(b"12").hexdigest()
        chunks = [fileblob1.checksum, fileblob2.checksum]
        difs_create_file_from_chunks("12", checksum, chunks)
        file = difs_get_file_from_chunks(checksum, chunks)

        self.assertEqual(file.checksum, checksum)

    def test_difs_concat_file_blobs_to_disk(self):
        fileblob1 = self.create_file_blob("1", "1")
        fileblob2 = self.create_file_blob("2", "2")
        checksum = sha1(b"12").hexdigest()
        chunks = [fileblob1.checksum, fileblob2.checksum]
        file = difs_create_file_from_chunks("12", checksum, chunks)

        with difs_concat_file_blobs_to_disk(file.blobs.all()) as fd:
            content = fd.read()
            self.assertEqual(content, b"12")
