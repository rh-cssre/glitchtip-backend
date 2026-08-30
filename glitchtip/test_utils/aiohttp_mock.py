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
funnels through it, and it sits after aiohttp has parsed the URL but before any
connector or resolver work, so nothing here dials or resolves. Code layered on
top still runs for real -- ``request_with_validated_redirects`` walking a
redirect chain, say -- so scripting a 302 with a ``Location`` header exercises
the real walk rather than a shortcut.
"""

import asyncio
import json
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from functools import wraps
from typing import Any, Self
from unittest import mock

import aiohttp
from multidict import CIMultiDict, CIMultiDictProxy
from yarl import URL

DEFAULT_ENCODING = "utf-8"


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

    async def json(self, *, encoding: str | None = None, **_: Any) -> Any:
        return json.loads(self._body.decode(encoding or self.get_encoding()))

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
    reach the same registration.
    """
    canonical = URL(url)
    if params:
        canonical = canonical.update_query(params)
    return canonical.with_query(sorted(canonical.query.items()))


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

        return async_wrapper if asyncio.iscoroutinefunction(func) else sync_wrapper

    async def _request(
        self, method: str, url: str | URL, **kwargs: Any
    ) -> MockResponse:
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
