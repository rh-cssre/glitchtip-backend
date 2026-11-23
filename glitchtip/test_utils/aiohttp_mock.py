"""Answer outbound aiohttp requests from a script instead of the network.

Register the responses a test expects, then run the code under test::

    with mock_aiohttp() as mocked:
        mocked.get("https://example.com/api/", payload={"id": 1})
        await thing_that_fetches()

    @mock_aiohttp()
    def test_thing(self, mocked):
        mocked.get("https://example.com/", status=500)

Registrations for the same method and URL are consumed in the order they were
made, so a test can script a sequence -- a 429 then a 200, to exercise a retry
-- and ``repeat=True`` pins one that answers every remaining call. A request
matching nothing raises ``ClientConnectionError`` naming the URL, which is what
makes "this host is never contacted" an assertion a test can rely on.

Why this exists rather than ``aioresponses``: that library builds a real
``aiohttp.ClientResponse`` around a real ``StreamReader``, so it depends on the
constructor signatures of two internal aiohttp classes. aiohttp 3.14 added a
required ``stream_writer`` argument to one and changed the protocol interface of
the other, which broke every mocked request with a ``TypeError``. Here the
response is a stand-in implementing only the surface a caller touches, so an
aiohttp release can break it only by changing an API our own production code
already uses -- in which case that code needs the fix too, and a failing test is
the right outcome rather than a blocked upgrade.

The seam is ``ClientSession._request``: every ``get``/``post``/``request`` call
funnels through it, so no connector, resolver or socket is reached. It also
means aiohttp's own URL building and header serialization are replaced rather
than exercised, so the errors those raise on a malformed URL or a header
carrying control characters are not modelled here. Keyword arguments *are*
checked against the real signature, so a call aiohttp would reject as a
TypeError still fails. Code layered on top runs for real --
``request_with_validated_redirects`` walks a scripted 302 chain rather than
taking a shortcut through the double.
"""

import base64
import json
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from functools import wraps
from inspect import iscoroutinefunction, signature
from typing import Any, Self
from unittest import mock

import aiohttp
from multidict import CIMultiDict, CIMultiDictProxy
from yarl import URL

DEFAULT_ENCODING = "utf-8"
JSON_CONTENT_TYPE = "application/json"

# Captured before any patch is installed, to check call signatures against.
REAL_REQUEST_SIGNATURE = signature(aiohttp.ClientSession._request)


class _Released:
    """Awaitable no-op, mirroring what ``ClientResponse.release()`` returns.

    aiohttp's ``release()`` is a plain method returning an awaitable, and
    callers split both ways: ``async with`` discards the result, while
    ``request_with_validated_redirects`` awaits it. Returning an awaitable
    serves both without leaving an un-awaited coroutine behind.
    """

    __slots__ = ()

    def __await__(self) -> Iterator[Any]:
        return iter(())


class MockContent:
    """The ``response.content`` stream, over an already-complete body.

    Only the bounded ``read(n)`` that production uses. A real
    ``aiohttp.StreamReader`` would need a protocol object whose interface
    changed in the very release this module exists to survive.
    """

    def __init__(self, body: bytes):
        self._body = body
        self._offset = 0

    async def read(self, n: int = -1) -> bytes:
        end = len(self._body) if n < 0 else self._offset + n
        chunk = self._body[self._offset : end]
        self._offset += len(chunk)
        return chunk


class MockResponse:
    """Stand-in for ``aiohttp.ClientResponse`` over a fixed body."""

    def __init__(
        self,
        url: URL,
        status: int,
        body: bytes,
        headers: CIMultiDictProxy,
    ):
        self.url = url
        self.status = status
        self.headers = headers
        self.content = MockContent(body)
        self._body = body

    def get_encoding(self) -> str:
        _, _, parameters = self.headers.get("Content-Type", "").partition(";")
        for parameter in parameters.split(";"):
            key, _, value = parameter.partition("=")
            if key.strip().lower() == "charset":
                return value.strip().strip('"') or DEFAULT_ENCODING
        return DEFAULT_ENCODING

    async def read(self) -> bytes:
        return self._body

    async def text(self, encoding: str | None = None) -> str:
        return self._body.decode(encoding or self.get_encoding())

    async def json(
        self,
        *,
        encoding: str | None = None,
        content_type: str | None = JSON_CONTENT_TYPE,
    ) -> Any:
        """Decode the body as JSON, refusing a non-JSON content type.

        The refusal is the point. Two callers -- the importer against an
        operator-supplied host, and the Stripe client's error branch -- call
        this on a response they do not control, where a proxy answering
        ``text/html`` raises ``ContentTypeError`` in production. A double that
        parsed anything would let a test assert behavior production never
        reaches.
        """
        if content_type:
            received = self.headers.get("Content-Type", "").split(";")[0].strip()
            if received.lower() != content_type.lower():
                raise aiohttp.ContentTypeError(
                    None,
                    (),
                    status=self.status,
                    message=(
                        f"Attempt to decode JSON with unexpected mimetype: {received}"
                    ),
                    headers=self.headers,
                )
        stripped = self._body.strip()
        if not stripped:
            return None
        return json.loads(stripped.decode(encoding or self.get_encoding()))

    def release(self) -> _Released:
        return _Released()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_exc_info: object) -> None:
        return None


