import orjson
from django.http import HttpRequest
from ninja.errors import HttpError
from ninja.parser import Parser

from apps.event_ingest.rust_envelope import (
    EnvelopeTooBig,
    decompress_body,
    request_content_encoding,
)


class ORJSONParser(Parser):
    def parse_body(self, request: HttpRequest):
        body = request.body
        # A Content-Encoded body (e.g. an SDK gzipping a /store/ event, or zstd)
        # arrives still compressed — nothing decompresses the request body
        # upstream of this parser — so decompress it in Rust before JSON parsing.
        # The decompressed-size cap is enforced inside gt_rust. Plain bodies (the
        # common case, no Content-Encoding) skip straight to orjson.
        encoding = request_content_encoding(request)
        if encoding is not None:
            try:
                body = decompress_body(body, encoding)
            except EnvelopeTooBig:
                raise HttpError(413, "Request body too large") from None
            except ValueError:
                raise HttpError(400, "Invalid compressed request body") from None
        return orjson.loads(body)
