import gzip
import tempfile
from pathlib import Path

import brotli
from django.test import SimpleTestCase, override_settings

from glitchtip.asgi import _is_hashed_asset, serve_static


class MockASGIApp:
    """Stands in for the Django application ServeStatic delegates to."""

    def __init__(self):
        self.called = False

    async def __call__(self, scope, receive, send):
        self.called = True


async def request(app, path, accept_encoding=None):
    """Drive one GET through an ASGI app and return (status, headers, body)."""
    headers = []
    if accept_encoding is not None:
        headers.append((b"accept-encoding", accept_encoding.encode()))
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "scheme": "http",
        "headers": headers,
        "client": ("127.0.0.1", 1),
        "server": ("127.0.0.1", 8000),
    }
    messages = []

    async def receive():
        return {"type": "http.disconnect"}

    async def send(message):
        messages.append(message)

    await app(scope, receive, send)
    if not messages:
        return None, {}, b""
    start = messages[0]
    body = b"".join(m.get("body", b"") for m in messages[1:])
    return (
        start["status"],
        {k.decode().lower(): v.decode() for k, v in start["headers"]},
        body,
    )


class HashedAssetTestCase(SimpleTestCase):
    """The build's own bundles are the only thing cached forever."""

    def test_hashed_bundles_at_the_tree_root_are_immutable(self):
        for url in (
            "/static/main-KVU4SSHB.js",
            "/static/chunk-B0tvkw2C2.js",
            "/static/styles-4VN2AIF7.css",
            # The source map beside a bundle carries the same hash.
            "/static/main-KVU4SSHB.js.map",
        ):
            with self.subTest(url=url):
                self.assertTrue(_is_hashed_asset("", url))

    def test_stable_names_are_not_immutable(self):
        for url in (
            # Everything under a subdirectory, hashed-looking or not: these
            # keep their names across a release, so caching one forever would
            # pin a client to a stale copy.
            "/static/assets/i18n/messages.fr.json",
            "/static/assets/fonts/material-symbols.woff2",
            "/static/admin/css/base.css",
            "/static/main-KVU4SSHB.js/../../evil.js",
            # No hash at all.
            "/static/index.html",
            "/static/favicon.ico",
        ):
            with self.subTest(url=url):
                self.assertFalse(_is_hashed_asset("", url))

    def test_nine_character_hashes_are_immutable(self):
        """The build mixes hash lengths; an exact-width pattern missed 22 of
        the 70 bundles and quietly gave them the short TTL instead."""
        self.assertTrue(_is_hashed_asset("", "/static/chunk-B5PKTg242.js"))


class StaticRootTestCase(SimpleTestCase):
    """Production serves the collected tree, precompressed copies and all."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.bundle = b"console.log('x');" * 100
        (root / "main-ABCDEFGH.js").write_bytes(self.bundle)
        (root / "main-ABCDEFGH.js.br").write_bytes(brotli.compress(self.bundle))
        (root / "main-ABCDEFGH.js.gz").write_bytes(gzip.compress(self.bundle))
        (root / "assets").mkdir()
        (root / "assets" / "messages.json").write_bytes(b'{"hello": "world"}')
        self.inner = MockASGIApp()
        with override_settings(DEBUG=False, STATIC_ROOT=str(root)):
            self.app = serve_static(self.inner)

    async def test_brotli_is_preferred_over_gzip(self):
        status, headers, body = await request(
            self.app, "/static/main-ABCDEFGH.js", "gzip, deflate, br, zstd"
        )
        self.assertEqual(status, 200)
        self.assertEqual(headers["content-encoding"], "br")
        self.assertEqual(headers["vary"], "Accept-Encoding")
        self.assertEqual(brotli.decompress(body), self.bundle)

    async def test_gzip_serves_a_client_without_brotli(self):
        _, headers, body = await request(
            self.app, "/static/main-ABCDEFGH.js", "gzip, deflate"
        )
        self.assertEqual(headers["content-encoding"], "gzip")
        self.assertEqual(gzip.decompress(body), self.bundle)

    async def test_a_client_accepting_nothing_gets_the_original(self):
        _, headers, body = await request(self.app, "/static/main-ABCDEFGH.js", None)
        self.assertNotIn("content-encoding", headers)
        self.assertEqual(body, self.bundle)

    async def test_the_hashed_bundle_is_cached_forever(self):
        _, headers, _ = await request(self.app, "/static/main-ABCDEFGH.js", "br")
        self.assertIn("immutable", headers["cache-control"])

    async def test_a_stable_name_keeps_the_short_ttl(self):
        _, headers, _ = await request(self.app, "/static/assets/messages.json", "gzip")
        self.assertEqual(headers["cache-control"], "max-age=3600, public")

    async def test_a_precompressed_copy_is_not_addressable_on_its_own(self):
        """Serving main.js.br as its own URL would hand the client brotli
        bytes labelled as JavaScript."""
        await request(self.app, "/static/main-ABCDEFGH.js.br", "gzip")
        self.assertTrue(self.inner.called)

    async def test_an_unknown_path_falls_through_to_django(self):
        await request(self.app, "/api/0/organizations/", "gzip")
        self.assertTrue(self.inner.called)


class DebugFindersTestCase(SimpleTestCase):
    """Dev has no collected tree: `docker compose up` never runs collectstatic,
    so the admin, allauth and SPA assets have to come off the finders."""

    async def test_files_are_served_without_collectstatic(self):
        with tempfile.TemporaryDirectory() as source:
            (Path(source) / "logo.svg").write_bytes(b"<svg/>")
            inner = MockASGIApp()
            with override_settings(
                DEBUG=True,
                STATICFILES_DIRS=[source],
                # A stale collected tree must not shadow the live sources.
                STATIC_ROOT=tempfile.gettempdir(),
            ):
                app = serve_static(inner)
                status, headers, body = await request(app, "/static/logo.svg", "gzip")
        self.assertEqual(status, 200)
        self.assertEqual(body, b"<svg/>")
        self.assertEqual(headers["cache-control"], "max-age=0, public")
        self.assertFalse(inner.called)

    async def test_a_file_added_after_startup_is_picked_up(self):
        """The frontend gets rebuilt while the server is running."""
        with tempfile.TemporaryDirectory() as source:
            inner = MockASGIApp()
            with override_settings(DEBUG=True, STATICFILES_DIRS=[source]):
                app = serve_static(inner)
                (Path(source) / "late.css").write_bytes(b"body{}")
                status, _, body = await request(app, "/static/late.css", "gzip")
        self.assertEqual(status, 200)
        self.assertEqual(body, b"body{}")

    async def test_traversal_outside_the_static_dirs_is_refused(self):
        with tempfile.TemporaryDirectory() as source:
            inner = MockASGIApp()
            with override_settings(DEBUG=True, STATICFILES_DIRS=[source]):
                app = serve_static(inner)
                await request(app, "/static/../../glitchtip/settings.py", "gzip")
        self.assertTrue(inner.called)