@dataclass
class RecordedRequest:
    """One request the code under test made, for asserting on the wire payload."""

    method: str
    url: URL
    kwargs: dict[str, Any]


@dataclass
class _Registration:
    method: str
    url: URL
    status: int
    body: bytes
    headers: CIMultiDictProxy
    repeat: bool
    used: bool = False


def _canonical(url: str | URL, params: Any = None) -> URL:
    """Normalize a URL so a registration and a request compare equal.

    Query order carries no meaning, and a caller may pass parameters either
    inside the URL or through aiohttp's ``params=``; both spellings have to
    reach the same registration. ``extend_query`` rather than ``update_query``
    because that is what ``ClientRequest`` does -- ``params`` appends to a query
    already in the URL, it does not replace it.

    Dropping the fragment and an explicitly-written default port matches what
    aiohttp puts on the wire. yarl compares those as distinct while rendering
    them identically, which without this produces a mismatch whose message
    names the same URL on both sides.
    """
    canonical = URL(url).with_fragment(None)
    if params:
        canonical = canonical.extend_query(params)
    canonical = canonical.with_query(sorted(canonical.query.items()))
    return URL(str(canonical))


class MockAiohttp:
    """A script of responses, installed over ``ClientSession._request``."""

    def __init__(self):
        self._registrations: list[_Registration] = []
        self._patcher: Any = None
        self.requests: list[RecordedRequest] = []

    def add(
        self,
        method: str,
        url: str | URL,
        *,
        status: int = 200,
        body: str | bytes = b"",
        payload: Any = None,
        headers: Mapping[str, str] | None = None,
        repeat: bool = False,
    ) -> None:
        """Script one answer for ``method url``.

        ``payload`` is serialized to a JSON body and sets the content type;
        ``body`` supplies the bytes verbatim. ``repeat`` answers every further
        matching request instead of being consumed by the first.
        """
        response_headers: CIMultiDict = CIMultiDict()
        if payload is not None:
            body = json.dumps(payload)
            response_headers["Content-Type"] = "application/json"
        if headers:
            response_headers.update(headers)
        self._registrations.append(
            _Registration(
                method=method.upper(),
                url=_canonical(url),
                status=status,
                body=body.encode() if isinstance(body, str) else body,
                headers=CIMultiDictProxy(response_headers),
                repeat=repeat,
            )
        )

    def get(self, url: str | URL, **kwargs: Any) -> None:
        self.add("GET", url, **kwargs)

    def post(self, url: str | URL, **kwargs: Any) -> None:
        self.add("POST", url, **kwargs)

    def start(self) -> None:
        # Bound methods are not descriptors, so `session._request(...)` calls
        # `_request` with the arguments as written and this registry as self;
        # the ClientSession never reaches it, which is the point.
        self._patcher = mock.patch.object(
            aiohttp.ClientSession, "_request", self._request
        )
        self._patcher.start()

    def stop(self) -> None:
        self._patcher.stop()
        self._patcher = None

    def __enter__(self) -> Self:
        self.start()
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.stop()

    def __call__(self, func: Callable) -> Callable:
        """Use as a decorator, passing a registry as the last positional arg.

        The registry is built per call rather than shared, so a decorated method
        that runs more than once does not inherit the previous run's script.
        """

        @wraps(func)
        def sync_wrapper(*args, **kwargs):
            with MockAiohttp() as mocked:
                return func(*args, mocked, **kwargs)

        @wraps(func)
        async def async_wrapper(*args, **kwargs):
            with MockAiohttp() as mocked:
                return await func(*args, mocked, **kwargs)

        return async_wrapper if iscoroutinefunction(func) else sync_wrapper

    async def _request(
        self, method: str, url: str | URL, **kwargs: Any
    ) -> MockResponse:
        # Bind against the real signature so a call aiohttp would reject -- an
        # unknown keyword, or one removed by an upgrade -- fails here too
        # instead of being silently accepted by **kwargs.
        REAL_REQUEST_SIGNATURE.bind(None, method, url, **kwargs)
        method = method.upper()
        requested = _canonical(url, kwargs.get("params"))
        self.requests.append(RecordedRequest(method, requested, kwargs))

        for registration in self._registrations:
            if registration.used and not registration.repeat:
                continue
            if registration.method != method or registration.url != requested:
                continue
            registration.used = True
            return MockResponse(
                requested,
                registration.status,
                registration.body,
                registration.headers,
            )

        unused = ", ".join(
            f"{r.method} {r.url}" for r in self._registrations if not r.used
        )
        raise aiohttp.ClientConnectionError(
            f"No response registered for {method} {requested}. "
            f"Unused registrations: {unused or 'none'}"
        )


def mock_aiohttp() -> MockAiohttp:
    """A script of aiohttp responses, usable as a context manager or decorator."""
    return MockAiohttp()


def basic_auth_credentials(header: str) -> str:
    """Decode an ``Authorization: Basic ...`` header back to ``login:password``.

    For asserting on what a caller put on the wire when it builds the header
    itself rather than handing credentials to aiohttp.
    """
    scheme, _, encoded = header.partition(" ")
    if scheme != "Basic":
        raise ValueError(f"not a Basic Authorization header: {header!r}")
    return base64.b64decode(encoded, validate=True).decode()
