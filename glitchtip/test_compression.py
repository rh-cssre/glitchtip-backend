import gzip
import secrets

import brotli
from django.http import HttpResponse, StreamingHttpResponse
from django.test import SimpleTestCase, override_settings
from django.urls import path

from glitchtip.compression import (
    MIN_COMPRESSIBLE_LENGTH,
    accepted_encodings,
    compress_response,
)

BODY = "compress me " * 500


async def compressible_view(request):
    return HttpResponse(BODY, content_type="text/plain")


async def tiny_view(request):
    return HttpResponse("no", content_type="text/plain")


async def streaming_body():
    yield BODY.encode()


async def streaming_view(request):
    return StreamingHttpResponse(streaming_body(), content_type="text/plain")


async def already_encoded_view(request):
    response = HttpResponse(BODY, content_type="text/plain")
    response["Content-Encoding"] = "br"
    return response


urlpatterns = [
    path("compressible/", compressible_view),
    path("tiny/", tiny_view),
    path("streaming/", streaming_view),
    path("already-encoded/", already_encoded_view),
]


class AcceptedEncodingsTestCase(SimpleTestCase):
    def test_plain_list(self):
        self.assertEqual(
            accepted_encodings("gzip, deflate, br"), {"gzip", "deflate", "br"}
        )

    def test_quality_zero_is_a_refusal_not_an_offer(self):
        """`gzip;q=0` means the client does not want gzip. Reading it as an
        offer would send a body it asked us not to send."""
        self.assertEqual(accepted_encodings("br, gzip;q=0"), {"br"})

    def test_weights_other_than_zero_are_accepted(self):
        self.assertEqual(accepted_encodings("gzip;q=0.5, br;q=1.0"), {"gzip", "br"})

    def test_junk_is_not_an_offer(self):
        self.assertEqual(accepted_encodings("gzip;q=banana"), set())

    def test_empty_and_wildcard(self):
        self.assertEqual(accepted_encodings(""), set())
        # A bare `*` is deliberately not expanded into a concrete encoding.
        self.assertEqual(accepted_encodings("*"), {"*"})


@override_settings(ROOT_URLCONF=__name__)
class CompressionMiddlewareTestCase(SimpleTestCase):
    """Runs through the project's real MIDDLEWARE, so this fails if the
    middleware is removed from settings or ordered somewhere it cannot see the
    finished body."""

    async def test_brotli_is_preferred_and_the_body_round_trips(self):
        response = await self.async_client.get(
            "/compressible/", headers={"accept-encoding": "gzip, deflate, br"}
        )
        self.assertEqual(response.headers["Content-Encoding"], "br")
        self.assertEqual(brotli.decompress(response.content).decode(), BODY)

    async def test_gzip_for_a_client_without_brotli(self):
        response = await self.async_client.get(
            "/compressible/", headers={"accept-encoding": "gzip, deflate"}
        )
        self.assertEqual(response.headers["Content-Encoding"], "gzip")
        self.assertEqual(gzip.decompress(response.content).decode(), BODY)

    async def test_a_client_refusing_brotli_gets_gzip(self):
        response = await self.async_client.get(
            "/compressible/", headers={"accept-encoding": "br;q=0, gzip"}
        )
        self.assertEqual(response.headers["Content-Encoding"], "gzip")

    async def test_a_client_accepting_nothing_gets_the_original(self):
        response = await self.async_client.get("/compressible/")
        self.assertNotIn("Content-Encoding", response.headers)
        self.assertEqual(response.content.decode(), BODY)

    async def test_the_response_varies_on_accept_encoding(self):
        """Without this a shared cache can hand a compressed body to a client
        that asked for none."""
        response = await self.async_client.get("/compressible/")
        self.assertIn("Accept-Encoding", response.headers["Vary"])

    async def test_content_length_matches_the_compressed_body(self):
        response = await self.async_client.get(
            "/compressible/", headers={"accept-encoding": "br"}
        )
        self.assertEqual(int(response.headers["Content-Length"]), len(response.content))

    async def test_compressed_responses_are_padded(self):
        """BREACH: an exact compressed length is a size oracle, so every
        compressed response carries a random number of extra bytes."""
        lengths = set()
        for _ in range(20):
            response = await self.async_client.get(
                "/compressible/", headers={"accept-encoding": "br"}
            )
            lengths.add(len(response.headers["X-Padding"]))
        self.assertGreater(len(lengths), 1)

    async def test_a_short_response_is_left_alone(self):
        response = await self.async_client.get(
            "/tiny/", headers={"accept-encoding": "br"}
        )
        self.assertNotIn("Content-Encoding", response.headers)
        self.assertLess(len(response.content), MIN_COMPRESSIBLE_LENGTH)

    async def test_an_already_encoded_response_is_not_compressed_twice(self):
        response = await self.async_client.get(
            "/already-encoded/", headers={"accept-encoding": "br"}
        )
        self.assertEqual(response.headers["Content-Encoding"], "br")
        self.assertEqual(response.content.decode(), BODY)

    async def test_a_streaming_response_passes_through(self):
        response = await self.async_client.get(
            "/streaming/", headers={"accept-encoding": "br"}
        )
        self.assertNotIn("Content-Encoding", response.headers)
        self.assertEqual(b"".join([chunk async for chunk in response]).decode(), BODY)


class IncompressibleContentTestCase(SimpleTestCase):
    """A body that grows under compression is sent as it was."""

    def test_random_bytes_are_left_uncompressed(self):
        incompressible = secrets.token_bytes(4096)
        response = HttpResponse(incompressible, content_type="application/octet-stream")
        request = type("R", (), {"META": {"HTTP_ACCEPT_ENCODING": "br, gzip"}})()
        compress_response(request, response)
        self.assertNotIn("Content-Encoding", response.headers)
        self.assertEqual(response.content, incompressible)
