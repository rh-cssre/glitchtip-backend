import io
import json
import uuid
import zipfile
from hashlib import sha1

from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse

from apps.sourcecode.models import DebugSymbolBundle
from glitchtip.test_utils.test_case import GlitchTipTestCaseMixin

from ..assemble import AssembleTask, ChunkFileState, _get_cache_key
from ..models import File, FileBlob


def build_bundle(manifest: dict, contents: dict[str, str]) -> tuple[bytes, str]:
    """Zip a manifest and its artifacts, returning the bytes and their SHA-1."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, mode="w") as zipf:
        zipf.writestr("manifest.json", json.dumps(manifest))
        for name, content in contents.items():
            zipf.writestr(name, content)
    data = buffer.getvalue()
    return data, sha1(data).hexdigest()


class ArtifactBundleAssembleTests(GlitchTipTestCaseMixin, TestCase):
    def setUp(self):
        self.create_logged_in_user()
        self.async_client.force_login(self.user)
        self.url = reverse(
            "api:artifact_bundle_assemble", args=[self.organization.slug]
        )

    async def assemble(self, manifest: dict, contents: dict[str, str]) -> str:
        data, checksum = build_bundle(manifest, contents)
        await FileBlob.objects.acreate(
            blob=SimpleUploadedFile(checksum, data),
            size=len(data),
            checksum=checksum,
        )
        res = await self.async_client.post(
            self.url,
            {
                "checksum": checksum,
                "chunks": [checksum],
                "projects": [self.project.slug],
            },
            content_type="application/json",
        )
        self.assertEqual(res.status_code, 200)
        return checksum

    def assertAssembleState(self, checksum: str, state: ChunkFileState):
        status = cache.get(
            _get_cache_key(AssembleTask.ARTIFACTS, self.organization.pk, checksum)
        )
        self.assertIsNotNone(status, "assemble status was never set")
        self.assertEqual(status[0], state, status[1])

    async def test_artifact_without_debug_id_or_release_is_skipped(self):
        """An unreferenceable artifact must not abort the rest of the upload.

        With no release and no debug-id header, a bundle row would violate the
        debug_id_or_release_required constraint. bulk_create(ignore_conflicts)
        does not swallow check violations, so the whole assemble used to raise
        and every other bundle in the upload was lost.
        """
        debug_id = str(uuid.uuid4())
        manifest = {
            "org": self.organization.slug,
            "project": self.project.slug,
            "files": {
                "files/_/_/good.js": {
                    "type": "minified_source",
                    "url": "~/good.js",
                    "headers": {"debug-id": debug_id, "sourcemap": "good.js.map"},
                },
                "files/_/_/good.js.map": {
                    "type": "source_map",
                    "url": "~/good.js.map",
                    "headers": {"debug-id": debug_id},
                },
                "files/_/_/orphan.js": {
                    "type": "minified_source",
                    "url": "~/orphan.js",
                    "headers": {},
                },
            },
        }
        checksum = await self.assemble(
            manifest,
            {
                "files/_/_/good.js": "function a(n,t){return n+t}",
                "files/_/_/good.js.map": "{}",
                "files/_/_/orphan.js": "function b(){}",
            },
        )

        self.assertAssembleState(checksum, ChunkFileState.OK)

        # The valid sibling bundle in the same upload still exists.
        bundle = await DebugSymbolBundle.objects.select_related(
            "file", "sourcemap_file"
        ).aget(organization=self.organization, debug_id=debug_id)
        self.assertEqual(bundle.file.name, "good.js")
        self.assertEqual(bundle.sourcemap_file.name, "good.js.map")

        # The unreferenceable artifact produced no bundle, and its file row
        # was not left behind.
        self.assertEqual(await DebugSymbolBundle.objects.acount(), 1)
        self.assertFalse(await File.objects.filter(name="orphan.js").aexists())

    async def test_skipped_source_map_still_referenced_is_kept(self):
        """A skipped artifact that another bundle points at must survive.

        A source map matched by the minified file's ``sourcemap`` header needs
        no debug-id of its own. It gets no bundle row, but deleting its File
        would break the sourcemap_file reference of the bundle that does.
        """
        debug_id = str(uuid.uuid4())
        manifest = {
            "org": self.organization.slug,
            "project": self.project.slug,
            "files": {
                "files/_/_/app.js": {
                    "type": "minified_source",
                    "url": "~/app.js",
                    "headers": {"debug-id": debug_id, "sourcemap": "app.js.map"},
                },
                "files/_/_/app.js.map": {
                    "type": "source_map",
                    "url": "~/app.js.map",
                    "headers": {},
                },
            },
        }
        checksum = await self.assemble(
            manifest,
            {
                "files/_/_/app.js": "function a(n,t){return n+t}",
                "files/_/_/app.js.map": "{}",
            },
        )

        self.assertAssembleState(checksum, ChunkFileState.OK)
        bundle = await DebugSymbolBundle.objects.select_related("sourcemap_file").aget(
            organization=self.organization, debug_id=debug_id
        )
        self.assertEqual(bundle.sourcemap_file.name, "app.js.map")
        self.assertTrue(await File.objects.filter(name="app.js.map").aexists())

    async def test_artifact_without_debug_id_is_kept_when_release_is_set(self):
        """A release makes a debug-id-less artifact resolvable, so keep it."""
        version = "1.0.0"
        manifest = {
            "org": self.organization.slug,
            "project": self.project.slug,
            "release": version,
            "files": {
                "files/_/_/plain.js": {
                    "type": "minified_source",
                    "url": "~/plain.js",
                    "headers": {},
                },
            },
        }
        data, checksum = build_bundle(
            manifest, {"files/_/_/plain.js": "function b(){}"}
        )
        await FileBlob.objects.acreate(
            blob=SimpleUploadedFile(checksum, data),
            size=len(data),
            checksum=checksum,
        )
        res = await self.async_client.post(
            self.url,
            {
                "checksum": checksum,
                "chunks": [checksum],
                "projects": [self.project.slug],
                "version": version,
            },
            content_type="application/json",
        )
        self.assertEqual(res.status_code, 200)

        self.assertAssembleState(checksum, ChunkFileState.OK)
        bundle = await DebugSymbolBundle.objects.select_related("file", "release").aget(
            organization=self.organization
        )
        self.assertIsNone(bundle.debug_id)
        self.assertEqual(bundle.release.version, version)
        self.assertEqual(bundle.file.name, "plain.js")
