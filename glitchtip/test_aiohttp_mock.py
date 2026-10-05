"""Guards on the aiohttp test double's own contract.

Tests elsewhere lean on three properties of ``mock_aiohttp`` that are easy to
weaken without noticing, because weakening them makes tests pass rather than
fail. A request nobody registered has to raise: that is what turns "only the
rebased URL is registered" into a real assertion that the importer never
contacts the foreign host, and a double that answered everything with a 200
would retire that test silently. Ordering and ``repeat`` are what let a test
script a retry sequence.
"""

import aiohttp
from django.test import SimpleTestCase

from glitchtip.ssrf_protection import request_with_validated_redirects
from glitchtip.test_utils.aiohttp_mock import basic_auth_credentials, mock_aiohttp
from glitchtip.test_utils.dns import resolves_to

URL = "https://example.com/resource"


class MockAiohttpTests(SimpleTestCase):
    async def test_unregistered_request_raises(self):
        with mock_aiohttp() as mocked:
            mocked.get(URL)
            async with aiohttp.ClientSession() as session:
                with self.assertRaises(aiohttp.ClientConnectionError):
                    await session.get("https://elsewhere.example/")

    async def test_method_and_query_are_part_of_the_match(self):
        with mock_aiohttp() as mocked:
            mocked.get(URL)
            async with aiohttp.ClientSession() as session:
                with self.assertRaises(aiohttp.ClientConnectionError):
                    await session.post(URL)
                with self.assertRaises(aiohttp.ClientConnectionError):
                    await session.get(URL, params={"cursor": "abc"})

    async def test_query_reaches_the_same_registration_either_spelling(self):
        with mock_aiohttp() as mocked:
            mocked.get(f"{URL}?b=2&a=1", status=204)
            async with aiohttp.ClientSession() as session:
                response = await session.get(URL, params={"a": "1", "b": "2"})
        self.assertEqual(response.status, 204)

    async def test_registrations_are_consumed_in_order(self):
        with mock_aiohttp() as mocked:
            mocked.get(URL, status=429)
            mocked.get(URL, status=200)
            async with aiohttp.ClientSession() as session:
                first = await session.get(URL)
                second = await session.get(URL)
                with self.assertRaises(aiohttp.ClientConnectionError):
                    await session.get(URL)
        self.assertEqual([first.status, second.status], [429, 200])

    async def test_repeat_answers_every_further_request(self):
        with mock_aiohttp() as mocked:
            mocked.get(URL, status=500, repeat=True)
            async with aiohttp.ClientSession() as session:
                statuses = [(await session.get(URL)).status for _ in range(3)]
        self.assertEqual(statuses, [500, 500, 500])

    async def test_body_is_readable_as_text_json_and_stream(self):
        with mock_aiohttp() as mocked:
            mocked.get(URL, payload={"id": 1}, repeat=True)
            async with aiohttp.ClientSession() as session:
                self.assertEqual(await (await session.get(URL)).json(), {"id": 1})
                self.assertEqual(await (await session.get(URL)).text(), '{"id": 1}')
                response = await session.get(URL)
                self.assertEqual(await response.content.read(4), b'{"id')
                self.assertEqual(await response.content.read(100), b'": 1}')

    async def test_requests_are_recorded_with_their_payload(self):
        with mock_aiohttp() as mocked:
            mocked.post(URL)
            async with aiohttp.ClientSession() as session:
                await session.post(URL, data={"mode": "flexible"})
        self.assertEqual(mocked.requests[0].method, "POST")
        self.assertEqual(mocked.requests[0].kwargs["data"], {"mode": "flexible"})

    async def test_json_refuses_a_non_json_content_type(self):
        """Match aiohttp, which raises rather than parsing an unexpected body.

        The importer and the Stripe error branch both call ``json()`` on a
        response they do not control; a double that parsed a proxy's
        ``text/html`` would let a test cover a path production never takes.
        """
        with mock_aiohttp() as mocked:
            mocked.get(
                URL, body="<html>502</html>", headers={"Content-Type": "text/html"}
            )
            mocked.get(
                f"{URL}/empty",
                payload=None,
                headers={"Content-Type": "application/json"},
            )
            async with aiohttp.ClientSession() as session:
                with self.assertRaises(aiohttp.ContentTypeError):
                    await (await session.get(URL)).json()
                self.assertIsNone(await (await session.get(f"{URL}/empty")).json())

    async def test_unknown_keyword_is_rejected_as_aiohttp_would(self):
        with mock_aiohttp() as mocked:
            mocked.get(URL)
            async with aiohttp.ClientSession() as session:
                with self.assertRaises(TypeError):
                    await session.get(URL, not_a_real_kwarg=1)

    async def test_a_scripted_redirect_drives_the_real_redirect_walk(self):
        """The layered code runs for real: only the double's answers are fake.

        ``request_with_validated_redirects`` disables aiohttp's own redirect
        following and walks the chain itself, re-validating each hop. Scripting
        a 302 exercises that walk -- including the ``await response.release()``
        on the hop it discards.
        """
        with mock_aiohttp() as mocked:
            mocked.get(
                "https://start.example/",
                status=302,
                headers={"Location": "https://final.example/"},
            )
            mocked.get("https://final.example/", status=200)
            with resolves_to():
                async with aiohttp.ClientSession() as session:
                    response = await request_with_validated_redirects(
                        session, "GET", "https://start.example/"
                    )
        self.assertEqual(response.status, 200)
        self.assertEqual(
            [str(request.url) for request in mocked.requests],
            ["https://start.example/", "https://final.example/"],
        )

    def test_basic_auth_credentials_decodes_a_header(self):
        self.assertEqual(
            basic_auth_credentials(aiohttp.encode_basic_auth("user", "pass")),
            "user:pass",
        )
        with self.assertRaises(ValueError):
            basic_auth_credentials("Bearer sometoken")

    def test_patch_is_removed_on_exit(self):
        original = aiohttp.ClientSession.__dict__["_request"]
        with mock_aiohttp():
            self.assertIsNot(aiohttp.ClientSession.__dict__["_request"], original)
        self.assertIs(aiohttp.ClientSession.__dict__["_request"], original)
