import gzip
import os
import tempfile
from pathlib import Path

import brotli
from django.http import HttpResponse
from django.test import SimpleTestCase, override_settings
from django.urls import path

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
            # Word-shaped names of the same length as a hash. `immutable` is
            # the one header a deploy cannot take back, so a file an operator
            # dropped into the tree by hand must not match on shape alone.
            "/static/main-override.css",
            "/static/vendor-analytics.js",
            "/static/runtime-polyfill.js",
        ):
            with self.subTest(url=url):
                self.assertFalse(_is_hashed_asset("", url))

    def test_a_wider_hash_is_still_immutable(self):
        """The build mixes hash widths within a single release. An exact-width
        pattern quietly gave the odd ones out the short TTL instead."""
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
        (root / "index.html").write_bytes(b"<!doctype html><html></html>")
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

    async def test_every_static_response_carries_nosniff(self):
        """MIDDLEWARE never runs for a file served here, so the headers it
        would have set have to be set at this layer instead."""
        _, headers, _ = await request(self.app, "/static/main-ABCDEFGH.js", "br")
        self.assertEqual(headers["x-content-type-options"], "nosniff")

    async def test_html_in_the_static_tree_cannot_be_framed(self):
        """collectstatic copies the SPA's index.html into the tree. That copy
        boots the real, session-authenticated app from the app's own origin, so
        without these headers it is a frameable, CSP-free second front door."""
        _, headers, _ = await request(self.app, "/static/index.html", "gzip")
        self.assertEqual(headers["x-frame-options"], "DENY")
        self.assertEqual(headers["content-security-policy"], "frame-ancestors 'none'")


class DebugFindersTestCase(SimpleTestCase):
    """Dev has no collected tree: `docker compose up` never runs collectstatic,
    so the admin, allauth and SPA assets have to come off the finders."""

    async def test_files_are_served_without_collectstatic(self):
        with tempfile.TemporaryDirectory() as source:
            (Path(source) / "logo.svg").write_bytes(b"<svg/>")
            inner = MockASGIApp()
            with override_settings(DEBUG=True, STATICFILES_DIRS=[source]):
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

    async def test_a_symlink_out_of_a_static_dir_is_refused(self):
        """Registering the finders' directories, rather than delegating to
        finders.find(), is what keeps ServeStatic's containment check -- and
        that check is the only thing that resolves symlinks. A link inside a
        static directory pointing outside it must not be served."""
        with (
            tempfile.TemporaryDirectory() as source,
            tempfile.TemporaryDirectory() as outside,
        ):
            secret = Path(outside) / "secret.txt"
            secret.write_bytes(b"not static")
            os.symlink(secret, Path(source) / "link.txt")
            inner = MockASGIApp()
            with override_settings(DEBUG=True, STATICFILES_DIRS=[source]):
                app = serve_static(inner)
                status, _, body = await request(app, "/static/link.txt", "gzip")
        self.assertIsNone(status)
        self.assertNotIn(b"not static", body)
        self.assertTrue(inner.called)

    async def test_nothing_in_development_is_cached(self):
        """Including the hashed bundles: a dev build can be configured not to
        hash, and a rebuild under the same name has to show up on reload."""
        with tempfile.TemporaryDirectory() as source:
            (Path(source) / "main-ABCDEFGH.js").write_bytes(b"x")
            with override_settings(DEBUG=True, STATICFILES_DIRS=[source]):
                app = serve_static(MockASGIApp())
                _, headers, _ = await request(app, "/static/main-ABCDEFGH.js", "gzip")
        self.assertEqual(headers["cache-control"], "max-age=0, public")


async def gzip_probe_view(request):
    """A body comfortably over the 200-byte floor Django's GZipMiddleware
    refuses to compress below."""
    return HttpResponse("compress me " * 500, content_type="text/plain")


urlpatterns = [path("gzip-probe/", gzip_probe_view)]


@override_settings(ROOT_URLCONF=__name__)
class GZipMiddlewareTestCase(SimpleTestCase):
    """ServeStatic covers /static/; GZipMiddleware covers everything Django
    renders itself. Between them every response GlitchTip sends is compressed
    with nothing to configure, which is the point of the pair -- so assert the
    half that runs inside Django, through the project's real MIDDLEWARE."""

    async def test_a_django_response_is_gzipped(self):
        response = await self.async_client.get(
            "/gzip-probe/", headers={"accept-encoding": "gzip"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["Content-Encoding"], "gzip")
        self.assertIn("Accept-Encoding", response.headers["Vary"])
        self.assertLess(len(response.content), 6000)

    async def test_a_client_that_does_not_accept_gzip_gets_the_original(self):
        response = await self.async_client.get("/gzip-probe/")
        self.assertNotIn("Content-Encoding", response.headers)
        self.assertEqual(response.content, b"compress me " * 500)
