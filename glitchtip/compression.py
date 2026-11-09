"""Compression for the responses Django renders itself.

ServeStatic answers /static/ from the .br and .gz copies collectstatic wrote at
build time (see glitchtip/asgi.py). This covers the other half: the single-page
app's index.html and every API response, which are built per request and so have
to be compressed per request.

Django ships GZipMiddleware for exactly this, and it is not used here for two
reasons. It only speaks gzip, and it hardcodes compression level 6 in
``django.utils.text.compress_string`` with no setting to change it.

Benchmarked against real API and index.html payloads, brotli at quality 4
produced a smaller body than gzip -6 for a little under half the CPU -- better
on both axes at once rather than a trade to weigh. Brotli costs no new
dependency either, because collectstatic already writes .br files with it.

Clients that do not offer brotli get gzip at level 1: somewhat more bytes than
level 6 for appreciably less CPU, which is the better side of that trade on a
deployment whose scaling signal is CPU.

Async-only, like the rest of this project's middleware, so a response never takes
a thread hop on the way out. GZipMiddleware is built on MiddlewareMixin, which
routes its response phase through ``sync_to_async(thread_sensitive=True)``.
"""

import gzip
import secrets

import brotli
from django.utils.cache import patch_vary_headers
from django.utils.decorators import async_only_middleware

# Below this a compressed body plus its headers tends to be larger than what it
# replaces. Matches the floor GZipMiddleware applies.
MIN_COMPRESSIBLE_LENGTH = 200

BROTLI_QUALITY = 4
GZIP_LEVEL = 1

# BREACH mitigation: an exact compressed length is a size oracle, so pad every
# compressed response by a random amount. Django hides its padding in the gzip
# header's filename field, which brotli has no equivalent of, so this uses a
# header for both codecs. Random rather than repeated bytes, so HTTP/2 header
# compression cannot squeeze them back out.
MAX_PADDING_BYTES = 100

# Ordered by preference: the first one the client accepts wins.
ENCODERS = (
    ("br", lambda data: brotli.compress(data, quality=BROTLI_QUALITY)),
    ("gzip", lambda data: gzip.compress(data, compresslevel=GZIP_LEVEL, mtime=0)),
)


def accepted_encodings(header: str) -> set[str]:
    """The encodings a client will actually take.

    ``Accept-Encoding: gzip;q=0`` is a client refusing gzip, not offering it, so
    anything weighted zero is dropped. A bare ``*`` is not expanded: serving a
    body encoded with something the client never named is allowed but not worth
    the risk, and no browser sends it alone.
    """
    accepted = set()
    for part in header.split(","):
        name, _, parameters = part.strip().partition(";")
        name = name.strip().lower()
        if not name:
            continue
        quality = 1.0
        for parameter in parameters.split(";"):
            key, _, value = parameter.partition("=")
            if key.strip().lower() == "q":
                try:
                    quality = float(value)
                except ValueError:
                    quality = 0.0
        if quality > 0:
            accepted.add(name)
    return accepted


def compress_response(request, response):
    """Compress ``response`` in place when it is worth it, and return it."""
    # Nothing in GlitchTip streams a response today. Compressing a stream means
    # carrying the encoder across awaits and giving up Content-Length; leaving
    # streams alone keeps this middleware simple, and means a download endpoint
    # added later cannot be silently slowed down by it.
    if response.streaming:
        return response
    if response.has_header("Content-Encoding"):
        return response
    if len(response.content) < MIN_COMPRESSIBLE_LENGTH:
        return response

    # Announced before the client's own header is consulted, so a shared cache
    # cannot hand a compressed body to a client that asked for none.
    patch_vary_headers(response, ("Accept-Encoding",))

    accepted = accepted_encodings(request.META.get("HTTP_ACCEPT_ENCODING", ""))
    for encoding, encode in ENCODERS:
        if encoding in accepted:
            break
    else:
        return response

    compressed = encode(response.content)
    # Already-compressed payloads can come out larger than they went in.
    if len(compressed) >= len(response.content):
        return response

    response.content = compressed
    response["Content-Encoding"] = encoding
    response["Content-Length"] = str(len(compressed))
    response["X-Padding"] = secrets.token_hex(
        secrets.randbelow(MAX_PADDING_BYTES // 2 + 1)
    )
    return response


@async_only_middleware
def compression_middleware(get_response):
    async def middleware(request):
        return compress_response(request, await get_response(request))

    return middleware
